"""Saved-report tools.

No tool here deletes anything. `propose_delete` only finds what would be deleted: the graph then
asks the user and carries it out in plain code (see graph.py), so the model cannot delete on its own.
Owner and conversation come from the run config, never from the model.
"""
import json
import logging
import re
from datetime import timedelta
from typing import Annotated, Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import InjectedState

from agent.observability import emit
from agent.safety.pii import scrub_text
from agent.storage.reports_store import Report, ReportStore

CONFIRMATION_TTL = timedelta(minutes=5)
UNDO_WINDOW = timedelta(minutes=10)
SNIPPET_CHARS = 150
NEEDS_CONFIRMATION = "needs_confirmation"
REPORT_CREATED = "report_created"
_QUARTER_RE = re.compile(r"\bQ[1-4]\b", re.IGNORECASE)


def _summary(report: Report) -> dict[str, Any]:
    return {"id": report.id, "title": report.title, "created_at": report.created_at[:10]}


def _latest_answer(messages: list[AnyMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage) and message.text and not message.tool_calls:
            return message.text
    return ""


def _missing_quarters(messages: list[AnyMessage], title: str) -> set[str]:
    """The quarters the user asked about, if the report's title names none of them."""
    question = next((m.text for m in reversed(messages) if isinstance(m, HumanMessage)), "")
    asked = {q.upper() for q in _QUARTER_RE.findall(question)}
    named = {q.upper() for q in _QUARTER_RE.findall(title)}
    return set() if asked & named or not asked else asked


def _result(message: ToolMessage) -> dict[str, Any]:
    """A tool's JSON result. A failed call (e.g. arguments the model got wrong) is reported by
    ToolNode as plain text, which goes back to the model to fix rather than crashing the turn."""
    try:
        result = json.loads(message.content)
    except (TypeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


def _latest_tool_results(messages: list[AnyMessage]) -> list[ToolMessage]:
    batch = []
    for message in reversed(messages):
        if not isinstance(message, ToolMessage):
            break
        batch.append(message)
    return batch


def created_report_id(messages: list[AnyMessage]) -> int | None:
    """The report just created by the latest tool results, if there is one."""
    for message in _latest_tool_results(messages):
        if message.name == "create_report":
            result = _result(message)
            if result.get("status") == REPORT_CREATED:
                return result["id"]
    return None


def matching_reports(store: ReportStore, user: str, conversation: str, filters: dict[str, Any]) -> list[Report]:
    """The user's reports matching a delete proposal's filters, right now."""
    return store.find(
        user,
        search=filters["search"] or None,
        conversation_id=conversation if filters["this_conversation"] else None,
        report_ids=filters["report_ids"] or None,
    )


def current_delete_matches(
    store: ReportStore, user: str, conversation: str, proposal: dict[str, Any],
) -> list[dict[str, Any]]:
    """What the proposal would delete now, which may differ from what the user was shown."""
    found = {r.id: r for f in proposal["filters"] for r in matching_reports(store, user, conversation, f)}
    return [_summary(found[i]) for i in sorted(found)]


def pending_delete(messages: list[AnyMessage]) -> dict[str, Any] | None:
    """The delete proposal from the latest batch of tool results, if there is one.

    The model may propose several deletions at once, so they are merged into one confirmation.
    """
    batch = _latest_tool_results(messages)
    proposals = [
        p for p in (_result(m) for m in batch if m.name == "propose_delete")
        if p.get("status") == NEEDS_CONFIRMATION
    ]
    if not proposals:
        return None
    reports = {r["id"]: r for p in proposals for r in p["reports"]}
    return {
        "reports": sorted(reports.values(), key=lambda r: r["id"]),
        "expires_at": min(p["expires_at"] for p in proposals),
        "filters": [p["filters"] for p in proposals],
    }


def make_report_tools(store: ReportStore) -> list[BaseTool]:
    """Tools for the saved reports library, all limited to the signed-in user."""

    @tool
    def create_report(
        title: str,
        content: str,
        config: RunnableConfig,
        state: Annotated[dict, InjectedState],
    ) -> str:
        """Publish a finished report. It is saved to the user's library and shown to them exactly as
        written, so use this instead of writing the report as your answer, and do not repeat it.

        Args:
            title: Short descriptive title.
            content: The complete report in markdown.
        """
        missing = _missing_quarters(state["messages"], title)
        if missing:
            emit("report_wrong_period", logging.WARNING, asked=sorted(missing), title=title)
            return json.dumps({
                "status": "error",
                "message": f"Not saved: the user asked about {', '.join(sorted(missing))}, but the title is '{title}'. "
                           "Write the report for the period they asked for, and name it in the title.",
            })
        settings = config["configurable"]
        clean_title, _ = scrub_text(title)
        clean_content, _ = scrub_text(content)
        report = store.save(settings["user_id"], settings["thread_id"], clean_title, clean_content)
        emit("report_created", report_id=report.id)
        return json.dumps({"status": REPORT_CREATED, **_summary(report)})

    @tool
    def save_report(
        title: str,
        config: RunnableConfig,
        state: Annotated[dict, InjectedState],
        content: str = "",
    ) -> str:
        """Save an earlier answer to the user's library. Only use it when the user asks to save.

        Args:
            title: Short descriptive title.
            content: Leave empty to save your latest answer word for word. Only pass text when
                saving something other than your latest answer.
        """
        body = content or _latest_answer(state["messages"])
        if not body:
            return json.dumps({"status": "error", "message": "There is nothing to save yet."})
        settings = config["configurable"]
        clean_title, _ = scrub_text(title)
        clean_body, _ = scrub_text(body)
        report = store.save(settings["user_id"], settings["thread_id"], clean_title, clean_body)
        return json.dumps({"status": "saved", **_summary(report)})

    @tool
    def list_reports(config: RunnableConfig, search: str = "", this_conversation: bool = False) -> str:
        """List the user's saved reports.

        Args:
            search: Only reports containing this text.
            this_conversation: Only reports saved in the current conversation.
        """
        settings = config["configurable"]
        reports = store.find(
            settings["user_id"],
            search=search or None,
            conversation_id=settings["thread_id"] if this_conversation else None,
        )
        return json.dumps({
            "status": "ok",
            "reports": [{**_summary(r), "snippet": r.content[:SNIPPET_CHARS]} for r in reports],
        })

    @tool
    def get_report(report_id: int, config: RunnableConfig) -> str:
        """Read the full text of one of the user's saved reports."""
        found = store.find(config["configurable"]["user_id"], report_ids=[report_id])
        if not found:
            return json.dumps({"status": "not_found", "message": f"The user has no report {report_id}."})
        return json.dumps({"status": "ok", **_summary(found[0]), "content": found[0].content})

    @tool
    def propose_delete(
        config: RunnableConfig,
        search: str = "",
        this_conversation: bool = False,
        report_ids: list[int] | None = None,
        all_reports: bool = False,
    ) -> str:
        """Start deleting saved reports. This deletes nothing: the application shows the user the exact
        list and asks them to confirm. Use it for every deletion request. Filters combine with AND.

        Args:
            search: Reports containing this text, e.g. "Client X".
            this_conversation: Reports saved in the current conversation.
            report_ids: Specific report IDs.
            all_reports: Every report the user has. Only when they clearly ask for all of them.
        """
        if not (search or this_conversation or report_ids or all_reports):
            return json.dumps({"status": "error", "message": "Say which reports to delete: give at least one filter."})
        settings = config["configurable"]
        filters = {"search": search, "this_conversation": this_conversation, "report_ids": report_ids}
        reports = matching_reports(store, settings["user_id"], settings["thread_id"], filters)
        if not reports:
            return json.dumps({"status": "no_matches", "message": "No saved reports of this user match. Nothing to delete."})
        store.audit(settings["user_id"], "delete_proposed", [r.id for r in reports], settings["thread_id"])
        emit("delete_proposed", count=len(reports))
        return json.dumps({
            "status": NEEDS_CONFIRMATION,
            "reports": [_summary(r) for r in reports],
            "expires_at": (store.now() + CONFIRMATION_TTL).isoformat(),
            "filters": filters,
        })

    @tool
    def restore_deleted_reports(config: RunnableConfig) -> str:
        """Undo the user's most recent report deletion, if it was within the last 10 minutes."""
        settings = config["configurable"]
        restored = store.restore_last_delete(settings["user_id"], UNDO_WINDOW, settings["thread_id"])
        if not restored:
            return json.dumps({"status": "nothing_to_restore", "message": "No deletion in the last 10 minutes."})
        return json.dumps({"status": "restored", "reports": [_summary(r) for r in restored]})

    return [create_report, save_report, list_reports, get_report, propose_delete, restore_deleted_reports]

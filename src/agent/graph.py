"""LangGraph agent graph.

    START -> input_guard -> load_examples -> agent <-> tools --(delete proposed)--> confirm_delete
                 |            |         |                            |    |    |
              (blocked)       |    (budget spent or       (confirmed)|    |    |(something else:
                 |            |     warehouse down)                  v    |    | back to input_guard
                 |            v         v                 execute_delete  |    | as a new request)
                 |       output_guard <- give_up                     |    |(cancelled)
                 v            |                                      v    v
                END <---------+------------------------------------ END  END
"""
import logging
import re
from collections.abc import Callable

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import Command, interrupt

from agent.golden import GoldenIndex, format_examples
from agent.observability import emit
from agent.resilience import (
    StopReason,
    TurnLimits,
    close_dangling_tool_calls,
    current_turn,
    sql_statuses,
    stop_reason,
)
from agent.safety.input_guard import REFUSALS, InputGuard
from agent.safety.pii import scrub_text
from agent.storage.reports_store import ReportStore
from agent.tools.reports import UNDO_WINDOW, created_report_id, current_delete_matches, pending_delete
from agent.users import User, UserDirectory

DEFAULT_LIMITS = TurnLimits()
HISTORY_MESSAGES = 4  # recent messages given to the input classifier for context
HISTORY_CHARS = 300

MODELS_DOWN_MESSAGE = (
    "I can't reach the AI service right now, so I couldn't answer that. "
    "Your conversation is saved. Please try again in a minute."
)
GIVE_UP_MESSAGES = {
    StopReason.SQL_FAILURES: "I couldn't get a working query for this after several attempts, so I'd rather not "
                             "guess. Could you rephrase it, or narrow it to a specific period or product group?",
    StopReason.LLM_CALLS: "This needed more steps than I can take in one go. Could you split it into smaller questions?",
    StopReason.TOKENS: "This needed more analysis than I can do in one go. Could you split it into smaller questions?",
    StopReason.WAREHOUSE_UNAVAILABLE: "The data warehouse is unavailable right now, so I can't run this analysis. "
                                      "Please try again in a few minutes.",
}
PARTIAL_ANSWER_INSTRUCTION = """

## Finish now
You cannot run more queries for this question. Answer using only the results already obtained in
this conversation. Say clearly which parts of the question you could not answer."""

_CONFIRM_WORDS = {"confirm"}
_CANCEL_WORDS = {"no", "n", "nope", "cancel", "stop", "abort", "never mind", "nevermind", "don't", "do not"}
# Sounds like agreement, but a destructive action needs the exact word: ask again rather than guess.
_AGREEMENT_WORDS = {"yes", "y", "yep", "yeah", "ok", "okay", "sure", "please", "delete", "them", "do", "it", "go", "ahead", "proceed"}


class AgentState(MessagesState):
    """Conversation messages, plus what the current turn needs to route correctly."""

    blocked: bool
    examples: str  # past expert work relevant to the current question
    approved_delete: list[int]


def _recent_history(messages: list[AnyMessage]) -> str:
    recent = [m for m in messages if isinstance(m, HumanMessage) or (isinstance(m, AIMessage) and m.text)]
    return "\n".join(f"{m.type}: {m.text[:HISTORY_CHARS]}" for m in recent[-HISTORY_MESSAGES:])


def build_graph(
    llm: Runnable,
    tools: list[BaseTool],
    system_prompt: Callable[[User], str],
    input_guard: InputGuard,
    users: UserDirectory,
    checkpointer: BaseCheckpointSaver,
    reports: ReportStore,
    summary_llm: Runnable | None = None,
    limits: TurnLimits = DEFAULT_LIMITS,
    golden: GoldenIndex | None = None,
) -> CompiledStateGraph:
    """Compile the graph. Its dependencies are passed in, so tests can use scripted models and tools."""

    def check_input(state: AgentState, config: RunnableConfig) -> dict:
        *previous, message = state["messages"]
        verdict = input_guard.check(message.text, _recent_history(previous))
        emit("input_checked", logging.INFO if verdict.allowed else logging.WARNING,
             category=verdict.category, source=verdict.source)
        if verdict.allowed:
            return {"blocked": False}
        return {"blocked": True, "messages": [AIMessage(REFUSALS[verdict.category])]}

    def load_examples(state: AgentState) -> dict:
        trios = golden.search(state["messages"][-1].text) if golden else []
        return {"examples": format_examples(trios)}

    def agent(state: AgentState, config: RunnableConfig) -> dict:
        user = users.get(config["configurable"]["user_id"])
        history = close_dangling_tool_calls(state["messages"])
        prompt = system_prompt(user) + state.get("examples", "")
        try:
            return {"messages": [llm.invoke([SystemMessage(prompt), *history])]}
        except Exception as e:
            emit("models_unavailable", logging.ERROR, error=str(e)[:300])
            return {"messages": [AIMessage(MODELS_DOWN_MESSAGE)]}

    def after_tools(state: AgentState) -> str:
        if pending_delete(state["messages"]):
            return "confirm_delete"
        if created_report_id(state["messages"]) is not None:
            return "present_report"
        return "give_up" if stop_reason(current_turn(state["messages"]), limits) else "agent"

    def present_report(state: AgentState, config: RunnableConfig) -> dict:
        """Show exactly what was saved, without paying the model to write it a second time."""
        report_id = created_report_id(state["messages"])
        report = reports.find(config["configurable"]["user_id"], report_ids=[report_id])[0]
        return {"messages": [AIMessage(f"{report.content}\n\n---\n_Saved to your library as report {report.id}._")]}

    def confirm_delete(state: AgentState, config: RunnableConfig) -> Command:
        """Pause until the user decides. Re-runs from the top on each answer, so this node changes
        nothing except by its returned Command."""
        settings = config["configurable"]
        user, conversation = settings["user_id"], settings["thread_id"]
        proposal = pending_delete(state["messages"])
        shown = proposal["reports"]
        reprompt = None
        while True:
            answer = interrupt({"reports": shown, "expires_at": proposal["expires_at"], "reprompt": reprompt})
            word = str(answer).strip().lower().rstrip(".!")
            if set(re.findall(r"[a-z']+", word)) <= _AGREEMENT_WORDS:
                reprompt = "unclear"
                continue
            if word in _CONFIRM_WORDS:
                # If the matches changed, the user has to see the new list: never delete unseen reports.
                current = current_delete_matches(reports, user, conversation, proposal)
                if current and {r["id"] for r in current} != {r["id"] for r in shown}:
                    shown, reprompt = current, "changed"
                    continue
            break

        ids = [r["id"] for r in shown]
        if word in _CONFIRM_WORDS:
            if reports.now().isoformat() > proposal["expires_at"]:
                reports.audit(settings["user_id"], "delete_expired", ids, settings["thread_id"])
                emit("delete_expired", count=len(ids))
                message = "That confirmation has expired, so nothing was deleted. Ask me again if you still want to."
                return Command(update={"messages": [AIMessage(message)]}, goto=END)
            return Command(update={"approved_delete": ids}, goto="execute_delete")

        reports.audit(settings["user_id"], "delete_cancelled", ids, settings["thread_id"])
        emit("delete_cancelled", count=len(ids))
        cancelled = AIMessage("Deletion cancelled. Nothing was deleted.")
        if word in _CANCEL_WORDS:
            return Command(update={"messages": [cancelled]}, goto=END)
        # Anything else is a new request, screened like any other message.
        return Command(update={"messages": [cancelled, HumanMessage(str(answer))]}, goto="input_guard")

    def execute_delete(state: AgentState, config: RunnableConfig) -> dict:
        """The only place reports are deleted: only the IDs the user saw, only if they own them."""
        settings = config["configurable"]
        deleted = reports.soft_delete(settings["user_id"], state["approved_delete"], settings["thread_id"])
        if not deleted:
            message = "Nothing was deleted: those reports no longer exist."
        else:
            minutes = int(UNDO_WINDOW.total_seconds() // 60)
            titles = "\n".join(f"- {r.title}" for r in deleted)
            emit("delete_executed", count=len(deleted))
            message = f"Deleted {len(deleted)} report(s):\n{titles}\n\nSay \"undo\" within {minutes} minutes to restore them."
        return {"approved_delete": [], "messages": [AIMessage(message)]}

    def give_up(state: AgentState, config: RunnableConfig) -> dict:
        turn = current_turn(state["messages"])
        reason = stop_reason(turn, limits)
        emit("turn_stopped", logging.WARNING, reason=reason.value)
        if summary_llm is not None and "ok" in sql_statuses(turn):
            user = users.get(config["configurable"]["user_id"])
            prompt = system_prompt(user) + PARTIAL_ANSWER_INSTRUCTION
            try:
                return {"messages": [summary_llm.invoke([SystemMessage(prompt), *state["messages"]])]}
            except Exception as e:
                emit("partial_summary_failed", logging.ERROR, error=str(e)[:300])
        return {"messages": [AIMessage(GIVE_UP_MESSAGES[reason])]}

    def check_output(state: AgentState) -> dict:
        message = state["messages"][-1]
        clean, found = scrub_text(message.text)
        if not found:
            return {}
        emit("output_scrubbed", logging.WARNING, found=found)
        # Same id: replaces the message in the history, so the PII is not kept either.
        return {"messages": [AIMessage(clean, id=message.id)]}

    graph = StateGraph(AgentState)
    graph.add_node("input_guard", check_input)
    graph.add_node("load_examples", load_examples)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode(tools))
    graph.add_node("give_up", give_up)
    graph.add_node("present_report", present_report)
    graph.add_node("confirm_delete", confirm_delete, destinations=("execute_delete", "input_guard", END))
    graph.add_node("execute_delete", execute_delete)
    graph.add_node("output_guard", check_output)

    graph.add_edge(START, "input_guard")
    graph.add_conditional_edges("input_guard", lambda s: END if s["blocked"] else "load_examples")
    graph.add_edge("load_examples", "agent")
    graph.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: "output_guard"})
    graph.add_conditional_edges("tools", after_tools, ["agent", "give_up", "confirm_delete", "present_report"])
    graph.add_edge("present_report", "output_guard")
    graph.add_edge("execute_delete", END)
    graph.add_edge("give_up", "output_guard")
    graph.add_edge("output_guard", END)
    return graph.compile(checkpointer=checkpointer)

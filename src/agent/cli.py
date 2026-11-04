"""Command-line chat interface.

Usage:
    python -m agent.cli [--user alice] [--verbose]
"""
import argparse
import json
import uuid

from google.auth.exceptions import DefaultCredentialsError
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command
from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from agent.app import build_agent
from agent.config import ConfigError, Settings
from agent.observability import Telemetry, setup_logging, summarize
from agent.trace_view import render_stats, render_trace
from agent.users import UserDirectory

HELP = """\
Ask questions about sales, customers, products and orders in plain English.

Commands:
  /new             start a new conversation
  /trace [id]      show every step of the last answer, or of a past one
  /stats [all]     show metrics for this session, or for all sessions
  /good, /bad [note]   rate the last answer
  /help            show this help
  /exit            quit (or Ctrl-D)
"""

EXAMPLES = [
    "What data do we have available?",
    "What was our monthly revenue over the last 6 months?",
    "Which of the products I manage perform best, and why?",
]

RECURSION_LIMIT = 40  # backstop only: TurnLimits normally ends a turn well before this


class ChatCLI:
    """The interactive chat loop for one signed-in user."""

    def __init__(self, user_id: str, verbose: bool) -> None:
        self.console = Console()
        users = UserDirectory()
        self.user = users.get(user_id)
        self.verbose = verbose
        self.thread_id = self._new_thread()
        self.session: PromptSession = PromptSession(history=InMemoryHistory())
        self.settings = Settings.from_env()
        setup_logging(self.settings.log_dir, self.settings.log_level)
        self.telemetry = Telemetry(self.settings.log_dir)
        with self.console.status("Connecting to Gemini and BigQuery…"):
            self.agent = build_agent(self.settings, users)

    @staticmethod
    def _new_thread() -> str:
        return uuid.uuid4().hex[:8]

    def run(self) -> None:
        """Read messages until the user exits."""
        examples = "\n".join(f"  • {e}" for e in EXAMPLES)
        self.console.print(Panel(
            f"Signed in as [bold]{self.user.name}[/]\nAccess: {self.user.scope.describe()}\n"
            f"Type /help for commands.\n\nTry:\n{examples}",
            title="Retail Analytics Assistant", border_style="cyan",
        ))
        while True:
            try:
                text = self.session.prompt(f"\n{self.user.id} › ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text:
                continue
            if text.lower() in ("exit", "quit"):
                break
            if text.startswith("/"):
                if not self._command(text):
                    break
                continue
            self._turn(text)
        self.console.print("Bye.")

    def _command(self, text: str) -> bool:
        cmd = text.split()[0].lower()
        if cmd in ("/exit", "/quit"):
            return False
        if cmd == "/new":
            self.thread_id = self._new_thread()
            self.console.print("[dim]Started a new conversation.[/]")
        elif cmd == "/help":
            self.console.print(HELP, markup=False)
        elif cmd == "/trace":
            self._show_trace(text.split()[1] if len(text.split()) > 1 else None)
        elif cmd == "/stats":
            self._show_stats(everything=text.split()[1:] == ["all"])
        elif cmd in ("/good", "/bad"):
            self._feedback(cmd[1:], text[len(cmd):].strip())
        else:
            self.console.print(f"[yellow]Unknown command {cmd}. Type /help.[/]")
        return True

    def _show_trace(self, trace_id: str | None) -> None:
        trace = self.telemetry.find_turn(trace_id)
        if trace is None:
            self.console.print("[yellow]No trace found.[/]")
        else:
            self.console.print(render_trace(trace))

    def _show_stats(self, everything: bool) -> None:
        if everything:
            stats, title = summarize(*self.telemetry.load()), "Metrics: all sessions"
        else:
            stats = summarize(self.telemetry.session_turns, self.telemetry.session_feedback)
            title = "Metrics: this session"
        self.console.print(render_stats(stats, title))

    def _feedback(self, score: str, comment: str) -> None:
        latest = self.telemetry.find_turn()
        if latest is None:
            self.console.print("[yellow]There is no answer to rate yet.[/]")
            return
        self.telemetry.record_feedback(latest["trace_id"], score, comment)
        self.console.print("[dim]Thanks for the feedback.[/]")

    def _turn(self, text: str) -> None:
        trace = self.telemetry.new_turn(self.user.id, self.thread_id, text)
        config = {
            "configurable": {"thread_id": self.thread_id, "user_id": self.user.id},
            "recursion_limit": RECURSION_LIMIT,
            "callbacks": [trace.callbacks],
        }
        graph_input: dict | Command = {"messages": [HumanMessage(text)]}
        final: AIMessage | None = None
        notified = False
        try:
            with trace:
                while True:
                    confirmation = None
                    with self.console.status("Thinking…") as status:
                        for update in self.agent.stream(graph_input, config, stream_mode="updates"):
                            if "__interrupt__" in update:
                                confirmation = update["__interrupt__"][0].value
                                continue
                            for node, output in update.items():
                                for msg in (output or {}).get("messages", []):
                                    if isinstance(msg, AIMessage) and msg.tool_calls:
                                        for call in msg.tool_calls:
                                            self._show_tool_call(call)
                                            status.update(f"{_tool_label(call)}…")
                                    elif isinstance(msg, ToolMessage):
                                        self._show_tool_result(msg)
                                        status.update("Analysing results…")
                                    elif isinstance(msg, AIMessage) and node == "confirm_delete":
                                        self.console.print(f"\n{msg.text}", markup=False)
                                        notified = True
                                    elif isinstance(msg, AIMessage):
                                        final = msg
                    if confirmation is None:
                        break
                    with trace.waiting_for_user():
                        answer = self._ask_confirmation(confirmation)
                    graph_input = Command(resume=answer)
        except KeyboardInterrupt:
            self.console.print("[dim]Cancelled. You can keep asking questions.[/]")
            return
        except Exception:  # per-turn boundary: the chat must never crash
            self.console.print(Panel(
                f"Sorry, something went wrong while answering. Your conversation is saved, so please try again.\n"
                f"[dim]Reference: {trace.trace_id}[/]",
                border_style="red",
            ))
            return

        if final is None and notified:
            return
        answer = final.text if final else ""
        self.console.print()
        self.console.print(Markdown(answer or "_I couldn't produce an answer. Please try rephrasing._"))
        model = final.response_metadata.get("model_name") if final else None
        if model and model != self.settings.model:
            self.console.print(f"[dim]Answered by the backup model ({model}).[/]")

    def _ask_confirmation(self, confirmation: dict) -> str:
        """Show exactly what would be deleted and return the user's answer."""
        reports = confirmation["reports"]
        table = Table(box=box.SIMPLE, show_edge=False)
        table.add_column("ID", justify="right")
        table.add_column("Title")
        table.add_column("Created")
        for report in reports:
            table.add_row(str(report["id"]), Text(report["title"]), report["created_at"])

        self.console.print()
        if confirmation["reprompt"] == "changed":
            self.console.print("[yellow]The matching reports have changed. Please check this list again.[/]")
        elif confirmation["reprompt"]:
            self.console.print("[yellow]To delete, please type exactly: confirm[/]")
        self.console.print(Panel(
            table, title=f"Delete {len(reports)} report(s)?", title_align="left", border_style="yellow",
        ))
        try:
            return self.session.prompt("Type 'confirm' to delete, or 'cancel' › ")
        except (EOFError, KeyboardInterrupt):
            return "cancel"

    def _show_tool_call(self, call: dict) -> None:
        self.console.print(f"[dim]▸ {_tool_label(call)}[/]")
        if self.verbose and call["name"] == "run_sql":
            self.console.print(Syntax(call["args"].get("sql", ""), "sql", theme="ansi_dark", word_wrap=True))

    def _show_tool_result(self, msg: ToolMessage) -> None:
        if not self.verbose or msg.name != "run_sql":
            return
        try:
            obs = json.loads(msg.content)
        except (TypeError, ValueError):
            return
        if obs.get("status") == "ok":
            self.console.print(f"[dim]  ✓ {obs['row_count']} rows[/]")
        else:
            self.console.print(f"[dim yellow]  ✗ {obs.get('status')}: {obs.get('message', '')[:200]}[/]")


def _tool_label(call: dict) -> str:
    if call["name"] == "run_sql":
        return f"Querying: {call['args'].get('purpose') or 'running SQL'}"
    labels = {
        "describe_schema": "Looking up available data",
        "create_report": "Writing the report",
        "save_report": "Saving the report",
        "list_reports": "Looking up your reports",
        "get_report": "Opening the report",
        "propose_delete": "Finding the reports to delete",
        "restore_deleted_reports": "Restoring your reports",
        "remember_preference": "Remembering your preference",
        "forget_preferences": "Forgetting your preferences",
    }
    if call["name"] in labels:
        return labels[call["name"]]
    return f"Using {call['name']}"


def main() -> None:
    """Parse arguments and start the chat, with clear messages for setup problems."""
    parser = argparse.ArgumentParser(description="Retail analytics chat assistant")
    parser.add_argument("--user", default="alice", help="user to sign in as")
    parser.add_argument("--verbose", "-v", action="store_true", help="show SQL and tool results")
    args = parser.parse_args()
    try:
        ChatCLI(args.user, args.verbose).run()
    except ConfigError as e:
        Console().print(f"[red]Configuration error:[/] {e}")
        raise SystemExit(1) from None
    except DefaultCredentialsError:
        Console().print("[red]Not signed in to Google Cloud.[/] Run: gcloud auth application-default login")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

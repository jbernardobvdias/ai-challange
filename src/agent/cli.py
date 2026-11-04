"""Command-line chat interface.

Usage:
    python -m agent.cli [--user alice] [--verbose]
"""
import argparse
import json
import logging
import uuid

from google.auth.exceptions import DefaultCredentialsError
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax

from agent.app import build_agent
from agent.config import ROOT, ConfigError, Settings
from agent.users import UserDirectory

HELP = """\
Ask questions about sales, customers, products and orders in plain English.

Commands:
  /new    start a new conversation
  /help   show this help
  /exit   quit (or Ctrl-D)
"""

EXAMPLES = [
    "What data do we have available?",
    "What was our monthly revenue over the last 6 months?",
    "Which of the products I manage perform best, and why?",
]

RECURSION_LIMIT = 40  # backstop only: TurnLimits normally ends a turn well before this
LOG_FILE = ROOT / "logs" / "agent.log"


class ChatCLI:
    def __init__(self, user_id: str, verbose: bool) -> None:
        self.console = Console()
        users = UserDirectory()
        self.user = users.get(user_id)
        self.verbose = verbose
        self.thread_id = self._new_thread()
        self.session: PromptSession = PromptSession(history=InMemoryHistory())
        self.settings = Settings.from_env()
        with self.console.status("Connecting to Gemini and BigQuery…"):
            self.agent = build_agent(self.settings, users)

    @staticmethod
    def _new_thread() -> str:
        return uuid.uuid4().hex[:8]

    def run(self) -> None:
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
            self.console.print(HELP)
        else:
            self.console.print(f"[yellow]Unknown command {cmd}. Type /help.[/]")
        return True

    def _turn(self, text: str) -> None:
        config = {
            "configurable": {"thread_id": self.thread_id, "user_id": self.user.id},
            "recursion_limit": RECURSION_LIMIT,
        }
        final: AIMessage | None = None
        try:
            with self.console.status("Thinking…") as status:
                updates = self.agent.stream({"messages": [HumanMessage(text)]}, config, stream_mode="updates")
                for update in updates:
                    for node_output in update.values():
                        for msg in (node_output or {}).get("messages", []):
                            if isinstance(msg, AIMessage) and msg.tool_calls:
                                for call in msg.tool_calls:
                                    self._show_tool_call(call)
                                    status.update(f"{_tool_label(call)}…")
                            elif isinstance(msg, ToolMessage):
                                self._show_tool_result(msg)
                                status.update("Analysing results…")
                            elif isinstance(msg, AIMessage):
                                final = msg
        except KeyboardInterrupt:
            self.console.print("[dim]Cancelled. You can keep asking questions.[/]")
            return
        except Exception:  # per-turn boundary: the chat must never crash
            reference = uuid.uuid4().hex[:8]
            logging.exception("turn failed, reference=%s", reference)
            self.console.print(Panel(
                f"Sorry, something went wrong while answering. Your conversation is saved, so please try again.\n"
                f"[dim]Reference: {reference}[/]",
                border_style="red",
            ))
            return

        answer = final.text if final else ""
        self.console.print()
        self.console.print(Markdown(answer or "_I couldn't produce an answer. Please try rephrasing._"))
        model = final.response_metadata.get("model_name") if final else None
        if model and model != self.settings.model:
            self.console.print(f"[dim]Answered by the backup model ({model}).[/]")

    def _show_tool_call(self, call: dict) -> None:
        self.console.print(f"[dim]▸ {_tool_label(call)}[/]")
        if self.verbose and call["name"] == "run_sql":
            self.console.print(Syntax(call["args"].get("sql", ""), "sql", theme="ansi_dark", word_wrap=True))

    def _show_tool_result(self, msg: ToolMessage) -> None:
        if not self.verbose:
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
    if call["name"] == "describe_schema":
        return "Looking up available data"
    return f"Using {call['name']}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Retail analytics chat assistant")
    parser.add_argument("--user", default="alice", help="user to sign in as")
    parser.add_argument("--verbose", "-v", action="store_true", help="show SQL and tool results")
    args = parser.parse_args()
    LOG_FILE.parent.mkdir(exist_ok=True)
    logging.basicConfig(
        filename=LOG_FILE, level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        ChatCLI(args.user, args.verbose).run()
    except ConfigError as e:
        Console().print(f"[red]Configuration error:[/] {e}")
        raise SystemExit(1)
    except DefaultCredentialsError:
        Console().print("[red]Not signed in to Google Cloud.[/] Run: gcloud auth application-default login")
        raise SystemExit(1)


if __name__ == "__main__":
    main()

import io

import pytest
from langchain_core.messages import AIMessage
from langgraph.types import Interrupt
from rich.console import Console

from agent import cli
from agent.config import Settings


class FakeAgent:
    def __init__(self, behaviour):
        self.behaviour = behaviour

    def stream(self, *_, **__):
        return self.behaviour()


@pytest.fixture
def chat(monkeypatch):
    def make(behaviour):
        monkeypatch.setenv("GOOGLE_API_KEY", "x")
        monkeypatch.setenv("GCP_PROJECT", "x")
        monkeypatch.setenv("GEMINI_MODEL", "primary-model")
        monkeypatch.setattr(cli, "build_agent", lambda *_: FakeAgent(behaviour))
        instance = cli.ChatCLI("alice", verbose=False)
        instance.console = Console(file=io.StringIO(), width=120)
        return instance
    return make


def output(instance) -> str:
    return instance.console.file.getvalue()


def test_crash_shows_a_reference_and_hides_details(chat):
    def explode():
        raise RuntimeError("secret internal detail")

    instance = chat(explode)
    instance._turn("hi")
    assert "Reference:" in output(instance)
    assert "secret internal detail" not in output(instance)


def test_ctrl_c_cancels_the_turn_not_the_app(chat):
    def interrupt():
        raise KeyboardInterrupt

    instance = chat(interrupt)
    instance._turn("hi")
    assert "Cancelled" in output(instance)


def reply_from(model):
    def behaviour():
        yield {"agent": {"messages": [AIMessage("Hello", response_metadata={"model_name": model})]}}
    return behaviour


def test_backup_model_is_disclosed(chat):
    instance = chat(reply_from("backup-model"))
    instance._turn("hi")
    assert "backup model (backup-model)" in output(instance)


def test_primary_model_is_not_mentioned(chat):
    instance = chat(reply_from("primary-model"))
    instance._turn("hi")
    assert "backup" not in output(instance)


CONFIRMATION = {
    "reports": [{"id": 1, "title": "[red]Sneaky[/red] title", "created_at": "2026-11-04"}],
    "expires_at": "2026-11-04T00:05:00+00:00",
    "reprompt": False,
}


class FakeSession:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)

    def prompt(self, _):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.mark.parametrize("interruption", [KeyboardInterrupt(), EOFError()])
def test_leaving_the_confirmation_prompt_means_cancel(chat, interruption):
    instance = chat(lambda: iter(()))
    instance.session = FakeSession(interruption)
    assert instance._ask_confirmation(CONFIRMATION) == "cancel"


def test_report_titles_are_shown_literally(chat):
    instance = chat(lambda: iter(()))
    instance.session = FakeSession("cancel")
    instance._ask_confirmation(CONFIRMATION)
    assert "[red]Sneaky[/red] title" in output(instance)


def test_turn_pauses_for_confirmation_and_resumes_with_the_answer(chat):
    inputs = []

    class PausingAgent:
        def stream(self, graph_input, *_, **__):
            inputs.append(graph_input)
            if len(inputs) == 1:
                yield {"__interrupt__": (Interrupt(value=CONFIRMATION),)}
            else:
                yield {"execute_delete": {"messages": [AIMessage("Deleted 1 report(s).")]}}

    instance = chat(lambda: iter(()))
    instance.agent = PausingAgent()
    instance.session = FakeSession("confirm")
    instance._turn("Delete it")
    assert inputs[1].resume == "confirm"
    assert "Deleted 1 report(s)." in output(instance)

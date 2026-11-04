import io

import pytest
from langchain_core.messages import AIMessage
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

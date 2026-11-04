import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.resilience import (
    CircuitBreaker,
    ErrorKind,
    Pacer,
    StopReason,
    TurnLimits,
    classify_llm_error,
    close_dangling_tool_calls,
    current_turn,
    stop_reason,
)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def breaker(threshold=2, cooldown_s=60):
    clock = Clock()
    return CircuitBreaker("test", threshold, cooldown_s, clock), clock


def test_breaker_opens_after_threshold_and_recovers_after_cooldown():
    b, clock = breaker()
    b.record_failure()
    assert b.allow()
    b.record_failure()
    assert not b.allow()
    clock.now = 60
    assert b.allow()


def test_failed_trial_reopens_immediately():
    b, clock = breaker()
    b.record_failure(), b.record_failure()
    clock.now = 60
    b.record_failure()
    assert not b.allow()


def test_success_resets_the_count():
    b, _ = breaker()
    b.record_failure()
    b.record_success()
    b.record_failure()
    assert b.allow()


def test_trip_honours_a_long_retry_delay():
    b, clock = breaker(cooldown_s=60)
    b.trip(71364)
    clock.now = 71363
    assert not b.allow()
    clock.now = 71364
    assert b.allow()


def test_trip_never_shorter_than_cooldown():
    b, clock = breaker(cooldown_s=60)
    b.trip(0)
    clock.now = 59
    assert not b.allow()


@pytest.mark.parametrize("error, kind, retry_after", [
    (Exception("429 RESOURCE_EXHAUSTED. {'retryDelay': '71364s'}"), ErrorKind.QUOTA, 71364),
    (Exception("Error calling model (RESOURCE_EXHAUSTED)"), ErrorKind.QUOTA, 0),
    (Exception("503 UNAVAILABLE. high demand"), ErrorKind.TRANSIENT, 0),
    (Exception("504 DEADLINE_EXCEEDED"), ErrorKind.TRANSIENT, 0),
    (TimeoutError(), ErrorKind.TRANSIENT, 0),
    (ConnectionError(), ErrorKind.TRANSIENT, 0),
    (Exception("400 INVALID_ARGUMENT"), ErrorKind.OTHER, 0),
    (Exception("404 NOT_FOUND"), ErrorKind.OTHER, 0),
])
def test_classify_llm_error(error, kind, retry_after):
    assert classify_llm_error(error) == (kind, retry_after)


def sql_result(status):
    return ToolMessage(json.dumps({"status": status}), tool_call_id="x", name="run_sql")


def reply(tokens=0):
    return AIMessage("", usage_metadata={"input_tokens": tokens, "output_tokens": 0, "total_tokens": tokens})


def test_current_turn_starts_after_the_latest_user_message():
    old, new = sql_result("error"), sql_result("ok")
    assert current_turn([HumanMessage("a"), old, HumanMessage("b"), new]) == [new]


@pytest.mark.parametrize("statuses, reason", [
    (["ok", "ok"], None),
    (["error", "error"], None),
    (["error", "rejected", "empty"], StopReason.SQL_FAILURES),
    (["error", "error", "ok", "error", "error"], None),  # progress resets the count
    (["ok", "unavailable"], StopReason.WAREHOUSE_UNAVAILABLE),
])
def test_stop_reason_from_sql_results(statuses, reason):
    assert stop_reason([sql_result(s) for s in statuses], TurnLimits()) is reason


def test_stop_reason_llm_calls():
    limits = TurnLimits(max_llm_calls=3)
    assert stop_reason([reply()] * 2, limits) is None
    assert stop_reason([reply()] * 3, limits) is StopReason.LLM_CALLS


def test_stop_reason_tokens():
    limits = TurnLimits(max_tokens=1000)
    assert stop_reason([reply(400), reply(500)], limits) is None
    assert stop_reason([reply(600), reply(500)], limits) is StopReason.TOKENS


def test_dangling_tool_calls_get_a_result():
    call = AIMessage("", tool_calls=[{"name": "run_sql", "args": {}, "id": "c1"}])
    repaired = close_dangling_tool_calls([HumanMessage("a"), call, HumanMessage("b")])
    assert isinstance(repaired[2], ToolMessage) and repaired[2].tool_call_id == "c1"
    assert isinstance(repaired[3], HumanMessage)


def test_answered_tool_calls_are_left_alone():
    call = AIMessage("", tool_calls=[{"name": "run_sql", "args": {}, "id": "c1"}])
    messages = [call, ToolMessage("{}", tool_call_id="c1", name="run_sql")]
    assert close_dangling_tool_calls(messages) == messages


def test_pacer_spaces_out_calls():
    clock, sleeps = Clock(), []
    pacer = Pacer(2.0, clock, sleeps.append)
    pacer.wait()  # the first call never waits
    assert sleeps == []
    clock.now = 0.5
    pacer.wait()
    assert sleeps == [1.5]


def test_pacer_does_not_slow_calls_that_are_already_far_apart():
    clock, sleeps = Clock(), []
    pacer = Pacer(2.0, clock, sleeps.append)
    pacer.wait()
    clock.now = 60
    pacer.wait()
    assert sleeps == []

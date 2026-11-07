import pytest
from langchain_core.messages import AIMessage

from agent.llm import ModelChain, ModelsUnavailable
from agent.resilience import CircuitBreaker, ModelsMisconfigured
from agent.safety.input_guard import Classification, InputGuard


class Model:
    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def invoke(self, _):
        self.calls += 1
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def link(model, threshold=2):
    return ("m", model, CircuitBreaker("m", threshold, cooldown_s=60))


OK = AIMessage("answer")
OVERLOADED = Exception("503 UNAVAILABLE")


def test_primary_answers_when_healthy():
    primary, fallback = Model(OK), Model(OK)
    assert ModelChain([link(primary), link(fallback)]).invoke("q") is OK
    assert fallback.calls == 0


def test_fallback_answers_when_primary_fails():
    primary, fallback = Model(OVERLOADED), Model(OK)
    assert ModelChain([link(primary), link(fallback)]).invoke("q") is OK
    assert primary.calls == 1 and fallback.calls == 1


def test_primary_is_skipped_once_its_circuit_opens():
    primary, fallback = Model(OVERLOADED), Model(OK)
    chain = ModelChain([link(primary), link(fallback)])
    for _ in range(5):
        chain.invoke("q")
    assert primary.calls == 2 and fallback.calls == 5


def test_quota_error_opens_the_circuit_at_once():
    primary, fallback = Model(Exception("429 RESOURCE_EXHAUSTED")), Model(OK)
    chain = ModelChain([link(primary), link(fallback)])
    chain.invoke("q"), chain.invoke("q")
    assert primary.calls == 1


def test_bad_request_does_not_open_the_circuit():
    primary, fallback = Model(Exception("400 INVALID_ARGUMENT")), Model(OK)
    chain = ModelChain([link(primary), link(fallback)])
    for _ in range(4):
        chain.invoke("q")
    assert primary.calls == 4


def test_empty_response_falls_through_to_the_next_model():
    primary, fallback = Model(AIMessage("")), Model(OK)
    assert ModelChain([link(primary), link(fallback)]).invoke("q") is OK


def test_tool_call_without_text_is_a_valid_response():
    call = AIMessage("", tool_calls=[{"name": "run_sql", "args": {}, "id": "c1"}])
    assert ModelChain([link(Model(call))]).invoke("q") is call


def test_raises_when_every_model_fails():
    with pytest.raises(ModelsUnavailable):
        ModelChain([link(Model(OVERLOADED)), link(Model(OVERLOADED))]).invoke("q")


def test_input_guard_stops_waiting_on_a_failing_classifier():
    classifier = Model(OVERLOADED)
    guard = InputGuard(ModelChain([link(classifier)]))
    verdicts = [guard.check("Monthly revenue?") for _ in range(5)]
    assert all(v.allowed and v.source == "classifier_unavailable" for v in verdicts)
    assert classifier.calls == 2


def test_input_guard_uses_a_healthy_classifier():
    guard = InputGuard(ModelChain([link(Model(Classification(category="off_topic")))]))
    assert guard.check("Write a poem").category == "off_topic"


RATE_LIMITED = Exception("429 RESOURCE_EXHAUSTED {'retryDelay': '30s'}")
DAILY_LIMIT = Exception("429 RESOURCE_EXHAUSTED {'retryDelay': '54000s'}")


def test_last_model_waits_out_a_short_rate_limit():
    slept, model = [], Model(RATE_LIMITED, OK)
    chain = ModelChain([link(model)], max_quota_wait_s=45, sleep=slept.append)
    assert chain.invoke("q") is OK
    assert slept == [30] and model.calls == 2


def test_rate_limit_is_waited_out_only_once():
    slept, model = [], Model(RATE_LIMITED)
    chain = ModelChain([link(model)], max_quota_wait_s=45, sleep=slept.append)
    with pytest.raises(ModelsUnavailable):
        chain.invoke("q")
    assert slept == [30] and model.calls == 2


def test_daily_limit_is_not_waited_for():
    slept = []
    chain = ModelChain([link(Model(DAILY_LIMIT))], max_quota_wait_s=45, sleep=slept.append)
    with pytest.raises(ModelsUnavailable):
        chain.invoke("q")
    assert slept == []


def test_primary_moves_to_the_fallback_rather_than_waiting():
    slept, primary, fallback = [], Model(RATE_LIMITED), Model(OK)
    chain = ModelChain([link(primary), link(fallback)], max_quota_wait_s=45, sleep=slept.append)
    assert chain.invoke("q") is OK
    assert slept == [] and primary.calls == 1


def test_classifier_chain_does_not_wait_by_default():
    slept, model = [], Model(RATE_LIMITED)
    with pytest.raises(ModelsUnavailable):
        ModelChain([link(model)], sleep=slept.append).invoke("q")
    assert slept == []


def test_failure_then_empty_response_is_not_treated_as_success():
    primary, fallback = Model(OVERLOADED), Model(AIMessage(""))
    with pytest.raises(ModelsUnavailable):
        ModelChain([link(primary), link(fallback)]).invoke("q")


def test_bad_api_key_is_reported_as_a_setup_problem():
    bad_key = Exception("400 INVALID_ARGUMENT API key not valid")
    with pytest.raises(ModelsMisconfigured):
        ModelChain([link(Model(bad_key)), link(Model(bad_key))]).invoke("q")


def test_retired_primary_model_still_falls_back():
    assert ModelChain([link(Model(Exception("404 NOT_FOUND"))), link(Model(OK))]).invoke("q") is OK


def test_setup_errors_do_not_open_the_circuit():
    breaker = CircuitBreaker("m", 1, 60)
    with pytest.raises(ModelsMisconfigured):
        ModelChain([("m", Model(Exception("401 UNAUTHENTICATED")), breaker)]).invoke("q")
    assert breaker.allow()

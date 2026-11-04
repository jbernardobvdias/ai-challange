"""Gemini models: each call tries the primary, then the fallback, skipping any known to be down."""
import logging
from collections.abc import Sequence
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_google_genai import ChatGoogleGenerativeAI

from agent.config import Settings
from agent.resilience import CircuitBreaker, ErrorKind, classify_llm_error
from agent.safety.input_guard import Classification

logger = logging.getLogger(__name__)


class ModelsUnavailable(Exception):
    """Every model failed or is known to be down."""


def _gemini(model: str, timeout_s: float, attempts: int) -> BaseChatModel:
    # The SDK has no request timeout by default and retries 5xx with long backoff,
    # so both are bounded here. `max_retries` is the total number of attempts.
    return ChatGoogleGenerativeAI(model=model, timeout=timeout_s, max_retries=attempts)


class ModelChain:
    """Tries each (name, model, breaker) in order until one gives a usable response."""

    def __init__(self, links: Sequence[tuple[str, Runnable, CircuitBreaker]]) -> None:
        self.links = links

    def invoke(self, messages: Any) -> Any:
        last_error: Exception | None = None
        for name, model, breaker in self.links:
            if not breaker.allow():
                continue
            try:
                response = model.invoke(messages)
            except Exception as e:
                last_error = e
                kind, retry_after = classify_llm_error(e)
                logger.warning("model %s failed (%s): %s", name, kind.value, str(e)[:200])
                if kind is ErrorKind.QUOTA:
                    breaker.trip(retry_after)
                elif kind is ErrorKind.TRANSIENT:
                    breaker.record_failure()
                continue
            breaker.record_success()
            # Gemini occasionally returns nothing, e.g. after a malformed function call.
            if isinstance(response, AIMessage) and not response.text and not response.tool_calls:
                logger.warning("model %s returned an empty response", name)
                continue
            return response
        raise ModelsUnavailable(str(last_error)[:300] if last_error else "no usable response from any model")


class Models:
    """Creates model chains. Chat chains share breakers: one outage is learned about once."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._primary_breaker = CircuitBreaker(settings.model, threshold=2, cooldown_s=60)
        self._fallback_breaker = CircuitBreaker(settings.fallback_model, threshold=3, cooldown_s=30)
        self._classifier_breaker = CircuitBreaker("classifier", threshold=2, cooldown_s=120)

    def chat(self, tools: Sequence[BaseTool] = ()) -> ModelChain:
        """Primary gets one quick attempt. The fallback is the last resort, so it gets more patience."""
        s = self.settings

        def bind(model: BaseChatModel) -> Runnable:
            return model.bind_tools(tools) if tools else model

        return ModelChain([
            (s.model, bind(_gemini(s.model, s.llm_timeout_s, attempts=1)), self._primary_breaker),
            (s.fallback_model, bind(_gemini(s.fallback_model, s.fallback_timeout_s, attempts=2)), self._fallback_breaker),
        ])

    def classifier(self) -> ModelChain:
        """Cheap, fast model for screening input. Failures are handled by the InputGuard."""
        s = self.settings
        model = _gemini(s.fallback_model, s.classifier_timeout_s, attempts=1)
        return ModelChain([("classifier", model.with_structured_output(Classification), self._classifier_breaker)])

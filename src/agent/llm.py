"""Gemini chat model with automatic fallback to a second model."""
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_google_genai import ChatGoogleGenerativeAI

from agent.config import Settings


def _gemini(model: str, timeout_s: float, attempts: int) -> BaseChatModel:
    # The SDK has no request timeout by default and retries 5xx with long backoff,
    # so both are bounded here. `max_retries` is the total number of attempts.
    return ChatGoogleGenerativeAI(model=model, timeout=timeout_s, max_retries=attempts)


def build_llm(settings: Settings, tools: list[BaseTool]) -> Runnable:
    """Primary model gets a single attempt; on any error the fallback model takes over."""
    primary = _gemini(settings.model, settings.llm_timeout_s, attempts=1).bind_tools(tools)
    fallback = _gemini(settings.fallback_model, settings.llm_timeout_s, attempts=2).bind_tools(tools)
    return primary.with_fallbacks([fallback])

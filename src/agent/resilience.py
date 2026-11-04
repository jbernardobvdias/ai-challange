"""Building blocks for surviving outages and bounding cost: circuit breaker, error
classification, and per-turn budgets."""
import json
import logging
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum

import httpx
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

from agent.observability import emit


class CircuitBreaker:
    """Stops calling a failing service for a while, so users don't each wait for its timeout.

    After `threshold` consecutive failures calls are refused for `cooldown_s`. The next call after
    that is a trial: success closes the circuit, failure reopens it straight away.
    """

    def __init__(
        self,
        name: str,
        threshold: int,
        cooldown_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name, self.threshold, self.cooldown_s, self._clock = name, threshold, cooldown_s, clock
        self._failures = 0
        self._open_until = 0.0
        self._lock = threading.Lock()  # tools may run in parallel

    def allow(self) -> bool:
        with self._lock:
            return self._clock() >= self._open_until

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._open_until = 0.0

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.threshold:
                self._open(self.cooldown_s)

    def trip(self, duration_s: float) -> None:
        """Open immediately, e.g. when the service says when quota returns."""
        with self._lock:
            self._failures = self.threshold
            self._open(max(duration_s, self.cooldown_s))

    def _open(self, duration_s: float) -> None:
        self._open_until = self._clock() + duration_s
        emit("circuit_opened", logging.WARNING, circuit=self.name, seconds=round(duration_s))


class Pacer:
    """Keeps a minimum gap between model calls, to stay within free-tier rate limits."""

    def __init__(
        self,
        min_interval_s: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval_s, self._clock, self._sleep = min_interval_s, clock, sleep
        self._next_allowed = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            delay = max(0.0, self._next_allowed - now)
            self._next_allowed = max(now, self._next_allowed) + self.min_interval_s
        if delay:
            self._sleep(delay)


class ErrorKind(Enum):
    """Why a model call failed, which decides how the circuit breaker reacts."""

    QUOTA = "quota"  # retrying soon is pointless
    TRANSIENT = "transient"  # overloaded or timed out
    OTHER = "other"  # e.g. a bad request; says nothing about the service's health


_QUOTA_RE = re.compile(r"\b(429|RESOURCE_EXHAUSTED)\b")
_TRANSIENT_RE = re.compile(r"\b(500|502|503|504|UNAVAILABLE|DEADLINE_EXCEEDED|INTERNAL)\b")
_RETRY_DELAY_RE = re.compile(r"""retryDelay['"]?\s*:\s*['"]?(\d+)""")


def classify_llm_error(error: Exception) -> tuple[ErrorKind, float]:
    """Return the kind of failure and, for quota errors, seconds until retrying makes sense."""
    if isinstance(error, (TimeoutError, ConnectionError, httpx.TransportError)):
        return ErrorKind.TRANSIENT, 0.0
    text = str(error)
    if _QUOTA_RE.search(text):
        delay = _RETRY_DELAY_RE.search(text)
        return ErrorKind.QUOTA, float(delay.group(1)) if delay else 0.0
    if _TRANSIENT_RE.search(text):
        return ErrorKind.TRANSIENT, 0.0
    return ErrorKind.OTHER, 0.0


@dataclass(frozen=True)
class TurnLimits:
    """Hard limits for answering one user message."""
    max_llm_calls: int = 8
    max_consecutive_sql_failures: int = 3
    max_tokens: int = 150_000


class StopReason(Enum):
    """Why a turn stopped before the model finished."""

    SQL_FAILURES = "sql_failures"
    LLM_CALLS = "llm_calls"
    TOKENS = "tokens"
    WAREHOUSE_UNAVAILABLE = "warehouse_unavailable"


def current_turn(messages: Sequence[AnyMessage]) -> list[AnyMessage]:
    """The messages since the user's latest message."""
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            return list(messages[i + 1:])
    return list(messages)


def sql_statuses(turn: Sequence[AnyMessage]) -> list[str]:
    """Status of each run_sql call in this turn, oldest first."""
    statuses = []
    for m in turn:
        if isinstance(m, ToolMessage) and m.name == "run_sql":
            try:
                statuses.append(json.loads(m.content)["status"])
            except (TypeError, ValueError, KeyError):
                statuses.append("error")
    return statuses


def _repeated_failed_sql(turn: Sequence[AnyMessage]) -> bool:
    """True if the model just re-sent SQL that had already failed: another try cannot help."""
    sql_by_call = {
        c["id"]: " ".join(str(c["args"].get("sql", "")).split()).lower()
        for m in turn if isinstance(m, AIMessage) for c in m.tool_calls if c["name"] == "run_sql"
    }
    results = [m for m in turn if isinstance(m, ToolMessage) and m.name == "run_sql"][-2:]
    if len(results) < 2 or "ok" in sql_statuses(results):
        return False
    first, second = (sql_by_call.get(m.tool_call_id) for m in results)
    return first is not None and first == second


def stop_reason(turn: Sequence[AnyMessage], limits: TurnLimits) -> StopReason | None:
    """Why this turn must stop calling the model, or None if it may continue."""
    statuses = sql_statuses(turn)
    if "unavailable" in statuses:
        return StopReason.WAREHOUSE_UNAVAILABLE
    if _repeated_failed_sql(turn):
        return StopReason.SQL_FAILURES

    failures = 0
    for status in reversed(statuses):
        if status == "ok":
            break
        failures += 1
    if failures >= limits.max_consecutive_sql_failures:
        return StopReason.SQL_FAILURES

    replies = [m for m in turn if isinstance(m, AIMessage)]
    if len(replies) >= limits.max_llm_calls:
        return StopReason.LLM_CALLS
    if sum((m.usage_metadata or {}).get("total_tokens", 0) for m in replies) >= limits.max_tokens:
        return StopReason.TOKENS
    return None


def close_dangling_tool_calls(messages: Sequence[AnyMessage]) -> list[AnyMessage]:
    """Answer tool calls left open by a cancelled or crashed turn.

    Gemini rejects a history containing a tool call with no result, which would otherwise break
    every later message in the conversation.
    """
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    repaired: list[AnyMessage] = []
    for m in messages:
        repaired.append(m)
        if isinstance(m, AIMessage):
            repaired += [
                ToolMessage(json.dumps({"status": "cancelled"}), tool_call_id=c["id"], name=c["name"])
                for c in m.tool_calls if c["id"] not in answered
            ]
    return repaired

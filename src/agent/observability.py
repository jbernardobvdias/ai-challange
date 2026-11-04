"""One trace per user turn, as JSON lines in logs/traces.jsonl.

A trace holds the full correspondence (user message, each model call, each tool call and result)
and domain events such as guard blocks or circuit breakers, joined by trace_id with the JSON logs.
Model, tool and node timings come from one LangChain callback handler. Everything else calls emit().

Telemetry must never break the agent, and must never store personal data: every string is scrubbed
when a trace is written. In production the same spans go to Cloud Trace and Langfuse.
"""
import contextvars
import hashlib
import json
import logging
import threading
import time
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage, SystemMessage
from langgraph.errors import GraphInterrupt

from agent.safety.pii import scrub_text

logger = logging.getLogger("agent.events")

MAX_STRING_CHARS = 4000
_INJECTED_ARGS = {"state", "config"}  # tool arguments the model does not write

_current: contextvars.ContextVar["TurnTrace | None"] = contextvars.ContextVar("current_trace", default=None)


def _clean(value: Any) -> Any:
    """Scrub personal data and bound the size of everything that gets stored."""
    if isinstance(value, str):
        text, _ = scrub_text(value)
        return text if len(text) <= MAX_STRING_CHARS else text[:MAX_STRING_CHARS] + "…[truncated]"
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _clean(str(value))


def emit(name: str, level: int = logging.INFO, **attrs: Any) -> None:
    """Record a domain event on the current trace and in the log."""
    attrs = _clean(attrs)
    trace = _current.get()
    if trace is not None:
        trace.add_event(name, attrs)
    logger.log(level, name, extra={"attrs": attrs})


class JsonLogFormatter(logging.Formatter):
    """One JSON object per line, with the trace ID, so logs and traces can be joined."""

    def format(self, record: logging.LogRecord) -> str:
        trace = _current.get()
        entry = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "trace_id": trace.trace_id if trace else None,
            "message": _clean(record.getMessage()),
        }
        if hasattr(record, "attrs"):
            entry["attrs"] = record.attrs
        if record.exc_info:
            entry["exception"] = _clean(self.formatException(record.exc_info))
        return json.dumps(entry, default=str)


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_dir / "agent.log")
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    logging.getLogger("agent").setLevel(logging.INFO)


def _message_text(message: BaseMessage) -> str:
    return message.text if isinstance(message.text, str) else str(message.content)


class _SpanRecorder(BaseCallbackHandler):
    """Turns LangChain callbacks into spans. Tools may run in parallel, hence the trace's lock."""

    def __init__(self, trace: "TurnTrace") -> None:
        self.trace = trace
        self.open: dict[Any, dict[str, Any]] = {}

    def _start(self, run_id: Any, kind: str, name: str, metadata: dict | None, **attrs: Any) -> None:
        self.open[run_id] = {
            "kind": kind, "name": name, "node": (metadata or {}).get("langgraph_node"),
            "_start": time.monotonic(), "status": "ok", **attrs,
        }

    def _end(self, run_id: Any, **attrs: Any) -> None:
        span = self.open.pop(run_id, None)
        if span is not None:
            self.trace.add_span(span | attrs)

    def on_chain_start(self, serialized: Any, inputs: Any, *, run_id: Any, metadata: dict | None = None, **kw: Any) -> None:
        # Only the graph's nodes, not the many small runnables inside them.
        if metadata and metadata.get("langgraph_node") == kw.get("name"):
            self._start(run_id, "node", kw["name"], metadata)

    def on_chain_end(self, outputs: Any, *, run_id: Any, **kw: Any) -> None:
        self._end(run_id)

    def on_chain_error(self, error: BaseException, *, run_id: Any, **kw: Any) -> None:
        if isinstance(error, GraphInterrupt):
            self._end(run_id, status="paused")
        else:
            self._end(run_id, status="error", error=str(error))

    def on_chat_model_start(self, serialized: Any, messages: list[list[BaseMessage]], *, run_id: Any,
                            metadata: dict | None = None, **kw: Any) -> None:
        batch = messages[0]
        system = next((m for m in batch if isinstance(m, SystemMessage)), None)
        self._start(
            run_id, "llm", (metadata or {}).get("ls_model_name", "unknown"), metadata,
            input_messages=len(batch),
            input_last=_message_text(batch[-1]) if batch else "",
            # Identifies the prompt version without repeating it in every span.
            system_prompt_sha=hashlib.sha256(_message_text(system).encode()).hexdigest()[:10] if system else None,
        )

    def on_llm_end(self, response: Any, *, run_id: Any, **kw: Any) -> None:
        message = response.generations[0][0].message
        usage = message.usage_metadata or {}
        self._end(
            run_id,
            output=_message_text(message),
            tool_calls=[{"name": c["name"], "args": c["args"]} for c in message.tool_calls],
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            finish_reason=message.response_metadata.get("finish_reason"),
        )

    def on_llm_error(self, error: BaseException, *, run_id: Any, **kw: Any) -> None:
        self._end(run_id, status="error", error=str(error))

    def on_tool_start(self, serialized: Any, input_str: str, *, run_id: Any, metadata: dict | None = None,
                      inputs: dict | None = None, **kw: Any) -> None:
        args = {k: v for k, v in (inputs or {}).items() if k not in _INJECTED_ARGS}
        self._start(run_id, "tool", (serialized or {}).get("name") or kw.get("name", "unknown"), metadata, args=args)

    def on_tool_end(self, output: Any, *, run_id: Any, **kw: Any) -> None:
        content = getattr(output, "content", output)
        try:
            status = json.loads(content).get("status", "ok")
        except (TypeError, ValueError, AttributeError):
            status = "ok"
        self._end(run_id, status=status, output=content)

    def on_tool_error(self, error: BaseException, *, run_id: Any, **kw: Any) -> None:
        self._end(run_id, status="error", error=str(error))


class TurnTrace:
    """Everything that happened while answering one user message."""

    def __init__(self, telemetry: "Telemetry", user_id: str, session_id: str, question: str) -> None:
        self.telemetry = telemetry
        self.trace_id = uuid.uuid4().hex[:12]
        self.user_id, self.session_id, self.question = user_id, session_id, question
        self.spans: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.outcome: str | None = None
        self.error: str | None = None
        self.waiting_s = 0.0
        self.callbacks = _SpanRecorder(self)
        self._lock = threading.Lock()
        self._token: contextvars.Token | None = None

    def add_span(self, span: dict[str, Any]) -> None:
        start = span.pop("_start")
        span["start_ms"] = round((start - self._started) * 1000)
        span["duration_ms"] = round((time.monotonic() - start) * 1000)
        with self._lock:
            self.spans.append(span)

    def add_event(self, name: str, attrs: dict[str, Any]) -> None:
        with self._lock:
            self.events.append({"name": name, "at_ms": round((time.monotonic() - self._started) * 1000), **attrs})

    @contextmanager
    def waiting_for_user(self) -> Iterator[None]:
        """Time spent waiting for the user is not latency."""
        start = time.monotonic()
        try:
            yield
        finally:
            self.waiting_s += time.monotonic() - start

    def __enter__(self) -> "TurnTrace":
        self._started = time.monotonic()
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._token = _current.set(self)
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> None:
        if exc_type is KeyboardInterrupt:
            self.outcome = "cancelled"
        elif exc is not None:
            self.outcome, self.error = "error", f"{exc_type.__name__}: {exc}"
            logger.error("turn failed", exc_info=(exc_type, exc, tb))
        try:
            self.telemetry.write_turn(self._record())
        except Exception:
            logger.exception("could not write trace")  # telemetry must never break the agent
        _current.reset(self._token)

    def _derive_outcome(self) -> str:
        names = [e["name"] for e in self.events]
        if "models_unavailable" in names:
            return "models_down"
        stopped = next((e for e in self.events if e["name"] == "turn_stopped"), None)
        if stopped:
            return f"gave_up:{stopped['reason']}"
        checks = [e for e in self.events if e["name"] == "input_checked"]
        if checks and checks[-1]["category"] != "allowed":
            return "refused"
        return "answered"

    def _record(self) -> dict[str, Any]:
        with self._lock:
            spans = sorted(self.spans, key=lambda s: s["start_ms"])
            events = list(self.events)
        llm = [s for s in spans if s["kind"] == "llm"]
        return _clean({
            "type": "turn",
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "started_at": self._started_at,
            "question": self.question,
            "outcome": self.outcome or self._derive_outcome(),
            "error": self.error,
            "latency_ms": round((time.monotonic() - self._started - self.waiting_s) * 1000),
            "waiting_ms": round(self.waiting_s * 1000),
            "llm_calls": len(llm),
            "total_tokens": sum(s.get("total_tokens", 0) for s in llm),
            "spans": spans,
            "events": events,
        })


class Telemetry:
    def __init__(self, log_dir: Path) -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        self.path = log_dir / "traces.jsonl"
        self.session_turns: list[dict[str, Any]] = []
        self.session_feedback: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def new_turn(self, user_id: str, session_id: str, question: str) -> TurnTrace:
        return TurnTrace(self, user_id, session_id, question)

    def _append(self, record: dict[str, Any]) -> None:
        with self._lock, self.path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def write_turn(self, record: dict[str, Any]) -> None:
        self.session_turns.append(record)
        self._append(record)

    def record_feedback(self, trace_id: str, score: str, comment: str = "") -> None:
        record = _clean({
            "type": "feedback", "trace_id": trace_id, "score": score, "comment": comment,
            "at": datetime.now(timezone.utc).isoformat(),
        })
        self.session_feedback.append(record)
        self._append(record)

    def load(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """All turns and feedback on disk. A damaged line is skipped, not fatal."""
        turns, feedback = [], []
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                (turns if record.get("type") == "turn" else feedback).append(record)
        return turns, feedback

    def find_turn(self, trace_id: str | None = None) -> dict[str, Any] | None:
        """A turn by ID or ID prefix. Without an ID, the latest one."""
        if trace_id is None:
            return self.session_turns[-1] if self.session_turns else None
        turns, _ = self.load()
        return next((t for t in reversed(turns) if t["trace_id"].startswith(trace_id)), None)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(int(len(ordered) * fraction), len(ordered) - 1)] if ordered else 0


def summarize(turns: list[dict[str, Any]], feedback: list[dict[str, Any]] = ()) -> dict[str, Any]:
    """Agent-level metrics, computed from traces so there is one source of truth."""
    latencies = [t["latency_ms"] for t in turns]
    spans = [s for t in turns for s in t["spans"]]
    events = [e for t in turns for e in t["events"]]
    llm = [s for s in spans if s["kind"] == "llm"]
    sql = [s for s in spans if s["kind"] == "tool" and s["name"] == "run_sql"]
    queries = [e for e in events if e["name"] == "bigquery_query"]
    sql_turns = [
        statuses for statuses in (
            [s["status"] for s in t["spans"] if s["kind"] == "tool" and s["name"] == "run_sql"] for t in turns
        ) if statuses
    ]
    checks = [e for e in events if e["name"] == "input_checked"]
    count = Counter(e["name"] for e in events)
    time_by_kind: Counter = Counter()
    for s in spans:
        if s["kind"] == "node":
            time_by_kind[s["name"]] += s["duration_ms"]
    return {
        "turns": len(turns),
        "outcomes": dict(Counter(t["outcome"] for t in turns)),
        "latency_p50_ms": _percentile(latencies, 0.5),
        "latency_p95_ms": _percentile(latencies, 0.95),
        "time_by_node_ms": dict(time_by_kind.most_common()),
        "llm_calls": len(llm),
        "llm_calls_by_model": dict(Counter(s["name"] for s in llm)),
        "llm_time_ms": sum(s["duration_ms"] for s in llm),
        "llm_errors": sum(s["status"] == "error" for s in llm),
        "tokens": sum(s.get("total_tokens", 0) for s in llm),
        "fallback_answers": count["fallback_used"],
        "model_failures_by_kind": dict(Counter(e["kind"] for e in events if e["name"] == "model_failed")),
        "circuits_opened": count["circuit_opened"],
        "sql_queries": len(sql),
        "sql_by_status": dict(Counter(s["status"] for s in sql)),
        "sql_first_try_success": (
            sum(statuses[0] == "ok" for statuses in sql_turns) / len(sql_turns) if sql_turns else None
        ),
        "sql_time_ms": sum(s["duration_ms"] for s in sql),
        "bytes_scanned": sum(e["bytes"] for e in queries if not e["cached"]),
        "cache_hits": sum(e["cached"] for e in queries),
        "input_blocked": dict(Counter(e["category"] for e in checks if e["category"] != "allowed")),
        "classifier_unavailable": sum(e["source"] == "classifier_unavailable" for e in checks),
        "sql_rejected": dict(Counter(e["code"] for e in events if e["name"] == "sql_rejected")),
        "pii_redactions": count["output_scrubbed"] + count["result_scrubbed"],
        "deletes_proposed": count["delete_proposed"],
        "deletes_executed": count["delete_executed"],
        "deletes_cancelled": count["delete_cancelled"],
        "deletes_expired": count["delete_expired"],
        "feedback": dict(Counter(f["score"] for f in feedback)),
    }

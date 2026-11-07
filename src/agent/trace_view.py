"""Renders traces and metrics for the CLI's /trace and /stats commands."""
import json
from typing import Any

from rich import box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

_FAILED = {"error", "rejected", "empty", "unavailable", "setup_error"}


def _seconds(ms: float) -> str:
    return f"{ms / 1000:.1f}s"


def _short(text: Any, limit: int = 110) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _detail(span: dict[str, Any]) -> str:
    if span.get("error"):
        return _short(span["error"])
    if span["kind"] == "llm":
        calls = span.get("tool_calls") or []
        action = ("→ " + ", ".join(c["name"] for c in calls)) if calls else _short(span.get("output", ""), 70)
        return f"[{span.get('node')}] {span.get('total_tokens', 0):,} tokens {action}"
    if span["name"] == "run_sql":
        output = _output_detail(span)
        sql = _short(span.get("args", {}).get("sql", ""), 80)
        return f"{span.get('args', {}).get('purpose', '')} | {output}\n{sql}"
    return _output_detail(span)


def _output_detail(span: dict[str, Any]) -> str:
    try:
        output = json.loads(span.get("output", ""))
    except (TypeError, ValueError):
        return ""
    if not isinstance(output, dict):
        return ""
    if "row_count" in output:
        return f"{output['row_count']} rows"
    return _short(output.get("message") or output.get("error_type") or "", 90)


def render_trace(trace: dict[str, Any]) -> RenderableType:
    """A timeline of one turn: every step, its timing and status, and what went wrong."""
    timeline = Table(box=box.SIMPLE, show_edge=False, expand=True)
    timeline.add_column("At", justify="right", no_wrap=True)
    timeline.add_column("Took", justify="right", no_wrap=True)
    timeline.add_column("Step", no_wrap=True)
    timeline.add_column("Status", no_wrap=True)
    timeline.add_column("Detail")

    entries = [(s["start_ms"], "span", s) for s in trace["spans"] if s["kind"] != "node"]
    entries += [(e["at_ms"], "event", e) for e in trace["events"]]
    for at, kind, item in sorted(entries, key=lambda entry: entry[0]):
        if kind == "span":
            status = item["status"]
            style = "red" if status in _FAILED else ""
            timeline.add_row(
                _seconds(at), _seconds(item["duration_ms"]), Text(item["name"]),
                Text(status, style=style), Text(_detail(item)),
            )
        else:
            attrs = {k: v for k, v in item.items() if k not in ("name", "at_ms")}
            timeline.add_row(_seconds(at), "", Text(item["name"], style="dim"), "", Text(_short(attrs), style="dim"))

    nodes = Table(box=None, show_header=False, padding=(0, 2))
    for span in sorted((s for s in trace["spans"] if s["kind"] == "node"), key=lambda s: -s["duration_ms"]):
        nodes.add_row(Text(f"{span['name']} [{span['status']}]"), _seconds(span["duration_ms"]))

    wait = f" (+{_seconds(trace['waiting_ms'])} waiting for you)" if trace["waiting_ms"] else ""
    header = Text(
        f"{trace['user_id']} · {trace['outcome']} · {_seconds(trace['latency_ms'])}{wait} · "
        f"{trace['llm_calls']} model calls · {trace['total_tokens']:,} tokens · "
        f"est. ${trace.get('estimated_cost_usd', 0):.5f}\n{_short(trace['question'], 120)}"
    )
    parts: list[RenderableType] = [header, timeline]
    if trace.get("error"):
        parts.append(Text(_short(trace["error"], 300), style="red"))
    parts += [Text("Time per graph step (repeats shown separately)", style="bold"), nodes]
    return Panel(Group(*parts), title=f"Trace {trace['trace_id']}", title_align="left", border_style="cyan")


def _fmt(value: Any) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{k}: {v:,}" if isinstance(v, int) else f"{k}: {v}" for k, v in value.items()) or "-"
    if value is None:
        return "-"
    return f"{value:,}" if isinstance(value, int) else str(value)


def render_stats(stats: dict[str, Any], title: str) -> RenderableType:
    """The output of summarize() as a table."""
    def percent(value: float | None) -> str | None:
        return f"{value:.0%}" if value is not None else None

    rows = [
        ("Reliability", [
            ("Turns", stats["turns"]),
            ("Outcomes", stats["outcomes"]),
        ]),
        ("Latency", [
            ("p50 / p95", f"{_seconds(stats['latency_p50_ms'])} / {_seconds(stats['latency_p95_ms'])}"),
            ("Per model call p50 / p95", f"{_seconds(stats['llm_latency_p50_ms'])} / {_seconds(stats['llm_latency_p95_ms'])}"),
            ("Per SQL call p50 / p95", f"{_seconds(stats['sql_latency_p50_ms'])} / {_seconds(stats['sql_latency_p95_ms'])}"),
            ("Time in model calls", _seconds(stats["llm_time_ms"])),
            ("Time in SQL tool", _seconds(stats["sql_time_ms"])),
        ]),
        ("Models", [
            ("Calls by model", stats["llm_calls_by_model"]),
            ("Tokens", stats["tokens"]),
            ("Tokens per turn", stats["tokens_per_turn"]),
            ("Estimated cost / per turn", f"${stats['estimated_cost_usd']:.4f} / ${stats['estimated_cost_per_turn_usd']:.5f}"),
            ("Failures by kind", stats["model_failures_by_kind"]),
            ("Answers from fallback", stats["fallback_answers"]),
            ("Circuits opened", stats["circuits_opened"]),
        ]),
        ("SQL", [
            ("Queries by status", stats["sql_by_status"]),
            ("First-try success", percent(stats["sql_first_try_success"])),
            ("Self-correction success", percent(stats["self_correction_success"])),
            ("Attempts per turn with SQL", stats["sql_attempts_per_turn"]),
            ("Empty-result rate", percent(stats["sql_empty_rate"])),
            ("Bytes scanned", stats["bytes_scanned"]),
            ("Bytes per turn", stats["bytes_per_turn"]),
            ("Cache hits", stats["cache_hits"]),
        ]),
        ("Safety", [
            ("Input blocked", stats["input_blocked"]),
            ("Classifier unavailable", stats["classifier_unavailable"]),
            ("SQL rejected", stats["sql_rejected"]),
            ("PII redactions", stats["pii_redactions"]),
        ]),
        ("Deletes", [
            ("Proposed / executed", f"{stats['deletes_proposed']} / {stats['deletes_executed']}"),
            ("Cancelled / expired", f"{stats['deletes_cancelled']} / {stats['deletes_expired']}"),
        ]),
        ("Feedback", [("Scores", stats["feedback"])]),
    ]
    table = Table(box=box.SIMPLE, show_edge=False, show_header=False)
    table.add_column(style="bold")
    table.add_column()
    table.add_column()
    for section, metrics in rows:
        for i, (name, value) in enumerate(metrics):
            table.add_row(section if i == 0 else "", name, Text(_fmt(value)))
    return Panel(table, title=title, title_align="left", border_style="cyan")

"""Last-line PII filters for query results and for the agent's final text.

The SQL guard is the primary control. These catch what slipped through by pattern: they cannot
recognise a bare person's name, which is why names are blocked at column level.
"""
import re

import pandas as pd

from agent.schema import Schema

# Pseudonymous customer keys: usable inside queries, never returned.
_INTERNAL_KEYS = {"user_id", "customer_id"}

_PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"),
    "phone": re.compile(r"(?<![\w.-])(?:\+\d{1,3}[ .-]?)?(?:\(\d{3}\)[ .-]?|\d{3}[ .-])\d{3}[ .-]\d{4}(?![\w-])"),
    "street_address": re.compile(
        r"\b\d{1,6} (?:[A-Z][\w'-]* ){1,4}(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Way|Court|Ct)\b\.?"
    ),
    "coordinates": re.compile(r"-?\b\d{1,3}\.\d{4,},\s*-?\d{1,3}\.\d{4,}\b"),
}


def scrub_text(text: str) -> tuple[str, dict[str, int]]:
    """Redact PII-looking values. Returns the clean text and a count per kind found."""
    found: dict[str, int] = {}
    for kind, pattern in _PATTERNS.items():
        text, count = pattern.subn(f"[{kind} redacted]", text)
        if count:
            found[kind] = count
    return text, found


def drop_sensitive_columns(df: pd.DataFrame, schema: Schema) -> tuple[pd.DataFrame, list[str]]:
    """Remove result columns named like PII columns or internal customer keys."""
    blocked = _INTERNAL_KEYS.union(*schema.pii_columns.values())
    dropped = [c for c in df.columns if c.lower() in blocked]
    return df.drop(columns=dropped), dropped

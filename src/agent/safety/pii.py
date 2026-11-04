"""Last-line PII filters for query results and for the agent's final text.

The SQL guard is the primary control: raw names, emails and addresses never reach the model at all.
These catch what slips through by pattern. A bare person's name has no pattern, so only labelled or
titled names are caught here; production adds Cloud DLP for this.
"""
import re

import pandas as pd

from agent.schema import Schema

_PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"),
    "phone": re.compile(r"(?<![\w.-])(?:\+\d{1,3}[ .-]?)?(?:\(\d{3}\)[ .-]?|\d{3}[ .-])\d{3}[ .-]\d{4}(?![\w-])"),
    "street_address": re.compile(
        r"\b\d{1,6} (?:[A-Z][\w'-]* ){1,4}(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Way|Court|Ct)\b\.?"
    ),
    "coordinates": re.compile(r"-?\b\d{1,3}\.\d{4,},\s*-?\d{1,3}\.\d{4,}\b"),
    "person_name": re.compile(
        r"(?:(?<![Bb]rand )(?<![Pp]roduct )(?<![Cc]ategory )\b[Nn]ame\s*[:=]\s*|\b(?:Mr|Mrs|Ms)\.?\s+)"
        r"[A-Z][a-z'-]+(?:\s+[A-Z][a-z'-]+)?"
    ),
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
    """Remove result columns named like PII columns."""
    blocked = set().union(*schema.pii_columns.values())
    dropped = [c for c in df.columns if c.lower() in blocked]
    return df.drop(columns=dropped), dropped

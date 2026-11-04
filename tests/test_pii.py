import pandas as pd
import pytest

from agent.safety.pii import drop_sensitive_columns, scrub_text
from agent.schema import Schema


@pytest.mark.parametrize("text, kind", [
    ("Contact jane.doe+x@example.co.uk now", "email"),
    ("Call (555) 123-4567", "phone"),
    ("Call 555-123-4567", "phone"),
    ("Call +1 555 123 4567", "phone"),
    ("Lives at 742 Evergreen Terrace Road", "street_address"),
    ("Lives at 12 Main St.", "street_address"),
    ("Located at 37.7749, -122.4194", "coordinates"),
])
def test_pii_is_redacted(text, kind):
    clean, found = scrub_text(text)
    assert found == {kind: 1}
    assert f"[{kind} redacted]" in clean


@pytest.mark.parametrize("text", [
    "Revenue was $1,234,567.89 in 2026-10-01, up 12.5%",
    "| Jeans | 12851 | $72,780.08 |",
    "7 For All Mankind grew 106.0% in October",
    "month,revenue\n2026-08,314190.70\n2026-09,360788.19",
    "Email is the third largest traffic source",
    "Order 1234567890 shipped",
])
def test_business_text_is_untouched(text):
    assert scrub_text(text) == (text, {})


def test_sensitive_result_columns_are_dropped():
    df = pd.DataFrame({"user_id": [1], "EMAIL": ["a"], "state": ["CA"], "total": [5.0]})
    clean, dropped = drop_sensitive_columns(df, Schema())
    assert list(clean.columns) == ["state", "total"]
    assert sorted(dropped) == ["EMAIL", "user_id"]

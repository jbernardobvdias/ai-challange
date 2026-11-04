import json

import pandas as pd
import pytest

from agent.schema import Schema
from agent.tools.sql import make_sql_tools


class FakeRunner:
    def __init__(self, result=None, error: Exception | None = None):
        self.result, self.error, self.queries = result, error, []

    def execute_query(self, sql: str) -> pd.DataFrame:
        self.queries.append(sql)
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def schema() -> Schema:
    return Schema()


def run_sql(runner, schema, max_rows=50) -> dict:
    tools = {t.name: t for t in make_sql_tools(runner, schema, max_rows)}
    return json.loads(tools["run_sql"].invoke({"sql": "SELECT 1", "purpose": "test"}))


def test_schema_prompt_hides_pii_columns(schema):
    prompt = schema.to_prompt()
    for column in schema.pii_columns["users"]:
        assert f"- {column} " not in prompt
    assert "- traffic_source " in prompt
    assert schema.pii_columns["users"] >= {"email", "first_name", "last_name", "street_address"}


def test_run_sql_ok(schema):
    result = run_sql(FakeRunner(pd.DataFrame({"month": ["2026-10"], "revenue": [123.456]})), schema)
    assert result["status"] == "ok"
    assert result["row_count"] == 1
    assert "123.46" in result["rows_csv"]


def test_run_sql_truncates_rows(schema):
    result = run_sql(FakeRunner(pd.DataFrame({"n": range(100)})), schema, max_rows=10)
    assert result["row_count"] == 100
    assert result["truncated"] is True
    assert len(result["rows_csv"].strip().splitlines()) == 11  # header + 10 rows


def test_run_sql_empty_result_gives_hint(schema):
    result = run_sql(FakeRunner(pd.DataFrame({"n": []})), schema)
    assert result["status"] == "empty"
    assert "filters" in result["message"]


def test_run_sql_error_is_returned_not_raised(schema):
    result = run_sql(FakeRunner(error=RuntimeError("Unrecognized name: order_date")), schema)
    assert result["status"] == "error"
    assert "order_date" in result["message"]

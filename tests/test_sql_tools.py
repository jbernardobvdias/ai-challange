import json

import pandas as pd
import pytest

from agent.config import ConfigError
from agent.safety.sql_guard import SqlGuard
from agent.schema import Schema
from agent.tools.sql import make_sql_tools
from agent.users import UserDirectory


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


def run_sql(runner, schema, max_rows=50, sql="SELECT 1", user="alice") -> dict:
    tools = make_sql_tools(runner, schema, SqlGuard(schema), UserDirectory(), max_rows)
    tool = {t.name: t for t in tools}["run_sql"]
    config = {"configurable": {"user_id": user}}
    return json.loads(tool.invoke({"sql": sql, "purpose": "test"}, config=config))


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
    assert "Jeans" in result["message"]  # tells the model the user's scope may be the reason


def test_run_sql_error_is_returned_not_raised(schema):
    result = run_sql(FakeRunner(error=RuntimeError("Unrecognized name: order_date")), schema)
    assert result["status"] == "error"
    assert "order_date" in result["message"]


def test_guard_runs_before_bigquery(schema):
    runner = FakeRunner(pd.DataFrame({"n": [1]}))
    sql = "SELECT email FROM `bigquery-public-data.thelook_ecommerce.users`"
    result = run_sql(runner, schema, sql=sql)
    assert (result["status"], result["error_type"]) == ("rejected", "pii_column")
    assert runner.queries == []


def test_bigquery_receives_the_rewritten_query(schema):
    runner = FakeRunner(pd.DataFrame({"n": [1]}))
    run_sql(runner, schema, sql="SELECT COUNT(*) AS n FROM `bigquery-public-data.thelook_ecommerce.orders`")
    assert "category IN ('Jeans', 'Swim', 'Dresses')" in runner.queries[0]


def test_user_id_is_not_exposed_to_the_model_as_an_argument(schema):
    tools = make_sql_tools(FakeRunner(), schema, SqlGuard(schema), UserDirectory(), 50)
    tool = {t.name: t for t in tools}["run_sql"]
    assert set(tool.args) == {"sql", "purpose"}


def test_unknown_user_cannot_run_queries(schema):
    runner = FakeRunner(pd.DataFrame({"n": [1]}))
    with pytest.raises(ConfigError):
        run_sql(runner, schema, user="mallory")
    assert runner.queries == []


def test_results_are_filtered_before_the_model_sees_them(schema):
    df = pd.DataFrame({"user_id": [7], "note": ["write to a@b.com"], "total": [1.0]})
    result = run_sql(FakeRunner(df), schema)
    assert result["columns"] == ["note", "total"]
    assert "a@b.com" not in result["rows_csv"]
    assert result["removed_columns"] == ["user_id"]

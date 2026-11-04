"""Data tools: describe the dataset and run SQL against BigQuery.

Tools never raise: failures are returned to the model as structured observations
so it can correct itself (see docs/technical_design.md §7).
"""
import json
from typing import Any

from langchain_core.tools import BaseTool, tool

from agent.bigquery_runner import BigQueryRunner
from agent.schema import Schema


def _observation(status: str, **fields: Any) -> str:
    return json.dumps({"status": status, **fields}, default=str)


def make_sql_tools(runner: BigQueryRunner, schema: Schema, max_rows: int) -> list[BaseTool]:

    @tool
    def describe_schema() -> str:
        """Describe the available tables and columns (what data exists and what it can be used for).
        Use it when the user asks about the structure of the data."""
        return schema.to_prompt()

    @tool
    def run_sql(sql: str, purpose: str) -> str:
        """Run one read-only BigQuery Standard SQL SELECT statement and return the result.

        Args:
            sql: A single SELECT statement using fully qualified table names,
                e.g. `bigquery-public-data.thelook_ecommerce.orders`.
            purpose: Short plain-English description of what the query computes,
                e.g. "monthly revenue in 2025". Shown to the user while the query runs.
        """
        try:
            df = runner.execute_query(sql)
        except Exception as e:  # BigQuery errors go back to the model to self-correct
            return _observation("error", error_type="bigquery_error", message=str(e)[:1500])

        if df.empty:
            return _observation(
                "empty",
                message="The query returned 0 rows. Check filters (spelling, date range, status values) "
                        "before concluding there is no data.",
            )
        shown = df.head(max_rows)
        return _observation(
            "ok",
            row_count=len(df),
            truncated=len(df) > max_rows,
            columns=list(df.columns),
            rows_csv=shown.to_csv(index=False, float_format="%.2f"),
        )

    return [describe_schema, run_sql]

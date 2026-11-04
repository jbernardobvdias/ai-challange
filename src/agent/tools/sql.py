"""Data tools: describe the dataset and run SQL against BigQuery.

Tools never raise: failures are returned to the model as structured observations
so it can correct itself (see docs/technical_design.md §7).

The user comes from the run config, set by the application after sign-in. It is not a tool
argument, so the model cannot choose whose data scope applies.
"""
import json
import logging
from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from agent.bigquery_runner import BigQueryRunner
from agent.safety.pii import drop_sensitive_columns, scrub_text
from agent.safety.sql_guard import SqlGuard, SqlRejected
from agent.schema import Schema
from agent.users import UserDirectory

logger = logging.getLogger(__name__)


def _observation(status: str, **fields: Any) -> str:
    return json.dumps({"status": status, **fields}, default=str)


def make_sql_tools(
    runner: BigQueryRunner,
    schema: Schema,
    guard: SqlGuard,
    users: UserDirectory,
    max_rows: int,
) -> list[BaseTool]:

    @tool
    def describe_schema() -> str:
        """Describe the available tables and columns (what data exists and what it can be used for).
        Use it when the user asks about the structure of the data."""
        return schema.to_prompt()

    @tool
    def run_sql(sql: str, purpose: str, config: RunnableConfig) -> str:
        """Run one read-only BigQuery Standard SQL SELECT statement and return the result.

        Args:
            sql: A single SELECT statement using fully qualified table names,
                e.g. `bigquery-public-data.thelook_ecommerce.orders`.
            purpose: Short plain-English description of what the query computes,
                e.g. "monthly revenue in 2025". Shown to the user while the query runs.
        """
        # Raises if there is no known user: without an identity there is no scope to apply.
        user = users.get(config["configurable"]["user_id"])

        try:
            safe_sql = guard.check(sql, user.scope)
        except SqlRejected as e:
            logger.warning("sql rejected user=%s code=%s: %s", user.id, e.code, e.message)
            return _observation("rejected", error_type=e.code, message=e.message, hint=e.hint)

        try:
            df = runner.execute_query(safe_sql)
        except Exception as e:  # BigQuery errors go back to the model to self-correct
            message, _ = scrub_text(str(e)[:1500])
            return _observation("error", error_type="bigquery_error", message=message)

        if df.empty:
            return _observation(
                "empty",
                message="The query returned 0 rows. Check filters (spelling, date range, status values) "
                        f"before concluding there is no data. This user can only see: {user.scope.describe()}.",
            )

        df, dropped = drop_sensitive_columns(df, schema)
        rows_csv, redacted = scrub_text(df.head(max_rows).to_csv(index=False, float_format="%.2f"))
        return _observation(
            "ok",
            row_count=len(df),
            truncated=len(df) > max_rows,
            columns=list(df.columns),
            rows_csv=rows_csv,
            **({"removed_columns": dropped} if dropped else {}),
            **({"redacted_values": redacted} if redacted else {}),
        )

    return [describe_schema, run_sql]

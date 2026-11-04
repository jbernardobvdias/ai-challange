"""Check every golden trio against the SQL guard and with a free BigQuery dry run.

In production this runs in the indexer before a trio becomes searchable, and periodically to catch
trios broken by schema changes.
"""
import sys

from google.cloud import bigquery

from agent.config import Settings
from agent.golden import load_trios
from agent.safety.sql_guard import SqlGuard, SqlRejected
from agent.schema import Schema
from agent.users import ProductScope


def main() -> int:
    settings = Settings.from_env()
    schema, client = Schema(), bigquery.Client(project=settings.gcp_project)
    guard = SqlGuard(schema)
    failures = 0
    for trio in load_trios(settings.golden_dir):
        try:
            safe = guard.check(trio.sql, ProductScope())
            job = client.query(safe, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False))
            print(f"  [OK]   {trio.id}: {job.total_bytes_processed:,} bytes")
        except (SqlRejected, Exception) as e:
            failures += 1
            print(f"  [FAIL] {trio.id}: {str(e)[:300]}")
    print(f"{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

"""Verify the local environment: .env, Gemini API key, BigQuery access.

Usage:
    python scripts/check_setup.py                 # run all checks
    python scripts/check_setup.py --write-schema  # also dump config/schema.yaml
"""
import argparse
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from agent.bigquery_runner import BigQueryRunner

ROOT = Path(__file__).resolve().parent.parent
TABLES = ["orders", "order_items", "products", "users"]

# Columns that must never reach the final output. Reviewed against the live schema.
PII_COLUMNS = {
    "users": {"first_name", "last_name", "email", "street_address",
              "postal_code", "latitude", "longitude", "user_geom"},
}


def ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def check_env() -> bool:
    print("1. Environment variables")
    good = True
    for var in ("GOOGLE_API_KEY", "GCP_PROJECT"):
        if os.getenv(var):
            ok(f"{var} is set")
        else:
            fail(f"{var} is missing (copy .env.example to .env and fill it in)")
            good = False
    return good


def check_gemini() -> bool:
    print("2. Gemini API")
    model = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
    try:
        from google import genai
        from google.genai import types

        # The SDK has no request timeout by default and retries 5xx up to 5 times
        # with backoff, which looks like a hang when the model is overloaded.
        http_options = types.HttpOptions(
            timeout=30_000,  # ms
            retry_options=types.HttpRetryOptions(attempts=2),
        )
        client = genai.Client(http_options=http_options)  # reads GOOGLE_API_KEY
        print(f"         calling {model} (30s timeout)...", flush=True)
        resp = client.models.generate_content(model=model, contents="Reply with the single word: OK")
        ok(f"{model} responded: {resp.text.strip()!r}")
        return True
    except Exception as e:
        fail(f"{model}: {e}")
        return False


def check_bigquery() -> BigQueryRunner | None:
    print("3. BigQuery")
    try:
        from google.cloud import bigquery

        runner = BigQueryRunner(project_id=os.getenv("GCP_PROJECT"))
        sql = f"SELECT COUNT(*) AS n_orders FROM `{runner.dataset_id}.orders`"

        dry = runner.client.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False))
        ok(f"dry run: query would scan {dry.total_bytes_processed:,} bytes")

        df = runner.execute_query(sql)
        ok(f"orders row count: {int(df.iloc[0]['n_orders']):,}")
        return runner
    except Exception as e:
        fail(str(e))
        print("         Hint: run `gcloud auth application-default login` and check GCP_PROJECT.")
        return None


def write_schema(runner: BigQueryRunner) -> None:
    print("4. Schema dump")
    schema = {"dataset": runner.dataset_id, "tables": {}}
    for table in TABLES:
        pii = PII_COLUMNS.get(table, set())
        columns = runner.get_table_schema(table)
        for col in columns:
            col["pii"] = col["name"] in pii
        schema["tables"][table] = {"description": "", "columns": columns}
        ok(f"{table}: {len(columns)} columns ({sum(c['pii'] for c in columns)} PII)")

    out = ROOT / "config" / "schema.yaml"
    out.write_text(yaml.safe_dump(schema, sort_keys=False, allow_unicode=True))
    ok(f"written to {out.relative_to(ROOT)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-schema", action="store_true", help="dump config/schema.yaml")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    results = [check_env(), check_gemini()]
    runner = check_bigquery()
    results.append(runner is not None)
    if runner and args.write_schema:
        write_schema(runner)

    print("\nAll checks passed." if all(results) else "\nSome checks failed - see above.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())

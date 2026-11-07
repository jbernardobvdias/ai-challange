import pandas as pd
import pytest
from google.api_core.exceptions import BadRequest, Forbidden, ServiceUnavailable, Unauthorized
from google.auth.exceptions import RefreshError

from agent.observability import Telemetry
from agent.resilience import CircuitBreaker
from agent.warehouse import QueryTooExpensive, Warehouse, WarehouseMisconfigured, WarehouseUnavailable


class FakeJob:
    def __init__(self, df=None, error=None, bytes_processed=1000):
        self.df, self.error, self.total_bytes_processed, self.cancelled = df, error, bytes_processed, False
        self.total_bytes_billed, self.job_id = bytes_processed, "job-1"

    def result(self, **_):
        if self.error:
            raise self.error
        return self

    def to_dataframe(self):
        return self.df

    def cancel(self):
        self.cancelled = True


class FakeClient:
    """`errors` are raised by successive real (non dry-run) queries; then `df` is returned."""

    def __init__(self, df=None, errors=(), bytes_processed=1000, dry_run_error=None, result_error=None):
        self.df = df if df is not None else pd.DataFrame({"n": [1]})
        self.errors, self.bytes_processed = list(errors), bytes_processed
        self.dry_run_error, self.result_error = dry_run_error, result_error
        self.dry_runs, self.runs, self.configs, self.jobs = 0, 0, [], []

    def query(self, sql, job_config=None, **_):
        if job_config.dry_run:
            self.dry_runs += 1
            if self.dry_run_error:
                raise self.dry_run_error
            return FakeJob(bytes_processed=self.bytes_processed)
        self.runs += 1
        self.configs.append(job_config)
        if self.errors:
            raise self.errors.pop(0)
        job = FakeJob(self.df, self.result_error)
        self.jobs.append(job)
        return job


def warehouse(client, **kwargs):
    defaults = dict(max_query_bytes=10_000, timeout_s=1, breaker=CircuitBreaker("bq", 2, 60))
    return Warehouse(client, **{**defaults, **kwargs})


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch):
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda _: None)


def test_returns_results_and_sets_the_billing_cap():
    client = FakeClient()
    assert warehouse(client).execute_query("SELECT 1")["n"].tolist() == [1]
    assert client.configs[0].maximum_bytes_billed == 10_000


def test_expensive_query_never_runs():
    client = FakeClient(bytes_processed=10_001)
    with pytest.raises(QueryTooExpensive):
        warehouse(client).execute_query("SELECT 1")
    assert client.runs == 0


def test_sql_error_goes_back_to_the_model_without_retry():
    client = FakeClient(dry_run_error=BadRequest("Unrecognized name"))
    wh = warehouse(client)
    for _ in range(5):
        with pytest.raises(BadRequest):
            wh.execute_query("SELECT nope")
    assert client.dry_runs == 5  # no retries, and the circuit stays closed
    assert client.runs == 0


def test_transient_error_is_retried():
    client = FakeClient(errors=[ServiceUnavailable("busy")])
    assert not warehouse(client).execute_query("SELECT 1").empty
    assert client.runs == 2


def test_persistent_outage_becomes_unavailable():
    client = FakeClient(errors=[ServiceUnavailable("down")] * 5)
    with pytest.raises(WarehouseUnavailable):
        warehouse(client).execute_query("SELECT 1")
    assert client.runs == 2


def test_circuit_opens_and_later_calls_fail_fast():
    client = FakeClient(errors=[ServiceUnavailable("down")] * 99)
    wh = warehouse(client)
    for i in range(5):
        with pytest.raises(WarehouseUnavailable):
            wh.execute_query(f"SELECT {i}")
    assert client.runs == 4  # 2 queries x 2 attempts, then the circuit is open


@pytest.mark.parametrize("error", [
    Forbidden("BigQuery API has not been used in project x"),
    Unauthorized("Request had invalid credentials"),
    RefreshError("Token expired"),
])
def test_login_and_api_problems_are_setup_errors_and_not_retried(error):
    client = FakeClient(errors=[error])
    wh = warehouse(client)
    with pytest.raises(WarehouseMisconfigured):
        wh.execute_query("SELECT 1")
    assert client.runs == 1
    assert wh.breaker.allow()


def test_wrong_project_is_a_setup_error_not_a_sql_error():
    client = FakeClient(dry_run_error=BadRequest("ProjectId must be non-empty"))
    with pytest.raises(WarehouseMisconfigured):
        warehouse(client).execute_query("SELECT 1")


def test_jobs_carry_labels_that_join_to_the_trace(tmp_path):
    client = FakeClient()
    with Telemetry(tmp_path).new_turn("Alice B", "t", "q") as trace:
        warehouse(client).execute_query("SELECT 1")
    assert client.configs[0].labels == {"app": "retail-analyst-agent", "trace_id": trace.trace_id, "user": "alice_b"}


def test_rate_limit_is_retried():
    client = FakeClient(errors=[Forbidden("rateLimitExceeded")])
    assert not warehouse(client).execute_query("SELECT 1").empty


def test_timed_out_job_is_cancelled_not_retried():
    client = FakeClient(result_error=TimeoutError())
    wh = warehouse(client)
    with pytest.raises(QueryTooExpensive, match="did not finish"):
        wh.execute_query("SELECT 1")
    assert client.runs == 1 and client.jobs[0].cancelled
    assert wh.breaker.allow()  # BigQuery is healthy; the query was the problem


def test_repeat_query_is_served_from_cache_even_during_an_outage():
    client = FakeClient()
    wh = warehouse(client)
    wh.execute_query("SELECT 1")
    client.errors = [ServiceUnavailable("down")] * 9
    assert not wh.execute_query("SELECT 1").empty
    assert client.runs == 1


def test_cache_entries_expire():
    now = [0.0]
    client = FakeClient()
    wh = warehouse(client, cache_ttl_s=10, clock=lambda: now[0])
    wh.execute_query("SELECT 1")
    now[0] = 11
    wh.execute_query("SELECT 1")
    assert client.runs == 2


def test_cached_results_cannot_be_modified_by_callers():
    wh = warehouse(FakeClient())
    wh.execute_query("SELECT 1").loc[0, "n"] = 99
    assert wh.execute_query("SELECT 1")["n"].tolist() == [1]

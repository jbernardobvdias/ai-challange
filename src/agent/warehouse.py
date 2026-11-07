"""Runs queries on BigQuery with cost limits, timeouts, retries, a circuit breaker and a short cache.

Wraps the client from BigQueryRunner. Exceptions tell the caller what to do next:
- BadRequest / NotFound: the SQL is wrong, so the model should fix it.
- QueryTooExpensive: too big or too slow, so the model should narrow the query.
- WarehouseUnavailable: nothing the model can do, so stop and tell the user.
- WarehouseMisconfigured: a project, API or login problem, so tell the user what to fix.
"""
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable

import pandas as pd
import requests
from google.api_core.exceptions import (
    BadGateway,
    BadRequest,
    Forbidden,
    GatewayTimeout,
    InternalServerError,
    NotFound,
    ServiceUnavailable,
    TooManyRequests,
    Unauthorized,
)
from google.api_core.retry import Retry
from google.auth.exceptions import GoogleAuthError
from google.cloud import bigquery
from google.cloud.bigquery.retry import DEFAULT_RETRY
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_random_exponential

from agent.observability import emit, job_labels
from agent.resilience import CircuitBreaker

_TRANSIENT = (
    BadGateway, GatewayTimeout, InternalServerError, ServiceUnavailable, TooManyRequests,
    ConnectionError, requests.exceptions.ConnectionError, requests.exceptions.Timeout,
)


class QueryTooExpensive(Exception):
    """The query scans too much or takes too long."""


class WarehouseMisconfigured(Exception):
    """BigQuery refused because of the project, API or login. The query isn't the problem."""


class WarehouseUnavailable(Exception):
    """BigQuery can't be reached or refuses access, so the model can't fix this."""


def _is_setup_error(error: Exception) -> bool:
    if isinstance(error, (BadRequest, NotFound)):  # usually bad SQL, unless it names the project
        return "ProjectId" in str(error) or "Not found: Project" in str(error)
    if isinstance(error, Forbidden):
        return not any(reason in str(error) for reason in ("rateLimitExceeded", "quotaExceeded"))
    return isinstance(error, (Unauthorized, GoogleAuthError))


def _is_transient(error: BaseException) -> bool:
    if isinstance(error, Forbidden):
        return "rateLimitExceeded" in str(error)
    return isinstance(error, _TRANSIENT)


class _TTLCache:
    def __init__(self, ttl_s: float, max_items: int, clock: Callable[[], float]) -> None:
        self.ttl_s, self.max_items, self._clock = ttl_s, max_items, clock
        self._items: OrderedDict[str, tuple[float, pd.DataFrame]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> pd.DataFrame | None:
        with self._lock:
            item = self._items.get(key)
            if item is None or self._clock() - item[0] > self.ttl_s:
                self._items.pop(key, None)
                return None
            return item[1].copy()

    def put(self, key: str, value: pd.DataFrame) -> None:
        with self._lock:
            self._items[key] = (self._clock(), value.copy())
            self._items.move_to_end(key)
            while len(self._items) > self.max_items:
                self._items.popitem(last=False)


class Warehouse:
    """Executes already-validated SQL on BigQuery within cost and time limits."""

    def __init__(
        self,
        client: bigquery.Client,
        max_query_bytes: int,
        timeout_s: float,
        breaker: CircuitBreaker | None = None,
        cache_ttl_s: float = 600,
        attempts: int = 2,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.max_query_bytes = max_query_bytes
        self.timeout_s = timeout_s
        self.breaker = breaker or CircuitBreaker("bigquery", threshold=2, cooldown_s=60)
        # The key is the rewritten SQL, which contains the user's scope, so results are never
        # shared between users with different access.
        self.cache = _TTLCache(cache_ttl_s, max_items=128, clock=clock)
        self._execute_with_retries = retry(
            retry=retry_if_exception(_is_transient),
            stop=stop_after_attempt(attempts),
            wait=wait_random_exponential(multiplier=0.5, max=4),
            reraise=True,
            before_sleep=lambda state: emit(
                "bigquery_retry", logging.WARNING, error=str(state.outcome.exception())[:200],
            ),
        )(self._execute)

    def execute_query(self, sql: str) -> pd.DataFrame:
        cached = self.cache.get(sql)
        if cached is not None:
            emit("bigquery_query", bytes=0, cached=True)
            return cached
        if not self.breaker.allow():
            raise WarehouseUnavailable("BigQuery circuit is open")
        try:
            df = self._execute_with_retries(sql)
        except Exception as e:
            if _is_setup_error(e):
                emit("warehouse_setup_error", logging.ERROR, error=str(e)[:300])
                raise WarehouseMisconfigured(str(e)[:300]) from e
            if isinstance(e, (BadRequest, NotFound, QueryTooExpensive)):
                self.breaker.record_success()  # BigQuery answered; the query is the problem
                raise
            self.breaker.record_failure()  # an outage
            emit("warehouse_unavailable", logging.ERROR, error=str(e)[:300])
            raise WarehouseUnavailable(str(e)[:300]) from e
        self.breaker.record_success()
        self.cache.put(sql, df)
        return df

    def _execute(self, sql: str) -> pd.DataFrame:
        api_retry: Retry = DEFAULT_RETRY.with_deadline(self.timeout_s)
        dry_run = self.client.query(
            sql,
            job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
            retry=api_retry, timeout=self.timeout_s,
        )
        needed = dry_run.total_bytes_processed or 0
        if needed > self.max_query_bytes:
            emit("query_too_expensive", logging.WARNING, bytes=needed, limit=self.max_query_bytes)
            raise QueryTooExpensive(f"Query would scan {needed:,} bytes; the limit is {self.max_query_bytes:,}.")

        job = self.client.query(
            sql,
            job_config=bigquery.QueryJobConfig(maximum_bytes_billed=self.max_query_bytes, labels=job_labels()),
            retry=api_retry, timeout=self.timeout_s, job_retry=None,
        )
        try:
            df = job.result(timeout=self.timeout_s, retry=api_retry).to_dataframe()
            emit("bigquery_query", bytes=needed, bytes_billed=job.total_bytes_billed, job_id=job.job_id,
                 cached=False, rows=len(df))
            return df
        except TimeoutError:
            job.cancel()  # stop paying for a result nobody will read
            emit("query_timed_out", logging.WARNING, job_id=job.job_id, seconds=self.timeout_s)
            raise QueryTooExpensive(f"Query did not finish within {self.timeout_s:.0f} seconds.") from None

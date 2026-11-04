"""Runs queries on BigQuery with cost limits, timeouts, retries, a circuit breaker and a short cache.

Wraps the client from BigQueryRunner. Exceptions tell the caller what to do next:
- BadRequest / NotFound: the SQL is wrong, so the model should fix it.
- QueryTooExpensive: the model should narrow the query.
- WarehouseUnavailable: nothing the model can do, so stop and tell the user.
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
)
from google.api_core.retry import Retry
from google.cloud import bigquery
from google.cloud.bigquery.retry import DEFAULT_RETRY
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_random_exponential

from agent.observability import emit
from agent.resilience import CircuitBreaker

_TRANSIENT = (
    BadGateway, GatewayTimeout, InternalServerError, ServiceUnavailable, TooManyRequests,
    ConnectionError, TimeoutError, requests.exceptions.ConnectionError, requests.exceptions.Timeout,
)


class QueryTooExpensive(Exception):
    def __init__(self, bytes_needed: int, bytes_allowed: int) -> None:
        super().__init__(f"Query would scan {bytes_needed:,} bytes; the limit is {bytes_allowed:,}.")


class WarehouseUnavailable(Exception):
    pass


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
        except (BadRequest, NotFound, QueryTooExpensive):
            self.breaker.record_success()  # BigQuery answered; the query is the problem
            raise
        except Exception as e:  # outages, but also expired credentials or a disabled API
            self.breaker.record_failure()
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
            raise QueryTooExpensive(needed, self.max_query_bytes)

        job = self.client.query(
            sql,
            job_config=bigquery.QueryJobConfig(maximum_bytes_billed=self.max_query_bytes),
            retry=api_retry, timeout=self.timeout_s, job_retry=None,
        )
        try:
            df = job.result(timeout=self.timeout_s, retry=api_retry).to_dataframe()
            emit("bigquery_query", bytes=needed, cached=False, rows=len(df))
            return df
        except TimeoutError:
            job.cancel()  # stop paying for a result nobody will read
            raise

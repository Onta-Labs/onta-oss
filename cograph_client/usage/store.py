"""Durable per-tenant API-usage store (daily buckets).

Holds pre-aggregated daily usage rows keyed by
``(tenant_id, day, kg_name, api_key_hint, route_class)`` — request count,
error count, and the summed request duration — so the dashboard's usage
panel reads a tiny relational range scan instead of raw request logs.
Rows are written as *increments* (``add``): the recorder buffers per-request
observations in process and flushes them here in batches.

Backends mirror the ``JobStore`` / ``KgStatsStore`` pattern so the deployment
is swappable:

- :class:`InMemoryUsageStore` — the zero-config default; non-durable,
  per-process.
- :class:`PostgresUsageStore` — durable, shared across tasks, over a generic
  Postgres DSN (``settings.database_url`` / ``OMNIX_DATABASE_URL``). Vendor
  neutral: a plain DSN, no cloud-provider identifiers.

Costs are deliberately NOT stored here — per-run cost already lives on the
job store (``cograph_jobs.cost``); the ``/usage`` endpoint composes the two
at read time so there is never a second source of truth for spend.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any, Optional, Protocol

from pydantic import BaseModel

from cograph_client.config import settings


class UsageBucket(BaseModel):
    """One daily usage row (or a batch increment onto one)."""

    tenant_id: str
    day: date
    # '' when the request wasn't scoped to a single KG (e.g. /ask, /jobs).
    kg_name: str = ""
    # Last-4 of the API key that made the request; '' for open-access.
    api_key_hint: str = ""
    # Coarse route family (ask/agent/query/explore/ingest/...) — small,
    # fixed cardinality; lets the dashboard answer "has this tenant queried
    # via the API yet" without storing paths.
    route_class: str = "other"
    requests: int = 0
    errors: int = 0
    duration_ms_sum: float = 0.0


def _key(b: UsageBucket) -> tuple[str, date, str, str, str]:
    return (b.tenant_id, b.day, b.kg_name, b.api_key_hint, b.route_class)


class UsageStore(Protocol):
    async def add(self, buckets: list[UsageBucket]) -> None: ...
    async def query_range(
        self, tenant_id: str, start_day: date, end_day: date
    ) -> list[UsageBucket]: ...


class InMemoryUsageStore:
    """Zero-config default; non-durable, per-process."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, date, str, str, str], UsageBucket] = {}
        self._lock = asyncio.Lock()

    async def add(self, buckets: list[UsageBucket]) -> None:
        async with self._lock:
            for b in buckets:
                row = self._rows.get(_key(b))
                if row is None:
                    self._rows[_key(b)] = b.model_copy(deep=True)
                else:
                    row.requests += b.requests
                    row.errors += b.errors
                    row.duration_ms_sum += b.duration_ms_sum

    async def query_range(
        self, tenant_id: str, start_day: date, end_day: date
    ) -> list[UsageBucket]:
        async with self._lock:
            return [
                r.model_copy(deep=True)
                for r in self._rows.values()
                if r.tenant_id == tenant_id and start_day <= r.day <= end_day
            ]


class PostgresUsageStore:
    """Durable ``UsageStore`` over a generic Postgres DSN via asyncpg.

    All queried fields are plain columns (no jsonb) — the row IS the
    aggregate. ``add`` is an upsert that *increments* the counters, so
    concurrent tasks (multiple ECS replicas) compose correctly. The pool and
    table are created lazily on first use; the DDL is idempotent.
    """

    _TABLE = "cograph_usage_daily"

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn if dsn is not None else settings.database_url
        self._pool: Any = None
        self._lock = asyncio.Lock()

    async def _ensure_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        async with self._lock:
            if self._pool is not None:
                return self._pool
            import asyncpg  # imported lazily so the dependency is optional

            pool = await asyncpg.create_pool(dsn=self._dsn)
            async with pool.acquire() as conn:
                await conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self._TABLE} (
                        tenant_id text NOT NULL,
                        day date NOT NULL,
                        kg_name text NOT NULL DEFAULT '',
                        api_key_hint text NOT NULL DEFAULT '',
                        route_class text NOT NULL DEFAULT 'other',
                        requests bigint NOT NULL DEFAULT 0,
                        errors bigint NOT NULL DEFAULT 0,
                        duration_ms_sum double precision NOT NULL DEFAULT 0,
                        PRIMARY KEY (tenant_id, day, kg_name, api_key_hint, route_class)
                    )
                    """
                )
                await conn.execute(
                    f"CREATE INDEX IF NOT EXISTS {self._TABLE}_tenant_day_idx "
                    f"ON {self._TABLE} (tenant_id, day)"
                )
            self._pool = pool
            return self._pool

    async def add(self, buckets: list[UsageBucket]) -> None:
        if not buckets:
            return
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.executemany(
                f"""
                INSERT INTO {self._TABLE}
                    (tenant_id, day, kg_name, api_key_hint, route_class,
                     requests, errors, duration_ms_sum)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (tenant_id, day, kg_name, api_key_hint, route_class)
                DO UPDATE SET
                    requests = {self._TABLE}.requests + EXCLUDED.requests,
                    errors = {self._TABLE}.errors + EXCLUDED.errors,
                    duration_ms_sum =
                        {self._TABLE}.duration_ms_sum + EXCLUDED.duration_ms_sum
                """,
                [
                    (
                        b.tenant_id,
                        b.day,
                        b.kg_name,
                        b.api_key_hint,
                        b.route_class,
                        b.requests,
                        b.errors,
                        b.duration_ms_sum,
                    )
                    for b in buckets
                ],
            )

    async def query_range(
        self, tenant_id: str, start_day: date, end_day: date
    ) -> list[UsageBucket]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT tenant_id, day, kg_name, api_key_hint, route_class,
                       requests, errors, duration_ms_sum
                FROM {self._TABLE}
                WHERE tenant_id = $1 AND day BETWEEN $2 AND $3
                """,
                tenant_id,
                start_day,
                end_day,
            )
        return [
            UsageBucket(
                tenant_id=r["tenant_id"],
                day=r["day"],
                kg_name=r["kg_name"],
                api_key_hint=r["api_key_hint"],
                route_class=r["route_class"],
                requests=r["requests"],
                errors=r["errors"],
                duration_ms_sum=r["duration_ms_sum"],
            )
            for r in rows
        ]


# A single per-process instance shared by the recorder (which flushes outside
# any request context) and the /usage read route — like the KG-stats store,
# writer and reader must see the same dict for the in-memory backend, so this
# is a module singleton rather than a request-scoped dependency.
_store: Optional[UsageStore] = None


def get_usage_store() -> UsageStore:
    """Return the process-wide usage store.

    :class:`PostgresUsageStore` when ``settings.database_url`` is set (durable,
    shared across ECS tasks), else :class:`InMemoryUsageStore`. The Postgres
    store creates its pool/table lazily, so calling this never touches the
    network.
    """
    global _store
    if _store is None:
        _store = (
            PostgresUsageStore() if settings.database_url else InMemoryUsageStore()
        )
    return _store


def reset_usage_store() -> None:
    """Test helper — clear the singleton."""
    global _store
    _store = None

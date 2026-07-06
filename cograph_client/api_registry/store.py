"""Per-tenant custom API-source storage (ONTA-2xx, Child 1).

ONTA-194 shipped an **operator-curated** catalog: entries are versioned JSON
data files, loaded at startup, with no runtime write path. This module reverses
that scope for a **per-tenant** layer only: a workspace can connect its own
private/internal APIs, stored durably and scoped strictly to that tenant. The
global public/enhanced catalog stays operator-curated and read-only.

Backends (same swappable pattern as ``enrichment/job_store.py``):

- ``InMemoryTenantApiSourceStore`` — the zero-config default; non-durable,
  per-process. Used by tests and by OSS self-host deployments without a DSN.
- ``PostgresTenantApiSourceStore`` — durable, shared-across-tasks, over a generic
  Postgres DSN (``settings.database_url``). Vendor-neutral: a plain DSN, no
  cloud-provider identifiers, works against any Postgres (local, Aurora, Neon,
  Supabase, ...).

Table ``tenant_api_sources`` holds ONE row per (tenant, slug): the full
``ApiSourceSpec`` serialized to ``spec_json``, plus ``enabled`` and audit
timestamps. The primary key ``(tenant_id, slug)`` enforces per-tenant slug
uniqueness AND makes every query naturally tenant-scoped — a store method is
*never* given a way to read across tenants.

Boundary: OSS. Pure ``cograph_client.*`` / stdlib — no ``from cograph.*``.
Secrets are NOT stored here; they live in the separate encrypted secret store
(Child 2). This module stores the spec only, which references a secret by
logical name, never by value.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from cograph_client.config import settings

from .catalog import LAYER_TENANT_CUSTOM
from .spec import ApiSourceSpec, validate_spec


# --------------------------------------------------------------------------- #
# The stored record
# --------------------------------------------------------------------------- #
@dataclass
class TenantApiSource:
    """One tenant-custom catalog entry as stored (spec + enable flag + audit)."""

    tenant_id: str
    slug: str
    spec: ApiSourceSpec
    enabled: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def materialized_spec(self) -> ApiSourceSpec:
        """The spec as it should appear in the catalog: tagged ``tenant_custom``
        and with the stored ``enabled`` flag applied.

        ``enabled`` is authoritative on the ROW (a PATCH toggles it without
        rewriting the spec body), so it wins over any stale ``spec.enabled``.
        Returns a copy so the stored spec object is never mutated in place.
        """
        import copy

        s = copy.deepcopy(self.spec)
        s.layer = LAYER_TENANT_CUSTOM
        s.enabled = self.enabled
        return s


# --------------------------------------------------------------------------- #
# The store protocol
# --------------------------------------------------------------------------- #
class TenantApiSourceStore(Protocol):
    async def list_for_tenant(self, tenant_id: str) -> list[TenantApiSource]: ...
    async def get(self, tenant_id: str, slug: str) -> Optional[TenantApiSource]: ...
    async def upsert(self, record: TenantApiSource) -> TenantApiSource: ...
    async def delete(self, tenant_id: str, slug: str) -> bool: ...


# --------------------------------------------------------------------------- #
# In-memory backend (default; tests + no-DSN self-host)
# --------------------------------------------------------------------------- #
class InMemoryTenantApiSourceStore:
    """Non-durable, per-process store. Keyed by ``(tenant_id, slug)`` so every
    lookup is naturally tenant-scoped — there is no cross-tenant read path."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], TenantApiSource] = {}
        self._lock = asyncio.Lock()

    async def list_for_tenant(self, tenant_id: str) -> list[TenantApiSource]:
        async with self._lock:
            rows = [r for (t, _), r in self._rows.items() if t == tenant_id]
        rows.sort(key=lambda r: r.slug)
        return [_copy_record(r) for r in rows]

    async def get(self, tenant_id: str, slug: str) -> Optional[TenantApiSource]:
        async with self._lock:
            r = self._rows.get((tenant_id, slug))
            return _copy_record(r) if r else None

    async def upsert(self, record: TenantApiSource) -> TenantApiSource:
        now = datetime.now(timezone.utc)
        async with self._lock:
            key = (record.tenant_id, record.slug)
            existing = self._rows.get(key)
            created = existing.created_at if existing else now
            stored = TenantApiSource(
                tenant_id=record.tenant_id,
                slug=record.slug,
                spec=record.spec,
                enabled=record.enabled,
                created_at=record.created_at or created,
                updated_at=now,
            )
            self._rows[key] = stored
            return _copy_record(stored)

    async def delete(self, tenant_id: str, slug: str) -> bool:
        async with self._lock:
            return self._rows.pop((tenant_id, slug), None) is not None


def _copy_record(r: TenantApiSource) -> TenantApiSource:
    import copy

    return TenantApiSource(
        tenant_id=r.tenant_id,
        slug=r.slug,
        spec=copy.deepcopy(r.spec),
        enabled=r.enabled,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


# --------------------------------------------------------------------------- #
# Postgres backend (durable, shared across ECS tasks)
# --------------------------------------------------------------------------- #
class PostgresTenantApiSourceStore:
    """Durable ``TenantApiSourceStore`` over a generic Postgres DSN via asyncpg.

    The pool + table are created lazily on first use so importing this module
    (and constructing the store) never touches the network; the DDL is
    idempotent. Vendor-neutral: the only configuration is a plain DSN.

    Every method is parameterized on ``tenant_id`` and the primary key is
    ``(tenant_id, slug)``, so a query can never span tenants.
    """

    _TABLE = "tenant_api_sources"

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
            from cograph_client.db.pool import get_pg_pool

            pool = await get_pg_pool(self._dsn)
            async with pool.acquire() as conn:
                await conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self._TABLE} (
                        tenant_id text NOT NULL,
                        slug text NOT NULL,
                        spec_json jsonb NOT NULL,
                        enabled boolean NOT NULL DEFAULT true,
                        created_at timestamptz NOT NULL DEFAULT now(),
                        updated_at timestamptz NOT NULL DEFAULT now(),
                        PRIMARY KEY (tenant_id, slug)
                    )
                    """
                )
            self._pool = pool
            return self._pool

    @staticmethod
    def _row_to_record(row: Any) -> TenantApiSource:
        raw = row["spec_json"]
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode()
        spec_dict = json.loads(raw) if isinstance(raw, str) else raw
        return TenantApiSource(
            tenant_id=row["tenant_id"],
            slug=row["slug"],
            spec=ApiSourceSpec.from_dict(spec_dict),
            enabled=row["enabled"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def list_for_tenant(self, tenant_id: str) -> list[TenantApiSource]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT tenant_id, slug, spec_json, enabled, created_at, updated_at "
                f"FROM {self._TABLE} WHERE tenant_id = $1 ORDER BY slug",
                tenant_id,
            )
        return [self._row_to_record(r) for r in rows]

    async def get(self, tenant_id: str, slug: str) -> Optional[TenantApiSource]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT tenant_id, slug, spec_json, enabled, created_at, updated_at "
                f"FROM {self._TABLE} WHERE tenant_id = $1 AND slug = $2",
                tenant_id,
                slug,
            )
        return self._row_to_record(row) if row else None

    async def upsert(self, record: TenantApiSource) -> TenantApiSource:
        pool = await self._ensure_pool()
        spec_json = json.dumps(record.spec.to_dict())
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO {self._TABLE}
                    (tenant_id, slug, spec_json, enabled, created_at, updated_at)
                VALUES ($1, $2, $3::jsonb, $4, now(), now())
                ON CONFLICT (tenant_id, slug) DO UPDATE SET
                    spec_json = EXCLUDED.spec_json,
                    enabled = EXCLUDED.enabled,
                    updated_at = now()
                RETURNING tenant_id, slug, spec_json, enabled, created_at, updated_at
                """,
                record.tenant_id,
                record.slug,
                spec_json,
                record.enabled,
            )
        return self._row_to_record(row)

    async def delete(self, tenant_id: str, slug: str) -> bool:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            status = await conn.execute(
                f"DELETE FROM {self._TABLE} WHERE tenant_id = $1 AND slug = $2",
                tenant_id,
                slug,
            )
        # asyncpg returns e.g. "DELETE 1" / "DELETE 0".
        return status.rsplit(" ", 1)[-1] != "0"


# --------------------------------------------------------------------------- #
# Store selection (mirrors make_job_store)
# --------------------------------------------------------------------------- #
_store: Optional[TenantApiSourceStore] = None


def make_tenant_api_source_store() -> TenantApiSourceStore:
    """Select the store backend from configuration, memoized per process.

    Returns a :class:`PostgresTenantApiSourceStore` when ``settings.database_url``
    is set (durable, shared across ECS tasks), else an
    :class:`InMemoryTenantApiSourceStore` (zero-config default). The Postgres
    store creates its pool/table lazily, so calling this never touches the
    network.
    """
    global _store
    if _store is None:
        _store = (
            PostgresTenantApiSourceStore()
            if settings.database_url
            else InMemoryTenantApiSourceStore()
        )
    return _store


def reset_tenant_api_source_store() -> None:
    """Test helper — clear the memoized store singleton."""
    global _store
    _store = None


# --------------------------------------------------------------------------- #
# Spec validation for a tenant-authored entry
# --------------------------------------------------------------------------- #
def validate_tenant_spec(spec: ApiSourceSpec) -> list[str]:
    """Validate a tenant-authored spec. Currently the same structural checks as
    the curated catalog (``validate_spec``) — a private API must be as well-formed
    as a curated one (https base_url, valid pagination/auth, etc.). Kept as a
    distinct entry point so tenant-only rules (e.g. a future per-tenant slug
    reservation) can be layered here without touching the curated validator."""
    return validate_spec(spec)


__all__ = [
    "TenantApiSource",
    "TenantApiSourceStore",
    "InMemoryTenantApiSourceStore",
    "PostgresTenantApiSourceStore",
    "make_tenant_api_source_store",
    "reset_tenant_api_source_store",
    "validate_tenant_spec",
    "LAYER_TENANT_CUSTOM",
]

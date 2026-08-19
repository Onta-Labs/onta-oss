"""Per-workspace extract-source config store (ONTA-554).

NOT ``ApiSourceSpec`` / ``tenant_api_sources``. Lookup APIs (NPPES, FRED) and
3rd-party extracts (HubSpot CRM dump, tenant Postgres) are different jobs.

Secrets are NOT stored here — only ``secret_ref`` logical names. Ciphertext
lives in the existing :mod:`infona_client.api_registry.secret_store` under
slug ``dlt:{extract_slug}``.

Backends mirror the api-sources store: in-memory default, Postgres when
``settings.database_url`` is set.
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from infona_client.config import settings
from infona_client.ingestion.models import DltExtractSource

SECRET_SLUG_PREFIX = "dlt:"


def secret_store_slug(extract_slug: str) -> str:
    """Namespace extract secrets so they never collide with api-sources slugs."""
    return f"{SECRET_SLUG_PREFIX}{extract_slug}"


@dataclass
class StoredExtractSource:
    tenant_id: str
    source: DltExtractSource
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @property
    def slug(self) -> str:
        return self.source.slug


class ExtractSourceStore(Protocol):
    async def list_for_tenant(self, tenant_id: str) -> list[StoredExtractSource]: ...
    async def get(self, tenant_id: str, slug: str) -> Optional[StoredExtractSource]: ...
    async def upsert(self, record: StoredExtractSource) -> StoredExtractSource: ...
    async def delete(self, tenant_id: str, slug: str) -> bool: ...


class InMemoryExtractSourceStore:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], StoredExtractSource] = {}
        self._lock = asyncio.Lock()

    async def list_for_tenant(self, tenant_id: str) -> list[StoredExtractSource]:
        async with self._lock:
            rows = [r for (t, _), r in self._rows.items() if t == tenant_id]
        rows.sort(key=lambda r: r.slug)
        return [_copy(r) for r in rows]

    async def get(self, tenant_id: str, slug: str) -> Optional[StoredExtractSource]:
        async with self._lock:
            r = self._rows.get((tenant_id, slug))
            return _copy(r) if r else None

    async def upsert(self, record: StoredExtractSource) -> StoredExtractSource:
        now = datetime.now(timezone.utc)
        async with self._lock:
            key = (record.tenant_id, record.source.slug)
            existing = self._rows.get(key)
            stored = StoredExtractSource(
                tenant_id=record.tenant_id,
                source=copy.deepcopy(record.source),
                created_at=(existing.created_at if existing else now),
                updated_at=now,
            )
            self._rows[key] = stored
            return _copy(stored)

    async def delete(self, tenant_id: str, slug: str) -> bool:
        async with self._lock:
            return self._rows.pop((tenant_id, slug), None) is not None


def _copy(r: StoredExtractSource) -> StoredExtractSource:
    return StoredExtractSource(
        tenant_id=r.tenant_id,
        source=r.source.model_copy(deep=True),
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


class PostgresExtractSourceStore:
    _TABLE = "tenant_extract_sources"

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
            from infona_client.db.pool import get_pg_pool

            pool = await get_pg_pool(self._dsn)
            async with pool.acquire() as conn:
                await conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self._TABLE} (
                        tenant_id text NOT NULL,
                        slug text NOT NULL,
                        spec_json jsonb NOT NULL,
                        created_at timestamptz NOT NULL DEFAULT now(),
                        updated_at timestamptz NOT NULL DEFAULT now(),
                        PRIMARY KEY (tenant_id, slug)
                    )
                    """
                )
            self._pool = pool
            return self._pool

    @staticmethod
    def _row_to_record(row: Any) -> StoredExtractSource:
        raw = row["spec_json"]
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode()
        spec_dict = json.loads(raw) if isinstance(raw, str) else raw
        return StoredExtractSource(
            tenant_id=row["tenant_id"],
            source=DltExtractSource.model_validate(spec_dict),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def list_for_tenant(self, tenant_id: str) -> list[StoredExtractSource]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT tenant_id, slug, spec_json, created_at, updated_at "
                f"FROM {self._TABLE} WHERE tenant_id = $1 ORDER BY slug",
                tenant_id,
            )
        return [self._row_to_record(r) for r in rows]

    async def get(self, tenant_id: str, slug: str) -> Optional[StoredExtractSource]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT tenant_id, slug, spec_json, created_at, updated_at "
                f"FROM {self._TABLE} WHERE tenant_id = $1 AND slug = $2",
                tenant_id,
                slug,
            )
        return self._row_to_record(row) if row else None

    async def upsert(self, record: StoredExtractSource) -> StoredExtractSource:
        pool = await self._ensure_pool()
        spec_json = json.dumps(record.source.model_dump(mode="json"))
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO {self._TABLE}
                    (tenant_id, slug, spec_json, created_at, updated_at)
                VALUES ($1, $2, $3::jsonb, now(), now())
                ON CONFLICT (tenant_id, slug) DO UPDATE SET
                    spec_json = EXCLUDED.spec_json,
                    updated_at = now()
                RETURNING tenant_id, slug, spec_json, created_at, updated_at
                """,
                record.tenant_id,
                record.source.slug,
                spec_json,
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
        return status.rsplit(" ", 1)[-1] != "0"


_store: Optional[ExtractSourceStore] = None


def make_extract_source_store() -> ExtractSourceStore:
    global _store
    if _store is None:
        _store = (
            PostgresExtractSourceStore()
            if settings.database_url
            else InMemoryExtractSourceStore()
        )
    return _store


def reset_extract_source_store() -> None:
    global _store
    _store = None

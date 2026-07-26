"""Durable storage for TENANT-layer type-attached skills.

Why a durable store and not an RDF literal on the type URI (the design call):

1. **A markdown body is a blob, not a triple.** The only literal escaper in the
   SPARQL builder (``graph/queries.py::_escape_literal``) handles ``\\``, ``"``
   and ``\\n`` — it does NOT handle ``\\r`` or ``\\t``, both of which occur in
   ordinary pasted markdown. Round-tripping author-supplied prose through it is
   a corruption (and injection) hazard for zero benefit.
2. **The ontology graph is on the hot read path.** ``list_types_query`` /
   ``fetch_types_by_layer`` are pulled for prompt assembly and for every layer
   resolution; hanging multi-KB bodies off those subjects would bloat reads that
   never want the body.
3. **The graph is what gets migrated and audited.** Skill bodies are authored
   content with revisions; a single-valued RDF literal has no version, so every
   edit is an unrecoverable DELETE/INSERT. A row has ``version`` + ``updated_at``.
4. **Write-path convergence.** ``tests/test_write_path_convergence.py`` is
   deny-by-default over all of ``cograph_client/``: a new module that builds
   raw ``insert_triples`` / graph ``DELETE`` fails CI. Skills are neither
   instance data (kg_writer's job) nor ontology schema, so putting them in the
   graph would mean minting a fourth allowlist category. Staying out of the
   graph keeps that guard's surface honest.

Backends (the same swappable pattern as ``api_registry/store.py`` and
``enrichment/job_store.py``):

- ``InMemoryTypeSkillStore`` — the zero-config default; non-durable, per-process.
- ``PostgresTypeSkillStore`` — durable, over a generic Postgres DSN
  (``settings.database_url``). Vendor-neutral: a plain DSN, no cloud identifiers.

Table ``type_skills`` holds ONE row per ``(tenant_id, type_name, slug)``. That
primary key both enforces per-type slug uniqueness AND makes every query
naturally tenant-scoped — no store method is given a way to read across tenants.
``type_name_key`` is the casefolded type name so lookups are case-tolerant
(agents pass whatever casing the user typed) while the display casing survives.

The two GLOBAL layers deliberately do NOT live here — they are curated content,
registered at startup from data files / the premium package
(``skills/registry.py``), exactly as the API-source catalog splits its
operator-curated global layers from its per-tenant store.

Boundary: OSS. Pure ``cograph_client.*`` / stdlib — no ``from cograph.*``.
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from cograph_client.config import settings
from cograph_client.graph.layers import Layer

from .models import TypeSkill


class TypeSkillStore(Protocol):
    async def list_for_tenant(
        self, tenant_id: str, type_name: Optional[str] = None
    ) -> list[TypeSkill]: ...

    async def get(
        self, tenant_id: str, type_name: str, slug: str
    ) -> Optional[TypeSkill]: ...

    async def upsert(self, skill: TypeSkill) -> TypeSkill: ...

    async def delete(self, tenant_id: str, type_name: str, slug: str) -> bool: ...


def _tkey(type_name: str) -> str:
    return (type_name or "").casefold()


def _copy(s: TypeSkill) -> TypeSkill:
    return copy.deepcopy(s)


# --------------------------------------------------------------------------- #
# In-memory backend (default; tests + no-DSN self-host)
# --------------------------------------------------------------------------- #
class InMemoryTypeSkillStore:
    """Non-durable, per-process store keyed ``(tenant_id, type_key, slug)``."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str], TypeSkill] = {}
        self._lock = asyncio.Lock()

    async def list_for_tenant(
        self, tenant_id: str, type_name: Optional[str] = None
    ) -> list[TypeSkill]:
        want = _tkey(type_name) if type_name else None
        async with self._lock:
            rows = [
                r
                for (t, tk, _), r in self._rows.items()
                if t == tenant_id and (want is None or tk == want)
            ]
        rows.sort(key=lambda r: (r.type_name.casefold(), r.slug))
        return [_copy(r) for r in rows]

    async def get(
        self, tenant_id: str, type_name: str, slug: str
    ) -> Optional[TypeSkill]:
        async with self._lock:
            r = self._rows.get((tenant_id, _tkey(type_name), slug))
            return _copy(r) if r else None

    async def upsert(self, skill: TypeSkill) -> TypeSkill:
        now = datetime.now(timezone.utc)
        async with self._lock:
            key = (skill.tenant_id or "", _tkey(skill.type_name), skill.slug)
            existing = self._rows.get(key)
            stored = _copy(skill)
            stored.layer = Layer.TENANT
            stored.created_at = existing.created_at if existing else now
            stored.updated_at = now
            stored.version = (existing.version + 1) if existing else 1
            self._rows[key] = stored
            return _copy(stored)

    async def delete(self, tenant_id: str, type_name: str, slug: str) -> bool:
        async with self._lock:
            return self._rows.pop((tenant_id, _tkey(type_name), slug), None) is not None


# --------------------------------------------------------------------------- #
# Postgres backend (durable, shared across tasks)
# --------------------------------------------------------------------------- #
class PostgresTypeSkillStore:
    """Durable ``TypeSkillStore`` over a generic Postgres DSN via asyncpg.

    The pool + table are created lazily on first use, so importing this module
    (and constructing the store) never touches the network; the DDL is
    idempotent. Uses the shared process-wide pool (``db/pool.py``) rather than
    growing a seventh private ``asyncpg.create_pool``.
    """

    _TABLE = "type_skills"

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
                        type_name_key text NOT NULL,
                        slug text NOT NULL,
                        type_name text NOT NULL,
                        title text NOT NULL DEFAULT '',
                        summary text NOT NULL DEFAULT '',
                        body text NOT NULL,
                        enabled boolean NOT NULL DEFAULT true,
                        version integer NOT NULL DEFAULT 1,
                        metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb,
                        created_at timestamptz NOT NULL DEFAULT now(),
                        updated_at timestamptz NOT NULL DEFAULT now(),
                        PRIMARY KEY (tenant_id, type_name_key, slug)
                    )
                    """
                )
            self._pool = pool
            return self._pool

    @staticmethod
    def _row(row: Any) -> TypeSkill:
        raw_meta = row["metadata"]
        if isinstance(raw_meta, (bytes, bytearray)):
            raw_meta = raw_meta.decode()
        meta = json.loads(raw_meta) if isinstance(raw_meta, str) else (raw_meta or {})
        return TypeSkill(
            slug=row["slug"],
            type_name=row["type_name"],
            body=row["body"],
            title=row["title"],
            summary=row["summary"],
            layer=Layer.TENANT,
            tenant_id=row["tenant_id"],
            enabled=row["enabled"],
            version=row["version"],
            metadata=dict(meta),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    _COLS = (
        "tenant_id, type_name_key, slug, type_name, title, summary, body, "
        "enabled, version, metadata, created_at, updated_at"
    )

    async def list_for_tenant(
        self, tenant_id: str, type_name: Optional[str] = None
    ) -> list[TypeSkill]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            if type_name:
                rows = await conn.fetch(
                    f"SELECT {self._COLS} FROM {self._TABLE} "
                    f"WHERE tenant_id = $1 AND type_name_key = $2 "
                    f"ORDER BY type_name_key, slug",
                    tenant_id,
                    _tkey(type_name),
                )
            else:
                rows = await conn.fetch(
                    f"SELECT {self._COLS} FROM {self._TABLE} "
                    f"WHERE tenant_id = $1 ORDER BY type_name_key, slug",
                    tenant_id,
                )
        return [self._row(r) for r in rows]

    async def get(
        self, tenant_id: str, type_name: str, slug: str
    ) -> Optional[TypeSkill]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {self._COLS} FROM {self._TABLE} "
                f"WHERE tenant_id = $1 AND type_name_key = $2 AND slug = $3",
                tenant_id,
                _tkey(type_name),
                slug,
            )
        return self._row(row) if row else None

    async def upsert(self, skill: TypeSkill) -> TypeSkill:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO {self._TABLE}
                    (tenant_id, type_name_key, slug, type_name, title, summary,
                     body, enabled, version, metadata, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 1, $9::jsonb, now(), now())
                ON CONFLICT (tenant_id, type_name_key, slug) DO UPDATE SET
                    type_name = EXCLUDED.type_name,
                    title = EXCLUDED.title,
                    summary = EXCLUDED.summary,
                    body = EXCLUDED.body,
                    enabled = EXCLUDED.enabled,
                    metadata = EXCLUDED.metadata,
                    version = {self._TABLE}.version + 1,
                    updated_at = now()
                RETURNING {self._COLS}
                """,
                skill.tenant_id,
                _tkey(skill.type_name),
                skill.slug,
                skill.type_name,
                skill.title,
                skill.summary,
                skill.body,
                skill.enabled,
                json.dumps(skill.metadata or {}),
            )
        return self._row(row)

    async def delete(self, tenant_id: str, type_name: str, slug: str) -> bool:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            status = await conn.execute(
                f"DELETE FROM {self._TABLE} "
                f"WHERE tenant_id = $1 AND type_name_key = $2 AND slug = $3",
                tenant_id,
                _tkey(type_name),
                slug,
            )
        # asyncpg returns e.g. "DELETE 1" / "DELETE 0".
        return status.rsplit(" ", 1)[-1] != "0"


# --------------------------------------------------------------------------- #
# Store selection (mirrors make_tenant_api_source_store / make_job_store)
# --------------------------------------------------------------------------- #
_store: Optional[TypeSkillStore] = None


def make_type_skill_store() -> TypeSkillStore:
    """Select the store backend from configuration, memoized per process.

    Returns a :class:`PostgresTypeSkillStore` when ``settings.database_url`` is
    set (durable, shared across tasks), else an :class:`InMemoryTypeSkillStore`
    (zero-config default). The Postgres store creates its pool/table lazily, so
    calling this never touches the network.
    """
    global _store
    if _store is None:
        _store = (
            PostgresTypeSkillStore()
            if settings.database_url
            else InMemoryTypeSkillStore()
        )
    return _store


def reset_type_skill_store() -> None:
    """Test helper — clear the memoized store singleton."""
    global _store
    _store = None


__all__ = [
    "TypeSkillStore",
    "InMemoryTypeSkillStore",
    "PostgresTypeSkillStore",
    "make_type_skill_store",
    "reset_type_skill_store",
]

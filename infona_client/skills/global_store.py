"""Durable storage for GLOBAL-ENHANCED type-attached skills (ONTA-399).

Layer B authoring needs skills that **survive process restart and redeploy**.
The process registry (``skills/registry.py``) is boot-time / file-seeded memory
and cannot version; this store is the durable SoT for authored Enhanced skills.

**Why a separate store (not the tenant ``TypeSkillStore``):**

1. Global skills have no ``tenant_id`` — co-mingling them in ``type_skills``
   would either invent a sentinel tenant (leaky) or break the tenant PK.
2. PUBLIC is reserved empty (ONTA-400). A dedicated store can refuse PUBLIC
   structurally instead of relying on every caller to remember.
3. Tenant isolation is vacuously true: there is no tenant column to get wrong.

**Backends** (same swappable pattern as ``skills/store.py``):

* ``InMemoryGlobalTypeSkillStore`` — zero-config default; non-durable.
* ``PostgresGlobalTypeSkillStore`` — durable over ``settings.database_url``.

Table ``global_type_skills`` holds ONE row per ``(layer, type_name_key, slug)``.
Only ``layer='enhanced'`` is writable today; Public refuses at the upsert gate.

The process registry remains the file-bootstrap / premium-plugin path. On
read, :func:`infona_client.skills.registry.global_skills_by_layer` merges the
durable mirror (this store's in-process write-through cache) so both
file-seeded and store-authored Enhanced skills appear to sync consumers
(operator Global Ontology browser) without making that path async.

Boundary: OSS. Pure ``infona_client.*`` / stdlib — no ``from infona.*``.
Enhanced *content* is premium; this *mechanism* is OSS.
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from infona_client.config import settings
from infona_client.graph.layer_content import ContentKind, assert_permits
from infona_client.graph.layers import Layer

from .models import TypeSkill, validate_skill

#: In-process write-through mirror of durable Enhanced skills, consumed by
#: the sync ``global_skills_by_layer`` merge. Keyed by layer.
_durable_mirror: dict[Layer, list[TypeSkill]] = {}
_mirror_lock = asyncio.Lock()


def _tkey(type_name: str) -> str:
    return (type_name or "").casefold()


def _copy(s: TypeSkill) -> TypeSkill:
    return copy.deepcopy(s)


def durable_skills_mirror() -> dict[Layer, list[TypeSkill]]:
    """Sync snapshot of the durable-store write-through cache.

    Used by ``registry.global_skills_by_layer`` so operator reads and
    entitlement-gated resolution see store-authored Enhanced skills without
    an await. Empty when nothing has been upserted / hydrated this process.
    """
    return {layer: [_copy(s) for s in skills] for layer, skills in _durable_mirror.items()}


def _mirror_upsert(skill: TypeSkill) -> None:
    bucket = _durable_mirror.setdefault(skill.layer, [])
    key = (skill.type_name.casefold(), skill.slug)
    for idx, existing in enumerate(bucket):
        if (existing.type_name.casefold(), existing.slug) == key:
            bucket[idx] = _copy(skill)
            return
    bucket.append(_copy(skill))


def _mirror_delete(layer: Layer, type_name: str, slug: str) -> None:
    bucket = _durable_mirror.get(layer)
    if not bucket:
        return
    want = (type_name.casefold(), slug)
    _durable_mirror[layer] = [
        s for s in bucket if (s.type_name.casefold(), s.slug) != want
    ]


def reset_durable_skills_mirror() -> None:
    """Test helper — drop the in-process durable mirror without touching the store."""
    _durable_mirror.clear()


class GlobalTypeSkillStore(Protocol):
    """Durable store for GLOBAL (Enhanced) type-attached skills."""

    async def list_for_layer(
        self, layer: Layer = Layer.ENHANCED, type_name: Optional[str] = None
    ) -> list[TypeSkill]: ...

    async def get(
        self, layer: Layer, type_name: str, slug: str
    ) -> Optional[TypeSkill]: ...

    async def upsert(self, skill: TypeSkill) -> TypeSkill: ...

    async def delete(self, layer: Layer, type_name: str, slug: str) -> bool: ...


def _refuse_if_not_enhanced(skill: TypeSkill) -> None:
    """Global store only accepts Enhanced; Public/Tenant raise."""
    if skill.layer is Layer.PUBLIC:
        assert_permits(
            Layer.PUBLIC,
            ContentKind.SKILLS,
            what=f"GlobalTypeSkillStore.upsert slug={skill.slug!r}",
        )
    if skill.layer is not Layer.ENHANCED:
        raise ValueError(
            "GlobalTypeSkillStore only accepts Layer.ENHANCED skills "
            f"(got {skill.layer.value}); tenant skills belong in TypeSkillStore, "
            "Public may not carry skills (ONTA-400)"
        )
    if skill.tenant_id:
        raise ValueError(
            "a global Enhanced skill is shared canon and must not carry a tenant_id"
        )
    errors = validate_skill(skill)
    if errors:
        raise ValueError("; ".join(errors))


# --------------------------------------------------------------------------- #
# In-memory backend
# --------------------------------------------------------------------------- #
class InMemoryGlobalTypeSkillStore:
    """Non-durable, per-process global skill store keyed ``(layer, type_key, slug)``."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str], TypeSkill] = {}
        self._lock = asyncio.Lock()

    async def list_for_layer(
        self, layer: Layer = Layer.ENHANCED, type_name: Optional[str] = None
    ) -> list[TypeSkill]:
        want = _tkey(type_name) if type_name else None
        async with self._lock:
            rows = [
                r
                for (ly, tk, _), r in self._rows.items()
                if ly == layer.value and (want is None or tk == want)
            ]
        rows.sort(key=lambda r: (r.type_name.casefold(), r.slug))
        return [_copy(r) for r in rows]

    async def get(
        self, layer: Layer, type_name: str, slug: str
    ) -> Optional[TypeSkill]:
        async with self._lock:
            r = self._rows.get((layer.value, _tkey(type_name), slug))
            return _copy(r) if r else None

    async def upsert(self, skill: TypeSkill) -> TypeSkill:
        _refuse_if_not_enhanced(skill)
        now = datetime.now(timezone.utc)
        async with self._lock:
            key = (skill.layer.value, _tkey(skill.type_name), skill.slug)
            existing = self._rows.get(key)
            stored = _copy(skill)
            stored.layer = Layer.ENHANCED
            stored.tenant_id = None
            stored.created_at = existing.created_at if existing else now
            stored.updated_at = now
            stored.version = (existing.version + 1) if existing else 1
            self._rows[key] = stored
            _mirror_upsert(stored)
            from infona_client.skills.registry import invalidate_skill_cache

            invalidate_skill_cache()
            return _copy(stored)

    async def delete(self, layer: Layer, type_name: str, slug: str) -> bool:
        if layer is not Layer.ENHANCED:
            return False
        async with self._lock:
            removed = self._rows.pop((layer.value, _tkey(type_name), slug), None)
        if removed is not None:
            _mirror_delete(layer, type_name, slug)
            from infona_client.skills.registry import invalidate_skill_cache

            invalidate_skill_cache()
            return True
        return False


# --------------------------------------------------------------------------- #
# Postgres backend
# --------------------------------------------------------------------------- #
class PostgresGlobalTypeSkillStore:
    """Durable Enhanced skill store over a generic Postgres DSN via asyncpg."""

    _TABLE = "global_type_skills"

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
                        layer text NOT NULL,
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
                        PRIMARY KEY (layer, type_name_key, slug),
                        CONSTRAINT global_type_skills_layer_chk
                            CHECK (layer = 'enhanced')
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
            layer=Layer(row["layer"]),
            tenant_id=None,
            enabled=row["enabled"],
            version=row["version"],
            metadata=dict(meta),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    _COLS = (
        "layer, type_name_key, slug, type_name, title, summary, body, "
        "enabled, version, metadata, created_at, updated_at"
    )

    async def list_for_layer(
        self, layer: Layer = Layer.ENHANCED, type_name: Optional[str] = None
    ) -> list[TypeSkill]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            if type_name:
                rows = await conn.fetch(
                    f"SELECT {self._COLS} FROM {self._TABLE} "
                    f"WHERE layer = $1 AND type_name_key = $2 "
                    f"ORDER BY type_name_key, slug",
                    layer.value,
                    _tkey(type_name),
                )
            else:
                rows = await conn.fetch(
                    f"SELECT {self._COLS} FROM {self._TABLE} "
                    f"WHERE layer = $1 ORDER BY type_name_key, slug",
                    layer.value,
                )
        return [self._row(r) for r in rows]

    async def get(
        self, layer: Layer, type_name: str, slug: str
    ) -> Optional[TypeSkill]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {self._COLS} FROM {self._TABLE} "
                f"WHERE layer = $1 AND type_name_key = $2 AND slug = $3",
                layer.value,
                _tkey(type_name),
                slug,
            )
        return self._row(row) if row else None

    async def upsert(self, skill: TypeSkill) -> TypeSkill:
        _refuse_if_not_enhanced(skill)
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO {self._TABLE}
                    (layer, type_name_key, slug, type_name, title, summary,
                     body, enabled, version, metadata, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 1, $9::jsonb, now(), now())
                ON CONFLICT (layer, type_name_key, slug) DO UPDATE SET
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
                Layer.ENHANCED.value,
                _tkey(skill.type_name),
                skill.slug,
                skill.type_name,
                skill.title,
                skill.summary,
                skill.body,
                skill.enabled,
                json.dumps(skill.metadata or {}),
            )
        stored = self._row(row)
        _mirror_upsert(stored)
        from infona_client.skills.registry import invalidate_skill_cache

        invalidate_skill_cache()
        return stored

    async def delete(self, layer: Layer, type_name: str, slug: str) -> bool:
        if layer is not Layer.ENHANCED:
            return False
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            status = await conn.execute(
                f"DELETE FROM {self._TABLE} "
                f"WHERE layer = $1 AND type_name_key = $2 AND slug = $3",
                layer.value,
                _tkey(type_name),
                slug,
            )
        deleted = status.rsplit(" ", 1)[-1] != "0"
        if deleted:
            _mirror_delete(layer, type_name, slug)
            from infona_client.skills.registry import invalidate_skill_cache

            invalidate_skill_cache()
        return deleted


# --------------------------------------------------------------------------- #
# Store selection + hydrate
# --------------------------------------------------------------------------- #
_store: Optional[GlobalTypeSkillStore] = None


def make_global_type_skill_store() -> GlobalTypeSkillStore:
    """Select the global skill store backend from configuration, memoized."""
    global _store
    if _store is None:
        _store = (
            PostgresGlobalTypeSkillStore()
            if settings.database_url
            else InMemoryGlobalTypeSkillStore()
        )
    return _store


def reset_global_type_skill_store() -> None:
    """Test helper — clear the memoized global store singleton and mirror."""
    global _store
    _store = None
    reset_durable_skills_mirror()


async def hydrate_global_skills_from_store(
    store: Optional[GlobalTypeSkillStore] = None,
) -> int:
    """Load durable Enhanced skills into the process mirror (restart recovery).

    Call at app startup after plugins so a redeployed process re-reads
    authored Enhanced content from Postgres (or the in-memory store in tests)
    without depending on the image's file seed. Returns the number of skills
    hydrated. Never raises — a store failure leaves the mirror empty.
    """
    from infona_client.skills.registry import invalidate_skill_cache

    target = store if store is not None else make_global_type_skill_store()
    try:
        rows = await target.list_for_layer(Layer.ENHANCED)
    except Exception:
        return 0
    # Rebuild the Enhanced mirror from the store (file-registry content is
    # separate and stays in ``registry._layers``).
    _durable_mirror[Layer.ENHANCED] = [_copy(s) for s in rows]
    invalidate_skill_cache()
    return len(rows)


__all__ = [
    "GlobalTypeSkillStore",
    "InMemoryGlobalTypeSkillStore",
    "PostgresGlobalTypeSkillStore",
    "make_global_type_skill_store",
    "reset_global_type_skill_store",
    "hydrate_global_skills_from_store",
    "durable_skills_mirror",
    "reset_durable_skills_mirror",
]

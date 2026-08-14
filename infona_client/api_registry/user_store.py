"""Per-user custom API-source storage.

A user registers an API source once; it is then visible/usable in every
workspace that user can access. Distinct from the per-workspace
``tenant_custom`` layer (``store.py``): this store is keyed by the auth
subject, not a tenant id.

Backends mirror ``store.py``:

- ``InMemoryUserApiSourceStore`` — zero-config default; tests + no-DSN self-host.
- ``PostgresUserApiSourceStore`` — durable, table ``user_api_sources``,
  PK ``(owner_subject, slug)``. Every query is subject-scoped — there is no
  cross-user read path.

Secrets are NOT stored here. They reuse the existing tenant secret store
with a synthetic scope ``user:{subject}`` (see :func:`user_secret_scope`) so
AAD/isolation stays; the crypto is not forked.

Boundary: OSS. Pure ``infona_client.*`` / stdlib — no ``from infona.*``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from infona_client.config import settings

from .catalog import LAYER_USER_CUSTOM
from .spec import ApiSourceSpec


def user_secret_scope(subject: str) -> str:
    """Synthetic tenant_id so user secrets reuse the tenant secret store.

    ``secret_aad("user:{subject}", slug, name)`` keeps AAD/row isolation
    without forking the crypto.
    """
    return f"user:{subject}"


def effective_owner_subject(api_key: str | None, subject: str | None) -> str | None:
    """Owner key for user-scoped sources.

    Prefer the auth-provider subject (Clerk user id). A static/anonymous API
    key has no subject — fall back to a stable fingerprint of the key so the
    same key shares sources across every tenant it can access (local bypass
    and legacy static keys). Missing key → None.
    """
    if subject:
        return subject
    if not api_key:
        return None
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:24]
    return f"static-key:{digest}"


# --------------------------------------------------------------------------- #
# The stored record
# --------------------------------------------------------------------------- #
@dataclass
class UserApiSource:
    """One user-custom catalog entry as stored (spec + enable flag + audit)."""

    owner_subject: str
    slug: str
    spec: ApiSourceSpec
    enabled: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def materialized_spec(self) -> ApiSourceSpec:
        """The spec as it should appear in the catalog: tagged ``user_custom``
        and with the stored ``enabled`` flag applied.

        ``enabled`` is authoritative on the ROW (a PATCH toggles it without
        rewriting the spec body), so it wins over any stale ``spec.enabled``.
        Returns a copy so the stored spec object is never mutated in place.
        """
        import copy

        s = copy.deepcopy(self.spec)
        s.layer = LAYER_USER_CUSTOM
        s.enabled = self.enabled
        return s


# --------------------------------------------------------------------------- #
# The store protocol
# --------------------------------------------------------------------------- #
class UserApiSourceStore(Protocol):
    async def list_for_subject(self, owner_subject: str) -> list[UserApiSource]: ...
    async def get(self, owner_subject: str, slug: str) -> Optional[UserApiSource]: ...
    async def upsert(self, record: UserApiSource) -> UserApiSource: ...
    async def delete(self, owner_subject: str, slug: str) -> bool: ...


# --------------------------------------------------------------------------- #
# In-memory backend (default; tests + no-DSN self-host)
# --------------------------------------------------------------------------- #
class InMemoryUserApiSourceStore:
    """Non-durable, per-process store. Keyed by ``(owner_subject, slug)`` so
    every lookup is naturally subject-scoped — there is no cross-user read path."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], UserApiSource] = {}
        self._lock = asyncio.Lock()

    async def list_for_subject(self, owner_subject: str) -> list[UserApiSource]:
        async with self._lock:
            rows = [r for (s, _), r in self._rows.items() if s == owner_subject]
        rows.sort(key=lambda r: r.slug)
        return [_copy_record(r) for r in rows]

    async def get(self, owner_subject: str, slug: str) -> Optional[UserApiSource]:
        async with self._lock:
            r = self._rows.get((owner_subject, slug))
            return _copy_record(r) if r else None

    async def upsert(self, record: UserApiSource) -> UserApiSource:
        now = datetime.now(timezone.utc)
        async with self._lock:
            key = (record.owner_subject, record.slug)
            existing = self._rows.get(key)
            created = existing.created_at if existing else now
            stored = UserApiSource(
                owner_subject=record.owner_subject,
                slug=record.slug,
                spec=record.spec,
                enabled=record.enabled,
                created_at=record.created_at or created,
                updated_at=now,
            )
            self._rows[key] = stored
            return _copy_record(stored)

    async def delete(self, owner_subject: str, slug: str) -> bool:
        async with self._lock:
            return self._rows.pop((owner_subject, slug), None) is not None


def _copy_record(r: UserApiSource) -> UserApiSource:
    import copy

    return UserApiSource(
        owner_subject=r.owner_subject,
        slug=r.slug,
        spec=copy.deepcopy(r.spec),
        enabled=r.enabled,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


# --------------------------------------------------------------------------- #
# Postgres backend (durable, shared across ECS tasks)
# --------------------------------------------------------------------------- #
class PostgresUserApiSourceStore:
    """Durable ``UserApiSourceStore`` over a generic Postgres DSN via asyncpg.

    The pool + table are created lazily on first use so importing this module
    (and constructing the store) never touches the network; the DDL is
    idempotent. Vendor-neutral: the only configuration is a plain DSN.

    Every method is parameterized on ``owner_subject`` and the primary key is
    ``(owner_subject, slug)``, so a query can never span users.
    """

    _TABLE = "user_api_sources"

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
                        owner_subject text NOT NULL,
                        slug text NOT NULL,
                        spec_json jsonb NOT NULL,
                        enabled boolean NOT NULL DEFAULT true,
                        created_at timestamptz NOT NULL DEFAULT now(),
                        updated_at timestamptz NOT NULL DEFAULT now(),
                        PRIMARY KEY (owner_subject, slug)
                    )
                    """
                )
            self._pool = pool
            return self._pool

    @staticmethod
    def _row_to_record(row: Any) -> UserApiSource:
        raw = row["spec_json"]
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode()
        spec_dict = json.loads(raw) if isinstance(raw, str) else raw
        return UserApiSource(
            owner_subject=row["owner_subject"],
            slug=row["slug"],
            spec=ApiSourceSpec.from_dict(spec_dict),
            enabled=row["enabled"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def list_for_subject(self, owner_subject: str) -> list[UserApiSource]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT owner_subject, slug, spec_json, enabled, created_at, updated_at "
                f"FROM {self._TABLE} WHERE owner_subject = $1 ORDER BY slug",
                owner_subject,
            )
        return [self._row_to_record(r) for r in rows]

    async def get(self, owner_subject: str, slug: str) -> Optional[UserApiSource]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT owner_subject, slug, spec_json, enabled, created_at, updated_at "
                f"FROM {self._TABLE} WHERE owner_subject = $1 AND slug = $2",
                owner_subject,
                slug,
            )
        return self._row_to_record(row) if row else None

    async def upsert(self, record: UserApiSource) -> UserApiSource:
        pool = await self._ensure_pool()
        spec_json = json.dumps(record.spec.to_dict())
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO {self._TABLE}
                    (owner_subject, slug, spec_json, enabled, created_at, updated_at)
                VALUES ($1, $2, $3::jsonb, $4, now(), now())
                ON CONFLICT (owner_subject, slug) DO UPDATE SET
                    spec_json = EXCLUDED.spec_json,
                    enabled = EXCLUDED.enabled,
                    updated_at = now()
                RETURNING owner_subject, slug, spec_json, enabled, created_at, updated_at
                """,
                record.owner_subject,
                record.slug,
                spec_json,
                record.enabled,
            )
        return self._row_to_record(row)

    async def delete(self, owner_subject: str, slug: str) -> bool:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            status = await conn.execute(
                f"DELETE FROM {self._TABLE} WHERE owner_subject = $1 AND slug = $2",
                owner_subject,
                slug,
            )
        return status.rsplit(" ", 1)[-1] != "0"


# --------------------------------------------------------------------------- #
# Store selection (mirrors make_tenant_api_source_store)
# --------------------------------------------------------------------------- #
_store: Optional[UserApiSourceStore] = None


def make_user_api_source_store() -> UserApiSourceStore:
    """Select the store backend from configuration, memoized per process.

    Returns a :class:`PostgresUserApiSourceStore` when ``settings.database_url``
    is set, else an :class:`InMemoryUserApiSourceStore`. The Postgres store
    creates its pool/table lazily, so calling this never touches the network.
    """
    global _store
    if _store is None:
        _store = (
            PostgresUserApiSourceStore()
            if settings.database_url
            else InMemoryUserApiSourceStore()
        )
    return _store


def reset_user_api_source_store() -> None:
    """Test helper — clear the memoized store singleton."""
    global _store
    _store = None


__all__ = [
    "UserApiSource",
    "UserApiSourceStore",
    "InMemoryUserApiSourceStore",
    "PostgresUserApiSourceStore",
    "make_user_api_source_store",
    "reset_user_api_source_store",
    "user_secret_scope",
    "effective_owner_subject",
    "LAYER_USER_CUSTOM",
]

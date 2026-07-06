"""Per-tenant encrypted secret storage for tenant-custom API sources
(ONTA-2xx, Child 2).

Secrets (a tenant's private API keys / bearer tokens) are **envelope-encrypted at
rest** by a :class:`SecretCipher` and stored here as **ciphertext only** — the
plaintext never touches this store, the ``spec_json``, an API response, a log, or
a trace. Decryption happens once, at call time, inside
``RegistryApiSource.execute()``.

Table ``tenant_api_secrets(tenant_id, slug, logical_name, ciphertext, scheme,
created_at, updated_at, PK(tenant_id, slug, logical_name))``:

- ``logical_name`` is the name the spec's auth references (``AuthSpec.secret_ref``)
  — e.g. ``"api_key"``. A source may hold several (one per auth slot).
- ``ciphertext`` is the opaque cipher envelope; ``scheme`` mirrors its tag for
  diagnostics / future migration.
- The composite PK scopes every query to one tenant; there is no cross-tenant
  read path, exactly like ``tenant_api_sources``.

Backends mirror ``store.py``: an in-memory default and a Postgres backend over
the shared pool. Deleting a source deletes its secrets (same slug scope).

Boundary: OSS. Pure ``cograph_client.*`` / stdlib — no ``from cograph.*``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from cograph_client.config import settings

from .crypto import SecretCipher, SecretCipherError


def secret_aad(tenant_id: str, slug: str, logical_name: str) -> str:
    """Additional-authenticated-data binding a ciphertext to its exact slot.

    Encrypting with this AAD means a ciphertext row copied to a different tenant,
    slug, or logical name fails authentication on decrypt — a stored secret can
    only ever be decrypted back into the exact slot it was written for."""
    return f"{tenant_id}/{slug}/{logical_name}"


@dataclass
class TenantApiSecret:
    tenant_id: str
    slug: str
    logical_name: str
    ciphertext: str
    scheme: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


# --------------------------------------------------------------------------- #
# Store protocol
# --------------------------------------------------------------------------- #
class TenantSecretStore(Protocol):
    async def put(self, secret: TenantApiSecret) -> None: ...
    async def get(
        self, tenant_id: str, slug: str, logical_name: str
    ) -> Optional[TenantApiSecret]: ...
    async def list_names(self, tenant_id: str, slug: str) -> list[str]: ...
    async def delete_for_source(self, tenant_id: str, slug: str) -> None: ...
    async def delete_one(self, tenant_id: str, slug: str, logical_name: str) -> bool: ...


# --------------------------------------------------------------------------- #
# In-memory backend
# --------------------------------------------------------------------------- #
class InMemoryTenantSecretStore:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str], TenantApiSecret] = {}
        self._lock = asyncio.Lock()

    async def put(self, secret: TenantApiSecret) -> None:
        now = datetime.now(timezone.utc)
        async with self._lock:
            key = (secret.tenant_id, secret.slug, secret.logical_name)
            existing = self._rows.get(key)
            self._rows[key] = TenantApiSecret(
                tenant_id=secret.tenant_id,
                slug=secret.slug,
                logical_name=secret.logical_name,
                ciphertext=secret.ciphertext,
                scheme=secret.scheme,
                created_at=(existing.created_at if existing else now),
                updated_at=now,
            )

    async def get(
        self, tenant_id: str, slug: str, logical_name: str
    ) -> Optional[TenantApiSecret]:
        async with self._lock:
            row = self._rows.get((tenant_id, slug, logical_name))
            # Return a copy so a caller mutating the returned record cannot mutate
            # the stored ciphertext (defensive; mirrors the sources store).
            return _copy_secret(row) if row else None

    async def list_names(self, tenant_id: str, slug: str) -> list[str]:
        async with self._lock:
            names = [
                ln for (t, s, ln) in self._rows if t == tenant_id and s == slug
            ]
        return sorted(names)

    async def delete_for_source(self, tenant_id: str, slug: str) -> None:
        async with self._lock:
            for key in [
                k for k in self._rows if k[0] == tenant_id and k[1] == slug
            ]:
                self._rows.pop(key, None)

    async def delete_one(self, tenant_id: str, slug: str, logical_name: str) -> bool:
        async with self._lock:
            return self._rows.pop((tenant_id, slug, logical_name), None) is not None


def _copy_secret(r: TenantApiSecret) -> TenantApiSecret:
    return TenantApiSecret(
        tenant_id=r.tenant_id,
        slug=r.slug,
        logical_name=r.logical_name,
        ciphertext=r.ciphertext,
        scheme=r.scheme,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


# --------------------------------------------------------------------------- #
# Postgres backend
# --------------------------------------------------------------------------- #
class PostgresTenantSecretStore:
    """Durable encrypted-secret store over a generic Postgres DSN via the shared
    pool. Stores ciphertext only. Every query is scoped by ``tenant_id`` (part of
    the PK) — no cross-tenant read path."""

    _TABLE = "tenant_api_secrets"

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
                        logical_name text NOT NULL,
                        ciphertext text NOT NULL,
                        scheme text NOT NULL DEFAULT '',
                        created_at timestamptz NOT NULL DEFAULT now(),
                        updated_at timestamptz NOT NULL DEFAULT now(),
                        PRIMARY KEY (tenant_id, slug, logical_name)
                    )
                    """
                )
            self._pool = pool
            return self._pool

    async def put(self, secret: TenantApiSecret) -> None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {self._TABLE}
                    (tenant_id, slug, logical_name, ciphertext, scheme, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, now(), now())
                ON CONFLICT (tenant_id, slug, logical_name) DO UPDATE SET
                    ciphertext = EXCLUDED.ciphertext,
                    scheme = EXCLUDED.scheme,
                    updated_at = now()
                """,
                secret.tenant_id,
                secret.slug,
                secret.logical_name,
                secret.ciphertext,
                secret.scheme,
            )

    async def get(
        self, tenant_id: str, slug: str, logical_name: str
    ) -> Optional[TenantApiSecret]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT tenant_id, slug, logical_name, ciphertext, scheme, "
                f"created_at, updated_at FROM {self._TABLE} "
                f"WHERE tenant_id = $1 AND slug = $2 AND logical_name = $3",
                tenant_id,
                slug,
                logical_name,
            )
        if row is None:
            return None
        return TenantApiSecret(
            tenant_id=row["tenant_id"],
            slug=row["slug"],
            logical_name=row["logical_name"],
            ciphertext=row["ciphertext"],
            scheme=row["scheme"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def list_names(self, tenant_id: str, slug: str) -> list[str]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT logical_name FROM {self._TABLE} "
                f"WHERE tenant_id = $1 AND slug = $2 ORDER BY logical_name",
                tenant_id,
                slug,
            )
        return [r["logical_name"] for r in rows]

    async def delete_for_source(self, tenant_id: str, slug: str) -> None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"DELETE FROM {self._TABLE} WHERE tenant_id = $1 AND slug = $2",
                tenant_id,
                slug,
            )

    async def delete_one(self, tenant_id: str, slug: str, logical_name: str) -> bool:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            status = await conn.execute(
                f"DELETE FROM {self._TABLE} "
                f"WHERE tenant_id = $1 AND slug = $2 AND logical_name = $3",
                tenant_id,
                slug,
                logical_name,
            )
        return status.rsplit(" ", 1)[-1] != "0"


# --------------------------------------------------------------------------- #
# Store selection
# --------------------------------------------------------------------------- #
_store: Optional[TenantSecretStore] = None


def make_tenant_secret_store() -> TenantSecretStore:
    """Select the secret-store backend from configuration, memoized per process.

    Postgres when ``settings.database_url`` is set, else in-memory. Lazy pool/DDL,
    so calling this never touches the network."""
    global _store
    if _store is None:
        _store = (
            PostgresTenantSecretStore()
            if settings.database_url
            else InMemoryTenantSecretStore()
        )
    return _store


def reset_tenant_secret_store() -> None:
    """Test helper — clear the memoized store singleton."""
    global _store
    _store = None


# --------------------------------------------------------------------------- #
# High-level encrypt/store + resolve/decrypt helpers
# --------------------------------------------------------------------------- #
async def store_secret(
    store: TenantSecretStore,
    cipher: SecretCipher,
    *,
    tenant_id: str,
    slug: str,
    logical_name: str,
    plaintext: str,
) -> None:
    """Encrypt ``plaintext`` (bound to its slot via AAD) and persist the ciphertext.

    The plaintext exists only as the argument here and inside ``cipher.encrypt`` —
    it is never written anywhere but as ciphertext."""
    aad = secret_aad(tenant_id, slug, logical_name)
    ciphertext = cipher.encrypt(plaintext, aad=aad)
    await store.put(
        TenantApiSecret(
            tenant_id=tenant_id,
            slug=slug,
            logical_name=logical_name,
            ciphertext=ciphertext,
            scheme=getattr(cipher, "scheme", ""),
        )
    )


async def resolve_secret(
    store: TenantSecretStore,
    cipher: SecretCipher,
    *,
    tenant_id: str,
    slug: str,
    logical_name: str,
) -> Optional[str]:
    """Fetch + decrypt a stored secret, or ``None`` if absent. Raises
    ``SecretCipherError`` on an integrity failure (never returns garbled text).

    Called ONLY at request time inside the executor — the one place plaintext is
    reconstructed."""
    row = await store.get(tenant_id, slug, logical_name)
    if row is None:
        return None
    aad = secret_aad(tenant_id, slug, logical_name)
    return cipher.decrypt(row.ciphertext, aad=aad)


def make_secret_resolver(tenant_id: str, slug: str):
    """Build the ``SecretResolver`` the executor calls for a tenant_custom source.

    Returns an async ``(logical_name) -> Optional[str]`` bound to this tenant+slug,
    using the process cipher + secret store. Returns ``None`` (no resolver) when no
    cipher is configured — then the executor treats a ``secret_ref`` source as
    dormant rather than un-authenticated. The resolver is closed over the tenant
    scope, so it can only ever decrypt THIS tenant's secrets for THIS source."""
    from .crypto import get_secret_cipher

    cipher = get_secret_cipher()
    if cipher is None:
        return None
    store = make_tenant_secret_store()

    async def _resolve(logical_name: str) -> Optional[str]:
        return await resolve_secret(
            store, cipher, tenant_id=tenant_id, slug=slug, logical_name=logical_name
        )

    return _resolve


__all__ = [
    "TenantApiSecret",
    "TenantSecretStore",
    "InMemoryTenantSecretStore",
    "PostgresTenantSecretStore",
    "make_tenant_secret_store",
    "reset_tenant_secret_store",
    "store_secret",
    "resolve_secret",
    "make_secret_resolver",
    "secret_aad",
    "SecretCipherError",
]

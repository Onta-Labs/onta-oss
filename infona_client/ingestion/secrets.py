"""Resolve ``secret_ref`` for dlt extract (ONTA-553 BYOK).

Refs:

* ``env:VAR`` — the caller's environment. OSS CLI path; always allowed.
* ``store:<slug>/<logical>`` — per-tenant encrypted secret store (same cipher
  as api-sources). Hosted path (ONTA-554).
* A bare name is treated as ``env:NAME`` for CLI convenience.

Inline tokens on the request body are accepted but never logged; they do not
go through this module.

No platform/shared key is implied. Missing secret → :class:`DltSecretMissing`,
never a 500.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from infona_client.ingestion.errors import DltSecretMissing
from infona_client.ingestion.models import DltAuthSpec, DltSourceSpec

SecretResolver = Callable[[str, str], Awaitable[Optional[str]]]
# (slug, logical_name) -> plaintext | None


@dataclass
class ResolvedSecrets:
    token: Optional[str] = None
    dsn: Optional[str] = None
    username: Optional[str] = None


def parse_secret_ref(ref: str) -> tuple[str, str, str]:
    """Return ``(scheme, locator, logical)``.

    * ``env:FOO`` → ``("env", "FOO", "FOO")``
    * ``store:dlt:hubspot/token`` → ``("store", "dlt:hubspot", "token")``
    * ``FOO`` → ``("env", "FOO", "FOO")``
    """
    raw = (ref or "").strip()
    if not raw:
        raise DltSecretMissing("empty secret_ref")
    if raw.startswith("env:"):
        name = raw[4:].strip()
        if not name:
            raise DltSecretMissing("secret_ref env: is missing a variable name")
        return "env", name, name
    if raw.startswith("store:"):
        rest = raw[6:]
        if "/" not in rest:
            raise DltSecretMissing(
                "store secret_ref must be store:<slug>/<logical_name>"
            )
        slug, logical = rest.rsplit("/", 1)
        slug, logical = slug.strip(), logical.strip()
        if not slug or not logical:
            raise DltSecretMissing(
                "store secret_ref must be store:<slug>/<logical_name>"
            )
        return "store", slug, logical
    return "env", raw, raw


def is_hosted_secret_ref(ref: Optional[str]) -> bool:
    """True when the ref is a persisted store pointer (Cloud hosted path)."""
    if not ref:
        return False
    return ref.strip().startswith("store:")


async def resolve_ref(
    ref: str,
    *,
    env: Optional[dict[str, str]] = None,
    store_get: Optional[SecretResolver] = None,
) -> str:
    scheme, locator, logical = parse_secret_ref(ref)
    if scheme == "env":
        source = env if env is not None else os.environ
        value = source.get(locator)
        if not value:
            raise DltSecretMissing(
                f"missing credential: set {locator} (BYOK). "
                f"secret_ref={ref!r} resolved to an empty value."
            )
        return value
    if store_get is None:
        raise DltSecretMissing(
            f"secret_ref {ref!r} needs the per-tenant secret store, which is "
            "not configured. Use env:VAR for OSS CLI BYOK, or save the source "
            "in Explorer so the token is stored encrypted."
        )
    value = await store_get(locator, logical)
    if not value:
        raise DltSecretMissing(
            f"missing stored credential for {locator}/{logical}. "
            "Re-save the source with the token (write-only; it is never echoed)."
        )
    return value


async def resolve_source_secrets(
    spec: DltSourceSpec,
    *,
    env: Optional[dict[str, str]] = None,
    store_get: Optional[SecretResolver] = None,
) -> ResolvedSecrets:
    """Resolve token + DSN for one extract. Inline ``auth.token`` wins over ref."""
    out = ResolvedSecrets()
    auth: Optional[DltAuthSpec] = spec.auth
    if auth is not None:
        out.username = auth.username
        if auth.token:
            out.token = auth.token
        elif auth.secret_ref:
            out.token = await resolve_ref(
                auth.secret_ref, env=env, store_get=store_get
            )
    dsn = spec.dsn
    if dsn:
        if dsn.startswith(("env:", "store:")):
            out.dsn = await resolve_ref(dsn, env=env, store_get=store_get)
        else:
            out.dsn = dsn
    elif spec.kind == "sql" and out.token:
        # SQL DSN may be stored as the auth secret (logical name ``dsn``).
        out.dsn = out.token
    return out

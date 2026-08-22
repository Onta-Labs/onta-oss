"""Bind extract-source secrets to the existing per-tenant cipher store."""

from __future__ import annotations

from typing import Optional

from infona_client.api_registry.crypto import get_secret_cipher
from infona_client.api_registry.secret_store import (
    make_tenant_secret_store,
    resolve_secret,
    store_secret,
)
from infona_client.ingestion.extract_source_store import secret_store_slug
from infona_client.ingestion.errors import DltSecretMissing


def tenant_store_getter(tenant_id: str):
    """Async (slug, logical) → plaintext for :func:`resolve_source_secrets`."""

    async def _get(slug: str, logical: str) -> Optional[str]:
        cipher = get_secret_cipher()
        if cipher is None:
            raise DltSecretMissing(
                "secret store has no cipher (set INFONA_SECRETS_KEY or the "
                "Cloud KMS plugin). Use env:VAR for OSS CLI BYOK."
            )
        return await resolve_secret(
            make_tenant_secret_store(),
            cipher,
            tenant_id=tenant_id,
            slug=slug,
            logical_name=logical,
        )

    return _get


async def put_extract_secrets(
    tenant_id: str, extract_slug: str, secrets: dict[str, str]
) -> None:
    if not secrets:
        return
    cipher = get_secret_cipher()
    if cipher is None:
        raise DltSecretMissing(
            "cannot store a secret: no cipher configured "
            "(set INFONA_SECRETS_KEY or the Cloud KMS plugin)."
        )
    store = make_tenant_secret_store()
    slug = secret_store_slug(extract_slug)
    for logical, plaintext in secrets.items():
        name = (logical or "").strip()
        value = plaintext if isinstance(plaintext, str) else ""
        if not name or not value:
            continue
        await store_secret(
            store,
            cipher,
            tenant_id=tenant_id,
            slug=slug,
            logical_name=name,
            plaintext=value,
        )


async def extract_has_secret(tenant_id: str, extract_slug: str) -> bool:
    try:
        names = await make_tenant_secret_store().list_names(
            tenant_id, secret_store_slug(extract_slug)
        )
    except Exception:  # noqa: BLE001
        return False
    return bool(names)


async def delete_extract_secrets(tenant_id: str, extract_slug: str) -> None:
    await make_tenant_secret_store().delete_for_source(
        tenant_id, secret_store_slug(extract_slug)
    )

"""User-scoped API source registry — one registration, every workspace.

Canonical CRUD surface (NOT under ``/graphs/{tenant}``):

  GET    /v1/me/api-sources            list the caller's user sources
  GET    /v1/me/api-sources/{slug}     read one (secrets REDACTED)
  POST   /v1/me/api-sources            create
  PATCH  /v1/me/api-sources/{slug}     edit
  DELETE /v1/me/api-sources/{slug}     delete

Auth: API key → :func:`effective_owner_subject`. Signed-in users use their
auth subject; a static key falls back to a stable fingerprint of the key
so the same key shares sources across every tenant it can access. Missing
key → 401.

Secrets reuse the tenant secret store under synthetic scope ``user:{subject}``
so AAD/isolation stays (see :func:`user_secret_scope`).

Boundary: OSS. Imports only ``infona_client.*`` / stdlib.
"""

from __future__ import annotations

from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Security

from infona_client.api.routes.api_sources import (
    ApiSourceSummary,
    CreateApiSourceRequest,
    OkResponse,
    UpdateApiSourceRequest,
    _has_stored_secret,
    _redacted_spec_dict,
    _require_cipher_if_secrets,
    _store_secrets,
    _summary,
)
from infona_client.api_registry import (
    LAYER_USER_CUSTOM,
    UserApiSource,
    invalidate_user_catalog,
    load_user_custom_catalog,
    make_tenant_secret_store,
    make_user_api_source_store,
    user_secret_scope,
    effective_owner_subject,
    validate_tenant_spec,
)
from infona_client.api_registry.spec import ApiSourceSpec
from infona_client.auth.api_keys import api_key_header
from infona_client.auth.workspace_store import resolve_subject

logger = structlog.stdlib.get_logger("infona.api_registry.user_routes")

router = APIRouter(prefix="/v1/me/api-sources")

def _require_subject(api_key: str | None = Security(api_key_header)) -> str:
    if not api_key:
        raise HTTPException(status_code=401, detail="Not authenticated")
    subject = effective_owner_subject(api_key, resolve_subject(api_key))
    if subject is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return subject


def _sources_store():
    return make_user_api_source_store()


def _secret_store():
    return make_tenant_secret_store()


def _parse_spec(raw: dict[str, Any], slug_override: Optional[str] = None) -> ApiSourceSpec:
    spec = ApiSourceSpec.from_dict(raw)
    if slug_override is not None:
        spec.slug = slug_override
    spec.layer = LAYER_USER_CUSTOM
    return spec


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@router.get("", response_model=list[ApiSourceSummary])
async def list_user_api_sources(
    subject: str = Depends(_require_subject),
):
    """List the caller's user-scoped sources only (not tenant_custom, not global)."""
    catalog = await load_user_custom_catalog(subject, _sources_store())
    out: list[ApiSourceSummary] = []
    for spec in sorted(catalog.all(), key=lambda s: s.slug):
        if spec.layer != LAYER_USER_CUSTOM:
            continue
        out.append(await _summary(spec, "", subject=subject))
    return out


@router.get("/{slug}")
async def get_user_api_source(
    slug: str,
    subject: str = Depends(_require_subject),
):
    """Read one of the caller's user sources (secrets REDACTED) + ``has_secret``."""
    store = _sources_store()
    rec = await store.get(subject, slug)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"no api source '{slug}'")
    spec = rec.materialized_spec()
    has_secret = await _has_stored_secret(user_secret_scope(subject), spec)
    body = _redacted_spec_dict(spec)
    body["has_secret"] = has_secret
    body["editable"] = True
    return body


@router.post("", response_model=ApiSourceSummary, status_code=201)
async def create_user_api_source(
    req: CreateApiSourceRequest,
    subject: str = Depends(_require_subject),
):
    """Create a user-scoped source. Secrets are encrypted under ``user:{subject}``."""
    spec = _parse_spec(req.spec)
    if req.enabled is not None:
        spec.enabled = req.enabled
    errs = validate_tenant_spec(spec)
    if errs:
        raise HTTPException(status_code=422, detail={"errors": errs})
    _require_cipher_if_secrets(req.secrets)

    store = _sources_store()
    if await store.get(subject, spec.slug) is not None:
        raise HTTPException(
            status_code=409, detail=f"api source '{spec.slug}' already exists"
        )
    saved = await store.upsert(
        UserApiSource(
            owner_subject=subject, slug=spec.slug, spec=spec, enabled=spec.enabled
        )
    )
    await _store_secrets(user_secret_scope(subject), spec.slug, req.secrets)
    invalidate_user_catalog(subject)
    return await _summary(saved.materialized_spec(), "", subject=subject)


@router.patch("/{slug}", response_model=ApiSourceSummary)
async def update_user_api_source(
    slug: str,
    req: UpdateApiSourceRequest,
    subject: str = Depends(_require_subject),
):
    """Edit a user-scoped source (spec body, enabled, and/or secrets)."""
    store = _sources_store()
    existing = await store.get(subject, slug)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"no editable api source '{slug}'")

    spec = _parse_spec(req.spec, slug_override=slug) if req.spec is not None else existing.spec
    spec.slug = slug
    spec.layer = LAYER_USER_CUSTOM
    enabled = req.enabled if req.enabled is not None else existing.enabled
    spec.enabled = enabled
    errs = validate_tenant_spec(spec)
    if errs:
        raise HTTPException(status_code=422, detail={"errors": errs})
    _require_cipher_if_secrets(req.secrets)

    saved = await store.upsert(
        UserApiSource(
            owner_subject=subject, slug=slug, spec=spec, enabled=enabled,
            created_at=existing.created_at,
        )
    )
    await _store_secrets(user_secret_scope(subject), slug, req.secrets)
    invalidate_user_catalog(subject)
    return await _summary(saved.materialized_spec(), "", subject=subject)


@router.delete("/{slug}", response_model=OkResponse)
async def delete_user_api_source(
    slug: str,
    subject: str = Depends(_require_subject),
):
    """Delete a user-scoped source + its stored secrets."""
    store = _sources_store()
    removed = await store.delete(subject, slug)
    if not removed:
        raise HTTPException(status_code=404, detail=f"no editable api source '{slug}'")
    try:
        await _secret_store().delete_for_source(user_secret_scope(subject), slug)
    except Exception:  # noqa: BLE001 — the source is gone; a secret-sweep hiccup is non-fatal
        logger.warning("user_api_source_secret_delete_failed", slug=slug)
    invalidate_user_catalog(subject)
    return OkResponse()

"""HTTP routes for the workspace-scoped (per-tenant) API source registry
(ONTA-2xx, Child 3) — the canonical CRUD + validate + test-call surface every
client (Explorer webapp, CLI, MCP) reaches through the shared SDK.

  GET    /graphs/{tenant}/api-sources            list (global read-only + tenant-custom)
  GET    /graphs/{tenant}/api-sources/{slug}     read one (secrets REDACTED)
  POST   /graphs/{tenant}/api-sources            create a tenant-custom source
  PATCH  /graphs/{tenant}/api-sources/{slug}     edit a tenant-custom source (global => 403)
  DELETE /graphs/{tenant}/api-sources/{slug}     delete a tenant-custom source
  POST   /graphs/{tenant}/api-sources/validate   validate a spec (no write)
  POST   /graphs/{tenant}/api-sources/test       run ONE smoke request (no write, no persist)

Authorization: ``get_tenant`` authorizes ``{tenant}`` against the caller's key
(403 on an unowned tenant). Global (``global_public`` / ``global_enhanced``)
entries are READ-ONLY: any mutation targeting a global slug returns 403.

Secrets: a create/update body may carry a write-only ``secrets: {<logicalName>:
<value>}`` map. Values are envelope-encrypted per tenant (Child 2) and stored in
the secret store; the spec's ``auth.secret_ref`` names one by logical name. A
secret VALUE is NEVER returned by list/get, echoed by test, or logged — only a
``has_secret`` boolean surfaces.

Boundary: OSS. Imports only ``cograph_client.*`` / stdlib.
"""

from __future__ import annotations

from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from cograph_client.api_registry import (
    LAYER_TENANT_CUSTOM,
    RegistryApiSource,
    TenantApiSource,
    get_api_source_catalog,
    get_secret_cipher,
    invalidate_tenant_catalog,
    load_tenant_custom_catalog,
    make_secret_resolver,
    make_tenant_api_source_store,
    make_tenant_secret_store,
    store_secret,
    validate_tenant_spec,
)
from cograph_client.api_registry.secret_store import secret_aad  # noqa: F401 (documented seam)
from cograph_client.api_registry.spec import ApiSourceSpec, validate_spec
from cograph_client.auth.api_keys import TenantContext, get_tenant

logger = structlog.stdlib.get_logger("cograph.api_registry.routes")

router = APIRouter(prefix="/graphs/{tenant}/api-sources")

_GLOBAL_LAYERS = {"global_public", "global_enhanced"}


# --------------------------------------------------------------------------- #
# Request / response models
# --------------------------------------------------------------------------- #
class ApiSourceSummary(BaseModel):
    """The list/summary shape (the SDK contract). Secret-free by construction."""

    slug: str
    title: str
    publisher: str
    description: str
    layer: str  # "global_public" | "global_enhanced" | "tenant_custom"
    authority_level: str
    entity_kinds: list[str]
    attributes: list[str]
    enabled: bool
    editable: bool  # true only for tenant_custom
    has_secret: bool


class ValidationError(BaseModel):
    path: str
    message: str


class ValidateResponse(BaseModel):
    valid: bool
    errors: list[ValidationError] = Field(default_factory=list)


class CreateApiSourceRequest(BaseModel):
    """Create body: the full spec plus an optional write-only secrets map.

    ``spec`` is an ``ApiSourceSpec`` JSON object (see the registry spec). ``secrets``
    maps a logical name (referenced by ``spec.auth.secret_ref``) to a plaintext
    VALUE — write-only, never returned. ``enabled`` overrides the spec's flag.
    """

    spec: dict[str, Any]
    secrets: dict[str, str] = Field(default_factory=dict)
    enabled: Optional[bool] = None


class UpdateApiSourceRequest(BaseModel):
    """Patch body. ``spec`` (if present) REPLACES the stored spec body; ``secrets``
    adds/replaces named secrets; ``enabled`` toggles the row. All optional so a
    caller can e.g. flip ``enabled`` alone."""

    spec: Optional[dict[str, Any]] = None
    secrets: dict[str, str] = Field(default_factory=dict)
    enabled: Optional[bool] = None


class TestApiSourceRequest(BaseModel):
    """Test-call body: run ONE smoke request through the executor. Provide EITHER
    an existing ``slug`` OR an inline ``spec`` (spec wins if both are given).
    ``sample_params`` are the parameter bindings for the smoke call."""

    slug: Optional[str] = None
    spec: Optional[dict[str, Any]] = None
    sample_params: dict[str, str] = Field(default_factory=dict)


class TestApiSourceResponse(BaseModel):
    ok: bool
    rows: list[dict[str, str]] = Field(default_factory=list)
    error: Optional[str] = None


class OkResponse(BaseModel):
    ok: bool = True


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _sources_store():
    return make_tenant_api_source_store()


def _secret_store():
    return make_tenant_secret_store()


async def _summary(
    spec: ApiSourceSpec, tenant_id: str, *, has_secret: Optional[bool] = None
) -> ApiSourceSummary:
    editable = spec.layer == LAYER_TENANT_CUSTOM
    if has_secret is None:
        has_secret = await _has_stored_secret(tenant_id, spec) if editable else False
    return ApiSourceSummary(
        slug=spec.slug,
        title=spec.title,
        publisher=spec.publisher,
        description=spec.description,
        layer=spec.layer,
        authority_level=spec.authority_level.value,
        entity_kinds=list(spec.coverage.entity_kinds),
        attributes=list(spec.coverage.attributes),
        enabled=spec.enabled,
        editable=editable,
        has_secret=bool(has_secret),
    )


async def _has_stored_secret(tenant_id: str, spec: ApiSourceSpec) -> bool:
    """Whether the tenant has a stored secret for this source (any logical name)."""
    try:
        names = await _secret_store().list_names(tenant_id, spec.slug)
    except Exception:  # noqa: BLE001 — a store hiccup must not break a read
        return False
    return bool(names)


def _redacted_spec_dict(spec: ApiSourceSpec) -> dict[str, Any]:
    """Serialize a spec for GET. The spec never carries a secret VALUE (only the
    ``auth.secret_ref`` logical name), so ``to_dict()`` is already secret-free; we
    return it verbatim. Kept as a named seam so any future value-bearing field is
    forced through here to be redacted."""
    return spec.to_dict()


def _validation_errors(spec: ApiSourceSpec) -> list[ValidationError]:
    return [ValidationError(path="spec", message=m) for m in validate_tenant_spec(spec)]


def _require_cipher_if_secrets(secrets: dict[str, str]) -> None:
    """Fail closed: if the caller sent secrets but no cipher is configured, refuse
    (never store plaintext). 503 = the deployment must set OMNIX_SECRETS_KEY or a
    cipher plugin before it can hold credentials."""
    if secrets and get_secret_cipher() is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "secret storage is not configured (no cipher); set OMNIX_SECRETS_KEY "
                "or register a secret-cipher plugin before storing credentials"
            ),
        )


async def _store_secrets(tenant_id: str, slug: str, secrets: dict[str, str]) -> None:
    if not secrets:
        return
    cipher = get_secret_cipher()
    if cipher is None:  # defensive; _require_cipher_if_secrets already guarded
        raise HTTPException(status_code=503, detail="secret storage is not configured")
    store = _secret_store()
    for logical_name, value in secrets.items():
        if not value:
            continue
        await store_secret(
            store, cipher, tenant_id=tenant_id, slug=slug,
            logical_name=logical_name, plaintext=value,
        )


def _parse_spec(raw: dict[str, Any], slug_override: Optional[str] = None) -> ApiSourceSpec:
    spec = ApiSourceSpec.from_dict(raw)
    if slug_override is not None:
        spec.slug = slug_override
    spec.layer = LAYER_TENANT_CUSTOM  # a tenant-authored spec is always tenant_custom
    return spec


def _guard_not_global(tenant_id: str, slug: str) -> None:
    """403 if ``slug`` names a GLOBAL (read-only) catalog entry. Global entries are
    operator-curated and may never be edited/deleted through the tenant routes."""
    entry = get_api_source_catalog().get(slug)  # global-only view (no tenant merge)
    if entry is not None and entry.layer in _GLOBAL_LAYERS:
        raise HTTPException(
            status_code=403,
            detail=f"'{slug}' is a global (read-only) source and cannot be modified",
        )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@router.get("", response_model=list[ApiSourceSummary])
async def list_api_sources(
    tenant: TenantContext = Depends(get_tenant),
):
    """List all sources visible to this tenant: global read-only entries +
    the tenant's own custom (editable) entries, flagged by ``layer`` /
    ``editable`` / ``has_secret``."""
    catalog = await load_tenant_custom_catalog(tenant.tenant_id, _sources_store())
    out: list[ApiSourceSummary] = []
    for spec in sorted(catalog.all(), key=lambda s: s.slug):
        out.append(await _summary(spec, tenant.tenant_id))
    return out


@router.get("/{slug}")
async def get_api_source(
    slug: str,
    tenant: TenantContext = Depends(get_tenant),
):
    """Read one source's full spec (secrets REDACTED/omitted) + ``has_secret``."""
    catalog = await load_tenant_custom_catalog(tenant.tenant_id, _sources_store())
    spec = catalog.get(slug)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"no api source '{slug}'")
    has_secret = (
        await _has_stored_secret(tenant.tenant_id, spec)
        if spec.layer == LAYER_TENANT_CUSTOM
        else False
    )
    body = _redacted_spec_dict(spec)
    body["has_secret"] = has_secret
    body["editable"] = spec.layer == LAYER_TENANT_CUSTOM
    return body


@router.post("", response_model=ApiSourceSummary, status_code=201)
async def create_api_source(
    req: CreateApiSourceRequest,
    tenant: TenantContext = Depends(get_tenant),
):
    """Create a tenant-custom source. 403 if the slug shadows a global one is NOT
    enforced — a tenant MAY shadow a global slug for its own workspace (that's the
    layer's purpose) — but the slug must be a valid tenant slug and the spec must
    validate. Secrets are encrypted per tenant."""
    spec = _parse_spec(req.spec)
    if req.enabled is not None:
        spec.enabled = req.enabled
    errs = validate_tenant_spec(spec)
    if errs:
        raise HTTPException(status_code=422, detail={"errors": errs})
    _require_cipher_if_secrets(req.secrets)

    store = _sources_store()
    if await store.get(tenant.tenant_id, spec.slug) is not None:
        raise HTTPException(
            status_code=409, detail=f"api source '{spec.slug}' already exists"
        )
    saved = await store.upsert(
        TenantApiSource(
            tenant_id=tenant.tenant_id, slug=spec.slug, spec=spec, enabled=spec.enabled
        )
    )
    await _store_secrets(tenant.tenant_id, spec.slug, req.secrets)
    invalidate_tenant_catalog(tenant.tenant_id)
    return await _summary(saved.materialized_spec(), tenant.tenant_id)


@router.patch("/{slug}", response_model=ApiSourceSummary)
async def update_api_source(
    slug: str,
    req: UpdateApiSourceRequest,
    tenant: TenantContext = Depends(get_tenant),
):
    """Edit a tenant-custom source (spec body, enabled, and/or secrets). A global
    slug => 403. Missing tenant entry => 404."""
    _guard_not_global(tenant.tenant_id, slug)
    store = _sources_store()
    existing = await store.get(tenant.tenant_id, slug)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"no editable api source '{slug}'")

    spec = _parse_spec(req.spec, slug_override=slug) if req.spec is not None else existing.spec
    spec.slug = slug
    spec.layer = LAYER_TENANT_CUSTOM
    enabled = req.enabled if req.enabled is not None else existing.enabled
    spec.enabled = enabled
    errs = validate_tenant_spec(spec)
    if errs:
        raise HTTPException(status_code=422, detail={"errors": errs})
    _require_cipher_if_secrets(req.secrets)

    saved = await store.upsert(
        TenantApiSource(
            tenant_id=tenant.tenant_id, slug=slug, spec=spec, enabled=enabled,
            created_at=existing.created_at,
        )
    )
    await _store_secrets(tenant.tenant_id, slug, req.secrets)
    invalidate_tenant_catalog(tenant.tenant_id)
    return await _summary(saved.materialized_spec(), tenant.tenant_id)


@router.delete("/{slug}", response_model=OkResponse)
async def delete_api_source(
    slug: str,
    tenant: TenantContext = Depends(get_tenant),
):
    """Delete a tenant-custom source + its stored secrets. Global slug => 403,
    missing tenant entry => 404."""
    _guard_not_global(tenant.tenant_id, slug)
    store = _sources_store()
    removed = await store.delete(tenant.tenant_id, slug)
    if not removed:
        raise HTTPException(status_code=404, detail=f"no editable api source '{slug}'")
    try:
        await _secret_store().delete_for_source(tenant.tenant_id, slug)
    except Exception:  # noqa: BLE001 — the source is gone; a secret-sweep hiccup is non-fatal
        logger.warning("api_source_secret_delete_failed", slug=slug)
    invalidate_tenant_catalog(tenant.tenant_id)
    return OkResponse()


@router.post("/validate", response_model=ValidateResponse)
async def validate_api_source(
    req: CreateApiSourceRequest,
    tenant: TenantContext = Depends(get_tenant),
):
    """Validate a spec against ``ApiSourceSpec`` (schema + URL lint + auth
    coherence). No write. Returns structured ``{valid, errors:[{path,message}]}``."""
    spec = _parse_spec(req.spec)
    errs = _validation_errors(spec)
    return ValidateResponse(valid=not errs, errors=errs)


@router.post("/test", response_model=TestApiSourceResponse)
async def test_api_source(
    req: TestApiSourceRequest,
    tenant: TenantContext = Depends(get_tenant),
):
    """Run ONE smoke request through the executor (SSRF-guarded). No KG write, no
    persistence. Provide an inline ``spec`` OR an existing ``slug``.

    For an inline spec that uses a ``secret_ref``, the smoke call resolves the
    secret from the tenant's store (if already saved) — so a test never echoes a
    secret. Rows are returned; a secret never appears in them (the executor keeps
    auth out of provenance/sources)."""
    if req.spec is not None:
        spec = _parse_spec(req.spec)
        errs = validate_tenant_spec(spec)
        if errs:
            return TestApiSourceResponse(ok=False, error="; ".join(errs))
    elif req.slug:
        catalog = await load_tenant_custom_catalog(tenant.tenant_id, _sources_store())
        spec = catalog.get(req.slug)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"no api source '{req.slug}'")
    else:
        raise HTTPException(status_code=422, detail="provide either 'slug' or 'spec'")

    resolver = None
    if spec.auth.secret_ref:
        resolver = make_secret_resolver(tenant.tenant_id, spec.slug)

    executor = RegistryApiSource()
    try:
        res = await executor.execute(
            spec, req.sample_params, max_rows=5, sample=True, secret_resolver=resolver
        )
    except Exception as exc:  # noqa: BLE001 — the executor shouldn't raise, but never leak details
        logger.warning("api_source_test_failed", slug=spec.slug, error=type(exc).__name__)
        return TestApiSourceResponse(ok=False, error="test call failed")

    if res.error or res.dormant:
        return TestApiSourceResponse(ok=False, rows=res.rows, error=res.error or "dormant")
    return TestApiSourceResponse(ok=True, rows=res.rows)

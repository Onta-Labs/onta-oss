"""User-owned tenant management — list / add / remove the caller's tenants.

These are the *single* backend routes both the CLI and the web Explorer use to
manage tenants, so the two surfaces can never drift. The work itself (reading and
writing the user's tenant list on their identity profile) is delegated to a
registered ``TenantProvider``; cograph-oss ships none, so an OSS-only deployment
returns 501 here. The premium Clerk integration registers a provider.

Auth: the caller proves identity with their own ``X-API-Key`` — the same key used
everywhere else. The provider resolves key → user and operates on that user's
tenants; no identity-provider admin secret is ever required client-side.
"""

import structlog
from fastapi import APIRouter, HTTPException, Security
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from cograph_client.auth.api_keys import api_key_header
from cograph_client.auth.tenant_directory import (
    Tenant,
    TenantProvider,
    TenantProviderError,
    get_tenant_provider,
    validate_new_tenant,
)
from cograph_client.auth.workspace_store import (
    make_workspace_store,
    ownership_enforced,
    resolve_subject,
)

logger = structlog.stdlib.get_logger("cograph.tenants")

router = APIRouter(prefix="/v1/me/tenants")


class TenantOut(BaseModel):
    id: str
    label: str


class TenantCreate(BaseModel):
    id: str = Field(..., description="Tenant slug (lowercase, 3–40 chars).")
    label: str = Field(..., description="Human-readable label.")


def _require_provider() -> TenantProvider:
    provider = get_tenant_provider()
    if provider is None:
        raise HTTPException(
            status_code=501,
            detail="Tenant management is not configured for this deployment.",
        )
    return provider


def _require_key(api_key: str | None) -> str:
    if not api_key:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return api_key


def _out(t: Tenant) -> TenantOut:
    return TenantOut(id=t.id, label=t.label)


@router.get("", response_model=list[TenantOut])
def list_tenants(api_key: str | None = Security(api_key_header)):
    provider = _require_provider()
    key = _require_key(api_key)
    try:
        return [_out(t) for t in provider.list_tenants(key)]
    except TenantProviderError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


async def _claim_or_check_ownership(api_key: str, tenant_id: str, label: str) -> None:
    """Workspace-registry gate for create (ONTA-227) — closes the self-add hole.

    Deliberate inversion of the accept/removal dual-write order: the registry
    row goes FIRST (``INSERT .. ON CONFLICT DO NOTHING`` + returned-row check
    is the concurrency guard, so it must come first), the provider second. If
    the provider write then fails, a same-caller retry heals it (the caller is
    now owner, so create passes and re-delegates).

    Semantics:
    - key carries no subject (static/anonymous) → skip entirely; behaves
      exactly as today.
    - id unregistered → lazy-claim (caller becomes owner + owner member row).
    - caller already owner/member (re-adding after switcher removal) → allow,
      no new row.
    - registered to someone else → 403 "workspace id is taken", but ONLY when
      enforcement is on (env flag + durable store — see ownership_enforced);
      otherwise allow-and-log (rollout step 1: writes on, enforcement off).

    Registry outages fail open when enforcement is off (create never needed a
    DB before this feature) and fail closed when it is on (the registry IS the
    security substrate then).
    """
    subject = resolve_subject(api_key)
    if subject is None:
        return
    store = make_workspace_store()
    try:
        claimed = await store.claim_workspace(tenant_id, subject, label)
        if claimed is not None:
            return  # this call won the claim; caller is now the owner
        ws = await store.get_workspace(tenant_id)
        if ws is None:
            return  # row vanished (manual cleanup); don't block
        if ws.owner_subject == subject:
            return
        if await store.get_member(tenant_id, subject) is not None:
            return
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — registry outage
        if ownership_enforced(store):
            raise
        logger.warning(
            "workspace_registry_unavailable", tenant=tenant_id, error=str(exc)
        )
        return
    if ownership_enforced(store):
        raise HTTPException(status_code=403, detail="workspace id is taken")
    logger.warning(
        "workspace_ownership_not_enforced_allow",
        tenant=tenant_id,
        hint="id is registered to another subject; allowed (enforcement off)",
    )


@router.post("", response_model=TenantOut, status_code=201)
async def add_tenant(body: TenantCreate, api_key: str | None = Security(api_key_header)):
    # async (unlike its sync siblings) because the workspace registry is
    # asyncpg-backed; the sync provider calls are bridged via run_in_threadpool
    # so they cannot block the event loop.
    provider = _require_provider()
    key = _require_key(api_key)
    try:
        # Validate before touching the provider so bad input is a clean 400 and
        # the rules stay identical to the Explorer's (validate_new_tenant is the
        # shared source of truth; it raises TenantProviderError(400)).
        tenant_id, label = validate_new_tenant(body.id, body.label)
        # Registry row first, provider second — see _claim_or_check_ownership.
        await _claim_or_check_ownership(key, tenant_id, label)
        return _out(await run_in_threadpool(provider.add_tenant, key, tenant_id, label))
    except TenantProviderError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.delete("/{tenant_id}")
def remove_tenant(tenant_id: str, api_key: str | None = Security(api_key_header)):
    provider = _require_provider()
    key = _require_key(api_key)
    try:
        provider.remove_tenant(key, tenant_id)
    except TenantProviderError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    return {"removed": tenant_id}

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

from cograph_client.auth.access import resolve_member_role
from cograph_client.auth.api_keys import api_key_header
from cograph_client.auth.capabilities import capability_for_role
from cograph_client.auth.tenant_directory import (
    Tenant,
    TenantProvider,
    TenantProviderError,
    ensure_label_available,
    get_tenant_provider,
    mint_untitled_tenant_id,
    next_untitled_label,
    validate_label,
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
    #: Membership role on this workspace: owner | writer | reader.
    role: str = "writer"
    #: Derived capability: read | write (write implies read).
    capability: str = "write"


class TenantCreate(BaseModel):
    """Both fields are optional: an empty body mints an auto-named workspace.

    That is the one-click "Add workspace" path — the Explorer (and any other
    client) POSTs nothing and gets back "Untitled workspace N" with a fresh
    slug, then renames it via PATCH. Supplying both keeps the explicit
    create-with-a-name flow the CLI uses.
    """

    id: str | None = Field(
        None, description="Tenant slug (lowercase, 3–40 chars). Auto-minted if omitted."
    )
    label: str | None = Field(
        None, description='Human-readable label. Defaults to "Untitled workspace N".'
    )


class TenantRename(BaseModel):
    label: str = Field(..., description="New human-readable label.")


def _require_provider() -> TenantProvider:
    provider = get_tenant_provider()
    if provider is None:
        raise HTTPException(
            status_code=501,
            detail="Tenant management is not configured for this deployment.",
        )
    return provider


def _require_rename(provider: TenantProvider):
    """Renaming is optional on the provider protocol (added after the original
    three methods), so a provider that predates it reports 501 instead of 500."""
    fn = getattr(provider, "rename_tenant", None)
    if fn is None:
        raise HTTPException(
            status_code=501,
            detail="Renaming a workspace is not supported by this deployment.",
        )
    return fn


def _require_key(api_key: str | None) -> str:
    if not api_key:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return api_key


def _out(t: Tenant, *, role: str = "writer") -> TenantOut:
    return TenantOut(
        id=t.id,
        label=t.label,
        role=role,
        capability=capability_for_role(role),
    )


@router.get("", response_model=list[TenantOut])
async def list_tenants(api_key: str | None = Security(api_key_header)):
    provider = _require_provider()
    key = _require_key(api_key)
    try:
        tenants = await run_in_threadpool(provider.list_tenants, key)
    except TenantProviderError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    subject = resolve_subject(key)
    out: list[TenantOut] = []
    for t in tenants:
        role = await resolve_member_role(t.id, subject)
        out.append(_out(t, role=role))
    return out


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
async def add_tenant(
    body: TenantCreate | None = None, api_key: str | None = Security(api_key_header)
):
    # async (unlike its sync siblings) because the workspace registry is
    # asyncpg-backed; the sync provider calls are bridged via run_in_threadpool
    # so they cannot block the event loop.
    provider = _require_provider()
    key = _require_key(api_key)
    body = body or TenantCreate()
    try:
        # The caller's current list serves two purposes: naming the auto-created
        # workspace, and enforcing "no two of my workspaces share a name". One
        # read covers both.
        owned = await run_in_threadpool(provider.list_tenants, key)
        # Validate before touching the provider so bad input is a clean 400 and
        # the rules stay identical across clients (tenant_directory is the
        # shared source of truth; it raises TenantProviderError).
        # OMITTED (null) means "pick one for me"; PRESENT-but-empty is a caller
        # mistake and stays a 400, as it always was.
        label = (
            next_untitled_label(owned)
            if body.label is None
            else validate_label(body.label)
        )
        tenant_id = (
            mint_untitled_tenant_id()
            if body.id is None
            else validate_new_tenant(body.id, label)[0]
        )
        ensure_label_available(owned, label)
        # Registry row first, provider second — see _claim_or_check_ownership.
        await _claim_or_check_ownership(key, tenant_id, label)
        created = await run_in_threadpool(provider.add_tenant, key, tenant_id, label)
        return _out(created, role="owner")
    except TenantProviderError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.patch("/{tenant_id}", response_model=TenantOut)
async def rename_tenant(
    tenant_id: str, body: TenantRename, api_key: str | None = Security(api_key_header)
):
    """Rename one of the caller's workspaces. The id is immutable — it keys the
    graph IRIs — so only the label changes."""
    provider = _require_provider()
    rename = _require_rename(provider)
    key = _require_key(api_key)
    try:
        label = validate_label(body.label)
        owned = await run_in_threadpool(provider.list_tenants, key)
        if not any(t.id == tenant_id for t in owned):
            raise TenantProviderError(404, f'Tenant "{tenant_id}" not found.')
        ensure_label_available(owned, label, exclude_id=tenant_id)
        renamed = await run_in_threadpool(rename, key, tenant_id, label)
    except TenantProviderError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    subject = resolve_subject(key)
    role = await resolve_member_role(tenant_id, subject)
    # Labels are per-user (each user's identity profile carries their own copy),
    # but the workspace registry holds ONE label that invite emails render. Keep
    # it in step when the OWNER renames — a member renaming their own view must
    # not rewrite what everyone else sees. Best-effort: the rename itself already
    # succeeded, and the registry is allowed to be unavailable elsewhere too.
    if role == "owner" and subject:
        try:
            await make_workspace_store().set_workspace_label(tenant_id, label)
        except Exception as exc:  # noqa: BLE001 — registry outage
            logger.warning(
                "workspace_registry_label_not_updated", tenant=tenant_id, error=str(exc)
            )
    return _out(renamed, role=role)


@router.delete("/{tenant_id}")
def remove_tenant(tenant_id: str, api_key: str | None = Security(api_key_header)):
    provider = _require_provider()
    key = _require_key(api_key)
    try:
        provider.remove_tenant(key, tenant_id)
    except TenantProviderError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    return {"removed": tenant_id}

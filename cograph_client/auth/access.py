"""Request-time tenant access checks (read vs write).

Membership roles live in :mod:`cograph_client.auth.workspace_store`. This
module is the ONE place mutating routes ask "may this subject write?" so
graph-level grants can plug in later without re-touching every handler.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException

from cograph_client.auth.api_keys import TenantContext, get_tenant
from cograph_client.auth.capabilities import (
    can_write,
    capability_for_role,
    normalize_role,
)
from cograph_client.auth.workspace_store import make_workspace_store


async def resolve_member_role(tenant_id: str, subject: str | None) -> str:
    """Best-effort membership role for ``subject`` on ``tenant_id``.

    * no subject (static key) → ``writer`` (full access, today's default)
    * owner registry match → ``owner``
    * membership row → normalized role
    * granted via Clerk metadata only (no row yet) → ``writer`` (back-compat)
    """
    if not subject:
        return "writer"
    store = make_workspace_store()
    try:
        member = await store.get_member(tenant_id, subject)
        if member is not None:
            return normalize_role(member.role)
        ws = await store.get_workspace(tenant_id)
        if ws is not None and ws.owner_subject == subject:
            return "owner"
    except Exception:
        # Registry outage: fail open to write so a DB blip cannot freeze
        # production for entitled users (same posture as ownership rollout).
        return "writer"
    return "writer"


async def attach_tenant_capability(tenant: TenantContext) -> TenantContext:
    """Fill ``role`` / ``capability`` on a resolved :class:`TenantContext`."""
    role = await resolve_member_role(tenant.tenant_id, tenant.subject)
    tenant.role = role
    tenant.capability = capability_for_role(role)
    return tenant


async def get_tenant_with_capability(
    tenant: TenantContext = Depends(get_tenant),
) -> TenantContext:
    """Like :func:`get_tenant` but with membership capability attached."""
    return await attach_tenant_capability(tenant)


async def require_tenant_write(
    tenant: TenantContext = Depends(get_tenant),
) -> TenantContext:
    """Reject read-only members on mutating routes (HTTP 403).

    Static / subject-less keys keep write access. Use as
    ``Depends(require_tenant_write)`` on ingest, enrich write, ontology
    mutations, KG create/delete, etc. Read routes keep plain ``get_tenant``.

    **The one sanctioned exception** is ``POST /graphs/{tenant}/agent``
    (ONTA-451): it is the single read/write MIXED route, so a blanket dependency
    here would also block the read-only turns a reader is entitled to. It uses
    :func:`get_tenant_with_capability` and gates at capability dispatch inside
    the planner instead, raising the same 403 wording. Any OTHER mutating route
    belongs on this dependency — a route that "mostly reads" is not a reason to
    hand-roll a second gate.
    """
    ctx = await attach_tenant_capability(tenant)
    if not can_write(ctx.role):
        raise HTTPException(
            status_code=403,
            detail=(
                "This workspace membership is read-only. "
                "Ask the owner for write access to make changes."
            ),
        )
    return ctx

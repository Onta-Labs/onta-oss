"""Workspace base-pin GET / preview / upgrade / rollback (ONTA-410).

Patched pin helpers are looked up on the facade via ``_host()``.
"""

from __future__ import annotations

from typing import Any

from fastapi import Body, Depends, HTTPException, Query

from infona_client.api.deps import get_neptune_client
from infona_client.auth.access import get_tenant_with_capability, require_tenant_write
from infona_client.auth.api_keys import TenantContext
from infona_client.auth.capabilities import can_write
from infona_client.graph.entitlement import is_entitled
from infona_client.graph.ontology_base_pin import BasePinReadError
from infona_client.graph.queries import tenant_graph_uri
from infona_client.models.ontology import (
    BasePinResponse,
    BasePinUpgradeRequest,
    CollisionRecordResponse,
    UpgradePreviewResponse,
)
from infona_client.api.routes.ontology_common import _base_pin_response, _host


async def get_workspace_base_pin(
    tenant: TenantContext = Depends(get_tenant_with_capability),
    client: Any = Depends(get_neptune_client),
):
    """Current workspace base pin + revision + upgrade affordance (ONTA-410).

    Ensures a pin when missing (soft backfill to latest) so the version strip
    always has a defined state. Pin **read** infrastructure failures → 503
    (never silent re-pin to latest).

    The backfill is a WRITE, so it is gated on the caller's write capability
    (ONTA-452): a read-only member sees the same pin, computed ephemerally, and
    opening the version strip no longer pins or auto-upgrades their workspace.
    """
    h = _host()
    entitled = is_entitled(tenant)
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    try:
        pin = await h.ensure_workspace_base_pin(
            client,
            tenant.tenant_id,
            entitled=entitled,
            persist=can_write(tenant.role),
        )
    except BasePinReadError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    latest = await h.latest_base_release_version(client, pin.base_layer)
    rev = await h._current_revision_counter(client, graph_uri)
    return _base_pin_response(
        pin,
        workspace_revision=rev,
        latest_available=latest,
    )


async def preview_workspace_base_upgrade(
    tenant: TenantContext = Depends(get_tenant_with_capability),
    client: Any = Depends(get_neptune_client),
    to_version: int | None = Query(
        None,
        ge=1,
        description="Target base release; omit for latest available",
    ),
):
    """Preview upgrading the workspace base pin (structural ChangeRecords)."""
    h = _host()
    entitled = is_entitled(tenant)
    try:
        preview = await h.preview_base_upgrade(
            client,
            tenant.tenant_id,
            entitled=entitled,
            to_version=to_version,
            # A preview is a read; its internal ensure must not write for a
            # read-only member (ONTA-452).
            persist=can_write(tenant.role),
        )
    except BasePinReadError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return UpgradePreviewResponse(
        from_version=preview.from_version,
        to_version=preview.to_version,
        base_layer=preview.base_layer,
        changes=list(preview.changes),
        collisions=[
            CollisionRecordResponse(
                type_name=c.type_name,
                slot_name=c.slot_name,
                kind=c.kind,
                detail=c.detail,
            )
            for c in preview.collisions
        ],
        deprecated_used=list(preview.deprecated_used),
        summary=list(preview.summary),
        from_fingerprint=preview.from_fingerprint,
        to_fingerprint=preview.to_fingerprint,
    )


async def post_workspace_base_upgrade(
    body: BasePinUpgradeRequest = Body(default_factory=BasePinUpgradeRequest),
    tenant: TenantContext = Depends(require_tenant_write),
    client: Any = Depends(get_neptune_client),
) -> BasePinResponse:
    """Upgrade the workspace base pin to ``to_version`` (or latest)."""
    h = _host()
    entitled = is_entitled(tenant)
    to_version = body.to_version if body is not None else None
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    try:
        pin = await h.upgrade_base_pin(
            client,
            tenant.tenant_id,
            entitled=entitled,
            to_version=to_version,
        )
    except BasePinReadError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    latest = await h.latest_base_release_version(client, pin.base_layer)
    rev = await h._current_revision_counter(client, graph_uri)
    return _base_pin_response(
        pin,
        workspace_revision=rev,
        latest_available=latest,
    )


async def post_workspace_base_rollback(
    tenant: TenantContext = Depends(require_tenant_write),
    client: Any = Depends(get_neptune_client),
) -> BasePinResponse:
    """Roll the workspace base pin back to its previous version."""
    h = _host()
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    try:
        pin = await h.rollback_base_pin(client, tenant.tenant_id)
    except BasePinReadError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    latest = await h.latest_base_release_version(client, pin.base_layer)
    rev = await h._current_revision_counter(client, graph_uri)
    return _base_pin_response(
        pin,
        workspace_revision=rev,
        latest_available=latest,
    )

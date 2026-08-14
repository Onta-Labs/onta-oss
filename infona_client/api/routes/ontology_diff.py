"""Structural ontology diff as ChangeRecords (ONTA-410).

Patched ``get_base_pin`` / ``diff_graphs`` are looked up via ``_host()``.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Query

from infona_client.api.deps import get_neptune_client
from infona_client.auth.api_keys import TenantContext, get_tenant
from infona_client.graph.ontology_base_pin import BasePinReadError
from infona_client.models.ontology import OntologyDiffResponse
from infona_client.api.routes.ontology_common import _host, _resolve_ontology_ref


async def get_ontology_diff(
    tenant: TenantContext = Depends(get_tenant),
    client: Any = Depends(get_neptune_client),
    from_ref: str | None = Query(
        None,
        alias="from",
        description="Source ref: current | revision:N | release:N | graph URI",
    ),
    to_ref: str | None = Query(
        None,
        alias="to",
        description="Target ref: current | revision:N | release:N | graph URI",
    ),
    from_revision: int | None = Query(
        None, ge=1, description="Shorthand for from=revision:N"
    ),
    to_revision: int | None = Query(
        None, ge=1, description="Shorthand for to=revision:N"
    ),
):
    """Structural ontology diff as ChangeRecords (ONTA-410).

    Reuses ``diff_graphs`` / ``diff_shapes`` so the viewer and the pure
    classifier see the same records. Missing snapshot graphs resolve to empty
    shapes (clear empty), never a 500. Deep-link a version/revision that does
    not exist → empty change list for that scope.
    """
    h = _host()
    # Resolve shorthand query params into refs.
    src = from_ref
    dst = to_ref
    if from_revision is not None:
        src = f"revision:{from_revision}"
    if to_revision is not None:
        dst = f"revision:{to_revision}"
    if not src or not dst:
        raise HTTPException(
            status_code=422,
            detail="provide from+to (or from_revision+to_revision) version refs",
        )

    # Base layer from pin so release:N maps correctly (soft; live public on miss).
    base_layer = "public"
    try:
        pin = await h.get_base_pin(client, tenant.tenant_id)
        if pin is not None:
            base_layer = pin.base_layer
    except BasePinReadError:
        base_layer = "public"

    try:
        from_label, from_uri = _resolve_ontology_ref(
            src, tenant_id=tenant.tenant_id, base_layer=base_layer
        )
        to_label, to_uri = _resolve_ontology_ref(
            dst, tenant_id=tenant.tenant_id, base_layer=base_layer
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    changes = await h.diff_graphs(client, from_uri, to_uri)
    verdict = h.classify_diff(changes)
    return OntologyDiffResponse(
        tenant_id=tenant.tenant_id,
        from_ref=from_label,
        to_ref=to_label,
        from_graph_uri=from_uri,
        to_graph_uri=to_uri,
        changes=list(changes),
        count=len(changes),
        compat_class=verdict.overall.value,
        requires_major=verdict.requires_major,
        summary=list(verdict.summary),
    )

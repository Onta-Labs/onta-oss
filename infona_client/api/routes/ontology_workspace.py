"""Layered workspace ontology reads (ONTA-397 / ONTA-535).

Reads go through the GraphStore catalog. ``client`` is unused (no SPARQL);
kept on signatures so route call sites stay stable.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends

from infona_client.api.deps import get_neptune_client
from infona_client.auth.access import get_tenant_with_capability
from infona_client.auth.api_keys import TenantContext, get_tenant
from infona_client.graph.entitlement import is_entitled
from infona_client.models.function import FunctionRef
from infona_client.models.ontology import (
    TypeResponse,
    WorkspaceOntologyResponse,
    WorkspaceOntologyType,
    WorkspaceTypeCount,
    WorkspaceTypeCountsResponse,
)
from infona_client.api.routes.ontology_common import _type_response


async def _workspace_catalog(tenant_id: str, subject: str | None = None):
    """Tenant + caller-user API-source catalog for the workspace overlay.

    Loads the workspace's ``tenant_custom`` entries and, when ``subject`` is
    set, that user's ``user_custom`` entries (visible across every workspace
    they can access). tenant_custom still shadows a same-slug user entry for
    THIS workspace only. Degrades to ``None`` on any failure so a broken
    source store never sinks the ontology read. Operator
    ``fetch_global_ontology`` never takes a tenant/user catalog — private
    entries must not leak onto the cross-tenant route.
    """
    try:
        from infona_client.api_registry.catalog import (
            get_api_source_catalog,
            load_tenant_custom_catalog,
            load_user_custom_catalog,
        )
        from infona_client.api_registry.store import make_tenant_api_source_store
        from infona_client.api_registry.user_store import make_user_api_source_store

        await load_tenant_custom_catalog(tenant_id, make_tenant_api_source_store())
        if subject:
            await load_user_custom_catalog(subject, make_user_api_source_store())
        return get_api_source_catalog(tenant_id, subject=subject)
    except Exception:
        return None


async def _workspace_ontology(
    tenant: TenantContext, client: Any
) -> WorkspaceOntologyResponse:
    """Effective (shadowed) ontology for ``tenant`` — LayerStack catalog merge.

    Full browser payload (ONTA-408): layered types + tenant-custom sources +
    tenant-layer skills overlay. Writes never go through this path.

    **Catalog layers (ONTA-535).** Types/attrs come from
    :mod:`ontology_catalog` (GraphStore) for every visible layer — Tenant >
    Enhanced (when entitled) > Public — with first-visible-layer-wins
    shadowing. Replaces the SPARQL ``LayerStack`` / ``fetch_ontology`` path
    that went out with the SPARQL backend (ONTA-527). ``client`` is unused
    (no SPARQL); kept on the signature so route call sites stay stable.
    """
    del client  # catalog path; no SPARQL
    return await _workspace_ontology_store(tenant)


async def _workspace_ontology_store(
    tenant: TenantContext,
) -> WorkspaceOntologyResponse:
    """Layered ontology from the GraphStore ontology catalog (ONTA-535).

    Reads each visible catalog layer (via :func:`layer_stack_for`) and merges
    with Tenant > Enhanced > Public shadowing. Per-layer status (available /
    type_count) is reported so the Explorer layer strip is never empty for a
    layer that was actually consulted.
    """
    from infona_client.graph.entitlement import layer_stack_for
    from infona_client.graph.layers import Layer
    from infona_client.graph.ontology_catalog import (
        list_attributes as cat_list_attrs,
        list_types as cat_list_types,
    )
    from infona_client.models.ontology import (
        GlobalOntologyAttribute,
        GlobalOntologyRelationship,
        WorkspaceOntologyLayer,
    )

    entitled = is_entitled(tenant)
    stack = layer_stack_for(tenant)

    # name → winning WorkspaceOntologyType (first-visible-layer wins).
    types_by_name: dict[str, WorkspaceOntologyType] = {}
    # parent_name → child names that survived shadowing (built after merge).
    raw_children: dict[str, list[str]] = {}
    layer_infos: list[WorkspaceOntologyLayer] = []

    for layer in stack.layers:
        layer_name = layer.value
        available = True
        try:
            if layer is Layer.TENANT:
                type_rows = await cat_list_types(
                    layer="tenant", tenant_id=tenant.tenant_id
                )
                attr_rows = await cat_list_attrs(
                    layer="tenant",
                    tenant_id=tenant.tenant_id,
                    type_name=None,
                )
            else:
                # Public / Enhanced global catalog — reads need no privilege.
                type_rows = await cat_list_types(layer=layer_name)
                attr_rows = await cat_list_attrs(
                    layer=layer_name, type_name=None
                )
        except Exception:
            # Degrade this layer only; others still contribute (ADR 0002 §1).
            available = False
            type_rows = []
            attr_rows = []

        attrs_by_domain: dict[str, list] = {}
        for a in attr_rows:
            attrs_by_domain.setdefault(a.domain, []).append(a)

        layer_infos.append(
            WorkspaceOntologyLayer(
                layer=layer_name,
                graph_uri=stack.graph_uri_for(layer),
                type_count=len(type_rows),
                available=available,
            )
        )

        for t in type_rows:
            if t.name in types_by_name:
                # Higher-precedence layer already owns this name.
                continue
            attributes: list[GlobalOntologyAttribute] = []
            relationships: list[GlobalOntologyRelationship] = []
            for a in attrs_by_domain.get(t.name, []):
                if a.kind == "relationship":
                    relationships.append(
                        GlobalOntologyRelationship(
                            name=a.name,
                            target_type=a.range_type or "Thing",
                            description=a.description or None,
                        )
                    )
                else:
                    attributes.append(
                        GlobalOntologyAttribute(
                            name=a.name,
                            datatype=a.datatype or "string",
                            description=a.description or None,
                        )
                    )
            if t.parent_type:
                raw_children.setdefault(t.parent_type, []).append(t.name)
            types_by_name[t.name] = WorkspaceOntologyType(
                name=t.name,
                description=t.description or None,
                parent_type=t.parent_type,
                attributes=attributes,
                relationships=relationships,
                subtypes=[],  # filled below from surviving children
                functions=[],  # GraphStore function attach still SPARQL-only
                layer=layer_name,
            )

    # Subtype inversion only over types that survived shadowing, so a
    # shadowed child never appears under a winner from another layer.
    for t in types_by_name.values():
        kids = [
            c for c in raw_children.get(t.name, []) if c in types_by_name
        ]
        # Rebuild with sorted subtypes (frozen model fields are mutable lists).
        t.subtypes = sorted(kids)

    types_out = sorted(
        types_by_name.values(),
        key=lambda t: (t.name.lower(), t.layer),
    )

    # Attach sources / skills / functions AFTER the catalog type merge.
    try:
        from infona_client.api_registry.user_store import effective_owner_subject
        from infona_client.graph.global_ontology import (
            _WorkspaceSkillIndex,
            _build_source_index,
            _load_tenant_skills,
        )

        subject = effective_owner_subject(tenant.api_key, tenant.subject)
        catalog = await _workspace_catalog(tenant.tenant_id, subject)
        source_idx = await _build_source_index(catalog=catalog)
        visible = {info.layer for info in layer_infos} | {"tenant"}
        tenant_skills = await _load_tenant_skills(tenant.tenant_id)
        skill_idx = _WorkspaceSkillIndex(
            tenant.tenant_id,
            visible_layers=visible,
            tenant_skills=tenant_skills,
        )
        fns_by_type: dict[str, list[FunctionRef]] = {}
        try:
            from infona_client.functions.store import make_function_store

            for rec in await make_function_store().list_for_tenant(tenant.tenant_id):
                fns_by_type.setdefault(rec.entity_type.casefold(), []).append(
                    FunctionRef(
                        name=rec.name,
                        entity_type=rec.entity_type,
                        endpoint_url=rec.endpoint_url,
                        description=rec.description,
                        layer=rec.layer or "tenant",
                    )
                )
        except Exception:
            pass
        for t in types_out:
            t.sources = source_idx.for_type(t.name)
            t.skills = skill_idx.for_type(t.name)
            t.functions = fns_by_type.get(t.name.casefold(), [])
    except Exception:
        pass

    return WorkspaceOntologyResponse(
        tenant_id=tenant.tenant_id,
        types=types_out,
        entitled=entitled,
        layers=layer_infos,
    )


async def get_workspace_ontology(
    tenant: TenantContext = Depends(get_tenant_with_capability),
    client: Any = Depends(get_neptune_client),
):
    """Effective layered ontology for this workspace (ONTA-397).

    Canonical full-payload read: layers status + shadowed types. Empty is 200
    with ``types: []``. Writes never go through this route.
    """
    return await _workspace_ontology(tenant, client)


async def workspace_type_counts(
    tenant: TenantContext = Depends(get_tenant),
):
    """Workspace-wide union of per-type entity counts (ONTA-409).

    Unions ``KgStats.type_breakdown`` across every knowledge graph in this
    tenant's durable stats store — one relational read, no SPARQL. Types with
    zero instances in every KG are omitted (so the response IS the Active set).
    """
    from infona_client.graph.kg_stats_store import (
        get_kg_stats_store,
        union_type_breakdowns,
    )

    store = get_kg_stats_store()
    try:
        rows = await store.list_for_tenant(tenant.tenant_id)
    except Exception:  # noqa: BLE001 — degrade to empty Active, never 500
        rows = []

    aggregated = union_type_breakdowns(rows)
    return WorkspaceTypeCountsResponse(
        tenant_id=tenant.tenant_id,
        types=[
            WorkspaceTypeCount(name=name, entity_count=total, by_kg=by_kg)
            for name, total, by_kg in aggregated
        ],
        kg_names=sorted({r.kg_name for r in rows}),
    )


async def list_types(
    tenant: TenantContext = Depends(get_tenant_with_capability),
    client: Any = Depends(get_neptune_client),
) -> list[TypeResponse]:
    """List effective types (tenant + visible global layers, shadowed)."""
    body = await _workspace_ontology(tenant, client)
    return [_type_response(t) for t in body.types]


async def get_type(
    type_name: str,
    tenant: TenantContext = Depends(get_tenant_with_capability),
    client: Any = Depends(get_neptune_client),
):
    """Type detail from the effective (shadowed) layered ontology."""
    from fastapi import HTTPException

    body = await _workspace_ontology(tenant, client)
    for t in body.types:
        if t.name == type_name:
            return _type_response(t)
    raise HTTPException(status_code=404, detail=f"Type '{type_name}' not found")


async def get_full_schema(
    tenant: TenantContext = Depends(get_tenant_with_capability),
    client: Any = Depends(get_neptune_client),
):
    """Complete effective schema (layered + shadowed). Used by the NL pipeline."""
    body = await _workspace_ontology(tenant, client)
    types: dict = {}
    for t in body.types:
        types[t.name] = {
            "attributes": [
                {"name": a.name, "datatype": a.datatype} for a in t.attributes
            ]
            + [
                {"name": r.name, "datatype": r.target_type} for r in t.relationships
            ],
            "functions": [f.name for f in t.functions],
            "layer": t.layer,
        }
    return {"types": types, "entitled": body.entitled, "tenant_id": body.tenant_id}

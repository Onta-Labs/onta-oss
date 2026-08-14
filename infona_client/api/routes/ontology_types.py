"""Tenant-catalog type / attribute / subtype writes.

Catalog upserts only — no instance triples. Schema declarations live in the
tenant catalog layer, never a global layer.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException

from infona_client.auth.access import require_tenant_write
from infona_client.auth.api_keys import TenantContext
from infona_client.models.ontology import AttributeAdd, SubtypeAdd, TypeCreate


async def create_type(
    body: TypeCreate,
    tenant: TenantContext = Depends(require_tenant_write),
):
    # WRITE path: tenant layer only. Never a global layer.
    from infona_client.graph.ontology_catalog import (
        upsert_attribute as cat_upsert_attr,
        upsert_type as cat_upsert_type,
    )

    await cat_upsert_type(
        name=body.name,
        description=body.description or "",
        parent_type=body.parent_type,
        layer="tenant",
        tenant_id=tenant.tenant_id,
    )
    for attr in body.attributes:
        await cat_upsert_attr(
            type_name=body.name,
            attr_name=attr.name,
            description=attr.description or "",
            datatype=attr.datatype or "string",
            layer="tenant",
            tenant_id=tenant.tenant_id,
        )
    return {"created": body.name, "attributes": len(body.attributes)}


async def add_attributes(
    type_name: str,
    body: AttributeAdd,
    tenant: TenantContext = Depends(require_tenant_write),
):
    # WRITE path: tenant layer only.
    from infona_client.graph.ontology_catalog import (
        upsert_attribute as cat_upsert_attr,
    )

    for attr in body.attributes:
        await cat_upsert_attr(
            type_name=type_name,
            attr_name=attr.name,
            description=attr.description or "",
            datatype=attr.datatype or "string",
            layer="tenant",
            tenant_id=tenant.tenant_id,
        )
    return {"type": type_name, "attributes_added": len(body.attributes)}


async def delete_attribute_route(
    type_name: str,
    attr_name: str,
    tenant: TenantContext = Depends(require_tenant_write),
):
    """Drop one tenant-catalog attribute declaration.

    Instance facts are left untouched. Explorer chips/columns that come from
    the declared schema (empty ``lead_sponsor`` after a KG wipe) disappear
    once this returns. Evicts the in-process type-summary cache for the
    tenant so a refresh does not keep serving the deleted attr for 30 min.
    """
    from infona_client.graph.ontology_catalog import (
        delete_attribute as cat_delete_attr,
    )
    from infona_client.graph.queries import require_valid_type_name

    require_valid_type_name(type_name)
    ok = await cat_delete_attr(
        type_name=type_name,
        attr_name=attr_name,
        layer="tenant",
        tenant_id=tenant.tenant_id,
    )
    if not ok:
        raise HTTPException(
            status_code=404,
            detail=f"Attribute '{attr_name}' not found on type '{type_name}'",
        )
    try:
        from infona_client.api.routes.explore import _summary_cache

        for key in [k for k in _summary_cache if k[0] == tenant.tenant_id]:
            _summary_cache.pop(key, None)
    except Exception:  # noqa: BLE001 — never fail a schema delete on cache
        pass
    return {"deleted": True, "type": type_name, "attribute": attr_name}


async def add_subtype(
    type_name: str,
    body: SubtypeAdd,
    tenant: TenantContext = Depends(require_tenant_write),
):
    # WRITE path: tenant layer only.
    from infona_client.graph.ontology_catalog import upsert_type as cat_upsert_type

    # Subtype is an OntoType with parent_type = type_name.
    await cat_upsert_type(
        name=body.subtype,
        parent_type=type_name,
        layer="tenant",
        tenant_id=tenant.tenant_id,
    )
    return {"parent": type_name, "subtype": body.subtype}

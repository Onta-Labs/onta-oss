"""Attribute alias register / rename / backfill / retire (ONTA-407).

Schema mutations go through ``commit_ontology`` on the facade (``_host()``)
so tests that patch ``ontology.commit_ontology`` still fire. Instance
predicate rewrite uses the existing ``backfill_aliases`` entrypoint.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException

from infona_client.api.deps import get_neptune_client
from infona_client.auth.access import require_tenant_write
from infona_client.auth.api_keys import TenantContext, get_tenant
from infona_client.graph.aliases import AliasStillReferencedError
from infona_client.graph.queries import kg_graph_uri, tenant_graph_uri
from infona_client.models.ontology import (
    AliasBackfill,
    AliasMapResponse,
    AliasRegister,
    AliasRename,
    AliasRetire,
    OntologyMutation,
    OntologyOpKind,
)
from infona_client.api.routes.ontology_common import _host


async def register_attribute_alias(
    body: AliasRegister,
    tenant: TenantContext = Depends(require_tenant_write),
    client: Any = Depends(get_neptune_client),
):
    """Register an attribute alias (old → new) on the tenant ontology graph.

    Alias-edge only. Prefer ``POST /aliases/rename`` for a full rename that
    also updates the schema declaration (always creates the alias).
    """
    h = _host()
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    try:
        result = await h.commit_ontology(
            client,
            graph_uri,
            [
                OntologyMutation(
                    op=OntologyOpKind.REGISTER_ALIAS,
                    type_name=body.type_name,
                    alias_from=body.from_slot,
                    alias_to=body.to_slot,
                    target_type=body.to_type,
                )
            ],
            actor=tenant.tenant_id,
            message=f"register_alias {body.from_slot} → {body.to_slot}",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    old_uri = new_uri = None
    if result.change_records:
        rec = result.change_records[0]
        old_uri, new_uri = rec.old_value, rec.new_value
    return {
        "type": body.type_name,
        "from_slot": body.from_slot,
        "to_slot": body.to_slot,
        "to_type": body.to_type or body.type_name,
        "old_attr_uri": old_uri,
        "new_attr_uri": new_uri,
        "version_before": result.version_before,
        "version_after": result.version_after,
    }


async def rename_attribute_with_alias(
    body: AliasRename,
    tenant: TenantContext = Depends(require_tenant_write),
    client: Any = Depends(get_neptune_client),
):
    """Full attribute rename — ALWAYS creates an alias (ONTA-407b).

    Ensures the new attribute declaration, records ``old aliasOf new``, and
    drops the old schema declaration. Instance triples keep the old predicate
    until ``POST /aliases/backfill``; retirement refuses while refs remain.
    """
    h = _host()
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    try:
        result = await h.commit_ontology(
            client,
            graph_uri,
            [
                OntologyMutation(
                    op=OntologyOpKind.RENAME_ATTRIBUTE,
                    type_name=body.type_name,
                    alias_from=body.from_slot,
                    alias_to=body.to_slot,
                    target_type=body.to_type,
                    datatype=body.datatype,
                    description=body.description,
                )
            ],
            actor=tenant.tenant_id,
            message=f"rename_attribute {body.from_slot} → {body.to_slot}",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    rename_rec = next(
        (c for c in result.change_records if c.kind.value == "rename_with_alias"),
        result.change_records[0] if result.change_records else None,
    )
    return {
        "type": body.type_name,
        "from_slot": body.from_slot,
        "to_slot": body.to_slot,
        "to_type": body.to_type or body.type_name,
        "old_attr_uri": rename_rec.old_value if rename_rec else None,
        "new_attr_uri": rename_rec.new_value if rename_rec else None,
        "version_before": result.version_before,
        "version_after": result.version_after,
        "change_records": [c.model_dump() for c in result.change_records],
    }


async def backfill_attribute_aliases(
    body: AliasBackfill,
    tenant: TenantContext = Depends(require_tenant_write),
    client: Any = Depends(get_neptune_client),
):
    """Rewrite old-predicate instance triples onto their alias targets.

    After a clean backfill (zero remaining refs), call
    ``DELETE /aliases`` (retire) to drop the alias edge.
    """
    h = _host()
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    data_graph = kg_graph_uri(tenant.tenant_id, body.kg_name)
    alias_map = await h.fetch_alias_map(client, graph_uri)
    if body.old_attr_uri:
        if body.old_attr_uri not in alias_map:
            raise HTTPException(
                status_code=404,
                detail=f"no alias registered for {body.old_attr_uri!r}",
            )
        alias_map = {body.old_attr_uri: alias_map[body.old_attr_uri]}
    rewritten = await h.backfill_aliases(
        client, data_graph, alias_map, batch_size=body.batch_size,
    )
    return {
        "kg_name": body.kg_name,
        "data_graph_uri": data_graph,
        "rewritten": rewritten,
        "aliases": alias_map,
    }


async def retire_attribute_alias(
    body: AliasRetire,
    tenant: TenantContext = Depends(require_tenant_write),
    client: Any = Depends(get_neptune_client),
):
    """Retire an alias after backfill — 409 while instance refs remain."""
    h = _host()
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    data_graph = kg_graph_uri(tenant.tenant_id, body.kg_name)
    try:
        result = await h.commit_ontology(
            client,
            graph_uri,
            [
                OntologyMutation(
                    op=OntologyOpKind.RETIRE_ALIAS,
                    type_name=body.type_name,
                    alias_from=body.from_slot,
                    data_graph_uri=data_graph,
                )
            ],
            actor=tenant.tenant_id,
            message=f"retire_alias {body.from_slot}",
        )
    except AliasStillReferencedError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "alias_still_referenced",
                "old_attr_uri": exc.old_attr_uri,
                "remaining": exc.remaining,
                "data_graph_uri": exc.data_graph_uri,
                "message": str(exc),
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "type": body.type_name,
        "from_slot": body.from_slot,
        "kg_name": body.kg_name,
        "retired": True,
        "version_before": result.version_before,
        "version_after": result.version_after,
    }


async def list_attribute_aliases(
    tenant: TenantContext = Depends(get_tenant),
    client: Any = Depends(get_neptune_client),
):
    """Return the flattened old→new attribute alias map for this tenant graph.

    Chains (``a → b → c``) collapse to one hop (``a → c``, ``b → c``). Empty
    when no aliases are registered. Cyclic chains are dropped (never hang).
    """
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    aliases = await _host().fetch_alias_map(client, graph_uri)
    return AliasMapResponse(aliases=aliases)

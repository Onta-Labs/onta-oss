"""Entity detail, Explorer search, and ER-rebuild routes.

``er_rebuild`` is a real merge via ``rewrite_subject`` plus one
``refresh_after_write`` — do not fork a second write path.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from fastapi import Depends, Query

from infona_client.api.deps import get_neptune_client
from infona_client.api.routes.explore_common import RDF_TYPE, RDFS, _esc, _from_graphs
from infona_client.auth.access import require_tenant_write
from infona_client.auth.api_keys import TenantContext, get_tenant
from infona_client.graph.entitlement import layer_stack_for
from infona_client.graph.iri import ENTITY_URI_PREFIX, TYPE_URI_PREFIX
from infona_client.graph.predicates import is_internal_predicate as _is_internal_predicate
from infona_client.graph.kg_writer import refresh_after_write
from infona_client.graph.layers import Layer, fetch_types_by_layer, layer_type_uri
from infona_client.graph.parser import parse_sparql_results
from infona_client.graph.ontology_queries import type_uri
from infona_client.graph.queries import (
    kg_graph_uri,
    skip_invalid_type_name,
    sparql_string_literal,
    tenant_graph_uri,
)

logger = structlog.stdlib.get_logger("infona.api.explore_entity")


async def get_entity_detail_route(
    kg_name: str,
    entity_id: str,
    tenant: TenantContext = Depends(get_tenant),
    client: Any = Depends(get_neptune_client),
):
    """Entity detail (properties + incident relationships).

    **Dual-backend (E9):** under ``INFONA_GRAPH_BACKEND=neo4j`` (or an injected
    GraphStore) uses :func:`infona_client.graph.explore_store.get_entity_detail`.
    On the default Neptune path, assembles the same shape via SPARQL point
    lookups on the KG graph.
    """
    from fastapi import HTTPException

    from infona_client.graph.explore_store import (
        get_entity_detail as pg_entity_detail,
        resolve_explore_session,
    )
    from infona_client.graph.iri import ONTO_PRED_PREFIX

    eid = entity_id.strip()
    if not eid:
        raise HTTPException(status_code=422, detail="entity_id is required")

    # GraphStore path (ONTA-534: GraphConfigError → 503, no SPARQL hang).
    from infona_client.graph.store import GraphConfigError

    try:
        resolve_explore_session(tenant_id=tenant.tenant_id, kg_name=kg_name)
    except GraphConfigError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "Graph store is not configured. Neo4j GraphStore is required "
                f"(ONTA-534). {exc}"
            ),
        ) from exc

    detail = await pg_entity_detail(
        tenant_id=tenant.tenant_id,
        kg_name=kg_name,
        entity_id=eid,
    )
    if detail is None:
        # ONTA-534: no SPARQL fallthrough — store answered "missing".
        raise HTTPException(status_code=404, detail=f"Entity '{eid}' not found")
    return {
        "id": detail.id,
        "name": detail.name,
        "primary_type": detail.primary_type,
        "source": detail.source,
        "labels": list(detail.labels),
        "properties": dict(detail.properties),
        "outgoing": [
            {
                "attr": r.attr,
                "rel_type": r.rel_type,
                "other_id": r.other_id,
                "other_name": r.other_name,
                "other_type": r.other_type,
                "direction": r.direction,
            }
            for r in detail.outgoing
        ],
        "incoming": [
            {
                "attr": r.attr,
                "rel_type": r.rel_type,
                "other_id": r.other_id,
                "other_name": r.other_name,
                "other_type": r.other_type,
                "direction": r.direction,
            }
            for r in detail.incoming
        ],
    }

    # Residual SPARQL path retired (ONTA-534) — kept below as unreachable
    # archaeology only; GraphStore path always returns or 404s above.
    kg_graph = kg_graph_uri(tenant.tenant_id, kg_name)  # pragma: no cover
    props_sparql = (
        f"SELECT ?p ?o FROM <{kg_graph}> WHERE {{\n"
        f"  <{eid}> ?p ?o .\n"
        f"}}"
    )
    out_sparql = (
        f"SELECT ?p ?o ?olabel ?otype FROM <{kg_graph}> WHERE {{\n"
        f"  <{eid}> ?p ?o .\n"
        f"  FILTER(isIRI(?o))\n"
        f"  OPTIONAL {{ ?o <{RDFS}#label> ?olabel }}\n"
        f"  OPTIONAL {{ ?o <{RDF_TYPE}> ?otype }}\n"
        f"}}"
    )
    in_sparql = (
        f"SELECT ?p ?s ?slabel ?stype FROM <{kg_graph}> WHERE {{\n"
        f"  ?s ?p <{eid}> .\n"
        f"  OPTIONAL {{ ?s <{RDFS}#label> ?slabel }}\n"
        f"  OPTIONAL {{ ?s <{RDF_TYPE}> ?stype }}\n"
        f"}}"
    )
    props_raw, out_raw, in_raw = await asyncio.gather(
        client.query(props_sparql),
        client.query(out_sparql),
        client.query(in_sparql),
    )
    _, prop_rows = parse_sparql_results(props_raw)
    if not prop_rows:
        raise HTTPException(status_code=404, detail=f"Entity '{eid}' not found")

    properties: dict = {}
    name: str | None = None
    primary_type: str | None = None
    labels: list[str] = []
    LABEL_PRED = f"{RDFS}#label"
    for r in prop_rows:
        p = r.get("p", "")
        o = r.get("o", "")
        if p == RDF_TYPE:
            leaf = o.rstrip("/").split("/")[-1] if o else ""
            if leaf and (primary_type is None or leaf < primary_type):
                primary_type = leaf
            if leaf and leaf not in labels:
                labels.append(leaf)
            continue
        if p == LABEL_PRED:
            name = o
            continue
        if _is_internal_predicate(p, is_relationship=o.startswith(ENTITY_URI_PREFIX)):
            continue
        leaf = p.rstrip("/").split("/")[-1]
        if leaf and leaf != "name":
            properties[leaf] = o
        elif leaf == "name" and name is None:
            name = o

    def _type_leaf(uri: str) -> str | None:
        if not uri:
            return None
        return uri.rstrip("/").split("/")[-1] or None

    _, out_rows = parse_sparql_results(out_raw)
    outgoing = []
    for r in out_rows:
        p = r.get("p", "")
        o = r.get("o", "")
        if not o or p == RDF_TYPE:
            continue
        if _is_internal_predicate(p, is_relationship=True):
            continue
        attr = p[len(ONTO_PRED_PREFIX):] if p.startswith(ONTO_PRED_PREFIX) else p.rstrip("/").split("/")[-1]
        outgoing.append(
            {
                "attr": attr,
                "rel_type": attr,
                "other_id": o,
                "other_name": r.get("olabel"),
                "other_type": _type_leaf(r.get("otype", "")),
                "direction": "out",
            }
        )

    _, in_rows = parse_sparql_results(in_raw)
    incoming = []
    for r in in_rows:
        p = r.get("p", "")
        s = r.get("s", "")
        if not s or p == RDF_TYPE:
            continue
        if _is_internal_predicate(p, is_relationship=True):
            continue
        attr = p[len(ONTO_PRED_PREFIX):] if p.startswith(ONTO_PRED_PREFIX) else p.rstrip("/").split("/")[-1]
        incoming.append(
            {
                "attr": attr,
                "rel_type": attr,
                "other_id": s,
                "other_name": r.get("slabel"),
                "other_type": _type_leaf(r.get("stype", "")),
                "direction": "in",
            }
        )

    return {
        "id": eid,
        "name": name,
        "primary_type": primary_type,
        "source": None,
        "labels": labels,
        "properties": properties,
        "outgoing": outgoing,
        "incoming": incoming,
    }


async def er_rebuild(
    kg_name: str,
    tenant: TenantContext = Depends(require_tenant_write),
    client: Any = Depends(get_neptune_client),
):
    """Second-pass entity resolution (MOE-22): collapse intra-batch fragments.

    Mutating: a real ER merge (``rewrite_subject``) plus post-write housekeeping,
    so ``require_tenant_write`` refuses a ``reader`` member with 403 (ONTA-451).

    Re-runs ER over the already-ingested KG so same-entity rows that couldn't
    see each other's index triples mid-batch now merge. Runs synchronously and
    returns per-type before/after counts (the merge volume is modest). Stale
    type-stats are recomputed in the background afterward so the Explorer
    reflects the new counts without blocking this response.
    """
    from infona_client.resolver.er.rebuild import rebuild_kg

    instance_graph = kg_graph_uri(tenant.tenant_id, kg_name)
    report = await rebuild_kg(client, instance_graph)
    # Shared post-write housekeeping path (kg_writer.refresh_after_write):
    # merge changed counts, not the type schema → affected_types=() (no
    # re-embed; still cache-invalidates + recomputes Explorer type-stats).
    await refresh_after_write(
        client, tenant_id=tenant.tenant_id, kg_name=kg_name, affected_types=()
    )
    return {"status": "complete", "kg": kg_name, **report}


async def search_explorer(
    kg_name: str = Query(..., alias="kg"),
    q: str = Query(..., min_length=1),
    kind: str = Query("type", pattern="^(type|attr)$"),
    tenant: TenantContext = Depends(get_tenant),
    client: Any = Depends(get_neptune_client),
):
    """Search types or attributes by name substring.

    kind=type  — returns matching type names + their instance counts.
    kind=attr  — returns every type that has an attribute matching the query.

    Ontology side is layered (ONTA-397): Public/Enhanced declarations are
    visible under the caller's LayerStack; same-name collisions collapse by
    first-visible-layer-wins when assembling the result set.
    """
    stack = layer_stack_for(tenant)
    from_clause = _from_graphs(stack.visible_graph_uris())
    kg_graph = kg_graph_uri(tenant.tenant_id, kg_name)
    q_lower = q.lower()

    if kind == "type":
        # Prefer the layered resolver so shadowing is explicit and one name
        # never appears twice across tenant + Public.
        types_by_layer = await fetch_types_by_layer(client, stack)
        all_names: set[str] = set()
        for layer_map in types_by_layer.values():
            all_names.update(layer_map)
        matched = sorted(n for n in all_names if q_lower in n.lower())

        results = []
        for type_name in matched:
            # Fail SOFT here, unlike the single-type routes above (ONTA-425).
            # These names come back from the ONTOLOGY, not from the caller, and
            # this loop is an ENUMERATION: letting `layer_type_uri` raise on one
            # corrupt stored name would 422 the whole search for every other
            # type, the all-or-nothing failure infona-oss#274 had to fix for KG
            # names. Skipping keeps the corruption observable in logs (and the
            # bad type genuinely unqueryable) without taking the listing down.
            if skip_invalid_type_name(type_name, "explore_search"):
                continue
            resolved = stack.resolve_type(type_name, types_by_layer)
            if resolved is None:
                continue
            layer, _ = resolved
            t_uri = layer_type_uri(layer, type_name)
            # Also count instances typed under the bare tenant URI (historical
            # writes) so a Public type with tenant-namespace instances is not
            # reported as empty solely because of the namespace split.
            tenant_t_uri = type_uri(type_name)
            count_uris = [t_uri] if t_uri == tenant_t_uri else [t_uri, tenant_t_uri]
            entity_count = 0
            for cu in count_uris:
                count_sparql = (
                    f"SELECT (COUNT(DISTINCT ?e) AS ?n) FROM <{kg_graph}> WHERE {{\n"
                    f"  ?e <{RDF_TYPE}> <{cu}> .\n"
                    f"  FILTER NOT EXISTS {{\n"
                    f"    ?e <{RDF_TYPE}> ?type2 .\n"
                    f'    FILTER(STRSTARTS(STR(?type2), "{TYPE_URI_PREFIX}") '
                    f'&& STR(?type2) < "{cu}")\n'
                    f"  }}\n"
                    f"}}"
                )
                try:
                    _, count_rows = parse_sparql_results(await client.query(count_sparql))
                    entity_count += int(count_rows[0].get("n", "0")) if count_rows else 0
                except Exception:
                    pass
            results.append({
                "name": type_name,
                "entity_count": entity_count,
                "layer": layer.value,
            })
        return results

    # kind == "attr" — union of visible layer graphs; dedupe by (attr, type name).
    #
    # GraphStore catalog FIRST (ONTA-534). The SPARQL read below is the ONLY
    # thing this branch ever did, it had no ``try``, and the SPARQL HTTP read
    # is retired under the shipped Neo4j GraphStore — so every Explorer
    # attribute search came back a 500. The catalog is the same declaration
    # source the ontology browser renders, read once per VISIBLE layer so
    # entitlement still decides what a workspace can see. The SPARQL arm stays
    # as a supplement (dual-arm tests, and any layer the catalog cannot answer
    # for); its failure is swallowed once the catalog has answered, because
    # "no attribute matches that substring" is the ORDINARY result of a search
    # and must not read as an error.
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for layer in stack.layers:
        for attr_name, type_name in await _catalog_attr_matches(
            layer, tenant.tenant_id, q_lower
        ):
            key = (attr_name, type_name)
            if key in seen:
                continue
            seen.add(key)
            out.append({"attr_name": attr_name, "type_name": type_name})

    sparql = (
        f"SELECT DISTINCT ?attrLabel ?type ?typeLabel {from_clause} WHERE {{\n"
        f"  ?attr <{RDF_TYPE}> <http://www.w3.org/1999/02/22-rdf-syntax-ns#Property> .\n"
        f"  ?attr <{RDFS}#label> ?attrLabel .\n"
        f"  ?attr <{RDFS}#domain> ?type .\n"
        f"  ?type <{RDFS}#label> ?typeLabel .\n"
        f'  FILTER(CONTAINS(LCASE(STR(?attrLabel)), "{_esc(q_lower)}"))\n'
        f"}}"
    )
    try:
        _, rows = parse_sparql_results(await client.query(sparql))
    except Exception as exc:  # noqa: BLE001 — retired client must not 500
        logger.debug(
            "explore_attr_search_sparql_failed",
            tenant_id=tenant.tenant_id,
            error=str(exc),
        )
        return out
    for r in rows:
        attr_name = r.get("attrLabel", "")
        type_name = r.get("typeLabel", "")
        if not attr_name or not type_name:
            continue
        key = (attr_name, type_name)
        if key in seen:
            continue
        seen.add(key)
        out.append({"attr_name": attr_name, "type_name": type_name})
    return out


async def _catalog_attr_matches(
    layer: Layer, tenant_id: str, q_lower: str
) -> list[tuple[str, str]]:
    """``(attr_name, type_name)`` declarations in ONE layer matching ``q_lower``.

    Reads :func:`infona_client.graph.ontology_catalog.list_attributes` for the
    layer — the same catalog the ontology browser and the NL planner's layer
    read (#447) use — and filters by the same case-insensitive substring the
    SPARQL ``CONTAINS(LCASE(...))`` applied. Best-effort per layer: an
    unconfigured store or a catalog error yields ``[]`` for THAT layer only,
    mirroring ``fetch_types_by_layer``'s "degrade to an empty layer, never
    error" contract (ADR 0002 §1), so one unreadable layer cannot take the
    search down for the others.
    """
    try:
        from infona_client.graph.ontology_catalog import list_attributes
        from infona_client.graph.store import get_optional_graph_store

        rows = await list_attributes(
            store=get_optional_graph_store(),
            type_name=None,
            layer=layer.value,
            tenant_id=tenant_id if layer is Layer.TENANT else None,
        )
    except Exception as exc:  # noqa: BLE001 — degrade to an empty layer
        logger.debug(
            "explore_attr_search_catalog_failed",
            layer=layer.value,
            tenant_id=tenant_id,
            error=str(exc),
        )
        return []
    matches: list[tuple[str, str]] = []
    for rec in rows or ():
        attr_name = getattr(rec, "name", "") or ""
        type_name = getattr(rec, "domain", "") or ""
        if not attr_name or not type_name:
            continue
        if q_lower not in attr_name.lower():
            continue
        matches.append((attr_name, type_name))
    return matches

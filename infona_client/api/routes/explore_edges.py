"""Type→type edge reads (stats graph + live scan, drift-gated variants)."""

from __future__ import annotations

import asyncio
from typing import Any

from infona_client.api.routes.explore_common import (
    RDF_TYPE,
    _PRIMARY_TYPE_GUARD,
    _CORE_SLOT_PRED,
    _STAT_ENTITY_COUNT,
    _STAT_FOR_PRED,
    _STAT_FOR_TYPE,
    _STAT_REL,
    _STAT_TARGET,
    _is_core_slot,
    _stats_graph_uri,
    _target_from_entity_uri,
    _type_leaf,
)
from infona_client.graph.iri import ENTITY_URI_PREFIX, ONTO_PRED_PREFIX, TYPE_URI_PREFIX
from infona_client.graph.ontology_queries import attr_uri
from infona_client.graph.parser import parse_sparql_results
from infona_client.graph.predicates import is_internal_predicate as _is_internal_predicate
from infona_client.graph.queries import kg_graph_uri, tenant_graph_uri
from infona_client.resolver import drift_control


async def _read_edges_from_stats(
    client: Any, tenant_id: str, kg_name: str
) -> list[tuple[str, str]] | None:
    """Type→type edges from the precomputed stats graph, or None if unmaterialized.

    Reads the SAME instance-derived ``targetType`` the per-type summary uses, so
    the overview and the detail view agree by construction. Returns None (not [])
    when no stat rows carry a target, so the caller can fall back to a live scan.
    """
    stats = _stats_graph_uri(tenant_id, kg_name)
    q = (
        f"SELECT DISTINCT ?src ?tgt FROM <{stats}> WHERE {{\n"
        f"  ?s <{_STAT_FOR_TYPE}> ?src ; <{_STAT_TARGET}> ?tgt .\n"
        f"}}"
    )
    _, rows = parse_sparql_results(await client.query(q))
    if not rows:
        return None
    out: list[tuple[str, str]] = []
    for r in rows:
        su, tu = r.get("src", ""), r.get("tgt", "")
        if su.startswith(TYPE_URI_PREFIX) and tu.startswith(TYPE_URI_PREFIX):
            out.append((su[len(TYPE_URI_PREFIX):], tu[len(TYPE_URI_PREFIX):]))
    return out


async def _read_edges_from_stats_drift(
    client: Any, tenant_id: str, kg_name: str
) -> list[tuple[str, str]] | None:
    """ADR 0004 drift-gated variant of :func:`_read_edges_from_stats`.

    Same instance-derived ``targetType`` edges, but each is additionally tagged
    with its **support** (the ``_STAT_REL`` entity-valued-object total), the
    **source type's entity count** (``entityCount``), and whether the upgraded
    predicate is a **core slot** (the ``onto/coreSlot`` marker that
    ``mark_core_slot`` writes in the ontology graph, keyed by the predicate URI).

    An edge is kept only when ``drift_control.should_declare(support,
    source_count, is_core_slot)`` is True — i.e. it clears the coverage+count
    floor, or is a core slot (exempt). Below-floor edges (the
    ``ManufacturerPartNumber.issuedby -> Retailer`` 6%-coverage shape) are
    excluded from the overview. Returns None when no stat rows carry a target,
    so the caller can fall back to a live scan (which is unfiltered — a fresh KG
    without materialized stats predates drift control and must not regress).

    Only invoked when the ``INFONA_DRIFT_CONTROL`` flag is ON; with the flag OFF
    the caller takes the unchanged :func:`_read_edges_from_stats` path.
    """
    stats = _stats_graph_uri(tenant_id, kg_name)
    onto = tenant_graph_uri(tenant_id)
    # Per targeted edge: source type, target type, support (rel), and the
    # predicate URI (so we can join the ontology core-slot marker). entityCount
    # for the source type lives on a separate stat triple, joined in by URI.
    q = (
        f"SELECT DISTINCT ?src ?tgt ?pred ?rel ?ec FROM <{stats}> WHERE {{\n"
        f"  ?s <{_STAT_FOR_TYPE}> ?src ; <{_STAT_TARGET}> ?tgt ; <{_STAT_FOR_PRED}> ?pred .\n"
        f"  OPTIONAL {{ ?s <{_STAT_REL}> ?rel }}\n"
        f"  OPTIONAL {{ ?src <{_STAT_ENTITY_COUNT}> ?ec }}\n"
        f"}}"
    )
    # Core-slot markers from the ontology graph, keyed by predicate (attr) URI.
    core_q = (
        f"SELECT DISTINCT ?attr FROM <{onto}> WHERE {{\n"
        f"  ?attr <{_CORE_SLOT_PRED}> ?v .\n"
        f"}}"
    )
    rows_raw, core_raw = await asyncio.gather(client.query(q), client.query(core_q))
    _, rows = parse_sparql_results(rows_raw)
    if not rows:
        return None
    _, core_rows = parse_sparql_results(core_raw)
    core_slots = {r.get("attr", "") for r in core_rows if r.get("attr")}

    out: list[tuple[str, str]] = []
    for r in rows:
        su, tu = r.get("src", ""), r.get("tgt", "")
        if not (su.startswith(TYPE_URI_PREFIX) and tu.startswith(TYPE_URI_PREFIX)):
            continue
        try:
            support = int(r.get("rel", "0") or "0")
        except ValueError:
            support = 0
        try:
            source_count = int(r.get("ec", "0") or "0")
        except ValueError:
            source_count = 0
        # ?pred is the INSTANCE predicate URI (…/onto/<pred>), not an attr URI.
        # core_slots holds ontology attr URIs, so derive the matching attr URI
        # from the source-type leaf + predicate leaf (same join the live scan does).
        src_leaf = su[len(TYPE_URI_PREFIX):]
        p_uri = r.get("pred", "")
        pred_leaf = (
            p_uri[len(ONTO_PRED_PREFIX):] if p_uri.startswith(ONTO_PRED_PREFIX)
            else p_uri.rstrip("/").split("/")[-1]
        )
        is_core = _is_core_slot(src_leaf, pred_leaf, core_slots)
        if not drift_control.should_declare(support, source_count, is_core):
            continue
        out.append((su[len(TYPE_URI_PREFIX):], tu[len(TYPE_URI_PREFIX):]))
    return out


async def _live_edge_scan(client: Any, kg_graph: str) -> list[tuple[str, str]]:
    """Fallback: derive type→type edges straight from instance triples.

    Used when stats aren't materialized yet (KG ingested before stats existed).
    Target type comes from the object entity URI leaf, matching the summary.
    """
    q = (
        f"SELECT DISTINCT ?type ?o FROM <{kg_graph}> WHERE {{\n"
        f"  ?e <{RDF_TYPE}> ?type .\n"
        f"  ?e ?p ?o .\n"
        f'  FILTER(STRSTARTS(STR(?type), "{TYPE_URI_PREFIX}"))\n'
        f'  FILTER(STRSTARTS(STR(?o), "{ENTITY_URI_PREFIX}"))\n'
        f"}}"
    )
    _, rows = parse_sparql_results(await client.query(q))
    out: list[tuple[str, str]] = []
    for r in rows:
        tu = r.get("type", "")
        src = tu[len(TYPE_URI_PREFIX):] if tu.startswith(TYPE_URI_PREFIX) else ""
        if not src or "/" in src:  # skip nested URIs like .../attrs/x
            continue
        tgt = _target_from_entity_uri(r.get("o", ""))
        if tgt:
            out.append((src, tgt))
    return out


async def _live_edge_scan_drift(
    client: Any, kg_graph: str, tenant_id: str
) -> list[tuple[str, str]]:
    """ADR 0004 drift-gated variant of :func:`_live_edge_scan`.

    Used (flag ON only) when a KG has NO materialized stats graph — legacy KGs
    ingested before stats/drift control existed. Without a floor here the
    ``ManufacturerPartNumber.issuedby -> Retailer`` 6%-coverage drift shape still
    surfaces in the overview for un-materialized KGs (the production gap this
    fixes); the flag-OFF path keeps the unfiltered :func:`_live_edge_scan`.

    Derives type→type edges straight from instance triples, but applies the SAME
    support floor as :func:`_read_edges_from_stats_drift`. Per (source type,
    predicate, target type) it computes the support (``COUNT(DISTINCT`` source
    entity), and per source type the entity count; an edge is kept only when
    ``drift_control.should_declare(support, source_count, is_core)`` is True.
    ``is_core`` reads the ontology ``onto/coreSlot`` marker, keyed by
    ``attr_uri(srcLeaf, predLeaf)`` (the instance predicate ``…/onto/<predName>``
    maps to the ontology attr ``…/types/<srcLeaf>/attrs/<predName>``).
    """
    onto = tenant_graph_uri(tenant_id)
    # (1) Per (source type, predicate, target type) support = distinct source
    # entities carrying that entity-valued object. Target leaf derived in SPARQL
    # from the object entity URI (…/entities/<TargetType>/<id>).
    edge_q = (
        f"SELECT ?type ?p ?tgt (COUNT(DISTINCT ?e) AS ?support) FROM <{kg_graph}> WHERE {{\n"
        f"  ?e <{RDF_TYPE}> ?type .\n"
        f"  ?e ?p ?o .\n"
        f'  FILTER(STRSTARTS(STR(?type), "{TYPE_URI_PREFIX}"))\n'
        f'  FILTER(STRSTARTS(STR(?o), "{ENTITY_URI_PREFIX}"))\n'
        f'  BIND(REPLACE(STR(?o), "^.*/entities/([^/]+)/.*$", "$1") AS ?tgt)\n'
        f"}} GROUP BY ?type ?p ?tgt"
    )
    # (2) Per source type entity count (source_count for the coverage ratio).
    count_q = (
        f"SELECT ?type (COUNT(DISTINCT ?e) AS ?ec) FROM <{kg_graph}> WHERE {{\n"
        f"  ?e <{RDF_TYPE}> ?type .\n"
        f'  FILTER(STRSTARTS(STR(?type), "{TYPE_URI_PREFIX}"))\n'
        f"}} GROUP BY ?type"
    )
    # (3) Core-slot markers from the ontology graph, keyed by attr URI — SAME
    # query shape as _read_edges_from_stats_drift.
    core_q = (
        f"SELECT DISTINCT ?attr FROM <{onto}> WHERE {{\n"
        f"  ?attr <{_CORE_SLOT_PRED}> ?v .\n"
        f"}}"
    )
    edge_raw, count_raw, core_raw = await asyncio.gather(
        client.query(edge_q), client.query(count_q), client.query(core_q)
    )

    _, count_rows = parse_sparql_results(count_raw)
    source_counts: dict[str, int] = {}
    for r in count_rows:
        tu = r.get("type", "")
        if not tu.startswith(TYPE_URI_PREFIX):
            continue
        try:
            source_counts[tu] = int(r.get("ec", "0") or "0")
        except ValueError:
            source_counts[tu] = 0

    _, core_rows = parse_sparql_results(core_raw)
    core_slots = {r.get("attr", "") for r in core_rows if r.get("attr")}

    _, edge_rows = parse_sparql_results(edge_raw)
    out: list[tuple[str, str]] = []
    for r in edge_rows:
        tu = r.get("type", "")
        src = tu[len(TYPE_URI_PREFIX):] if tu.startswith(TYPE_URI_PREFIX) else ""
        if not src or "/" in src:  # skip nested URIs like .../attrs/x
            continue
        tgt = r.get("tgt", "")
        if not tgt or "/" in tgt:
            continue
        try:
            support = int(r.get("support", "0") or "0")
        except ValueError:
            support = 0
        source_count = source_counts.get(tu, 0)
        p_uri = r.get("p", "")
        pred_leaf = (
            p_uri[len(ONTO_PRED_PREFIX):] if p_uri.startswith(ONTO_PRED_PREFIX)
            else p_uri.rstrip("/").split("/")[-1]
        )
        is_core = _is_core_slot(src, pred_leaf, core_slots)
        if not drift_control.should_declare(support, source_count, is_core):
            continue
        out.append((src, tgt))
    return out


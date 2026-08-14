"""Live SPARQL scans and precomputed stats-graph reads.

Residual SPARQL arm for hermetic tests / QC. Production Neo4j uses
GraphStore. Client parameters are untyped so this sibling stays off the
residual SPARQL-client allowlist.
"""

from __future__ import annotations

import asyncio
from typing import Any

from infona_client.api.routes.explore_common import (
    RDF_PROPERTY,
    RDF_TYPE,
    RDFS,
    _PRIMARY_TYPE_GUARD,
    _ST_FLAG_AGGREGATES,
    _STAT_CNT,
    _STAT_ENTITY_COUNT,
    _STAT_FOR_PRED,
    _STAT_FOR_TYPE,
    _STAT_REL,
    _STAT_SPATIAL,
    _STAT_TARGET,
    _STAT_TEMPORAL,
    _from_graphs,
    _stats_graph_uri,
    _target_from_entity_uri,
)
from infona_client.graph.iri import ENTITY_URI_PREFIX, TYPE_URI_PREFIX
from infona_client.graph.parser import parse_sparql_results
from infona_client.graph.predicates import (
    companion_leaves as _companion_leaves,
    is_internal_predicate as _is_internal_predicate,
)
from infona_client.spatiotemporal.extract import (
    INTERVAL_END_LOCALS,
    INTERVAL_START_LOCALS,
    VALIDITY_BOUND_LOCALS,
)

# Re-import accumulator from common via explore_flags identity
from infona_client.api.routes.explore_common import _IndexFlagAccumulator  # noqa: E402


async def _live_scan(
    client: Any, kg_graph: str, t_uri: str
) -> tuple[int, list[dict], dict]:
    """Fallback: one instance scan → (entity_count, per-predicate records, flags).

    Used only when precomputed stats are absent for a type. The rdf:type row
    yields the entity count; ``rel`` is the entity-valued object total; flags
    are the per-type spatio-temporal index markers (same rules as recompute).
    """
    pred_sparql = (
        f"SELECT ?p (COUNT(DISTINCT ?e) AS ?cnt) (SAMPLE(?o) AS ?sample)\n"
        f'  (SUM(IF(STRSTARTS(STR(?o), "{ENTITY_URI_PREFIX}"), 1, 0)) AS ?rel)\n'
        f"{_ST_FLAG_AGGREGATES}"
        f"FROM <{kg_graph}> WHERE {{\n"
        f"  ?e <{RDF_TYPE}> <{t_uri}> .\n"
        f"  ?e ?p ?o .\n"
        # Primary-type attribution: when ?e is multi-typed, count it only under
        # its smallest asserted type URI so the fallback matches the precomputed
        # stats (see _PRIMARY_TYPE_GUARD). Single-typed: vacuously satisfied.
        f"  FILTER NOT EXISTS {{\n"
        f"    ?e <{RDF_TYPE}> ?type2 .\n"
        f'    FILTER(STRSTARTS(STR(?type2), "{TYPE_URI_PREFIX}") '
        f'&& STR(?type2) < "{t_uri}")\n'
        f"  }}\n"
        f"}} GROUP BY ?p ORDER BY DESC(?cnt)"
    )
    _, rows = parse_sparql_results(await client.query(pred_sparql))
    entity_count = 0
    records: list[dict] = []
    facc = _IndexFlagAccumulator()
    for r in rows:
        p_uri = r.get("p", "")
        try:
            cnt = int(r.get("cnt", "0"))
        except ValueError:
            cnt = 0
        try:
            rel = int(r.get("rel", "0"))
        except ValueError:
            rel = 0
        if p_uri == RDF_TYPE:
            entity_count = cnt
            continue
        # Skip internal/housekeeping predicates so they never become user-facing
        # attributes/relationships (onto/batch_id, er/blockKey, er/erSignal_*, …).
        # A predicate carrying entity-valued objects (rel > 0) is a relationship,
        # exempt from the literal-only housekeeping markers (FIX 2) — so a real
        # relationship named e.g. `onto/source` is kept, not hidden.
        if _is_internal_predicate(p_uri, is_relationship=rel > 0):
            continue
        try:
            geo = int(r.get("geo", "0") or "0")
        except ValueError:
            geo = 0
        try:
            tmp = int(r.get("tmp", "0") or "0")
        except ValueError:
            tmp = 0
        if geo or tmp:
            facc.add(p_uri, geo, tmp)
        records.append({"p": p_uri, "cnt": cnt, "rel": rel,
                        "target": _target_from_entity_uri(r.get("sample", ""))})
    return entity_count, records, facc.flags()


async def _read_type_stats(
    client: Any, tenant_id: str, kg_name: str, t_uri: str
) -> tuple[int, list[dict], dict] | None:
    """Read precomputed stats for one type, or None if not materialized."""
    stats = _stats_graph_uri(tenant_id, kg_name)
    ec_q = (
        f"SELECT ?ec ?sp ?tp FROM <{stats}> WHERE {{\n"
        f"  <{t_uri}> <{_STAT_ENTITY_COUNT}> ?ec .\n"
        f"  OPTIONAL {{ <{t_uri}> <{_STAT_SPATIAL}> ?sp }}\n"
        f"  OPTIONAL {{ <{t_uri}> <{_STAT_TEMPORAL}> ?tp }}\n"
        f"}}"
    )
    pred_q = (
        f"SELECT ?pred ?cnt ?rel ?target FROM <{stats}> WHERE {{\n"
        f"  ?s <{_STAT_FOR_TYPE}> <{t_uri}> ; <{_STAT_FOR_PRED}> ?pred ; <{_STAT_CNT}> ?cnt .\n"
        f"  OPTIONAL {{ ?s <{_STAT_REL}> ?rel }}\n"
        f"  OPTIONAL {{ ?s <{_STAT_TARGET}> ?target }}\n"
        f"}}"
    )
    ec_raw, pred_raw = await asyncio.gather(client.query(ec_q), client.query(pred_q))
    _, ec_rows = parse_sparql_results(ec_raw)
    if not ec_rows:
        return None
    try:
        entity_count = int(ec_rows[0].get("ec", "0"))
    except ValueError:
        entity_count = 0
    # "1"^^xsd:boolean is an equally valid true — accept both lexical forms so
    # a future writer/backfill touching the stats graph can't silently read False.
    flags = {
        "spatially_indexed": ec_rows[0].get("sp", "") in ("true", "1"),
        "temporally_indexed": ec_rows[0].get("tp", "") in ("true", "1"),
    }
    _, pred_rows = parse_sparql_results(pred_raw)
    records: list[dict] = []
    for r in pred_rows:
        try:
            cnt = int(r.get("cnt", "0"))
        except ValueError:
            cnt = 0
        try:
            rel = int(r.get("rel", "0"))
        except ValueError:
            rel = 0
        target_uri = r.get("target", "")
        target = target_uri[len(TYPE_URI_PREFIX):] if target_uri.startswith(TYPE_URI_PREFIX) else None
        records.append({"p": r.get("pred", ""), "cnt": cnt, "rel": rel, "target": target})
    return entity_count, records, flags

async def _read_all_type_stats(
    client: Any, tenant_id: str, kg_name: str
) -> dict[str, tuple[int, list[dict], dict]]:
    """Precomputed stats for EVERY type in a KG → ``{type_uri: (count, records, flags)}``.

    :func:`_read_type_stats` with the type binding dropped: two queries over the
    tiny stats graph, grouped by type. Returns ``{}`` when the KG has no
    materialized entity counts (legacy KG), so the caller can fall back to
    :func:`_live_scan_all`.
    """
    stats = _stats_graph_uri(tenant_id, kg_name)
    ec_q = (
        f"SELECT ?type ?ec ?sp ?tp FROM <{stats}> WHERE {{\n"
        f"  ?type <{_STAT_ENTITY_COUNT}> ?ec .\n"
        f"  OPTIONAL {{ ?type <{_STAT_SPATIAL}> ?sp }}\n"
        f"  OPTIONAL {{ ?type <{_STAT_TEMPORAL}> ?tp }}\n"
        f"}}"
    )
    pred_q = (
        f"SELECT ?type ?pred ?cnt ?rel ?target FROM <{stats}> WHERE {{\n"
        f"  ?s <{_STAT_FOR_TYPE}> ?type ; <{_STAT_FOR_PRED}> ?pred ; <{_STAT_CNT}> ?cnt .\n"
        f"  OPTIONAL {{ ?s <{_STAT_REL}> ?rel }}\n"
        f"  OPTIONAL {{ ?s <{_STAT_TARGET}> ?target }}\n"
        f"}}"
    )
    ec_raw, pred_raw = await asyncio.gather(client.query(ec_q), client.query(pred_q))
    _, ec_rows = parse_sparql_results(ec_raw)
    out: dict[str, tuple[int, list[dict], dict]] = {}
    for r in ec_rows:
        t_uri = r.get("type", "")
        if _type_leaf(t_uri) is None:
            continue
        # Accept both boolean lexical forms (see _read_type_stats).
        out[t_uri] = (
            _to_int(r.get("ec")),
            [],
            {
                "spatially_indexed": r.get("sp", "") in ("true", "1"),
                "temporally_indexed": r.get("tp", "") in ("true", "1"),
            },
        )
    if not out:
        return {}
    _, pred_rows = parse_sparql_results(pred_raw)
    for r in pred_rows:
        entry = out.get(r.get("type", ""))
        if entry is None:
            continue
        target_uri = r.get("target", "")
        entry[1].append({
            "p": r.get("pred", ""),
            "cnt": _to_int(r.get("cnt")),
            "rel": _to_int(r.get("rel")),
            "target": (
                target_uri[len(TYPE_URI_PREFIX):]
                if target_uri.startswith(TYPE_URI_PREFIX) else None
            ),
        })
    return out


async def _live_scan_all(
    client: Any, kg_graph: str
) -> dict[str, tuple[int, list[dict], dict]]:
    """Fallback for a KG with no materialized stats: ONE whole-KG scan.

    Same ``GROUP BY ?type ?p`` shape :func:`recompute_kg_stats` uses (including
    the primary-type attribution guard), so the fallback and the precomputed
    path agree. One query for the whole KG, never one per type.
    """
    scan = (
        f"SELECT ?type ?p (COUNT(DISTINCT ?e) AS ?cnt) (SAMPLE(?o) AS ?sample)\n"
        f'  (SUM(IF(STRSTARTS(STR(?o), "{ENTITY_URI_PREFIX}"), 1, 0)) AS ?rel)\n'
        f"{_ST_FLAG_AGGREGATES}"
        f"FROM <{kg_graph}> WHERE {{\n"
        f"  ?e <{RDF_TYPE}> ?type .\n"
        f"  ?e ?p ?o .\n"
        f'  FILTER(STRSTARTS(STR(?type), "{TYPE_URI_PREFIX}"))\n'
        f"{_PRIMARY_TYPE_GUARD}"
        f"}} GROUP BY ?type ?p"
    )
    _, rows = parse_sparql_results(await client.query(scan))
    counts: dict[str, int] = {}
    records: dict[str, list[dict]] = {}
    faccs: dict[str, _IndexFlagAccumulator] = {}
    for r in rows:
        t_uri = r.get("type", "")
        if _type_leaf(t_uri) is None:
            continue
        p_uri = r.get("p", "")
        cnt = _to_int(r.get("cnt"))
        rel = _to_int(r.get("rel"))
        if p_uri == RDF_TYPE:
            counts[t_uri] = cnt
            continue
        # Same hygiene as the per-type live scan: housekeeping predicates never
        # become user-facing attributes/relationships.
        if _is_internal_predicate(p_uri, is_relationship=rel > 0):
            continue
        geo, tmp = _to_int(r.get("geo")), _to_int(r.get("tmp"))
        if geo or tmp:
            faccs.setdefault(t_uri, _IndexFlagAccumulator()).add(p_uri, geo, tmp)
        records.setdefault(t_uri, []).append({
            "p": p_uri,
            "cnt": cnt,
            "rel": rel,
            "target": _target_from_entity_uri(r.get("sample", "")),
        })
    out: dict[str, tuple[int, list[dict], dict]] = {}
    for t_uri in set(counts) | set(records):
        facc = faccs.get(t_uri)
        out[t_uri] = (
            counts.get(t_uri, 0),
            records.get(t_uri, []),
            facc.flags() if facc else {},
        )
    return out


async def _read_declared_schema(
    client: Any, graph_uris: list[str]
) -> tuple[dict[str, dict], dict[str, dict[str, dict[str, str]]]]:
    """Declared types + attribute definitions across the visible ontology layers.

    Returns ``({type_uri: {label, comment, parent}}, {type_uri: {attr_uri:
    {name, range}}})``: the ``get_type_summary`` ontology queries with the
    ``<t_uri>`` binding replaced by a selected ``?type`` / ``?domain``.

    Both reads degrade to empty on failure (mirroring
    ``fetch_types_by_layer``): the declarations decorate the population data,
    so an ontology hiccup must not sink the schema read.
    """
    from_clause = _from_graphs(graph_uris)
    type_q = (
        f"SELECT ?type ?label ?comment ?parent {from_clause} WHERE {{\n"
        f"  ?type <{RDF_TYPE}> <{RDFS}#Class> .\n"
        f"  ?type <{RDFS}#label> ?label .\n"
        f"  OPTIONAL {{ ?type <{RDFS}#comment> ?comment }}\n"
        f"  OPTIONAL {{ ?type <{RDFS}#subClassOf> ?parent }}\n"
        f"}}"
    )
    attr_q = (
        f"SELECT ?domain ?attr ?attrLabel ?range {from_clause} WHERE {{\n"
        f"  ?attr <{RDF_TYPE}> <{RDF_PROPERTY}> .\n"
        f"  ?attr <{RDFS}#domain> ?domain .\n"
        f"  ?attr <{RDFS}#label> ?attrLabel .\n"
        f"  OPTIONAL {{ ?attr <{RDFS}#range> ?range }}\n"
        f"}}"
    )
    type_raw, attr_raw = await asyncio.gather(
        client.query(type_q), client.query(attr_q), return_exceptions=True
    )
    types: dict[str, dict] = {}
    if isinstance(type_raw, BaseException):
        logger.warning("schema_type_declarations_failed", exc_info=type_raw)
    else:
        _, type_rows = parse_sparql_results(type_raw)
        for r in type_rows:
            t_uri = r.get("type", "")
            if _type_leaf(t_uri) is None:
                continue
            types.setdefault(t_uri, {
                "label": r.get("label", ""),
                "comment": r.get("comment", ""),
                "parent": r.get("parent", ""),
            })
    attrs: dict[str, dict[str, dict[str, str]]] = {}
    if isinstance(attr_raw, BaseException):
        logger.warning("schema_attr_declarations_failed", exc_info=attr_raw)
    else:
        _, attr_rows = parse_sparql_results(attr_raw)
        for r in attr_rows:
            domain, a_uri = r.get("domain", ""), r.get("attr", "")
            if not a_uri or _type_leaf(domain) is None:
                continue
            attrs.setdefault(domain, {})[a_uri] = {
                "name": r.get("attrLabel", ""),
                "range": r.get("range", ""),
            }
    return types, attrs


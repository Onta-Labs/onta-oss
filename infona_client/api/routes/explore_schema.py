"""Whole-KG schema and type-edge Explorer endpoints (ONTA-418)."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import Depends, Query

from infona_client.api.deps import get_neptune_client
from infona_client.api.routes.explore_common import (
    _DRIFT_COVERAGE,
    _DRIFT_FLOOR_COUNT,
    _DRIFT_FLOOR_COV,
    _DRIFT_IS_CORE,
    _DRIFT_KEPT,
    _DRIFT_KEY,
    _DRIFT_POINT_KEPT,
    _DRIFT_POINT_OF,
    _DRIFT_QUARANTINED,
    _DRIFT_RECORDED_AT,
    _DRIFT_SOURCE_COUNT,
    _DRIFT_SUPPORT,
    _LAYER_TYPE_NAMESPACES,
    _SCHEMA_COVERAGE_NOTE,
    _assemble_summary,
    _dedupe_undirected,
    _drift_history_graph_uri,
    _host,
    _is_sparql_client_type,
    _retired_sparql_client,
    _sorted_slots,
    _to_float,
    _to_int,
    _type_leaf,
    logger,
)
from infona_client.graph.layers import type_namespace
from infona_client.graph.predicates import ATTR_META_SUFFIXES
from infona_client.api.routes.explore_edges import (
    _live_edge_scan,
    _live_edge_scan_drift,
    _read_edges_from_stats,
    _read_edges_from_stats_drift,
)
from infona_client.api.routes.explore_scan import (
    _live_scan_all,
    _read_all_type_stats,
    _read_declared_schema,
)
from infona_client.auth.access import require_tenant_write
from infona_client.auth.api_keys import TenantContext, get_tenant
from infona_client.graph.entitlement import layer_stack_for
from infona_client.graph.parser import parse_sparql_results
from infona_client.graph.queries import kg_graph_uri
from infona_client.resolver import drift_control


async def _schema_from_graph_store(
    *,
    tenant_id: str,
    kg_name: str,
    type_names: list[str] | None,
    min_coverage: float,
    include_empty: bool,
    limit: int,
) -> dict:
    """Population-aware KG schema via GraphStore (P-A1a type_summary compose).

    Raises :class:`~infona_client.graph.store.GraphConfigError` when no store is
    configured (caller maps to 503 under the real SPARQL client / Neo4j-only).
    """
    from infona_client.graph import explore_store as es
    from infona_client.graph import ontology_catalog as oc

    counts = await es.type_counts(tenant_id=tenant_id, kg_name=kg_name)
    if counts is None:
        from infona_client.graph.store import GraphConfigError

        raise GraphConfigError("GraphStore explore path unavailable for type_counts")
    count_map = {r.name: int(r.entity_count) for r in counts}

    declared_names: list[str] = []
    try:
        declared = await oc.list_types(tenant_id=tenant_id, layer="tenant")
        declared_names = [t.name for t in declared if t.name]
    except Exception:
        logger.debug("schema_graphstore_list_types_failed", exc_info=True)

    all_names = sorted(
        set(count_map) | set(declared_names),
        key=lambda n: (-count_map.get(n, 0), n.lower()),
    )
    wanted = {t.lower() for t in (type_names or []) if t}
    names = [n for n in all_names if n.lower() in wanted] if wanted else list(all_names)

    async def _one(name: str):
        try:
            return await es.type_summary(
                tenant_id=tenant_id, kg_name=kg_name, type_name=name
            )
        except Exception:
            logger.warning(
                "schema_graphstore_type_summary_failed",
                type_name=name,
                kg=kg_name,
                exc_info=True,
            )
            return None

    rows = await asyncio.gather(*[_one(n) for n in names]) if names else []

    assembled: list[dict] = []
    for name, row in zip(names, rows):
        if row is None:
            if not include_empty and not wanted:
                continue
            # Declared or instance-named type with no summary row — minimal shell.
            summary = {
                "name": name,
                "description": "",
                "parent_type": None,
                "entity_count": count_map.get(name, 0),
                "attributes": [],
                "relationships": [],
                "spatially_indexed": False,
                "temporally_indexed": False,
            }
        else:
            summary = row.as_api_dict()
        summary["attributes"], attrs_omitted = _sorted_slots(
            list(summary.get("attributes") or []), min_coverage
        )
        summary["relationships"], rels_omitted = _sorted_slots(
            list(summary.get("relationships") or []), min_coverage
        )
        entity_count = int(summary.get("entity_count") or 0)
        summary["populated"] = entity_count > 0
        summary["declared_only"] = entity_count == 0 and name in declared_names
        summary["attributes_withheld"] = attrs_omitted
        summary["relationships_withheld"] = rels_omitted
        assembled.append(summary)

    if not include_empty and not wanted:
        assembled = [t for t in assembled if t["entity_count"] > 0]

    assembled.sort(key=lambda t: (-t["entity_count"], t["name"]))
    kept = assembled[:limit]
    return {
        "kg": kg_name,
        "types": kept,
        "total_types": len(assembled),
        "truncated": len(assembled) > len(kept),
        "omitted_type_names": [t["name"] for t in assembled[limit:]],
        "available_type_names": all_names if (wanted and not assembled) else [],
        "stats_source": "graph_store",
        "coverage_note": _SCHEMA_COVERAGE_NOTE,
    }


async def _type_edges_from_graph_store(
    *, tenant_id: str, kg_name: str
) -> list[tuple[str, str]]:
    """Undirected type→type edges from per-type relationship summaries."""
    from infona_client.graph import explore_store as es

    counts = await es.type_counts(tenant_id=tenant_id, kg_name=kg_name)
    if counts is None:
        from infona_client.graph.store import GraphConfigError

        raise GraphConfigError("GraphStore explore path unavailable for type_counts")
    pairs: list[tuple[str, str]] = []
    for row in counts:
        if row.entity_count <= 0:
            continue
        try:
            summary = await es.type_summary(
                tenant_id=tenant_id, kg_name=kg_name, type_name=row.name
            )
        except Exception:
            logger.debug(
                "type_edges_summary_failed", type_name=row.name, exc_info=True
            )
            continue
        if summary is None:
            continue
        for rel in summary.relationships:
            tgt = rel.target_type
            if tgt:
                pairs.append((row.name, tgt))
    return pairs


async def get_kg_schema(
    kg_name: str,
    type_names: list[str] | None = Query(
        None, alias="type",
        description="Repeatable. Narrows to these type names (drill-in).",
    ),
    min_coverage: float = Query(
        0.0, ge=0.0, le=100.0,
        description="Withhold attributes/relationships below this coverage PERCENT.",
    ),
    include_empty: bool = Query(
        True, description="Include types declared in the ontology with 0 instances here."
    ),
    limit: int = Query(50, ge=1, le=500, description="Max types returned (entity_count desc)."),
    tenant: TenantContext = Depends(get_tenant),
    client: Any = Depends(get_neptune_client),
):
    """Population-aware schema for ONE KG: every type with its POPULATED slots.

    The whole-KG counterpart of ``/types/{type}/summary``, same per-type shape,
    assembled by the same ``_assemble_summary`` (so the same predicate hygiene
    applies: internal ER/batch predicates and legacy per-attribute provenance
    companions never surface), but for every type in one request. This is a
    BACKEND join on purpose: the stats it reads are already materialized for the
    whole KG, so it costs 3-4 queries, where a client-side loop over the per-type
    summary would be 1+N round trips.

    Declared-but-empty types and attributes are INCLUDED and marked
    (``populated: false`` / ``declared_only: true``), never hidden. Hiding them
    made agents assert "that type does not exist" or substitute a wrong type
    (ONTA-248 / ONTA-258). ``min_coverage`` is the one filter that withholds
    slots, and it only acts when the caller explicitly sets it.

    **GraphStore / Neo4j:** composes :func:`explore_store.type_counts` +
    :func:`explore_store.type_summary` + ontology catalog declarations
    (``stats_source=graph_store``). Required under production GraphStore so
    MCP ``inspect_graph_schema`` does not hit retired SPARQL (ONTA-534 residual).

    Not exposed here: sample VALUES. Nothing serves them over HTTP today (the NL
    pipeline computes them inside ``/ask`` only). Deliberately out of scope.
    """
    from fastapi import HTTPException

    from infona_client.graph.store import GraphConfigError

    try:
        gs_result = await _schema_from_graph_store(
            tenant_id=tenant.tenant_id,
            kg_name=kg_name,
            type_names=type_names,
            min_coverage=min_coverage,
            include_empty=include_empty,
            limit=limit,
        )
        # Production Neo4j (real retired SPARQL client) always uses GraphStore — even for
        # empty KGs — so we never fall through to SparqlClientRetired (500).
        # Dual-arm unit tests seed AsyncMock SPARQL only while the autouse
        # MemoryGraphStore is empty: fall through when both are empty mocks.
        if gs_result.get("types") or _is_sparql_client_type(client):
            return gs_result
    except GraphConfigError as exc:
        if _is_sparql_client_type(client) and not getattr(client, "_allow_http", False):
            raise HTTPException(
                status_code=503,
                detail=(
                    "Graph store is not configured. Neo4j GraphStore is required "
                    f"(ONTA-534). {exc}"
                ),
            ) from exc
        # Residual SPARQL dual-arm (hermetic unit tests / QC archaeology).
        pass

    stack = layer_stack_for(tenant)
    kg_graph = kg_graph_uri(tenant.tenant_id, kg_name)

    (declared_types, declared_attrs), stats_by_uri = await asyncio.gather(
        _read_declared_schema(client, stack.visible_graph_uris()),
        _read_all_type_stats(client, tenant.tenant_id, kg_name),
    )
    stats_source = "precomputed"
    if not stats_by_uri:
        # Legacy KG with no materialized stats: ONE whole-KG scan, not one per type.
        stats_by_uri = await _live_scan_all(client, kg_graph)
        stats_source = "live_scan"

    # Collapse type URIs onto type NAMES. Declarations shadow by layer precedence
    # (first visible layer wins); instance stats pick the URI carrying the most
    # entities, so a Public declaration whose instances were written under the
    # historical tenant namespace resolves to ONE entry rather than a populated
    # orphan plus a phantom empty type.
    layer_rank = {type_namespace(layer): i for i, layer in enumerate(stack.layers)}

    def _rank(uri: str) -> int:
        for ns in _LAYER_TYPE_NAMESPACES:
            if uri.startswith(ns):
                return layer_rank.get(ns, len(layer_rank))
        return len(layer_rank)

    decl_uri_by_name: dict[str, str] = {}
    for t_uri in declared_types:
        leaf = _type_leaf(t_uri)
        if leaf is None:
            continue
        current = decl_uri_by_name.get(leaf)
        if current is None or _rank(t_uri) < _rank(current):
            decl_uri_by_name[leaf] = t_uri
    stats_uri_by_name: dict[str, str] = {}
    for t_uri, (count, _recs, _flags) in stats_by_uri.items():
        leaf = _type_leaf(t_uri)
        if leaf is None:
            continue
        current = stats_uri_by_name.get(leaf)
        if current is None or count > stats_by_uri[current][0]:
            stats_uri_by_name[leaf] = t_uri

    # Case-INSENSITIVE, because the caller is usually an LLM that may lowercase a
    # type name. An exact-match miss would answer "no such type", which is the
    # very failure mode this endpoint exists to prevent.
    wanted = {t.lower() for t in (type_names or []) if t}
    all_names = sorted(set(decl_uri_by_name) | set(stats_uri_by_name))
    names = [n for n in all_names if n.lower() in wanted] if wanted else all_names

    assembled: list[dict] = []
    for name in names:
        d_uri = decl_uri_by_name.get(name)
        s_uri = stats_uri_by_name.get(name)
        onto_row = declared_types.get(d_uri, {}) if d_uri else {}
        parent_uri = onto_row.get("parent", "")
        parent_type = parent_uri.rstrip("/").split("/")[-1] if parent_uri else None
        # Attribute declarations from the winning layer AND from the URI the
        # instances actually use, so the predicate→name/range lookup resolves in
        # either namespace.
        attr_defs: dict[str, dict[str, str]] = {}
        for u in (d_uri, s_uri):
            if u:
                attr_defs.update(declared_attrs.get(u, {}))
        entity_count, records, flags = (
            stats_by_uri[s_uri] if s_uri else (0, [], {})
        )
        records = list(records)
        # DECLARED-but-unpopulated slots: synthesized as count-0 records so the
        # agent sees every attribute the schema promises (marked unpopulated),
        # never a silently shortened list. Deduped by display name because a
        # populated relationship's instance predicate (`onto/<leaf>`) differs
        # from its ontology declaration URI (`types/<T>/attrs/<leaf>`).
        real_names = {
            (attr_defs.get(r.get("p", ""), {}).get("name")
             or r.get("p", "").rstrip("/").split("/")[-1])
            for r in records
        }
        seen = {n.lower() for n in real_names}
        for a_uri, defn in attr_defs.items():
            nm = defn.get("name") or a_uri.rstrip("/").split("/")[-1]
            if not nm or nm.lower() in seen:
                continue
            # Do not synthesize a base name that would retroactively turn a
            # POPULATED `<nm>_<suffix>` record into a legacy provenance companion:
            # `_assemble_summary`'s set-wise classifier hides `<base>_<suffix>`
            # only when `<base>` is present, so adding an empty `data` would make
            # a real, populated `data_provenance` disappear. Never trade a
            # populated slot for a declared-empty one.
            if any(f"{nm}_{sfx}" in real_names for sfx in ATTR_META_SUFFIXES):
                continue
            seen.add(nm.lower())
            records.append({"p": a_uri, "cnt": 0, "rel": 0, "target": None})

        summary = _assemble_summary(
            name, onto_row, parent_type, entity_count, records, attr_defs,
            index_flags=flags,
        )
        summary["attributes"], attrs_omitted = _sorted_slots(
            summary["attributes"], min_coverage
        )
        summary["relationships"], rels_omitted = _sorted_slots(
            summary["relationships"], min_coverage
        )
        summary["populated"] = entity_count > 0
        summary["declared_only"] = s_uri is None
        summary["attributes_withheld"] = attrs_omitted
        summary["relationships_withheld"] = rels_omitted
        assembled.append(summary)

    # Drop zero-instance types only on explicit request, and never when the
    # caller named the types it wants.
    if not include_empty and not wanted:
        assembled = [t for t in assembled if t["entity_count"] > 0]

    assembled.sort(key=lambda t: (-t["entity_count"], t["name"]))
    kept = assembled[:limit]
    return {
        "kg": kg_name,
        "types": kept,
        "total_types": len(assembled),
        "truncated": len(assembled) > len(kept),
        # Names only: cheap, and it keeps "this type EXISTS" true even when the
        # cap withholds its slots (the caller can drill in with ?type=).
        "omitted_type_names": [t["name"] for t in assembled[limit:]],
        # A `type=` filter that matched nothing answers with the names that DO
        # exist, so a typo reads as "you meant one of these" instead of the
        # "that type does not exist" conclusion this endpoint exists to prevent.
        "available_type_names": all_names if (wanted and not assembled) else [],
        "stats_source": stats_source,
        "coverage_note": _SCHEMA_COVERAGE_NOTE,
    }


async def get_type_edges(
    kg_name: str,
    tenant: TenantContext = Depends(get_tenant),
    client: Any = Depends(get_neptune_client),
):
    """Undirected type→type edges for the Explorer overview graph.

    Derived from instance data (the precomputed stats graph, with a live-scan
    fallback) rather than the ontology's declared ``rdfs:range``. This keeps the
    overview consistent with the per-type detail view: a relationship that
    exists in the data but whose ontology range was never upgraded to a type
    URI (e.g. a predicate first seen as a primitive attribute) is now drawn in
    both places. Returns ``[{source, target, weight}]``.

    ADR 0004 (flag ``INFONA_DRIFT_CONTROL``): when ON, the stats read also
    respects the support floor — a low-support drift edge (e.g.
    ``ManufacturerPartNumber.issuedby -> Retailer`` at 6% coverage) is excluded
    from the overview, while high-coverage and core-slot edges are kept. With
    the flag OFF the read is byte-identical to before (no filtering).
    """
    from fastapi import HTTPException

    from infona_client.graph.store import GraphConfigError

    try:
        pairs = await _type_edges_from_graph_store(
            tenant_id=tenant.tenant_id, kg_name=kg_name
        )
        # Same dual-arm rule as /schema: real retired SPARQL client always GraphStore;
        # empty Memory + AsyncMock may fall through to SPARQL edge stats tests.
        if pairs or _is_sparql_client_type(client):
            return _dedupe_undirected(pairs)
    except GraphConfigError as exc:
        if _is_sparql_client_type(client) and not getattr(client, "_allow_http", False):
            raise HTTPException(
                status_code=503,
                detail=(
                    "Graph store is not configured. Neo4j GraphStore is required "
                    f"(ONTA-534). {exc}"
                ),
            ) from exc
        pass

    # ACT only when enabled AND not observe-only. Observe-only collects the
    # coverage distribution (via the recompute drift report) without touching the
    # overview, so the floor can be set from real data before it filters anything.
    drift_on = drift_control.drift_control_enabled() and not drift_control.observe_only()
    if drift_on:
        edges = await _read_edges_from_stats_drift(client, tenant.tenant_id, kg_name)
    else:
        edges = await _read_edges_from_stats(client, tenant.tenant_id, kg_name)
    if edges is None:
        # No materialized stats graph (legacy KG). The live scan must honor the
        # drift floor too when ACTING, else below-floor drift edges leak into the
        # overview for un-materialized KGs. Observe-only / flag OFF: unchanged scan.
        kg_graph = kg_graph_uri(tenant.tenant_id, kg_name)
        if drift_on:
            edges = await _live_edge_scan_drift(client, kg_graph, tenant.tenant_id)
        else:
            edges = await _live_edge_scan(client, kg_graph)
    return _dedupe_undirected(edges)


async def recompute_stats(
    kg_name: str,
    tenant: TenantContext = Depends(require_tenant_write),
    client: Any = Depends(get_neptune_client),
):
    """Schedule a recompute of the precomputed type-stats for a KG.

    Returns immediately; the ~15s whole-KG scan runs in the background so it
    never hits the ALB response timeout.

    Mutating: it rewrites the per-KG stats graph (and schedules a whole-KG
    scan), so ``require_tenant_write`` refuses a ``reader`` member with 403
    (ONTA-451). Being allowlisted in the write-path convergence guard — it IS
    the stats action rather than a writer of instance data — says nothing about
    who may trigger it.
    """
    _host().schedule_recompute(client, tenant.tenant_id, kg_name)
    return {"status": "scheduled", "kg": kg_name}


async def get_drift_history(
    kg_name: str,
    limit: int = Query(100, ge=1, le=1000),
    tenant: TenantContext = Depends(get_tenant),
    client: Any = Depends(get_neptune_client),
):
    """Read the accumulated observe-only drift distribution for a KG (COG-57).

    Returns the persisted recompute snapshots (newest first), each with the run's
    effective floors, kept/quarantined totals, and the full per-relationship
    coverage distribution. This is the durable, queryable replacement for
    log-scraping CloudWatch — the data ADR 0004 sets ``INFONA_DRIFT_FLOOR_COV``
    from. Raw distribution access only; histogram/floor analysis is done offline.
    """
    hist = _drift_history_graph_uri(tenant.tenant_id, kg_name)
    q = (
        f"SELECT ?snap ?recordedAt ?floorCov ?floorCount ?kept ?quarantined "
        f"?key ?coverage ?support ?sourceCount ?isCore ?pointKept\n"
        f"FROM <{hist}> WHERE {{\n"
        f"  ?snap <{_DRIFT_RECORDED_AT}> ?recordedAt ;\n"
        f"        <{_DRIFT_FLOOR_COV}> ?floorCov ;\n"
        f"        <{_DRIFT_FLOOR_COUNT}> ?floorCount ;\n"
        f"        <{_DRIFT_KEPT}> ?kept ;\n"
        f"        <{_DRIFT_QUARANTINED}> ?quarantined .\n"
        f"  OPTIONAL {{\n"
        f"    ?pt <{_DRIFT_POINT_OF}> ?snap ;\n"
        f"        <{_DRIFT_KEY}> ?key ;\n"
        f"        <{_DRIFT_COVERAGE}> ?coverage ;\n"
        f"        <{_DRIFT_SUPPORT}> ?support ;\n"
        f"        <{_DRIFT_SOURCE_COUNT}> ?sourceCount ;\n"
        f"        <{_DRIFT_IS_CORE}> ?isCore ;\n"
        f"        <{_DRIFT_POINT_KEPT}> ?pointKept .\n"
        f"  }}\n"
        f"}} ORDER BY DESC(?recordedAt)"
    )
    try:
        _, rows = parse_sparql_results(await client.query(q))
    except Exception:
        logger.warning("drift_history_read_failed", tenant=tenant.tenant_id, kg=kg_name, exc_info=True)
        return {"kg": kg_name, "snapshots": []}

    # Reassemble flat (snapshot × point) rows into nested snapshots, preserving
    # the recordedAt-desc order. A snapshot with no points (empty distribution)
    # still appears, with coverages == [].
    snapshots: dict[str, dict] = {}
    for r in rows:
        sid = r.get("snap", "")
        if not sid:
            continue
        snap = snapshots.get(sid)
        if snap is None:
            snap = {
                "recorded_at": r.get("recordedAt", ""),
                "floor_cov": _to_float(r.get("floorCov")),
                "floor_count": _to_int(r.get("floorCount")),
                "kept": _to_int(r.get("kept")),
                "quarantined": _to_int(r.get("quarantined")),
                "coverages": [],
            }
            snapshots[sid] = snap
        if r.get("key"):
            snap["coverages"].append({
                "key": r["key"],
                "coverage": _to_float(r.get("coverage")),
                "support": _to_int(r.get("support")),
                "source_count": _to_int(r.get("sourceCount")),
                "is_core_slot": r.get("isCore") == "true",
                "kept": r.get("pointKept") == "true",
            })
    return {"kg": kg_name, "snapshots": list(snapshots.values())[:limit]}



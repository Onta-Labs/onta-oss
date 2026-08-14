"""Stats recompute, drift persist, drop, and fire-and-forget schedule.

``schedule_recompute`` evicts ``_summary_cache`` synchronously, then
coalesces the background scan per (tenant, kg). Production Neo4j skips
the SPARQL rewrite; eviction is the product-visible recompute.

Writes stay off this module — ``refresh_after_write`` is the housekeeping
hook callers already use.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from infona_client.api.routes.explore_common import (
    RDF_TYPE,
    _CORE_SLOT_PRED,
    _DRIFT_COVERAGE,
    _DRIFT_FLOOR_COUNT,
    _DRIFT_FLOOR_COV,
    _DRIFT_IS_CORE,
    _DRIFT_KEPT,
    _DRIFT_KEY,
    _DRIFT_KG,
    _DRIFT_NS,
    _DRIFT_POINT_KEPT,
    _DRIFT_POINT_OF,
    _DRIFT_QUARANTINED,
    _DRIFT_RECORDED_AT,
    _DRIFT_SOURCE_COUNT,
    _DRIFT_SUPPORT,
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
    _STATS_NS,
    _XSD,
    _IndexFlagAccumulator,
    _drift_history_graph_uri,
    _is_core_slot,
    _retired_sparql_client,
    _stat_node,
    _stats_graph_uri,
    _esc,
    _target_from_entity_uri,
    _typed,
    logger,
)
from infona_client.api.routes.explore_resolve import invalidate_summary_cache
from infona_client.graph.iri import ENTITY_URI_PREFIX, TYPE_URI_PREFIX
from infona_client.graph.parser import parse_sparql_results
from infona_client.graph.predicates import companion_leaves as _companion_leaves
from infona_client.graph.predicates import is_internal_predicate as _is_internal_predicate
from infona_client.graph.queries import kg_graph_uri, tenant_graph_uri
from infona_client.resolver import drift_control


async def recompute_kg_stats(client: Any, tenant_id: str, kg_name: str) -> dict:
    """Recompute the stats graph for a KG in one whole-KG scan.

    Run at ingest time (or via the recompute endpoint / backfill). Replaces the
    KG's stats graph atomically and busts the in-memory cache for its types.

    Cache eviction is FIRST and unconditional. The Explorer type-summary
    endpoint is GraphStore-live on Neo4j; a retired SPARQL scan must not
    leave a 30-minute stale ``fieldStats`` sitting in ``_summary_cache``.
    """
    invalidate_summary_cache(tenant_id, kg_name)

    # Production GraphStore: type_summary_pg already reads Entity props live.
    # The SPARQL stats-graph rewrite is leftover Neptune. Skip it so we never
    # raise SparqlClientRetired after a successful enrich write.
    if _retired_sparql_client(client):
        return {"kg": kg_name, "backend": "graphstore", "cache_evicted": True}

    kg = kg_graph_uri(tenant_id, kg_name)
    stats = _stats_graph_uri(tenant_id, kg_name)
    scan = (
        f"SELECT ?type ?p (COUNT(DISTINCT ?e) AS ?cnt) (SAMPLE(?o) AS ?sample)\n"
        f'  (SUM(IF(STRSTARTS(STR(?o), "{ENTITY_URI_PREFIX}"), 1, 0)) AS ?rel)\n'
        f"{_ST_FLAG_AGGREGATES}"
        f"FROM <{kg}> WHERE {{\n"
        f"  ?e <{RDF_TYPE}> ?type .\n"
        f"  ?e ?p ?o .\n"
        f'  FILTER(STRSTARTS(STR(?type), "{TYPE_URI_PREFIX}"))\n'
        f"{_PRIMARY_TYPE_GUARD}"
        f"}} GROUP BY ?type ?p"
    )
    _, rows = parse_sparql_results(await client.query(scan))

    entity_counts: dict[str, int] = {}
    total_edges = 0  # sum of entity-valued-object totals across predicate rows
    triples: list[str] = []
    # ADR 0004: raw type-level relationship declarations (type_uri, pred_uri,
    # support). Source counts are resolved AFTER the loop, once entity_counts is
    # fully populated (rows are grouped by type+pred, so a type's rdf:type count
    # row may come after its predicate rows). Only collected when the flag is ON.
    drift_enabled = drift_control.drift_control_enabled()
    rel_decls: list[tuple[str, str, int]] = []
    flag_accs: dict[str, _IndexFlagAccumulator] = {}
    # LEGACY companion classification (ONTA-262): pre-pass building each type's
    # literal-predicate leaf set so the loop below can skip per-attribute
    # provenance companions written on the old ATTRIBUTE namespace
    # (`attrs/<attr>_<suffix>` → instance predicate `onto/<attr>_<suffix>`),
    # which URI-shape filtering can't catch. Only literal-valued rows (rel == 0)
    # participate, so a real relationship is never misclassified. Keeps freshly
    # recomputed stats clean of legacy companions the same way the internal-
    # predicate filter does (assembly filters too, as a backstop).
    type_pred_leaves: dict[str, set[str]] = {}
    for r in rows:
        p = r.get("p", "")
        if not p or p == RDF_TYPE:
            continue
        try:
            r_rel = int(r.get("rel", "0"))
        except ValueError:
            r_rel = 0
        if r_rel > 0:
            continue
        type_pred_leaves.setdefault(r.get("type", ""), set()).add(
            p.rstrip("/").split("/")[-1]
        )
    legacy_companions_by_type = {
        t: _companion_leaves(leaves) for t, leaves in type_pred_leaves.items()
    }
    for r in rows:
        type_uri_str = r.get("type", "")
        leaf = type_uri_str[len(TYPE_URI_PREFIX):] if type_uri_str.startswith(TYPE_URI_PREFIX) else ""
        if not leaf or "/" in leaf:  # skip nested URIs like .../attrs/x
            continue
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
            entity_counts[type_uri_str] = cnt
            continue
        # Don't materialize stats for internal/housekeeping predicates — they
        # must never reach the Explorer's Attributes/Relationships panel
        # (onto/batch_id, er/blockKey, er/erSignal_*, rdf*/rdfs*). Filtering here
        # keeps freshly recomputed stats clean (assembly filters too, as a
        # backstop for already-materialized stats). A predicate with entity-valued
        # objects (rel > 0) is a relationship, exempt from the literal-only
        # housekeeping markers (FIX 2) so a real `onto/source` edge is materialized.
        if _is_internal_predicate(p_uri, is_relationship=rel > 0):
            continue
        if rel == 0 and p_uri.rstrip("/").split("/")[-1] in legacy_companions_by_type.get(
            type_uri_str, ()
        ):
            continue
        # Per-type spatio-temporal index flags, from the same scan rows.
        try:
            geo = int(r.get("geo", "0") or "0")
        except ValueError:
            geo = 0
        try:
            tmp = int(r.get("tmp", "0") or "0")
        except ValueError:
            tmp = 0
        if geo or tmp:
            flag_accs.setdefault(type_uri_str, _IndexFlagAccumulator()).add(p_uri, geo, tmp)
        total_edges += rel
        node = _stat_node(type_uri_str, p_uri)
        stat = (
            f"<{node}> <{_STAT_FOR_TYPE}> <{type_uri_str}> ; "
            f"<{_STAT_FOR_PRED}> <{p_uri}> ; "
            f"<{_STAT_CNT}> {cnt} ; <{_STAT_REL}> {rel}"
        )
        target = _target_from_entity_uri(r.get("sample", ""))
        if target:
            stat += f" ; <{_STAT_TARGET}> <{TYPE_URI_PREFIX}{target}>"
        triples.append(stat + " .")
        # A type-level relationship is a predicate carrying entity-valued
        # objects (rel > 0). Those are the declarations the drift floor gates.
        if drift_enabled and rel > 0:
            rel_decls.append((type_uri_str, p_uri, rel))
    pred_row_count = len(triples)
    for type_uri_str, n in entity_counts.items():
        triples.append(f"<{type_uri_str}> <{_STAT_ENTITY_COUNT}> {n} .")
    # Materialize the per-type index markers (only when true — absence = false).
    for type_uri_str, facc in flag_accs.items():
        if facc.spatial:
            triples.append(f"<{type_uri_str}> <{_STAT_SPATIAL}> true .")
        if facc.temporal:
            triples.append(f"<{type_uri_str}> <{_STAT_TEMPORAL}> true .")

    if triples:
        body = "\n".join(triples)
        update = (
            f"DROP SILENT GRAPH <{stats}> ;\n"
            f"INSERT DATA {{ GRAPH <{stats}> {{\n{body}\n}} }}"
        )
    else:
        update = f"DROP SILENT GRAPH <{stats}>"
    await client.update(update)

    invalidate_summary_cache(tenant_id, kg_name)

    # Ingest changed the data → the KG's stored triple count is stale. Drop it
    # so the next `list_kgs` recomputes (and re-stores) it once. Local import
    # avoids an import cycle between this module and knowledge_graphs.
    from infona_client.api.routes.knowledge_graphs import invalidate_triple_count
    await invalidate_triple_count(client, tenant_id, kg_name)

    # Materialize the per-KG dashboard summary (entity/edge totals + per-type
    # breakdown) into the durable store, so the dashboard reads a tiny relational
    # row instead of querying Neptune. This rides the one shared write/refresh
    # path — it is not a second computation. Best-effort: a store hiccup must not
    # fail the recompute (reads fall back to lazy backfill from the stats graph).
    type_breakdown = {
        (t[len(TYPE_URI_PREFIX):] if t.startswith(TYPE_URI_PREFIX) else t): n
        for t, n in entity_counts.items()
    }
    try:
        from infona_client.graph.kg_stats_store import KgStats, get_kg_stats_store
        from infona_client.graph.kg_summary import resolve_summary

        store = get_kg_stats_store()
        prev = await store.get(tenant_id, kg_name)
        # Regenerate the one-line summary only when the type SET changed since it
        # was last generated (or there's none yet) — an enrichment write that
        # just fills attributes on existing types keeps the existing line instead
        # of paying for an LLM call. A transient miss keeps the old line and
        # leaves the signature stale so the next recompute retries.
        ai_description, ai_description_types = await resolve_summary(
            prev.ai_description if prev else "",
            prev.ai_description_types if prev else [],
            type_breakdown,
            kg_name,
        )

        await store.upsert(
            KgStats(
                tenant_id=tenant_id,
                kg_name=kg_name,
                entity_count=sum(entity_counts.values()),
                edge_count=total_edges,
                type_breakdown=type_breakdown,
                ai_description=ai_description,
                ai_description_types=ai_description_types,
            )
        )
    except Exception:  # noqa: BLE001 — store write is best-effort
        logger.warning("kg_stats_store_upsert_failed", kg=kg_name, exc_info=True)

    result = {"types": len(entity_counts), "predicate_rows": pred_row_count}
    if drift_enabled:
        result["drift"] = await _build_drift_report(
            client, tenant_id, kg_name, rel_decls, entity_counts
        )
    return result


async def _build_drift_report(
    client: Any,
    tenant_id: str,
    kg_name: str,
    rel_decls: list[tuple[str, str, int]],
    entity_counts: dict[str, int],
) -> dict:
    """Build + log the ADR 0004 drift report for a recompute pass (flag ON only).

    Resolves each raw relationship declaration into the ``{key, support,
    source_count, is_core_slot}`` shape ``drift_control.drift_report`` consumes:
    source_count is the source type's entity count; ``is_core_slot`` reads the
    ``onto/coreSlot`` marker (keyed by predicate URI) from the ontology graph.
    The report (effective floors + kept/quarantined split) is returned and
    logged so the drift dashboard / tenant changelog can read it.
    """
    onto = tenant_graph_uri(tenant_id)
    core_q = (
        f"SELECT DISTINCT ?attr FROM <{onto}> WHERE {{\n"
        f"  ?attr <{_CORE_SLOT_PRED}> ?v .\n"
        f"}}"
    )
    _, core_rows = parse_sparql_results(await client.query(core_q))
    core_slots = {r.get("attr", "") for r in core_rows if r.get("attr")}

    declarations: list[dict] = []
    for type_uri_str, pred_uri, support in rel_decls:
        type_leaf = type_uri_str[len(TYPE_URI_PREFIX):] if type_uri_str.startswith(TYPE_URI_PREFIX) else type_uri_str
        pred_leaf = pred_uri.rstrip("/").split("/")[-1]
        declarations.append({
            "key": f"{type_leaf}.{pred_leaf}",
            "support": support,
            "source_count": entity_counts.get(type_uri_str, 0),
            # pred_uri is …/onto/<pred>; core_slots holds ontology attr URIs, so
            # match on attr_uri(type_leaf, pred_leaf), not the raw predicate URI.
            "is_core_slot": _is_core_slot(type_leaf, pred_leaf, core_slots),
        })
    report = drift_control.drift_report(declarations)
    logger.info(
        "drift_report",
        tenant=tenant_id,
        kg=kg_name,
        observe_only=drift_control.observe_only(),
        floor_cov=report["floor_cov"],
        floor_count=report["floor_count"],
        kept=report["kept"],
        quarantined=report["quarantined"],
        quarantine=report["quarantine"],
        # Full per-relationship coverage distribution — the observe-only signal
        # the floor should ultimately be set from (not the hand-tuned 20%).
        coverages=report["coverages"],
    )
    # COG-57: also persist the distribution to a durable, queryable store so it
    # survives past CloudWatch's 30-day log retention. Best-effort — a history
    # write must never fail a recompute (it is observability, not correctness).
    await _persist_drift_history(client, tenant_id, kg_name, report)
    return report


async def _persist_drift_history(
    client: Any, tenant_id: str, kg_name: str, report: dict
) -> None:
    """Append one drift-report snapshot to the per-KG drift-history graph (COG-57).

    Writes the run's effective floors + kept/quarantined totals as a snapshot
    node, plus one point node per relationship in ``report["coverages"]`` (the
    full distribution, kept and quarantined alike). APPEND-only — never DROPs the
    graph — so the distribution accumulates across recomputes and tenants/KGs,
    which is the data ADR 0004 needs to set ``INFONA_DRIFT_FLOOR_COV`` from a real
    histogram instead of the hand-calibrated 20%.

    Wrapped in try/except: persistence is observability, so a Neptune write
    failure here is logged and swallowed rather than failing the recompute.
    """
    hist = _drift_history_graph_uri(tenant_id, kg_name)
    snap = f"{_DRIFT_NS}snap/{uuid.uuid4().hex}"
    recorded_at = datetime.now(timezone.utc).isoformat()

    triples = [
        f"<{snap}> <{_DRIFT_RECORDED_AT}> {_typed(recorded_at, 'dateTime')} ; "
        f'<{_DRIFT_KG}> "{_esc(kg_name)}" ; '
        f"<{_DRIFT_FLOOR_COV}> {_typed(report['floor_cov'], 'decimal')} ; "
        f"<{_DRIFT_FLOOR_COUNT}> {_typed(report['floor_count'], 'integer')} ; "
        f"<{_DRIFT_KEPT}> {_typed(report['kept'], 'integer')} ; "
        f"<{_DRIFT_QUARANTINED}> {_typed(report['quarantined'], 'integer')} ."
    ]
    for c in report.get("coverages", []):
        pt = f"{snap}/p/{hashlib.md5(c['key'].encode()).hexdigest()}"
        triples.append(
            f"<{pt}> <{_DRIFT_POINT_OF}> <{snap}> ; "
            f'<{_DRIFT_KEY}> "{_esc(c["key"])}" ; '
            f"<{_DRIFT_COVERAGE}> {_typed(c['coverage'], 'decimal')} ; "
            f"<{_DRIFT_SUPPORT}> {_typed(c['support'], 'integer')} ; "
            f"<{_DRIFT_SOURCE_COUNT}> {_typed(c['source_count'], 'integer')} ; "
            f"<{_DRIFT_IS_CORE}> {_typed(str(bool(c['is_core_slot'])).lower(), 'boolean')} ; "
            f"<{_DRIFT_POINT_KEPT}> {_typed(str(bool(c['kept'])).lower(), 'boolean')} ."
        )

    body = "\n".join(triples)
    try:
        await client.update(f"INSERT DATA {{ GRAPH <{hist}> {{\n{body}\n}} }}")
    except Exception:
        logger.warning("drift_history_persist_failed", tenant=tenant_id, kg=kg_name, exc_info=True)

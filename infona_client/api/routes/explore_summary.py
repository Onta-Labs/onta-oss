"""Type-summary and KG dashboard-summary Explorer reads."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import Depends

from infona_client.api.deps import get_neptune_client
from infona_client.api.routes.explore_common import (
    RDF_PROPERTY,
    RDF_TYPE,
    RDFS,
    _STAT_ENTITY_COUNT,
    _STAT_REL,
    _SUMMARY_TTL_SECONDS,
    _assemble_summary,
    _retired_sparql_client,
    _stats_graph_uri,
)
from infona_client.api.routes.explore_resolve import _resolve_layered_type
from infona_client.api.routes.explore_scan import _live_scan, _read_type_stats
from infona_client.api.routes.explore_state import _summary_cache
from infona_client.auth.api_keys import TenantContext, get_tenant
from infona_client.graph.iri import TYPE_URI_PREFIX
from infona_client.graph.ontology_queries import type_uri
from infona_client.graph.parser import parse_sparql_results
from infona_client.graph.queries import kg_graph_uri, require_valid_type_name, tenant_graph_uri


async def get_type_summary(
    kg_name: str,
    type_name: str,
    tenant: TenantContext = Depends(get_tenant),
    client: Any = Depends(get_neptune_client),
):
    """Bundle all Explorer panel data for one type in one call.

    **GraphStore / Neo4j (P-A1a):** instance inventory via
    :func:`infona_client.graph.explore_store.type_summary` — same
    ``INSTANCE_OF`` count path as type-counts so ``vis`` overview and
    ``vis <Type>`` drill-in agree. Returns 404 only when the type is neither
    declared in the tenant ontology nor has instances in this KG.

    **Legacy SPARQL (Neptune):** serves from precomputed stats (fast); falls
    back to a live scan if stats for this type are not yet materialized. All
    percentages are relative to entity_count.

    A ``type_name`` that cannot sit inside an IRI is a 422 (ONTA-425), rejected
    here rather than three store round trips later, so the caller is told what is
    wrong instead of getting a 500 out of the store's parser.
    """
    from fastapi import HTTPException

    from infona_client.graph.explore_store import type_summary as pg_type_summary
    from infona_client.graph.store import GraphConfigError

    require_valid_type_name(type_name)
    cache_key = (tenant.tenant_id, kg_name, type_name)
    cached = _summary_cache.get(cache_key)
    if cached is not None and (time.monotonic() - cached[0]) < _SUMMARY_TTL_SECONDS:
        return cached[1]

    # GraphStore path (Neo4j / Memory) — P-A1a vis drill-in.
    try:
        pg_row = await pg_type_summary(
            tenant_id=tenant.tenant_id,
            kg_name=kg_name,
            type_name=type_name,
        )
        # Store path answered: None → unknown type (no instances + not declared).
        if pg_row is None:
            raise HTTPException(
                status_code=404,
                detail=f"Type '{type_name}' not found in KG '{kg_name}'",
            )
        result = pg_row.as_api_dict()
        from infona_client.blueprint.sample_mark import (
            sample_index_for_kg,
            stamp_type_summary,
        )

        index = await sample_index_for_kg(tenant.tenant_id, kg_name)
        stamp_type_summary(result, type_name, index)
        _summary_cache[cache_key] = (time.monotonic(), result)
        return result
    except GraphConfigError as exc:
        # ONTA-534: a *real* retired SPARQL client must not fall through to dead SPARQL
        # HTTP (120s hang). ``type is`` not ``isinstance`` — AsyncMock(spec= the SPARQL client) fools isinstance. Duck-typed doubles keep the dual arm.
        if _retired_sparql_client(client):
            raise HTTPException(
                status_code=503,
                detail=(
                    "Graph store is not configured. Neo4j GraphStore is required "
                    f"(ONTA-534). {exc}"
                ),
            ) from exc
        # Residual SPARQL dual-arm (hermetic unit tests / QC archaeology).
        pass

    # Layered resolve (ONTA-397): Public/Enhanced types are visible when the
    # tenant graph is empty. Instance counts still use the tenant-namespace
    # type URI for historical data, plus the winning layer URI.
    kg_graph = kg_graph_uri(tenant.tenant_id, kg_name)
    resolved = await _resolve_layered_type(client, tenant, type_name)
    if resolved is not None:
        t_uri, onto_graph, _layer = resolved
    else:
        # No layered declaration — fall back to tenant URI so live-scan of
        # instance-only types (no schema yet) still works.
        onto_graph = tenant_graph_uri(tenant.tenant_id)
        t_uri = type_uri(type_name)

    onto_sparql = (
        f"SELECT ?label ?comment ?parent FROM <{onto_graph}> WHERE {{\n"
        f"  <{t_uri}> <{RDFS}#label> ?label .\n"
        f"  OPTIONAL {{ <{t_uri}> <{RDFS}#comment> ?comment }}\n"
        f"  OPTIONAL {{ <{t_uri}> <{RDFS}#subClassOf> ?parent }}\n"
        f"}}"
    )
    attr_def_sparql = (
        f"SELECT ?attr ?attrLabel ?range FROM <{onto_graph}> WHERE {{\n"
        f"  ?attr <{RDF_TYPE}> <{RDF_PROPERTY}> .\n"
        f"  ?attr <{RDFS}#domain> <{t_uri}> .\n"
        f"  ?attr <{RDFS}#label> ?attrLabel .\n"
        f"  OPTIONAL {{ ?attr <{RDFS}#range> ?range }}\n"
        f"}}"
    )

    # Ontology lookups (tiny) + precomputed stats, all concurrent.
    onto_raw, attr_def_raw, stats = await asyncio.gather(
        client.query(onto_sparql),
        client.query(attr_def_sparql),
        _read_type_stats(client, tenant.tenant_id, kg_name, t_uri),
    )

    _, onto_rows = parse_sparql_results(onto_raw)
    onto_row = onto_rows[0] if onto_rows else {}
    parent_uri = onto_row.get("parent", "")
    parent_type = parent_uri.rstrip("/").split("/")[-1] if parent_uri else None

    _, attr_def_rows = parse_sparql_results(attr_def_raw)
    attr_defs: dict[str, dict[str, str]] = {
        r["attr"]: {"name": r.get("attrLabel", ""), "range": r.get("range", "")}
        for r in attr_def_rows
        if r.get("attr")
    }

    if stats is not None:
        entity_count, pred_records, index_flags = stats
    else:
        # Stats not materialized for this type — fall back to a live scan.
        entity_count, pred_records, index_flags = await _live_scan(client, kg_graph, t_uri)

    result = _assemble_summary(
        type_name, onto_row, parent_type, entity_count, pred_records, attr_defs,
        index_flags=index_flags,
    )
    from infona_client.blueprint.sample_mark import (
        sample_index_for_kg,
        stamp_type_summary,
    )

    stamp_type_summary(
        result, type_name, await sample_index_for_kg(tenant.tenant_id, kg_name)
    )
    _summary_cache[cache_key] = (time.monotonic(), result)
    return result

async def read_kg_summary_from_stats(
    client: Any, tenant_id: str, kg_name: str
):
    """Aggregate a KG's per-type stats graph into KG-level totals.

    Reads the *precomputed* stats graph (tiny — one row per type / per
    type-predicate), NOT the full instance graph, so this is cheap. Returns
    ``(entity_total, edge_total, {type_leaf: count})`` or ``None`` when the KG
    has no materialized entity counts yet (e.g. a KG that predates the stats
    graph and has never been recomputed) — the caller should then schedule a
    recompute. This is the source the dashboard-summary store is backfilled from.
    """
    stats = _stats_graph_uri(tenant_id, kg_name)
    ec_q = f"SELECT ?t ?ec FROM <{stats}> WHERE {{ ?t <{_STAT_ENTITY_COUNT}> ?ec }}"
    _, ec_rows = parse_sparql_results(await client.query(ec_q))
    if not ec_rows:
        return None
    breakdown: dict[str, int] = {}
    entity_total = 0
    for r in ec_rows:
        t = r.get("t", "")
        leaf = t[len(TYPE_URI_PREFIX):] if t.startswith(TYPE_URI_PREFIX) else t
        try:
            n = int(r.get("ec", "0"))
        except (ValueError, TypeError):
            n = 0
        breakdown[leaf] = n
        entity_total += n
    edge_q = f"SELECT (SUM(?rel) AS ?total) FROM <{stats}> WHERE {{ ?s <{_STAT_REL}> ?rel }}"
    _, edge_rows = parse_sparql_results(await client.query(edge_q))
    try:
        edge_total = int(edge_rows[0].get("total", "0")) if edge_rows else 0
    except (ValueError, TypeError):
        edge_total = 0
    return entity_total, edge_total, breakdown


async def backfill_kg_summary(
    client: Any, tenant_id: str, kg_name: str, *, persist: bool = True
):
    """Seed the durable dashboard-summary store for one KG from existing stats.

    Used to lazily fill rows for KGs that predate the store (the first time
    they're listed) without a fresh whole-KG scan. Returns the upserted
    :class:`KgStats`, or ``None`` if the KG's stats graph isn't materialized yet
    (the caller schedules a recompute, which will populate the store directly).

    ``persist=False`` computes the SAME row and returns it WITHOUT storing it
    (ONTA-452). A read-only member gets identical numbers on the listing while
    the lazy materialization stays a write only writers perform.
    """
    agg = await read_kg_summary_from_stats(client, tenant_id, kg_name)
    if agg is None:
        return None
    entity_total, edge_total, breakdown = agg
    from infona_client.graph.kg_stats_store import KgStats, get_kg_stats_store

    row = KgStats(
        tenant_id=tenant_id,
        kg_name=kg_name,
        entity_count=entity_total,
        edge_count=edge_total,
        type_breakdown=breakdown,
    )
    if not persist:
        return row
    await get_kg_stats_store().upsert(row)
    return row


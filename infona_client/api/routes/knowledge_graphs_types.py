"""Browsing: type counts and per-type attribute usage within a KG."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from infona_client.api.deps import get_neptune_client
from infona_client.api.routes.knowledge_graphs_common import RDF_TYPE
from infona_client.auth.api_keys import TenantContext, get_tenant
from infona_client.graph.iri import IRI_BASE, TYPE_URI_PREFIX
from infona_client.graph.parser import parse_sparql_results
from infona_client.graph.queries import kg_graph_uri


# Predicates the resolver attaches to every entity at ingest time.
# Always present, always 100%, drown out the actual columns the user
# cares about — hidden from /type usage by default, opt-in via
# ?include_system=true. Sourced from schema_resolver.py.
SYSTEM_PREDICATES: frozenset[str] = frozenset({
    "http://www.w3.org/2000/01/rdf-schema#label",
    f"{IRI_BASE}/onto/ingested_at",
    f"{IRI_BASE}/onto/source",
})


class TypeCount(BaseModel):
    name: str
    entity_count: int
    # Spatio-temporal index markers, read from the precomputed stats graph
    # (recompute_kg_stats materializes them; absence = False). Spatial = the
    # type's instances carry geo:wktLiteral geometry; temporal = they carry
    # validity bounds or a complete start+end date pair.
    spatially_indexed: bool = False
    temporally_indexed: bool = False
    # INF-591: sample rows counted separately so Explorer never blends them
    # into a single "current" number. ``sample_is_current`` is omitted.
    sample_count: int = 0
    acquired_count: int = 0


class AttributeUsage(BaseModel):
    name: str
    datatype: str = "string"
    count: int


class RelationshipUsage(BaseModel):
    name: str
    target_type: str | None = None
    count: int


class EntitySample(BaseModel):
    uri: str
    label: str = ""


class TypeUsage(BaseModel):
    name: str
    description: str = ""
    parent_type: str | None = None
    entity_count: int
    attributes: list[AttributeUsage] = []
    relationships: list[RelationshipUsage] = []
    samples: list[EntitySample] = []


async def list_type_counts(
    kg_name: str,
    tenant: TenantContext = Depends(get_tenant),
    client: Any = Depends(get_neptune_client),
):
    """List every type that has instances in this KG, sorted by entity count.

    Tenant-global ontology types with zero instances in this KG are not
    returned here — fetch them via /ontology/types if the caller needs the
    full schema.

    **Dual-backend (E5):** when ``INFONA_GRAPH_BACKEND=neo4j`` (or a process
    GraphStore is configured for that backend), counts come from
    :func:`infona_client.graph.explore_store.type_counts` instead of SPARQL.
    Spatio-temporal index flags are still best-effort from the stats graph
    (Neptune path only; Neo4j returns False until stats port).
    """
    # GraphStore path (E5 explore_store) — same response shape.
    from infona_client.graph.explore_store import type_counts as pg_type_counts

    pg_rows = await pg_type_counts(
        tenant_id=tenant.tenant_id, kg_name=kg_name
    )
    if pg_rows is not None:
        from infona_client.blueprint.sample_mark import sample_index_for_kg

        index = await sample_index_for_kg(tenant.tenant_id, kg_name)
        out: list[TypeCount] = []
        for r in pg_rows:
            sample_n = index.count_for_type(r.name)
            out.append(
                TypeCount(
                    name=r.name,
                    entity_count=r.entity_count,
                    spatially_indexed=False,
                    temporally_indexed=False,
                    sample_count=sample_n,
                    acquired_count=max(r.entity_count - sample_n, 0),
                )
            )
        return out

    graph = kg_graph_uri(tenant.tenant_id, kg_name)
    sparql = (
        f"SELECT ?type (COUNT(DISTINCT ?e) AS ?cnt) FROM <{graph}> WHERE {{\n"
        f"  ?e <{RDF_TYPE}> ?type .\n"
        f'  FILTER(STRSTARTS(STR(?type), "{TYPE_URI_PREFIX}"))\n'
        f"}} GROUP BY ?type ORDER BY DESC(?cnt)"
    )
    raw, index_flags = await asyncio.gather(
        client.query(sparql),
        _read_type_index_flags(client, tenant.tenant_id, kg_name),
    )
    _, bindings = parse_sparql_results(raw)
    from infona_client.blueprint.sample_mark import sample_index_for_kg

    sample_index = await sample_index_for_kg(tenant.tenant_id, kg_name)
    out: list[TypeCount] = []
    for row in bindings:
        t = row.get("type", "")
        if not t.startswith(TYPE_URI_PREFIX):
            continue
        # Skip nested URIs like .../types/{Type}/attrs/{name} which aren't types
        leaf = t[len(TYPE_URI_PREFIX):]
        if "/" in leaf:
            continue
        try:
            count = int(row.get("cnt", "0"))
        except ValueError:
            count = 0
        spatial, temporal = index_flags.get(leaf, (False, False))
        sample_n = sample_index.count_for_type(leaf)
        out.append(TypeCount(
            name=leaf,
            entity_count=count,
            spatially_indexed=spatial,
            temporally_indexed=temporal,
            sample_count=sample_n,
            acquired_count=max(count - sample_n, 0),
        ))
    return out


async def _read_type_index_flags(
    client: Any, tenant_id: str, kg_name: str
) -> dict[str, tuple[bool, bool]]:
    """Per-type (spatially_indexed, temporally_indexed) from the stats graph.

    The markers are materialized by ``recompute_kg_stats``; a KG whose stats
    were never recomputed (or whose types carry neither marker) simply yields
    no rows — every type then defaults to (False, False). Best-effort: the
    flags decorate the type list, so a stats-graph hiccup must not take down
    the endpoint that powers the Explorer rail.
    """
    # Local import: explore imports this module (locally) for the triple-count
    # invalidation hook, so a module-level import here would create a cycle.
    from infona_client.api.routes.explore import (
        _STAT_SPATIAL,
        _STAT_TEMPORAL,
        _stats_graph_uri,
    )

    stats = _stats_graph_uri(tenant_id, kg_name)
    sparql = (
        f"SELECT ?type ?sp ?tp FROM <{stats}> WHERE {{\n"
        f"  {{ ?type <{_STAT_SPATIAL}> ?sp }} UNION {{ ?type <{_STAT_TEMPORAL}> ?tp }}\n"
        f"}}"
    )
    flags: dict[str, tuple[bool, bool]] = {}
    try:
        _, rows = parse_sparql_results(await client.query(sparql))
    except Exception:  # noqa: BLE001 — decoration only, never fail the list
        return flags
    for row in rows:
        t = row.get("type", "")
        if not t.startswith(TYPE_URI_PREFIX):
            continue
        leaf = t[len(TYPE_URI_PREFIX):]
        spatial, temporal = flags.get(leaf, (False, False))
        # Accept both boolean lexical forms ("true" and "1") — see _read_type_stats.
        if row.get("sp", "") in ("true", "1"):
            spatial = True
        if row.get("tp", "") in ("true", "1"):
            temporal = True
        flags[leaf] = (spatial, temporal)
    return flags


async def get_type_usage(
    kg_name: str,
    type_name: str,
    include_system: bool = False,
    tenant: TenantContext = Depends(get_tenant),
    client: Any = Depends(get_neptune_client),
):
    """Per-type breakdown for one type in one KG.

    Combines the tenant-global ontology definition (attribute names,
    datatypes, parent type) with per-KG instance numbers (entity count,
    attribute usage, sample entities) so the caller doesn't have to make
    three round-trips and re-join the results client-side.

    **GraphStore / Neo4j (ONTA-535):** inventory via
    :func:`infona_client.graph.explore_store.type_summary` + sample entities
    from :func:`~infona_client.graph.explore_store.list_entities_by_type`.
    System/internal keys are already filtered by the summary path (same
    ``is_internal_property_key`` authority as grep/records); ``include_system``
    is a SPARQL-branch opt-in and is ignored on the store path (internals
    never surface as domain columns).
    """
    from infona_client.graph.explore_store import (
        list_entities_by_type as pg_list_entities,
        resolve_explore_session,
        type_summary as pg_type_summary,
    )
    from infona_client.graph.queries import require_valid_type_name
    from infona_client.graph.store import GraphConfigError

    require_valid_type_name(type_name)

    # GraphStore path (ONTA-535) — same TypeUsage shape as the SPARQL branch.
    # When a store is configured, None means unknown type → 404 (do not fall
    # through to SPARQL). ONTA-534: GraphConfigError → 503 (no hang).
    try:
        resolve_explore_session(tenant_id=tenant.tenant_id, kg_name=kg_name)
        pg_row = await pg_type_summary(
            tenant_id=tenant.tenant_id,
            kg_name=kg_name,
            type_name=type_name,
        )
    except GraphConfigError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "Graph store is not configured. Neo4j GraphStore is required "
                f"(ONTA-534). {exc}"
            ),
        ) from exc
    if pg_row is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Type '{type_name}' not found in tenant ontology "
                f"or KG '{kg_name}'"
            ),
        )
    del include_system  # store path never surfaces internal keys
    samples: list[EntitySample] = []
    try:
        page = await pg_list_entities(
            tenant_id=tenant.tenant_id,
            kg_name=kg_name,
            type_name=type_name,
            limit=3,
        )
        if page is not None:
            for ent in page.entities:
                samples.append(
                    EntitySample(
                        uri=ent.id,
                        label=ent.name or ent.id.rstrip("/").split("/")[-1],
                    )
                )
    except Exception:
        samples = []
    return TypeUsage(
        name=pg_row.name,
        description=pg_row.description or "",
        parent_type=pg_row.parent_type,
        entity_count=pg_row.entity_count,
        attributes=[
            AttributeUsage(
                name=a.name,
                datatype=a.datatype or "string",
                count=a.count,
            )
            for a in pg_row.attributes
        ],
        relationships=[
            RelationshipUsage(
                name=r.name,
                target_type=r.target_type,
                count=r.count,
            )
            for r in pg_row.relationships
        ],
        samples=samples,
    )


def _xsd_to_datatype(uri: str) -> str:
    if not uri:
        return "string"
    if uri.startswith(TYPE_URI_PREFIX):
        return uri[len(TYPE_URI_PREFIX):]
    last = uri.split("#")[-1] if "#" in uri else uri.split("/")[-1]
    return {
        "string": "string",
        "integer": "integer",
        "float": "float",
        "boolean": "boolean",
        "dateTime": "datetime",
        "Resource": "uri",
    }.get(last, "string")

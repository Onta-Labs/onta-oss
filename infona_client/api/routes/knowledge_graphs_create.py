"""Create a knowledge graph for a tenant."""

from __future__ import annotations

from typing import Any

from fastapi import Depends

from infona_client.analytics import distinct_id_for, emit
from infona_client.api.deps import get_neptune_client
from infona_client.api.routes.knowledge_graphs_common import (
    INFONA_ONTO,
    KG_TRIPLE_COUNT,
    KGCreate,
    KGInfo,
    _kg_meta_uri,
)
from infona_client.auth.access import require_tenant_write
from infona_client.auth.api_keys import TenantContext
from infona_client.graph.parser import parse_sparql_results
from infona_client.graph.queries import _escape_literal, tenant_graph_uri


async def create_kg(
    body: KGCreate,
    tenant: TenantContext = Depends(require_tenant_write),
    client: Any = Depends(get_neptune_client),
):
    """Create a new knowledge graph for a tenant.

    **Neo4j:** ``:KnowledgeGraph`` registry upsert (idempotent).

    **Legacy SPARQL:** guarded with ``FILTER NOT EXISTS`` so calling it twice
    never duplicates the registration triples and never clobbers an existing
    registration (or its ``kg_description``). This is the same registration the
    shared write path performs via ``ensure_kg_registered`` (ONTA-153) — here we
    additionally write the description, which only the explicit "New KG" flow
    supplies.

    On a re-POST of an existing KG the guarded INSERT no-ops; we then return the
    *existing* KGInfo (real description + triple count) rather than claiming an
    empty/zero KG, so the response never lies about a KG that may already hold
    real data.

    Safety: ``body.name`` is pattern-validated by ``KGCreate`` (``[a-zA-Z0-9_-]``)
    so it's URI-safe, but the free-text ``description`` and (defensively) the name
    are escaped via the canonical ``_escape_literal`` before going into a SPARQL
    literal — no statement-breakout on a ``"`` / ``\\`` / newline.
    """
    from infona_client.graph.kg_registry import neo4j_kg_registry_active, upsert_registered_kg

    if neo4j_kg_registry_active():
        row = await upsert_registered_kg(
            tenant.tenant_id,
            body.name,
            description=body.description or "",
            triple_count=0,
            only_if_absent=False,
        )
        emit(
            "kg_created",
            distinct_id=distinct_id_for(tenant.subject, tenant.tenant_id),
            tenant=tenant.tenant_id,
            kg=body.name,
            has_description=bool(body.description),
        )
        return KGInfo(
            name=row["name"],
            description=row.get("description") or body.description or "",
            triple_count=int(row.get("triple_count") or 0),
        )

    base = tenant_graph_uri(tenant.tenant_id)
    kg_uri = _kg_meta_uri(tenant.tenant_id, body.name)

    insert_lines = [
        f'    <{kg_uri}> <{INFONA_ONTO}/kg_name> "{_escape_literal(body.name)}" .',
        f"    <{kg_uri}> <{KG_TRIPLE_COUNT}> 0 .",
    ]
    if body.description:
        insert_lines.append(
            f'    <{kg_uri}> <{INFONA_ONTO}/kg_description> '
            f'"{_escape_literal(body.description)}" .'
        )
    insert_block = "\n".join(insert_lines)
    sparql = (
        f"WITH <{base}>\n"
        f"INSERT {{\n{insert_block}\n}}\n"
        f"WHERE {{\n"
        f"  FILTER NOT EXISTS {{ <{kg_uri}> <{INFONA_ONTO}/kg_name> ?n }}\n"
        f"}}"
    )

    await client.update(sparql)

    # Product-analytics event (ONTA-323). Fire-and-forget, no-op without a sink,
    # never raises. Attributed to the authenticated subject (Clerk user id), else
    # a stable system:<tenant> id. Emitted on a successful create POST; the route
    # is idempotent, so a re-POST of an existing KG re-emits — a bounded,
    # self-attributed signal, acceptable for a product funnel.
    emit(
        "kg_created",
        distinct_id=distinct_id_for(tenant.subject, tenant.tenant_id),
        tenant=tenant.tenant_id,
        kg=body.name,
        has_description=bool(body.description),
    )

    # The INSERT is idempotent, so a re-POST no-ops it. Read the registration
    # back and report what's actually stored: a pre-existing KG keeps its real
    # description + triple count; a freshly-created one reads back as the values
    # we just wrote (description as given, count 0).
    read = (
        f"SELECT ?desc ?count FROM <{base}> WHERE {{\n"
        f"  <{kg_uri}> <{INFONA_ONTO}/kg_name> ?n .\n"
        f"  OPTIONAL {{ <{kg_uri}> <{INFONA_ONTO}/kg_description> ?desc }}\n"
        f"  OPTIONAL {{ <{kg_uri}> <{KG_TRIPLE_COUNT}> ?count }}\n"
        f"}}"
    )
    try:
        _, rows = parse_sparql_results(await client.query(read))
    except Exception:
        rows = []
    if rows:
        row = rows[0]
        raw_count = row.get("count")
        count = int(raw_count) if raw_count not in (None, "") and raw_count.isdigit() else 0
        return KGInfo(
            name=body.name,
            description=row.get("desc", "") or "",
            triple_count=count,
        )
    # Read-back failed (e.g. Neptune hiccup) — fall back to the values we wrote.
    return KGInfo(name=body.name, description=body.description, triple_count=0)

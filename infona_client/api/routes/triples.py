"""Raw triple CRUD (Neptune) / dual-backend gate (Neo4j).

**Neptune (default):** inserts / selects / deletes on the tenant named graph
via SPARQL builders. Unchanged.

**Neo4j (``INFONA_GRAPH_BACKEND=neo4j``):** public raw triple SPO is not a
faithful surface over the Assertion / Entity model — there is no kg scope on
these routes, and free-form predicates do not map 1:1 to property keys. Return
**410 Gone** and point callers at high-level typed write paths
(``insert_facts`` via ingest / enrich / agent / KG-scoped APIs).
"""

from fastapi import APIRouter, Depends, HTTPException

from infona_client.api.deps import get_neptune_client
from infona_client.auth.api_keys import TenantContext, get_tenant
from infona_client.auth.access import require_tenant_write
from infona_client.graph.client import NeptuneClient
from infona_client.graph.kg_writer import graph_backend
from infona_client.graph.parser import parse_sparql_results
from infona_client.graph.queries import (
    delete_triples,
    insert_triples,
    select_triples,
    tenant_graph_uri,
)
from infona_client.models.triple import TripleBatch, TripleCreate, TripleDelete

router = APIRouter()

_TRIPLES_GONE_DETAIL = (
    "Raw /triples SPO is not available when INFONA_GRAPH_BACKEND=neo4j. "
    "Use high-level APIs (ingest, enrich, agent, explore, insert_facts / "
    "kg-scoped writes) instead of free-form triples."
)


def reject_raw_triples_if_neo4j() -> None:
    """Hard-break public raw triple routes under neo4j backend (E9)."""
    if graph_backend() == "neo4j":
        raise HTTPException(status_code=410, detail=_TRIPLES_GONE_DETAIL)


@router.post("/graphs/{tenant}/triples", response_model=TripleBatch)
async def create_triples(
    body: TripleCreate,
    tenant: TenantContext = Depends(require_tenant_write),
    client: NeptuneClient = Depends(get_neptune_client),
):
    reject_raw_triples_if_neo4j()
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    triple_tuples = [(t.subject, t.predicate, t.object) for t in body.triples]
    sparql = insert_triples(graph_uri, triple_tuples)
    await client.update(sparql)
    return TripleBatch(inserted=len(body.triples))


@router.get("/graphs/{tenant}/triples")
async def get_triples(
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
    subject: str | None = None,
    predicate: str | None = None,
    object: str | None = None,
    limit: int = 100,
):
    reject_raw_triples_if_neo4j()
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    sparql = select_triples(graph_uri, subject, predicate, object, limit)
    raw = await client.query(sparql)
    vars, bindings = parse_sparql_results(raw)
    return {"vars": vars, "bindings": bindings}


@router.delete("/graphs/{tenant}/triples", response_model=TripleBatch)
async def remove_triples(
    body: TripleDelete,
    tenant: TenantContext = Depends(require_tenant_write),
    client: NeptuneClient = Depends(get_neptune_client),
):
    reject_raw_triples_if_neo4j()
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    triple_tuples = [(t.subject, t.predicate, t.object) for t in body.triples]
    sparql = delete_triples(graph_uri, triple_tuples)
    await client.update(sparql)
    return TripleBatch(deleted=len(body.triples))

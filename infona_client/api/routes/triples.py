from fastapi import APIRouter, Depends

from infona_client.api.deps import get_neptune_client
from infona_client.auth.api_keys import TenantContext, get_tenant
from infona_client.auth.access import require_tenant_write
from infona_client.graph.client import NeptuneClient
from infona_client.graph.parser import parse_sparql_results
from infona_client.graph.queries import (
    delete_triples,
    insert_triples,
    select_triples,
    tenant_graph_uri,
)
from infona_client.models.triple import TripleBatch, TripleCreate, TripleDelete

router = APIRouter()


@router.post("/graphs/{tenant}/triples", response_model=TripleBatch)
async def create_triples(
    body: TripleCreate,
    tenant: TenantContext = Depends(require_tenant_write),
    client: NeptuneClient = Depends(get_neptune_client),
):
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
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    triple_tuples = [(t.subject, t.predicate, t.object) for t in body.triples]
    sparql = delete_triples(graph_uri, triple_tuples)
    await client.update(sparql)
    return TripleBatch(deleted=len(body.triples))

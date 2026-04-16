from fastapi import APIRouter, Depends

from cograph.api.deps import get_neptune_client
from cograph.auth.api_keys import TenantContext, get_tenant
from cograph.graph.client import NeptuneClient
from cograph.graph.parser import parse_sparql_results
from cograph.graph.queries import (
    delete_triples,
    insert_triples,
    select_triples,
    tenant_graph_uri,
)
from cograph.models.triple import TripleBatch, TripleCreate, TripleDelete

router = APIRouter()


@router.post("/graphs/{tenant}/triples", response_model=TripleBatch)
async def create_triples(
    body: TripleCreate,
    tenant: TenantContext = Depends(get_tenant),
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
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    triple_tuples = [(t.subject, t.predicate, t.object) for t in body.triples]
    sparql = delete_triples(graph_uri, triple_tuples)
    await client.update(sparql)
    return TripleBatch(deleted=len(body.triples))

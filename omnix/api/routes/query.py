from fastapi import APIRouter, Depends, Request

from omnix.api.deps import get_neptune_client
from omnix.api.rate_limit import limiter
from omnix.auth.api_keys import TenantContext, get_tenant
from omnix.graph.client import NeptuneClient
from omnix.graph.parser import parse_sparql_results
from omnix.models.query import SPARQLQuery, SPARQLResult, SPARQLUpdate

router = APIRouter()


@router.post("/graphs/{tenant}/query", response_model=SPARQLResult)
@limiter.limit("500/minute")
async def execute_query(
    request: Request,
    body: SPARQLQuery,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    raw = await client.query(body.query)
    vars, bindings = parse_sparql_results(raw)
    return SPARQLResult(vars=vars, bindings=bindings)


@router.post("/graphs/{tenant}/update")
@limiter.limit("500/minute")
async def execute_update(
    request: Request,
    body: SPARQLUpdate,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    await client.update(body.update)
    return {"status": "ok"}

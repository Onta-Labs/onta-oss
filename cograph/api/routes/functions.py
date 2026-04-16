from fastapi import APIRouter, Depends

from cograph.api.deps import get_neptune_client
from cograph.auth.api_keys import TenantContext, get_tenant
from cograph.graph.client import NeptuneClient
from cograph.graph.parser import parse_sparql_results
from cograph.graph.queries import (
    list_functions_query,
    register_function_triple,
    tenant_graph_uri,
)
from cograph.models.function import FunctionRef, FunctionRegister, FunctionTier

router = APIRouter()


@router.post("/graphs/{tenant}/functions", status_code=201)
async def register_function(
    body: FunctionRegister,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    sparql = register_function_triple(
        graph_uri,
        entity_type=body.entity_type,
        function_name=body.name,
        endpoint_url=body.endpoint_url,
        description=body.description,
    )
    await client.update(sparql)
    return {"registered": body.name, "entity_type": body.entity_type}


@router.get("/graphs/{tenant}/functions", response_model=list[FunctionRef])
async def list_functions(
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
    entity_type: str | None = None,
):
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    sparql = list_functions_query(graph_uri, entity_type)
    raw = await client.query(sparql)
    _, bindings = parse_sparql_results(raw)
    return [
        FunctionRef(
            name=row.get("name", ""),
            entity_type=row.get("type", "").split("/")[-1],
            endpoint_url=row.get("endpoint"),
            description=row.get("desc", ""),
            tier=FunctionTier.CUSTOM,
        )
        for row in bindings
    ]

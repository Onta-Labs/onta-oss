from fastapi import APIRouter, Depends, Request

from omnix.api.deps import get_neptune_client
from omnix.api.rate_limit import limiter
from omnix.auth.api_keys import TenantContext, get_tenant
from omnix.config import settings
from omnix.graph.client import NeptuneClient
from omnix.graph.queries import kg_graph_uri, tenant_graph_uri
from omnix.models.query import NLQuery, NLResult
from omnix.nlp.pipeline import NLQueryPipeline

router = APIRouter()


@router.post("/graphs/{tenant}/ask", response_model=NLResult)
@limiter.limit("1000/minute")
async def ask_question(
    request: Request,
    body: NLQuery,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    # Ontology always lives in the base tenant graph
    ontology_graph = tenant_graph_uri(tenant.tenant_id)
    # Instance data may be in a KG-specific graph
    instance_graph = kg_graph_uri(tenant.tenant_id, body.kg_name) if body.kg_name else ontology_graph
    pipeline = NLQueryPipeline(client, settings.anthropic_api_key)
    if body.model:
        pipeline._query_model = body.model
        # Auto-detect provider from model ID format
        if "/" in body.model:
            pipeline._query_provider = "openrouter"
        else:
            pipeline._query_provider = "cerebras"
    return await pipeline.ask(body.question, ontology_graph, instance_graph)

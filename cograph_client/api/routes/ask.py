from fastapi import APIRouter, Depends, Request

from cograph_client.api.deps import get_neptune_client
from cograph_client.api.rate_limit import limiter
from cograph_client.auth.api_keys import TenantContext, get_tenant
from cograph_client.config import settings
from cograph_client.graph.client import NeptuneClient
from cograph_client.graph.queries import kg_graph_uri, tenant_graph_uri
from cograph_client.models.query import NLQuery, NLResult
from cograph_client.nlp.pipeline import NLQueryPipeline

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
    return await pipeline.ask(body.question, ontology_graph, instance_graph, exclude_questions=body.exclude_questions)

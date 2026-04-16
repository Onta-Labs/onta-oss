from fastapi import APIRouter, Depends, Request

from cograph.api.deps import get_neptune_client
from cograph.api.rate_limit import limiter
from cograph.auth.api_keys import TenantContext, get_tenant
from cograph.config import settings
from cograph.graph.client import NeptuneClient
from cograph.graph.queries import kg_graph_uri, tenant_graph_uri
from cograph.models.query import NLQuery, NLResult
from cograph.nlp.pipeline import NLQueryPipeline

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

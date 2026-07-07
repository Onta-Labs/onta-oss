import structlog
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

logger = structlog.stdlib.get_logger("cograph.api.ask")


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
    # Route-level safety net: `pipeline.ask` already retries and degrades to a
    # graceful NLResult internally, but any UNEXPECTED failure that still
    # escapes it (e.g. an error outside the per-attempt loop) must not surface
    # as a bare HTTP 500 with no body — the /ask contract is always an NLResult.
    # Log the question + traceback at the boundary and return a 200 NLResult
    # explaining that the question couldn't be answered.
    try:
        return await pipeline.ask(
            body.question, ontology_graph, instance_graph, exclude_questions=body.exclude_questions
        )
    except Exception:
        logger.error(
            "ask_route_unhandled_error",
            question=body.question,
            kg_name=body.kg_name,
            tenant=tenant.tenant_id,
            exc_info=True,
        )
        return NLResult(
            answer=(
                "Could not answer this question due to an internal error. "
                "Please try rephrasing or narrowing the question, or try again shortly."
            ),
            sparql="",
            explanation="",
        )

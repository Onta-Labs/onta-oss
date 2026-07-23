import time

import structlog
from fastapi import APIRouter, Depends, Request

from cograph_client.analytics import distinct_id_for, emit
from cograph_client.api.deps import get_enrichment_job_store, get_neptune_client
from cograph_client.api.rate_limit import limiter
from cograph_client.auth.api_keys import TenantContext, get_tenant
from cograph_client.config import settings
from cograph_client.graph.client import NeptuneClient
from cograph_client.graph.queries import kg_graph_uri, tenant_graph_uri
from cograph_client.models.query import NLQuery, NLResult
from cograph_client.nlp.pipeline import NLQueryPipeline
from cograph_client.pipeline.answer_run import record_answer_run

router = APIRouter()

logger = structlog.stdlib.get_logger("cograph.api.ask")


@router.post("/graphs/{tenant}/ask", response_model=NLResult)
@limiter.limit("1000/minute")
async def ask_question(
    request: Request,
    body: NLQuery,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
    job_store=Depends(get_enrichment_job_store),
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
    start = time.monotonic()
    try:
        result = await pipeline.ask(
            body.question, ontology_graph, instance_graph, exclude_questions=body.exclude_questions
        )
        _emit_query_executed(tenant, body.kg_name, start, result, ok=True)
        # ONTA-389: mint answer run so operators can open Job Trace (P7 + P0/A9).
        # Documented path: response.run_id → GET /operator/jobs/{run_id}/trace.
        run_id = await record_answer_run(
            job_store=job_store,
            tenant_id=tenant.tenant_id,
            kg_name=body.kg_name or "",
            question=body.question,
            answer=result.answer,
            sparql=result.sparql or "",
            citations=list(getattr(result, "citations", None) or []),
            coverage_caveat=getattr(result, "coverage_caveat", "") or "",
            ok=True,
            timing=getattr(result, "timing", None) or {},
            source="ask",
        )
        if run_id:
            result.run_id = run_id
        return result
    except Exception:
        logger.error(
            "ask_route_unhandled_error",
            question=body.question,
            kg_name=body.kg_name,
            tenant=tenant.tenant_id,
            exc_info=True,
        )
        degraded = NLResult(
            answer=(
                "Could not answer this question due to an internal error. "
                "Please try rephrasing or narrowing the question, or try again shortly."
            ),
            sparql="",
            explanation="",
        )
        _emit_query_executed(tenant, body.kg_name, start, degraded, ok=False)
        run_id = await record_answer_run(
            job_store=job_store,
            tenant_id=tenant.tenant_id,
            kg_name=body.kg_name or "",
            question=body.question,
            answer=degraded.answer,
            sparql="",
            citations=[],
            coverage_caveat="",
            ok=False,
            error="ask_route_unhandled_error",
            source="ask",
        )
        if run_id:
            degraded.run_id = run_id
        return degraded


def _emit_query_executed(
    tenant: TenantContext,
    kg_name: str | None,
    start: float,
    result: NLResult,
    *,
    ok: bool,
) -> None:
    """Product-analytics event for an executed NL query (ONTA-323, ONTA-355).

    Fire-and-forget, no-op without a registered sink, never raises. Attributed
    to the authenticated subject (Clerk user id), else a stable system:<tenant>
    id. ``ok`` distinguishes a normal answer from the route's graceful-degrade
    path (an unexpected error that still returned a 200 NLResult).

    ONTA-355 adds cheap result-quality signal derived from the answer payload's
    existing ``timing`` metadata (no row data, no PII): ``result_count`` (rows
    the query returned) and ``returned_rows`` (did it return anything). ``mode``
    tags this as the NL (``/ask``) path so events separate cleanly from any
    future agent route in the same stream.
    """
    rows = result.timing.get("rows")
    result_count = int(rows) if isinstance(rows, (int, float)) else 0
    emit(
        "query_executed",
        distinct_id=distinct_id_for(tenant.subject, tenant.tenant_id),
        tenant=tenant.tenant_id,
        kg=kg_name or "",
        mode="nl",
        latency_ms=round((time.monotonic() - start) * 1000, 1),
        ok=ok,
        result_count=result_count,
        returned_rows=result_count > 0,
    )

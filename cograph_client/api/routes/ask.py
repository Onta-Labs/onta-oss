import time

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request

from cograph_client.analytics import distinct_id_for, emit
from cograph_client.api.deps import get_enrichment_job_store, get_neptune_client
from cograph_client.api.rate_limit import limiter
from cograph_client.auth.api_keys import TenantContext, get_tenant
from cograph_client.config import settings
from cograph_client.graph.client import NeptuneClient
from cograph_client.graph.entitlement import layer_stack_for
from cograph_client.graph.kg_status import (
    KG_EMPTY,
    KG_MISSING,
    empty_kg_message,
    kg_data_status,
    list_kg_names,
    missing_kg_message,
)
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
    # Ontology always lives in the base tenant graph (writes); reads are
    # layered via LayerStack so C extends A/B (ONTA-397).
    ontology_graph = tenant_graph_uri(tenant.tenant_id)
    # Instance data may be in a KG-specific graph
    instance_graph = kg_graph_uri(tenant.tenant_id, body.kg_name) if body.kg_name else ontology_graph

    # ONTA-413: separate "the graph is missing/empty" from "the query matched
    # nothing". SPARQL against a nonexistent named graph returns zero rows, not
    # an error, so without this probe all three states collapsed into the single
    # answer "No results found." and an MCP/CLI caller could not tell a typo'd
    # kg_name from a genuinely empty result. Two O(1) ASKs, one round-trip.
    if body.kg_name:
        status = await kg_data_status(client, tenant.tenant_id, body.kg_name)
        if status == KG_MISSING:
            # A 404 is the honest answer for a resource that does not exist, and
            # it is what lets the caller self-correct: the SDK raises OntaError
            # with the status + body, and the MCP server renders that as a tool
            # error. The available-KG list rides along in the detail so an agent
            # can retry with a real name without a second round-trip.
            available = await list_kg_names(client, tenant.tenant_id)
            logger.info(
                "ask_kg_not_found",
                tenant=tenant.tenant_id,
                kg_name=body.kg_name,
                available=len(available),
            )
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "kg_not_found",
                    "message": missing_kg_message(body.kg_name, available),
                    "kg_name": body.kg_name,
                    "available_kgs": available,
                },
            )
        if status == KG_EMPTY:
            # Registered but holds nothing. This is NOT an error (the caller
            # named a real KG), so the /ask contract stays an NLResult - but the
            # answer says so explicitly instead of "No results found.", and we
            # skip the SPARQL generation entirely since there is provably
            # nothing for it to match.
            return NLResult(
                answer=empty_kg_message(body.kg_name),
                sparql="",
                explanation=(
                    f"The knowledge graph '{body.kg_name}' is registered in this "
                    "workspace but contains zero triples, so no query was run."
                ),
            )

    # Visible layer graphs (tenant + Public, + Enhanced when entitled). Passed
    # so generated SPARQL can walk subClassOf edges that live in global layers,
    # and so the ontology summary includes Public/Enhanced types.
    stack = layer_stack_for(tenant)
    layer_graph_uris = stack.visible_graph_uris()
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
            body.question,
            ontology_graph,
            instance_graph,
            exclude_questions=body.exclude_questions,
            layer_graph_uris=layer_graph_uris,
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

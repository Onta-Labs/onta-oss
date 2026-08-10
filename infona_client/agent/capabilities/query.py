"""Read-only Q&A capability — wraps the existing NL→SPARQL ask pipeline.

This is the only capability that needs no plan/confirm round-trip: a question
does not mutate the graph, so the agent answers immediately. The planner
special-cases the ``question`` intent and calls :meth:`QueryCapability.answer`
directly. We still register it as a capability (so ``get_capabilities()`` is the
single source of truth for what the agent can do, and the classifier prompt can
include its ``describe()`` line), and we still implement ``plan``/``execute`` so
it satisfies the protocol: ``plan`` emits a single no-write ``answer`` step and
``execute`` fulfils it by delegating to :meth:`answer`.

Reuses :class:`infona_client.nlp.pipeline.NLQueryPipeline.ask` — the exact same
engine the ``/ask`` route calls — so the agent and the legacy route share one
Q&A implementation (no divergence).

ONTA-389: a completed answer mints a trackable answer run (``category=answer``)
with live P0/A9 + P7/A7 stage_trace when a job store is available on the
context. The response carries ``run_id`` so operators can open
``GET /operator/jobs/{run_id}/trace``. Clarifies and other non-answer chat
turns do **not** mint a job.
"""

from __future__ import annotations

import structlog

from infona_client.agent.kg_scope import SCOPE_NONE
from infona_client.agent.registry import AgentContext, PlanStep
from infona_client.graph.kg_status import (
    KG_EMPTY,
    KG_MISSING,
    empty_kg_message,
    kg_data_status,
    list_kg_names,
    missing_kg_message,
)
from infona_client.graph.queries import kg_graph_uri, tenant_graph_uri
from infona_client.graph.sparql_scope import CrossTenantQueryError
from infona_client.pipeline.answer_run import record_answer_run

logger = structlog.stdlib.get_logger("infona.agent.capabilities.query")


class QueryCapability:
    name = "query"
    # The planner's KG gate deliberately does NOT apply here: ``answer`` runs its
    # own, richer ONTA-413 probe below, which additionally distinguishes
    # registered-but-empty and honours the base-graph union (a workspace whose
    # instances live in the tenant graph must still get an answer). Declaring
    # "none" keeps that as the single read-path check rather than adding a second,
    # coarser one on top of it.
    kg_scope_policy = SCOPE_NONE
    #: Read-only: answering a question runs SPARQL SELECTs and writes nothing to
    #: the tenant's graphs, so a read-only member may dispatch it (ONTA-451).
    writes = False

    def describe(self) -> str:
        return (
            "Answer a read-only question about the data in the knowledge graph "
            "(counts, lookups, relationships) by generating and running SPARQL. "
            "Use for any 'how many', 'which', 'what', 'list', 'show me' question."
        )

    async def answer(self, ctx: AgentContext, question: str) -> dict:
        """Run the ask pipeline and return ``{answer, sparql, rows, narrative, run_id?}``.

        Builds the pipeline the same way ``api/routes/ask.py`` does: ontology
        from the tenant graph, instance data from the KG-specific graph.

        When ``ctx.extras["enrichment_job_store"]`` is present, mints an answer
        run (P7 A7 + P0/A9) and echoes its ``run_id`` for operator Job Trace.

        ONTA-413: a missing KG short-circuits to a ``{"kind": "clarify"}``
        payload rather than an exception. ``/agent``'s contract is
        ``{kind: answer|clarify|plan|result}`` and the Explorer chat renders on
        that shape, so raising here would be a breaking contract change; a
        clarify naming the missing KG (and the real ones) is both in-contract and
        directly actionable. An empty-but-registered KG stays an ``answer`` that
        says so explicitly, and skips the wasted SPARQL generation.
        """
        ontology_graph = tenant_graph_uri(ctx.tenant_id)
        if ctx.kg_name:
            status = await kg_data_status(ctx.neptune, ctx.tenant_id, ctx.kg_name)
            if status == KG_MISSING:
                available = await list_kg_names(ctx.neptune, ctx.tenant_id)
                return {
                    "kind": "clarify",
                    "question": missing_kg_message(ctx.kg_name, available),
                    "options": list(available),
                }
            if status == KG_EMPTY:
                return {
                    "answer": empty_kg_message(ctx.kg_name),
                    "sparql": "",
                    "narrative": "",
                    "citations": [],
                    "coverage_caveat": "",
                    "rows": [],
                }
        pipeline = self._build_pipeline(ctx)
        instance_graph = (
            kg_graph_uri(ctx.tenant_id, ctx.kg_name) if ctx.kg_name else ontology_graph
        )
        try:
            result = await pipeline.ask(question, ontology_graph, instance_graph)
        except CrossTenantQueryError:
            # ONTA-424: the generated query reached outside this workspace and
            # was refused before the store saw it. `/ask` has a route-level
            # boundary handler that turns this into a degraded NLResult; the
            # agent has none — `planner.handle` does not catch, and `api/app.py`
            # registers no handler for it — so letting it escape here would be a
            # bare 500 that also breaks the `{kind: …}` response contract and
            # loses the conversation turn. Degrade in-contract instead. The
            # security event is already logged by `graph/sparql_scope.py`, and
            # nothing about the offending query is echoed back to the user.
            logger.warning(
                "agent_answer_cross_tenant_query_refused",
                tenant=ctx.tenant_id,
                kg_name=ctx.kg_name or "",
            )
            return {
                "answer": (
                    "Could not answer this question: the query that was "
                    "generated for it could not be confined to this workspace, "
                    "so it was not run. Please rephrase and try again."
                ),
                "sparql": "",
                "narrative": "",
                "citations": [],
                "coverage_caveat": "",
                "rows": [],
            }
        out = {
            "answer": result.answer,
            "sparql": result.sparql,
            "narrative": getattr(result, "narrative_answer", ""),
            # Honest-answer metadata (ONTA-280): echo per-fact citations + the
            # coverage caveat so the agent interface has parity with /ask (empty
            # unless INFONA_ANSWER_CITATIONS_ENABLED). Serialized to plain dicts
            # so the returned payload stays JSON-friendly.
            "citations": [c.model_dump() for c in getattr(result, "citations", [])],
            "coverage_caveat": getattr(result, "coverage_caveat", ""),
            # The pipeline does not surface raw rows on NLResult; the formatted
            # answer + sparql are what callers render. Keep the key present (empty)
            # so the contract is stable for clients that look for it.
            "rows": [],
        }
        # ONTA-389: mint answer run for operator Job Trace (P7 + P0/A9).
        # Documented path: response.run_id → GET /operator/jobs/{run_id}/trace.
        job_store = (getattr(ctx, "extras", None) or {}).get("enrichment_job_store")
        run_id = await record_answer_run(
            job_store=job_store,
            tenant_id=ctx.tenant_id,
            kg_name=ctx.kg_name or "",
            question=question,
            answer=result.answer,
            sparql=result.sparql or "",
            citations=out["citations"],
            coverage_caveat=out["coverage_caveat"] or "",
            ok=True,
            thread_id=getattr(ctx, "session_id", None),
            medium=getattr(ctx, "medium", "") or "",
            timing=getattr(result, "timing", None) or {},
            source="agent",
        )
        if run_id:
            out["run_id"] = run_id
            out["job_id"] = run_id  # alias — same id the Jobs / operator APIs use
        return out

    def _build_pipeline(self, ctx: AgentContext):
        # Lazy import so importing the agent registry never drags in the heavy
        # pipeline module (and its anthropic client) at app-boot registration.
        from infona_client.nlp.pipeline import NLQueryPipeline

        return NLQueryPipeline(ctx.neptune, ctx.anthropic_key)

    async def plan(self, ctx: AgentContext, instruction: str) -> list[PlanStep]:
        # A question is read-only: a single no-write step the planner can also
        # fast-path. confidence 1.0 — answering is always applicable to a
        # question; the planner decides whether the intent IS a question.
        return [
            PlanStep(
                capability=self.name,
                action="answer",
                params={"question": instruction},
                rationale="Read-only question; answer directly with SPARQL.",
                confidence=1.0,
                preview={"summary": "Runs a read-only SPARQL query; no writes."},
                cost={},
            )
        ]

    async def execute(self, ctx: AgentContext, step: PlanStep) -> dict:
        question = step.params.get("question", "")
        out = await self.answer(ctx, question)
        # `answer` may return its OWN kind (ONTA-413's missing-KG clarify);
        # default to "answer" so every other path is byte-identical to before.
        return {**out, "kind": out.get("kind", "answer")}

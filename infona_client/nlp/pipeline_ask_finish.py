"""Success-path format / rephrase / citations / NLResult for Cypher /ask."""
from __future__ import annotations

import time

import structlog

from infona_client.graph.parser import unbound_projection_vars
from infona_client.models.query import NLResult
from infona_client.nlp.pipeline_helpers import ONTOLOGY_EMPTY, ONTOLOGY_FETCH_ERROR

logger = structlog.stdlib.get_logger("infona.nlp.pipeline")


from infona_client.nlp.token_usage import STAGE_REPHRASE

class PipelineAskFinishMixin:
    async def _ask_cypher_finish(self, st, *, attempt: int, t0: float) -> NLResult:
        """Compose the successful NLResult (format, rephrase, citations)."""
        cypher = st.cypher
        ontology = st.ontology
        explanation = st.explanation
        functions_needed = st.functions_needed
        honest_empty_note = st.honest_empty_note
        last_gen = st.last_gen
        last_params = st.last_params
        money_leaf_bound = st.money_leaf_bound
        question = st.question
        tenant_id = st.tenant_id
        kg_name = st.kg_name
        data_graph = st.data_graph
        graph_uri = st.graph_uri
        layer_graph_uris = st.layer_graph_uris
        kg_declared_names = st.kg_declared_names
        kg_active_types = st.kg_active_types
        ontology_source = st.ontology_source
        forced_params = st.forced_params
        variables = st.variables
        bindings = st.bindings
        timing = st.timing
        token_ledger = st.token_ledger
        run_manifest = st.run_manifest

        # Unbound projection honesty
        missing_vars = unbound_projection_vars(variables, bindings)
        if missing_vars:
            timing["unbound_projection_vars"] = ", ".join(missing_vars)
            logger.info(
                "unbound_projection_vars",
                vars=missing_vars,
                question=question,
            )

        # ONTA-454 KG coverage caveat
        kg_coverage_note = ""
        if bindings:
            # Prefer type names from the executed gen when Cypher has no
            # SPARQL type IRIs for referenced_types().
            kg_coverage_note = await self._kg_coverage_caveat(
                cypher,
                ontology,
                data_graph,
                graph_uri,
                layer_graph_uris,
                kg_declared_names,
                kg_active_types,
                ontology_source
                if ontology_source in ("semantic", "full")
                else "full",
                timing,
                query_params=forced_params,
            )

        answer = await self._format_answer(
            bindings,
            explanation,
            missing_vars=missing_vars,
            data_graph=data_graph,
        )
        answer += honest_empty_note
        if kg_coverage_note:
            answer += f"\n\nCoverage note: {kg_coverage_note}"

        t_reph = time.time()
        narrative_answer = await self._rephrase_via_openrouter(
            question, bindings
        )
        rephrase_usage = getattr(self, "_last_rephrase_usage", None)
        self._last_rephrase_usage = None
        if rephrase_usage:
            token_ledger.record(
                stage=STAGE_REPHRASE,
                attempt=attempt,
                model=str(rephrase_usage.get("model") or ""),
                provider=str(
                    rephrase_usage.get("provider") or "openrouter"
                ),
                prompt_tokens=rephrase_usage.get("prompt_tokens"),
                completion_tokens=rephrase_usage.get("completion_tokens"),
                total_tokens=rephrase_usage.get("total_tokens"),
            )
        if honest_empty_note and narrative_answer:
            narrative_answer += honest_empty_note
        if kg_coverage_note and narrative_answer:
            narrative_answer += f"\n\nCoverage note: {kg_coverage_note}"
        timing["rephrase_ms"] = round((time.time() - t_reph) * 1000, 1)

        citations = []
        coverage_caveat = ""
        run_coverage = (
            run_manifest.coverage()
            if hasattr(run_manifest, "coverage")
            else run_manifest
        )
        if self._answer_citations_enabled:
            from infona_client.nlp.answer_meta import (
                build_citations,
                build_coverage_caveat,
            )

            citations = await build_citations(
                self.neptune, data_graph, variables, bindings
            )
            stale_count = sum(1 for c in citations if not c.is_current)
            coverage_caveat = build_coverage_caveat(
                run_coverage,
                stale_count=stale_count,
                total_cited=len(citations),
            )
            if citations:
                timing["citations"] = len(citations)
        elif run_coverage is not None:
            from infona_client.nlp.answer_meta import build_coverage_caveat

            coverage_caveat = build_coverage_caveat(run_coverage)
        if kg_coverage_note:
            coverage_caveat = "; ".join(
                p for p in (kg_coverage_note, coverage_caveat) if p
            )

        timing["total_ms"] = round((time.time() - t0) * 1000, 1)
        timing["attempts"] = attempt + 1
        timing["rows"] = len(bindings)
        timing.update(token_ledger.totals_for_timing())
        cov_ok = last_gen.get("_coverage")
        q_conf = ""
        q_conf_reason = ""
        q_clarify = ""
        if cov_ok is not None:
            try:
                timing.update(cov_ok.to_timing())
                q_conf = cov_ok.confidence
                q_conf_reason = cov_ok.reason or ""
                q_clarify = cov_ok.clarification_prompt or ""
            except Exception:
                pass
        try:
            from infona_client.nlp.ask_process_log import log_ask_event

            log_ask_event(
                "ask_result",
                question=question,
                tenant_id=tenant_id,
                kg=kg_name,
                answer=(answer or "")[:1500],
                cypher=cypher,
                params=last_params,
                rows=len(bindings),
                query_confidence=q_conf,
                query_confidence_reason=q_conf_reason,
                money_leaf_bound=money_leaf_bound,
                timing={
                    k: timing.get(k)
                    for k in (
                        "query_probe",
                        "money_leaf_top",
                        "money_leaf_hard_bound",
                        "numeric_grounding_prop",
                        "dim_values_present",
                        "query_confidence",
                        "attempts",
                        "total_ms",
                        "ontology_type_names",
                        "semantic_type_names",
                        "populated_type_names",
                        "ontology_semantic_ignored",
                    )
                    if k in timing
                },
            )
        except Exception:
            pass
        return NLResult(
            answer=answer,
            sparql=cypher,
            explanation=explanation,
            ontology=ontology,
            narrative_answer=narrative_answer,
            functions_invoked=functions_needed,
            timing=timing,
            citations=citations,
            coverage_caveat=coverage_caveat,
            token_usage=token_ledger.to_list(),
            query_confidence=q_conf,
            query_confidence_reason=q_conf_reason,
            clarification_prompt=q_clarify,
        )


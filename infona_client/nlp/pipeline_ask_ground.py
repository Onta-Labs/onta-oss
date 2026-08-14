"""Planning-layer grounding (build / probe / subgraph / numeric / dims).

Invariant: unique-only money bind; never short-circuit the LLM.
"""
from __future__ import annotations

import time
from typing import Any

import structlog

from infona_client.models.query import NLResult
from infona_client.nlp.cypher_generate import ontology_from_graph_store
from infona_client.nlp.pipeline_helpers import ONTOLOGY_EMPTY, ONTOLOGY_FETCH_ERROR
from infona_client.nlp.token_usage import STAGE_REPHRASE, TokenUsageLedger, pop_attached_usage, stage_for_attempt

logger = structlog.stdlib.get_logger("infona.nlp.pipeline")


class PipelineAskGroundMixin:
    async def _ask_cypher_ground(self, st):
        """Fill grounding fields on ``st``. Returns an early ``NLResult`` or None."""
        question = st.question
        ontology = st.ontology
        type_names = st.type_names
        ontology_source = st.ontology_source
        kg_active_types = st.kg_active_types
        populated_type_names = st.populated_type_names
        semantic_type_names = st.semantic_type_names
        tenant_id = st.tenant_id
        kg_name = st.kg_name
        store = st.store
        timing = st.timing
        t0 = st.t0
        token_ledger = st.token_ledger

        # Ontology-subgraph + numeric grounding (planning layer) — structured
        # prompt context only. Never short-circuits the LLM (always-LLM rule).
        grounding_text = ""
        # Unique dim-registry binds for post-gen coverage (leaf+value required).
        # Initialized outside the try so coverage gates always see a defined list.
        dim_binds: list = []
        # Live inventory for zero-instance / pollution-type coverage gate.
        build_ctx = None
        populated_types_for_coverage: tuple[str, ...] | None = None
        # Money leaf hard-bind (probe / numeric plan → params after gen).
        money_leaf_bound: str | None = None
        money_cue_bound: str | None = None
        try:
            from infona_client.nlp.ontology_subgraph_match import (
                format_grounding_for_prompt,
                ground_ask_plan,
            )
            from infona_client.nlp.numeric_plan_grounding import (
                format_numeric_grounding_for_prompt,
                ground_numeric_plan,
                merge_grounding_texts,
            )
            from infona_client.nlp.ontology_mention_index import (
                get_process_mention_index,
                get_resolve_context,
                lookup_query_embedding,
            )

            names_for_ground = type_names or None
            if not names_for_ground and ontology:
                from infona_client.nlp.cypher_generate import (
                    extract_type_names_from_ontology,
                )

                names_for_ground = extract_type_names_from_ontology(ontology) or None
            # Live GraphStore inventory first — scopes money leaf ranking to
            # types populated in THIS KG (anti tuition_usd pollution).
            build_text = ""
            build_ctx = None
            populated_for_numeric: list[str] | None = None
            try:
                from infona_client.nlp.query_build import (
                    collect_query_build_context,
                    format_query_build_for_prompt,
                )

                build_ctx = await collect_query_build_context(
                    store,
                    tenant_id=tenant_id,
                    kg=kg_name,
                    question=question,
                )
                build_text = format_query_build_for_prompt(build_ctx)
                if build_ctx is not None and build_ctx.types:
                    timing["query_build"] = "present"
                    timing["query_build_types"] = float(
                        len(build_ctx.populated_type_names)
                    )
                    if build_ctx.question_type_hits:
                        timing["query_build_type_hits"] = ", ".join(
                            build_ctx.question_type_hits
                        )[:200]
                    # Vague “how many records?” + ≥2 live types → ask, don’t guess.
                    try:
                        from infona_client.nlp.ask_process_log import log_ask_event
                        from infona_client.nlp.query_ambiguity import (
                            ambiguous_count_needs_clarify,
                            format_type_count_clarification,
                        )

                        if ambiguous_count_needs_clarify(
                            question, build_ctx.types
                        ):
                            clarify = format_type_count_clarification(
                                build_ctx.types
                            )
                            timing["query_ambiguity_clarify"] = 1.0
                            timing["query_confidence"] = "low"
                            timing["query_confidence_reason"] = (
                                "ambiguous count: multiple populated types, "
                                "question did not name one"
                            )
                            log_ask_event(
                                "ask_clarify",
                                question=question,
                                tenant_id=tenant_id,
                                kg=kg_name,
                                reason="ambiguous_count",
                                populated_types=list(
                                    build_ctx.populated_type_names
                                )[:20],
                                answer=clarify,
                            )
                            timing.update(token_ledger.totals_for_timing())
                            return NLResult(
                                answer=clarify,
                                sparql="",
                                explanation="clarification: ambiguous count",
                                ontology=ontology,
                                timing={
                                    **timing,
                                    "total_ms": round(
                                        (time.time() - t0) * 1000, 1
                                    ),
                                    "attempts": 0,
                                },
                                token_usage=token_ledger.to_list(),
                                query_confidence="low",
                                query_confidence_reason=str(
                                    timing["query_confidence_reason"]
                                ),
                                clarification_prompt=clarify,
                            )
                    except Exception:
                        logger.debug(
                            "query_ambiguity_check_failed", exc_info=True
                        )
                    if build_ctx.populated_type_names:
                        populated_for_numeric = list(build_ctx.populated_type_names)
                        # Zero-instance pollution gate (#local high-conf empty).
                        populated_types_for_coverage = build_ctx.populated_type_names
            except Exception:
                logger.debug("query_build_context_failed", exc_info=True)
                build_text = ""
                build_ctx = None
            if not populated_for_numeric and kg_active_types:
                populated_for_numeric = sorted(kg_active_types)
            if populated_types_for_coverage is None and populated_for_numeric:
                populated_types_for_coverage = tuple(populated_for_numeric)
            # Optional ONTA-537 mention index + precomputed query embedding
            # when the ask path already has them (best-effort; hermetic without).
            _rctx = get_resolve_context()
            _midx = (
                _rctx.mention_index
                if _rctx is not None and _rctx.mention_index is not None
                else get_process_mention_index()
            )
            _qemb = lookup_query_embedding(question, _rctx)
            grounded = ground_ask_plan(
                question,
                ontology,
                type_names=names_for_ground,
                mention_index=_midx,
                query_embedding=_qemb,
            )
            loc_text = format_grounding_for_prompt(grounded)
            if grounded is not None:
                timing["grounding_confidence"] = grounded.confidence
                if grounded.template:
                    timing["grounding_template"] = grounded.template
                if grounded.path is not None:
                    timing["grounding_path"] = grounded.path.describe()
            num_plan = ground_numeric_plan(
                question,
                ontology,
                type_names=names_for_ground,
                mention_index=_midx,
                query_embedding=_qemb,
                populated_types=populated_for_numeric,
            )
            num_text = format_numeric_grounding_for_prompt(num_plan)
            if num_plan is not None:
                timing["numeric_grounding_confidence"] = num_plan.confidence
                if num_plan.prop_key:
                    timing["numeric_grounding_prop"] = num_plan.prop_key
                # Hard-bind only unique resolve — never probe[0] guess (#378).
                if (
                    getattr(num_plan, "confidence", "") == "unique"
                    and num_plan.prop_key
                ):
                    money_leaf_bound = num_plan.prop_key
                if num_plan.template:
                    timing["numeric_grounding_template"] = num_plan.template
            # Low-cardinality dim registry: known enums + entity dims as
            # prompt context only (always-LLM; never short-circuits Cypher).
            # Structured unique binds also feed post-gen constraint coverage.
            dim_text = ""
            dim_registry_obj = None
            try:
                from infona_client.nlp.dim_registry import (
                    get_cached_dim_registry,
                    planning_dim_context,
                )

                dim_text, dim_binds = await planning_dim_context(
                    store,
                    tenant_id=tenant_id,
                    kg=kg_name,
                    question=question,
                )
                if dim_text:
                    timing["dim_registry"] = "present"
                if dim_binds:
                    timing["dim_binds_count"] = float(len(dim_binds))
                    timing["dim_bound_leaves"] = ", ".join(
                        getattr(b.dim, "leaf", "") for b in dim_binds
                    )[:200]
                try:
                    dim_registry_obj = get_cached_dim_registry(
                        tenant_id, kg_name
                    )
                except Exception:
                    dim_registry_obj = None
            except Exception:
                logger.debug("dim_registry_grounding_failed", exc_info=True)
                dim_text = ""
                dim_binds = []
            # Cheap read-only probe: dim values + money leaf candidates.
            # Merged into grounding before LLM (always-LLM; never short-circuit).
            probe_text = ""
            try:
                from infona_client.nlp.query_probe import build_probe_context

                probe_ctx = await build_probe_context(
                    store,
                    tenant_id=tenant_id,
                    kg=kg_name,
                    question=question,
                    ontology_summary=ontology or "",
                    registry=dim_registry_obj,
                    binds=dim_binds,
                    populated_types=populated_for_numeric,
                    build_ctx=build_ctx,
                    type_hint=(
                        build_ctx.question_type_hits[0]
                        if build_ctx is not None and build_ctx.question_type_hits
                        else None
                    ),
                )
                # Prefer money + dim-values sections (build already in build_text).
                probe_bits = [
                    probe_ctx.dim_values_text or "",
                    probe_ctx.money_text or "",
                ]
                probe_text = "\n\n".join(
                    b.strip() for b in probe_bits if b and b.strip()
                )
                if probe_text:
                    timing["query_probe"] = "present"
                if probe_ctx.extra.get("dim_values_present"):
                    timing["dim_values_present"] = 1.0
                if probe_ctx.money_candidates:
                    timing["money_leaf_candidates"] = float(
                        len(probe_ctx.money_candidates)
                    )
                    top = probe_ctx.money_candidates[0]
                    timing["money_leaf_top"] = top.leaf
                    # Prompt-only: do not hard-bind probe ranking.
                    if probe_ctx.money_cue:
                        timing["money_cue"] = probe_ctx.money_cue
                        money_cue_bound = probe_ctx.money_cue
            except Exception:
                logger.debug("query_probe_failed", exc_info=True)
                probe_text = ""
            # Order: build inventory, dim values/money probe, subgraph,
            # numeric plan, dim registry binds.
            grounding_text = merge_grounding_texts(
                build_text, probe_text, loc_text, num_text, dim_text
            )
            # Structured ask process log (input + grounding spine).
            try:
                from infona_client.nlp.ask_process_log import log_ask_event

                log_ask_event(
                    "ask_grounding",
                    question=question,
                    tenant_id=tenant_id,
                    kg=kg_name,
                    ontology_source=ontology_source,
                    ontology=(ontology or "")[:4000],
                    grounding_text=(grounding_text or "")[:4000],
                    money_leaf_bound=money_leaf_bound,
                    money_cue=money_cue_bound,
                    dim_binds=[
                        f"{getattr(b, 'token', '')}->{getattr(getattr(b, 'dim', None), 'leaf', '')}"
                        for b in (dim_binds or [])[:12]
                    ],
                    populated_types=list(populated_types_for_coverage or [])[:20],
                    ontology_type_names=list(type_names or [])[:40],
                    semantic_type_names=list(semantic_type_names or [])[:40],
                    populated_type_names=list(populated_type_names)[:40],
                    query_model=f"{self._query_provider}:{self._query_model}",
                )
            except Exception:
                pass
        except Exception:
            logger.debug("ontology_subgraph_grounding_failed", exc_info=True)
            grounding_text = ""

        st.grounding_text = grounding_text
        st.dim_binds = dim_binds
        st.build_ctx = build_ctx
        st.populated_types_for_coverage = populated_types_for_coverage
        st.money_leaf_bound = money_leaf_bound
        st.money_cue_bound = money_cue_bound
        return None

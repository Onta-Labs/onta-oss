"""Empty-generation ontology escalation (semantic → full) + re-ground."""
from __future__ import annotations

import time

import structlog

from infona_client.models.query import NLResult
from infona_client.nlp.pipeline_helpers import ONTOLOGY_EMPTY, ONTOLOGY_FETCH_ERROR

logger = structlog.stdlib.get_logger("infona.nlp.pipeline")


class PipelineAskEscalateMixin:
    async def _ask_cypher_escalate_empty(self, st, *, attempt: int) -> str:
        """Widen ontology after an empty generation. Returns error_feedback."""
        if not st.last_was_empty_query:
            return ""
        full_ontology_loaded = st.full_ontology_loaded
        ontology = st.ontology
        ontology_source = st.ontology_source
        grounding_text = st.grounding_text
        dim_binds = st.dim_binds
        populated_types_for_coverage = st.populated_types_for_coverage
        kg_active_types = st.kg_active_types
        graph_uri = st.graph_uri
        data_graph = st.data_graph
        layer_graph_uris = st.layer_graph_uris
        question = st.question
        store = st.store
        tenant_id = st.tenant_id
        kg_name = st.kg_name
        timing = st.timing

        if not full_ontology_loaded:
            try:
                full_ontology = await self._fetch_ontology(
                    graph_uri,
                    data_graph,
                    layer_graph_uris=layer_graph_uris,
                )
                if (
                    full_ontology
                    and full_ontology.strip()
                    and full_ontology
                    not in (ONTOLOGY_FETCH_ERROR, ONTOLOGY_EMPTY)
                ):
                    ontology = full_ontology
                    ontology_source = "full"
                    timing["ontology_escalated_to_full_attempt"] = (
                        attempt
                    )
                    # Re-ground after ontology escalation.
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
                        from infona_client.nlp.cypher_generate import (
                            extract_type_names_from_ontology,
                        )

                        names_esc = (
                            extract_type_names_from_ontology(ontology)
                            or None
                        )
                        from infona_client.nlp.ontology_mention_index import (
                            get_process_mention_index,
                            get_resolve_context,
                            lookup_query_embedding,
                        )

                        _rctx_esc = get_resolve_context()
                        _midx_esc = (
                            _rctx_esc.mention_index
                            if _rctx_esc is not None
                            and _rctx_esc.mention_index is not None
                            else get_process_mention_index()
                        )
                        _qemb_esc = lookup_query_embedding(
                            question, _rctx_esc
                        )
                        grounded_esc = ground_ask_plan(
                            question,
                            ontology,
                            type_names=names_esc,
                            mention_index=_midx_esc,
                            query_embedding=_qemb_esc,
                        )
                        pop_esc = (
                            list(kg_active_types)
                            if kg_active_types
                            else None
                        )
                        num_esc = ground_numeric_plan(
                            question,
                            ontology,
                            type_names=names_esc,
                            mention_index=_midx_esc,
                            query_embedding=_qemb_esc,
                            populated_types=pop_esc,
                        )
                        dim_esc = ""
                        try:
                            from infona_client.nlp.dim_registry import (
                                planning_dim_context,
                            )

                            dim_esc, dim_binds = await planning_dim_context(
                                store,
                                tenant_id=tenant_id,
                                kg=kg_name,
                                question=question,
                            )
                            if dim_binds:
                                timing["dim_binds_count"] = float(
                                    len(dim_binds)
                                )
                                timing["dim_bound_leaves"] = ", ".join(
                                    getattr(b.dim, "leaf", "")
                                    for b in dim_binds
                                )[:200]
                        except Exception:
                            dim_esc = ""
                        build_esc = ""
                        build_ctx_esc = None
                        try:
                            from infona_client.nlp.query_build import (
                                collect_query_build_context,
                                format_query_build_for_prompt,
                            )

                            build_ctx_esc = await collect_query_build_context(
                                store,
                                tenant_id=tenant_id,
                                kg=kg_name,
                                question=question,
                            )
                            build_esc = format_query_build_for_prompt(
                                build_ctx_esc
                            )
                            if (
                                build_ctx_esc is not None
                                and build_ctx_esc.populated_type_names
                            ):
                                populated_types_for_coverage = (
                                    build_ctx_esc.populated_type_names
                                )
                        except Exception:
                            build_esc = ""
                        probe_esc = ""
                        try:
                            from infona_client.nlp.dim_registry import (
                                get_cached_dim_registry,
                            )
                            from infona_client.nlp.query_probe import (
                                build_probe_context,
                            )

                            reg_esc = get_cached_dim_registry(
                                tenant_id, kg_name
                            )
                            pop_for_probe = (
                                list(populated_types_for_coverage)
                                if populated_types_for_coverage
                                else pop_esc
                            )
                            pctx = await build_probe_context(
                                store,
                                tenant_id=tenant_id,
                                kg=kg_name,
                                question=question,
                                ontology_summary=ontology or "",
                                registry=reg_esc,
                                binds=dim_binds,
                                populated_types=pop_for_probe,
                                build_ctx=build_ctx_esc,
                            )
                            probe_esc = "\n\n".join(
                                b.strip()
                                for b in (
                                    pctx.dim_values_text,
                                    pctx.money_text,
                                )
                                if b and b.strip()
                            )
                            if pctx.money_candidates:
                                timing["money_leaf_candidates"] = float(
                                    len(pctx.money_candidates)
                                )
                        except Exception:
                            probe_esc = ""
                        grounding_text = merge_grounding_texts(
                            build_esc,
                            probe_esc,
                            format_grounding_for_prompt(grounded_esc),
                            format_numeric_grounding_for_prompt(num_esc),
                            dim_esc,
                        )
                    except Exception:
                        pass
            except Exception:
                logger.debug(
                    "ontology_escalation_fetch_failed", exc_info=True
                )
            full_ontology_loaded = True
        error_feedback = (
            "The previous attempt returned an EMPTY or unparseable "
            "Cypher query. You MUST output a VALID, non-empty Cypher "
            "query in the `cypher` field, using the exact type/"
            "attribute names from the ontology schema above. Never "
            "return an empty string."
        )

        st.full_ontology_loaded = full_ontology_loaded
        st.ontology = ontology
        st.ontology_source = ontology_source
        st.grounding_text = grounding_text
        st.dim_binds = dim_binds
        st.populated_types_for_coverage = populated_types_for_coverage
        return error_feedback

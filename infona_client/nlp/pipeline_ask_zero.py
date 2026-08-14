"""Zero-row recovery: enum mismatch, honest-empty, ontology escalation."""
from __future__ import annotations

import time

import structlog

from infona_client.models.query import NLResult
from infona_client.nlp.pipeline_helpers import ONTOLOGY_EMPTY, ONTOLOGY_FETCH_ERROR

logger = structlog.stdlib.get_logger("infona.nlp.pipeline")


class PipelineAskZeroMixin:
    async def _ask_cypher_zero_row(self, st, *, attempt: int, max_attempts: int):
        """Return ``"continue"`` to retry, or None to proceed to format."""
        if st.bindings or attempt >= max_attempts - 1:
            return None
        cypher = st.cypher
        ontology = st.ontology
        ontology_source = st.ontology_source
        full_ontology_loaded = st.full_ontology_loaded
        forced_params = st.forced_params
        question = st.question
        graph_uri = st.graph_uri
        data_graph = st.data_graph
        layer_graph_uris = st.layer_graph_uris
        timing = st.timing
        honest_empty_note = st.honest_empty_note
        last_error = st.last_error
        last_was_enum_filter_mismatch = False

        try:
            from infona_client.nlp.enum_filter import (
                enum_mismatch_feedback,
                impossible_enum_contains,
            )

            # Works when the query still carries SPARQL-shaped FILTERs
            # or type URIs; no-op on pure template Cypher.
            mismatches = impossible_enum_contains(cypher, ontology)
            if mismatches:
                last_error = enum_mismatch_feedback(
                    mismatches, previous_sparql=cypher
                )
                last_was_enum_filter_mismatch = True
                timing["enum_filter_mismatch_retry"] = 1.0
                timing["enum_filter_mismatches"] = float(len(mismatches))
                logger.info(
                    "enum_filter_mismatch_retry",
                    count=len(mismatches),
                    question=question,
                )
                st.last_error = last_error
                st.last_was_enum_filter_mismatch = last_was_enum_filter_mismatch
                st.ontology = ontology
                st.ontology_source = ontology_source
                st.full_ontology_loaded = full_ontology_loaded
                st.honest_empty_note = honest_empty_note
                return "continue"
        except Exception:
            logger.debug(
                "enum_filter_mismatch_check_failed", exc_info=True
            )

        if not full_ontology_loaded and ontology_source == "semantic":
            from infona_client.nlp.empty_type_guard import (
                empty_declared_types,
                honest_empty_targets,
                zero_row_escalation_feedback,
            )

            honest = honest_empty_targets(
                question, cypher, ontology, params=forced_params
            )
            full_ontology = ""
            if not honest:
                try:
                    full_ontology = await self._fetch_ontology(
                        graph_uri,
                        data_graph,
                        layer_graph_uris=layer_graph_uris,
                    )
                except Exception:
                    logger.debug(
                        "ontology_zero_row_escalation_failed",
                        exc_info=True,
                    )
                    full_ontology_loaded = True
            full_ontology_usable = bool(
                full_ontology
                and full_ontology.strip()
                and full_ontology
                not in (ONTOLOGY_FETCH_ERROR, ONTOLOGY_EMPTY)
            )
            if full_ontology_usable and not honest:
                honest = honest_empty_targets(
                    question,
                    cypher,
                    full_ontology,
                    params=forced_params,
                )
            if honest:
                names = ", ".join(sorted(honest))
                timing["zero_row_honest_empty"] = 1.0
                timing["zero_row_honest_empty_types"] = names
                honest_empty_note = (
                    f"\n\nNote: {names} "
                    f"{'is' if len(honest) == 1 else 'are'} declared in the "
                    "ontology but currently ha"
                    f"{'s' if len(honest) == 1 else 've'} no instances in "
                    "this knowledge graph."
                )
                logger.info(
                    "zero_row_honest_empty",
                    types=sorted(honest),
                    question=question,
                )
                full_ontology_loaded = (
                    full_ontology_loaded or full_ontology_usable
                )
            elif full_ontology_usable:
                ontology = full_ontology
                ontology_source = "full"
                timing["ontology_escalated_to_full_attempt"] = attempt + 1
                timing["ontology_zero_row_escalation"] = 1.0
                last_was_enum_filter_mismatch = True
                last_error = zero_row_escalation_feedback(
                    bool(empty_declared_types(full_ontology))
                )
                full_ontology_loaded = True
                logger.info(
                    "ontology_zero_row_escalation",
                    question=question,
                    attempt=attempt,
                )
                st.last_error = last_error
                st.last_was_enum_filter_mismatch = last_was_enum_filter_mismatch
                st.ontology = ontology
                st.ontology_source = ontology_source
                st.full_ontology_loaded = full_ontology_loaded
                st.honest_empty_note = honest_empty_note
                return "continue"

        st.ontology = ontology
        st.ontology_source = ontology_source
        st.full_ontology_loaded = full_ontology_loaded
        st.honest_empty_note = honest_empty_note
        st.last_error = last_error
        st.last_was_enum_filter_mismatch = last_was_enum_filter_mismatch
        return None

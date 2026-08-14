"""Post-gen filter / schema / constraint gates. Always-LLM: regenerate only."""
from __future__ import annotations

import time

import structlog

from infona_client.models.query import NLResult
from infona_client.nlp.pipeline_helpers import ONTOLOGY_EMPTY, ONTOLOGY_FETCH_ERROR

logger = structlog.stdlib.get_logger("infona.nlp.pipeline")


class PipelineAskValidateMixin:
    def _ask_cypher_validate_gen(self, st, *, attempt: int, max_attempts: int, t0: float):
        """Return ``("continue", err)``, ``("return", NLResult)``, or ``("ok", None)``."""
        gen = st.gen
        if gen.get("stub") or gen.get("fixture"):
            return ("ok", None)
        cypher_raw = st.cypher_raw
        question = st.question
        params = st.params
        dim_binds = st.dim_binds
        populated_types_for_coverage = st.populated_types_for_coverage
        ontology = st.ontology
        explanation = st.explanation
        timing = st.timing
        token_ledger = st.token_ledger
        schema_inventory = st.schema_inventory
        last_gen = st.last_gen

        from infona_client.nlp.cypher_filter_integrity import (
            check_cypher_filter_integrity,
            filter_integrity_feedback,
        )
        from infona_client.nlp.query_constraint_coverage import (
            check_constraint_coverage,
            coverage_feedback,
            fail_closed_answer,
        )
        from infona_client.nlp.schema_valid_cypher import (
            check_schema_valid_cypher,
            fail_closed_schema_answer,
            schema_valid_feedback,
        )

        filt_reason = check_cypher_filter_integrity(
            cypher_raw,
            question=question,
            template=gen.get("template"),
            params=params,
        )
        if filt_reason:
            last_error = filter_integrity_feedback(
                filt_reason, previous_cypher=cypher_raw
            )
            cov_fail = check_constraint_coverage(
                question,
                cypher_raw,
                params=params,
                template=gen.get("template"),
                integrity_reason=filt_reason,
                dim_binds=dim_binds,
                populated_types=populated_types_for_coverage,
            )
            timing.update(cov_fail.to_timing())
            if attempt < max_attempts - 1:
                last_was_enum_filter_mismatch = True
                timing["cypher_filter_integrity_retry"] = 1.0
                logger.info(
                    "cypher_filter_integrity_retry",
                    reason=filt_reason[:200],
                    question=question,
                    attempt=attempt,
                )
                st.last_error = last_error
                st.last_was_enum_filter_mismatch = True
                return ("continue", last_error)
            timing.update(token_ledger.totals_for_timing())
            return ("return", NLResult(
                answer=(
                    "Could not answer: generated Cypher would apply "
                    "filters incorrectly (OPTIONAL MATCH value filter "
                    "does not constrain results). Fail closed rather "
                    "than return a silent unfiltered total."
                ),
                sparql=cypher_raw,
                explanation=explanation,
                ontology=ontology,
                timing={
                    **timing,
                    "total_ms": round((time.time() - t0) * 1000, 1),
                    "attempts": attempt + 1,
                    "cypher_filter_integrity_reject": 1.0,
                },
                token_usage=token_ledger.to_list(),
                query_confidence=cov_fail.confidence,
                query_confidence_reason=cov_fail.reason,
                clarification_prompt=cov_fail.clarification_prompt,
            ))

        # Schema-valid predicates: free-form must not invent
        # relationship types / attr leaves (HAS_OFFERED_IN vs
        # offered_in → OFFERED_IN). Prefer precomputed GraphStore
        # inventory (catalog + populated leaves); ontology text
        # only when store probe failed. Post-gen gate only.
        schema_res = check_schema_valid_cypher(
            cypher_raw,
            ontology or "",
            params=params,
            template=gen.get("template"),
            inventory=schema_inventory,
        )
        timing.update(schema_res.to_timing())
        if not schema_res.ok:
            last_error = schema_valid_feedback(
                schema_res, previous_cypher=cypher_raw
            )
            cov_schema = check_constraint_coverage(
                question,
                cypher_raw,
                params=params,
                template=gen.get("template"),
                schema_reason=schema_res.reason,
                dim_binds=dim_binds,
                populated_types=populated_types_for_coverage,
            )
            timing.update(cov_schema.to_timing())
            if attempt < max_attempts - 1:
                last_was_enum_filter_mismatch = True
                timing["schema_valid_cypher_retry"] = 1.0
                logger.info(
                    "schema_valid_cypher_retry",
                    reason=(schema_res.reason or "")[:200],
                    invented_rels=list(schema_res.invented_rel_types)[:8],
                    invented_props=list(schema_res.invented_prop_keys)[:8],
                    question=question,
                    attempt=attempt,
                )
                st.last_error = last_error
                st.last_was_enum_filter_mismatch = True
                return ("continue", last_error)
            timing.update(token_ledger.totals_for_timing())
            return ("return", NLResult(
                answer=fail_closed_schema_answer(schema_res),
                sparql=cypher_raw,
                explanation=explanation,
                ontology=ontology,
                timing={
                    **timing,
                    "total_ms": round((time.time() - t0) * 1000, 1),
                    "attempts": attempt + 1,
                    "schema_valid_cypher_reject": 1.0,
                },
                token_usage=token_ledger.to_list(),
                query_confidence=cov_schema.confidence,
                query_confidence_reason=cov_schema.reason
                or schema_res.reason,
                clarification_prompt=cov_schema.clarification_prompt,
            ))

        cov = check_constraint_coverage(
            question,
            cypher_raw,
            params=params,
            template=gen.get("template"),
            dim_binds=dim_binds,
            populated_types=populated_types_for_coverage,
        )
        timing.update(cov.to_timing())
        if not cov.ok and cov.fail_closed:
            last_error = coverage_feedback(
                cov, previous_cypher=cypher_raw
            )
            if attempt < max_attempts - 1:
                last_was_enum_filter_mismatch = True
                timing["query_constraint_coverage_retry"] = 1.0
                if cov.empty_plan_types:
                    timing["query_zero_instance_type_retry"] = 1.0
                logger.info(
                    "query_constraint_coverage_retry",
                    reason=(cov.reason or "")[:200],
                    unbound=list(cov.unbound_tokens)[:8],
                    empty_plan_types=list(cov.empty_plan_types)[:8],
                    question=question,
                    attempt=attempt,
                )
                st.last_error = last_error
                st.last_was_enum_filter_mismatch = True
                return ("continue", last_error)
            timing.update(token_ledger.totals_for_timing())
            return ("return", NLResult(
                answer=fail_closed_answer(cov),
                sparql=cypher_raw,
                explanation=explanation,
                ontology=ontology,
                timing={
                    **timing,
                    "total_ms": round((time.time() - t0) * 1000, 1),
                    "attempts": attempt + 1,
                    "query_constraint_coverage_reject": 1.0,
                },
                token_usage=token_ledger.to_list(),
                query_confidence=cov.confidence,
                query_confidence_reason=cov.reason,
                clarification_prompt=cov.clarification_prompt,
            ))
        # Stash last good coverage for the success NLResult.
        last_gen["_coverage"] = cov

        st.last_gen = last_gen
        return ("ok", None)

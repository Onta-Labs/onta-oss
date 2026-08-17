"""NLQueryPipeline._ask_cypher orchestrator.

Product /ask is always LLM Cypher. Timing key is ``cypher_exec_ms`` (never a
Neptune exec label). Money-leaf hard-bind is unique-resolve only. Semantic
top-K must not hide THIS-KG populated types.
"""
from __future__ import annotations

import time

import structlog

from infona_client.graph.queries import parse_kg_graph_uri
from infona_client.graph.sparql_scope import CrossTenantQueryError, tenant_of_graph
from infona_client.models.query import NLResult
from infona_client.nlp.cypher_generate import records_to_bindings
from infona_client.nlp.cypher_scope import (
    CrossTenantCypherError,
    CypherScopeError,
    confine_generated_cypher,
    scrub_cypher_error,
)
from infona_client.nlp.pipeline_ask_state import AskCypherState
from infona_client.nlp.pipeline_helpers import _cypher_uses_forbidden_shapes
from infona_client.nlp.pipeline_llm import CEREBRAS_LENGTH_RECOVERY_TOKENS, EmptyLLMResponse
from infona_client.nlp.token_usage import TokenUsageLedger, pop_attached_usage, stage_for_attempt

logger = structlog.stdlib.get_logger("infona.nlp.pipeline")


class PipelineAskMixin:
    """Owns ``_ask_cypher``; stages live on sibling mixins."""

    async def _ask_cypher(
        self,
        question: str,
        *,
        graph_uri: str,
        data_graph: str,
        exclude_questions: list[str] | None = None,
        layer_graph_uris: list[str] | None = None,
        run_manifest: "object | None" = None,
    ) -> NLResult:
        """Neo4j /ask path with SPARQL-parity recovery mechanisms (ONTA-530).

        **Product rule:** user-facing NL→Cypher generation always uses the LLM
        (:meth:`_try_llm_cypher`). Deterministic fixtures
        (``try_deterministic_cypher``) are **not** consulted on this path — they
        remain for unit tests of template builders and non-ask helpers such as
        :meth:`select_entity_uris` (internal URI resolution only).

        Execution is always via GraphStore with session-forced ``tenant_id`` /
        ``kg`` — never trust model-supplied scope values.

        Cypher text is returned in :attr:`NLResult.sparql` for wire
        compatibility with existing clients (field name historical).
        """
        t0 = time.time()
        timing: dict[str, float | str] = {
            "model": f"{self._query_provider}:{self._query_model}",
            "query_language": "cypher",
            "graph_backend": "neo4j",
        }
        token_ledger = TokenUsageLedger()

        parsed = parse_kg_graph_uri(data_graph)
        if not parsed:
            tid = tenant_of_graph(data_graph) or ""
            kg = data_graph.rstrip("/").rsplit("/", 1)[-1] if data_graph else ""
            if not tid or not kg or kg == tid:
                return NLResult(
                    answer=(
                        "Could not answer: Neo4j /ask requires a per-KG instance "
                        "graph URI (…/graphs/{tenant}/kg/{kg})."
                    ),
                    sparql="",
                    explanation="",
                    timing={**timing, "total_ms": round((time.time() - t0) * 1000, 1)},
                    token_usage=token_ledger.to_list(),
                )
            tenant_id, kg_name = tid, kg
        else:
            tenant_id, kg_name = parsed

        store = self._graph_store
        if store is None:
            try:
                from infona_client.graph.store import get_graph_store

                store = get_graph_store()
            except Exception:
                store = None

        st = AskCypherState()
        st.question = question
        st.graph_uri = graph_uri
        st.data_graph = data_graph
        st.exclude_questions = exclude_questions
        st.layer_graph_uris = layer_graph_uris
        st.run_manifest = run_manifest
        st.t0 = t0
        st.timing = timing
        st.token_ledger = token_ledger
        st.tenant_id = tenant_id
        st.kg_name = kg_name
        st.store = store

        # ONTA-544: no-key / fixture-flag only. When a model is configured
        # this returns None and /ask stays always-LLM Cypher.
        from infona_client.nlp.ask_cached_plan import try_cached_plan_ask

        cached = await try_cached_plan_ask(self, st)
        if cached is not None:
            return cached

        await self._ask_cypher_load_ontology(st)
        early = await self._ask_cypher_ground(st)
        if early is not None:
            return early

        ontology = st.ontology
        ontology_source = st.ontology_source
        full_ontology_loaded = st.full_ontology_loaded
        examples_text = st.examples_text
        grounding_text = st.grounding_text
        alias_map = st.alias_map
        money_leaf_bound = st.money_leaf_bound
        money_cue_bound = st.money_cue_bound

        max_attempts = 3
        last_error = ""
        cypher = ""
        explanation = ""
        functions_needed: list[str] = []
        last_was_empty_query = False
        last_was_enum_filter_mismatch = False
        last_was_length_truncated = False
        length_recovery_stage = 0
        honest_empty_note = ""
        last_gen: dict = {}
        last_params: dict = {}

        from infona_client.graph.scope import GraphScope
        from infona_client.graph.store import GraphQueryError

        for attempt in range(max_attempts):
            honest_empty_note = ""
            try:
                gen_recovery: dict = {}
                if last_was_length_truncated:
                    length_recovery_stage += 1
                    if length_recovery_stage >= 2:
                        gen_recovery["prefer_fallback"] = True
                    else:
                        gen_recovery["max_completion_tokens"] = (
                            CEREBRAS_LENGTH_RECOVERY_TOKENS
                        )

                # Production NL→Cypher is always LLM (never fixture short-circuit).
                error_feedback = ""
                st.last_was_empty_query = last_was_empty_query
                st.full_ontology_loaded = full_ontology_loaded
                st.ontology = ontology
                st.ontology_source = ontology_source
                st.grounding_text = grounding_text
                st.dim_binds = st.dim_binds
                st.populated_types_for_coverage = st.populated_types_for_coverage
                if last_was_empty_query:
                    error_feedback = await self._ask_cypher_escalate_empty(
                        st, attempt=attempt
                    )
                    full_ontology_loaded = st.full_ontology_loaded
                    ontology = st.ontology
                    ontology_source = st.ontology_source
                    grounding_text = st.grounding_text
                elif last_was_enum_filter_mismatch:
                    error_feedback = last_error
                elif attempt > 0 and last_error:
                    error_feedback = (
                        f"The previous query failed with: {last_error}\n"
                        f"Query was: {cypher}\n"
                        "Please fix the Cypher and try again. Keep "
                        "$tenant_id / $kg parameters; do not hardcode scope."
                    )

                gen = await self._try_llm_cypher(
                    question,
                    ontology,
                    tenant_id=tenant_id,
                    kg_name=kg_name,
                    examples_text=examples_text,
                    error_feedback=error_feedback,
                    grounding_text=grounding_text,
                    **gen_recovery,
                )

                last_was_length_truncated = False
                last_was_enum_filter_mismatch = False
                last_was_empty_query = False

                if gen is None:
                    last_error = last_error or "no generator produced Cypher"
                    last_was_empty_query = True
                    continue

                usage_blob = pop_attached_usage(gen)
                if usage_blob is not None:
                    token_ledger.record(
                        stage=stage_for_attempt(attempt),
                        attempt=attempt,
                        model=str(usage_blob.get("model") or self._query_model or ""),
                        provider=str(
                            usage_blob.get("provider")
                            or self._query_provider
                            or ""
                        ),
                        prompt_tokens=usage_blob.get("prompt_tokens"),
                        completion_tokens=usage_blob.get("completion_tokens"),
                        total_tokens=usage_blob.get("total_tokens"),
                    )

                last_gen = gen
                cypher_raw = gen.get("cypher") or gen.get("sparql") or ""
                params = dict(gen.get("params") or {})
                # Hard-bind money leaf so "cost"/"price" cannot execute as bare
                # $cost_prop with wrong name → high-conf empty sum.
                if money_leaf_bound:
                    try:
                        from infona_client.nlp.ask_process_log import (
                            apply_money_leaf_params,
                            log_ask_event,
                        )

                        before = dict(params)
                        params = apply_money_leaf_params(
                            params,
                            money_leaf=money_leaf_bound,
                            money_cue=money_cue_bound,
                        )
                        timing["money_leaf_hard_bound"] = money_leaf_bound
                        log_ask_event(
                            "ask_gen_attempt",
                            attempt=attempt,
                            question=question,
                            tenant_id=tenant_id,
                            kg=kg_name,
                            cypher=cypher_raw,
                            params_before=before,
                            params_after=params,
                            explanation=explanation,
                            money_leaf_bound=money_leaf_bound,
                            template=gen.get("template"),
                        )
                    except Exception:
                        from infona_client.nlp.ask_process_log import (
                            apply_money_leaf_params,
                        )

                        params = apply_money_leaf_params(
                            params,
                            money_leaf=money_leaf_bound,
                            money_cue=money_cue_bound,
                        )
                        timing["money_leaf_hard_bound"] = money_leaf_bound
                else:
                    try:
                        from infona_client.nlp.ask_process_log import log_ask_event

                        log_ask_event(
                            "ask_gen_attempt",
                            attempt=attempt,
                            question=question,
                            tenant_id=tenant_id,
                            kg=kg_name,
                            cypher=cypher_raw,
                            params=params,
                            explanation=gen.get("explanation") or "",
                            template=gen.get("template"),
                        )
                    except Exception:
                        pass
                last_params = params
                explanation = gen.get("explanation") or explanation
                functions_needed = gen.get("functions_needed") or functions_needed

                if gen.get("stub") or gen.get("fixture"):
                    timing["cypher_stub"] = 1.0
                else:
                    timing["cypher_stub"] = 0.0

                if not str(cypher_raw).strip():
                    last_error = "Empty query"
                    last_was_empty_query = True
                    continue

                if alias_map:
                    cypher_raw = self._rewrite_cypher_alias_leaves(cypher_raw, alias_map)

                if store is None:
                    return NLResult(
                        answer=(
                            "Could not answer: Neo4j GraphStore is not configured "
                            "(set INFONA_GRAPH_BACKEND=neo4j and inject a store)."
                        ),
                        sparql=cypher_raw,
                        explanation=explanation,
                        ontology=ontology,
                        timing={
                            **timing,
                            "total_ms": round((time.time() - t0) * 1000, 1),
                        },
                        token_usage=token_ledger.to_list(),
                    )

                forbidden = _cypher_uses_forbidden_shapes(cypher_raw)
                if forbidden and not (gen.get("stub") or gen.get("fixture")):
                    last_error = forbidden
                    if attempt < max_attempts - 1:
                        last_was_enum_filter_mismatch = True
                        last_error = (
                            f"FORBIDDEN shape: {forbidden}\n"
                            f"Query was: {cypher_raw}\n"
                            "Rewrite using MATCH (e:Entity {tenant_id:$tenant_id, kg:$kg})"
                            "-[:INSTANCE_OF]->(c:Class) and OPTIONAL MATCH "
                            "(a:Assertion {subject_id:e.id})-[:PREDICATE]->(p:Property). "
                            "NEVER use HAS_ASSERTION, predicate_key, or Assertion.prop_key."
                        )
                        timing["cypher_forbidden_shape"] = 1.0
                        continue
                    timing.update(token_ledger.totals_for_timing())
                    return NLResult(
                        answer=f"Could not answer: generated Cypher {forbidden}",
                        sparql=cypher_raw,
                        explanation=explanation,
                        ontology=ontology,
                        timing={
                            **timing,
                            "total_ms": round((time.time() - t0) * 1000, 1),
                            "attempts": attempt + 1,
                            "cypher_forbidden_shape": 1.0,
                        },
                        token_usage=token_ledger.to_list(),
                    )

                st.gen = gen
                st.cypher_raw = cypher_raw
                st.params = params
                st.explanation = explanation
                st.last_gen = last_gen
                st.ontology = ontology
                gate = self._ask_cypher_validate_gen(
                    st, attempt=attempt, max_attempts=max_attempts, t0=t0
                )
                if gate[0] == "continue":
                    last_error = gate[1]
                    last_was_enum_filter_mismatch = True
                    last_gen = st.last_gen
                    continue
                if gate[0] == "return":
                    return gate[1]
                last_gen = st.last_gen

                try:
                    cypher, forced_params = confine_generated_cypher(
                        cypher_raw,
                        tenant_id=tenant_id,
                        kg=kg_name,
                        params=params,
                    )
                except CrossTenantCypherError:
                    raise
                except CypherScopeError as exc:
                    last_error = exc.detail
                    if attempt < max_attempts - 1 and not (
                        gen.get("stub") or gen.get("fixture")
                    ):
                        timing["cypher_scope_error"] = 1.0
                        continue
                    timing.update(token_ledger.totals_for_timing())
                    return NLResult(
                        answer=f"Could not answer: {exc.detail}",
                        sparql=cypher_raw,
                        explanation=explanation,
                        ontology=ontology,
                        timing={
                            **timing,
                            "total_ms": round((time.time() - t0) * 1000, 1),
                            "cypher_scope_error": 1.0,
                            "attempts": attempt + 1,
                        },
                        token_usage=token_ledger.to_list(),
                    )

                t_exec = time.time()
                try:
                    session = store.session(
                        GraphScope.for_instance(tenant_id, kg_name)
                    )
                    records, exec_path = await self._execute_confined_cypher(
                        session, gen, cypher, forced_params
                    )
                    timing["cypher_exec_path"] = exec_path
                    timing[
                        f"cypher_exec_ms{f'_retry{attempt}' if attempt > 0 else ''}"
                    ] = round((time.time() - t_exec) * 1000, 1)
                except GraphQueryError as exc:
                    scrubbed = scrub_cypher_error(str(exc))
                    last_error = scrubbed
                    timing[
                        f"cypher_exec_ms{f'_retry{attempt}' if attempt > 0 else ''}"
                    ] = round((time.time() - t_exec) * 1000, 1)
                    if attempt >= max_attempts - 1:
                        timing.update(token_ledger.totals_for_timing())
                        return NLResult(
                            answer=f"Could not answer: {scrubbed}",
                            sparql=cypher,
                            explanation=explanation,
                            ontology=ontology,
                            timing={
                                **timing,
                                "total_ms": round((time.time() - t0) * 1000, 1),
                                "attempts": attempt + 1,
                            },
                            token_usage=token_ledger.to_list(),
                        )
                    timing["cypher_retry"] = 1.0
                    continue

                variables, bindings = records_to_bindings(records)

                st.bindings = bindings
                st.cypher = cypher
                st.ontology = ontology
                st.ontology_source = ontology_source
                st.full_ontology_loaded = full_ontology_loaded
                st.forced_params = forced_params
                st.honest_empty_note = honest_empty_note
                st.last_error = last_error
                zr = await self._ask_cypher_zero_row(
                    st, attempt=attempt, max_attempts=max_attempts
                )
                ontology = st.ontology
                ontology_source = st.ontology_source
                full_ontology_loaded = st.full_ontology_loaded
                honest_empty_note = st.honest_empty_note
                last_error = st.last_error
                if zr == "continue":
                    last_was_enum_filter_mismatch = st.last_was_enum_filter_mismatch
                    continue

                st.cypher = cypher
                st.ontology = ontology
                st.explanation = explanation
                st.functions_needed = functions_needed
                st.honest_empty_note = honest_empty_note
                st.last_gen = last_gen
                st.last_params = last_params
                st.money_leaf_bound = money_leaf_bound
                st.variables = variables
                st.bindings = bindings
                st.forced_params = forced_params
                return await self._ask_cypher_finish(st, attempt=attempt, t0=t0)

            except (CrossTenantQueryError, CrossTenantCypherError):
                raise
            except EmptyLLMResponse as e:
                last_error = str(e)
                last_was_empty_query = True
                last_was_length_truncated = e.finish_reason == "length"
                logger.warning(
                    "ask_cypher_attempt_failed",
                    attempt=attempt,
                    error=last_error,
                    question=question,
                )
                continue
            except Exception as e:
                last_error = str(e)
                last_was_empty_query = not (cypher or "").strip()
                last_was_length_truncated = (
                    isinstance(e, EmptyLLMResponse) and e.finish_reason == "length"
                )
                logger.warning(
                    "ask_cypher_attempt_failed",
                    attempt=attempt,
                    error=last_error,
                    question=question,
                )
                continue

        timing["total_ms"] = round((time.time() - t0) * 1000, 1)
        timing["attempts"] = max_attempts
        timing.update(token_ledger.totals_for_timing())
        _final_conf = str(timing.get("query_confidence") or "")
        _final_reason = str(timing.get("query_confidence_reason") or "")
        _final_clarify = str(timing.get("clarification_prompt") or "")
        return NLResult(
            answer=(
                f"Could not answer after {max_attempts} attempts. "
                f"Last error: {last_error}"
            ),
            sparql=cypher,
            explanation=explanation,
            ontology=ontology,
            timing=timing,
            token_usage=token_ledger.to_list(),
            query_confidence=_final_conf,
            query_confidence_reason=_final_reason,
            clarification_prompt=_final_clarify,
        )

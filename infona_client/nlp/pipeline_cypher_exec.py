"""Confine and execute Cypher. Timing key is cypher_exec_ms — never neptune_exec_ms."""
from __future__ import annotations

import json
import os
import re
import time
from functools import partial
from typing import Any, Iterable

import httpx
import structlog

from infona_client.graph.iri import ENTITY_URI_PREFIX, IRI_BASE, TYPE_URI_PREFIX
from infona_client.graph.parser import parse_sparql_results, unbound_projection_vars
from infona_client.graph.queries import parse_kg_graph_uri, skip_invalid_type_name
from infona_client.graph.sparql_scope import (
    CrossTenantQueryError,
    confine_generated_query,
    tenant_of_graph,
)
from infona_client.models.query import NLResult
from infona_client.nlp.cypher_generate import (
    neo4j_ask_enabled,
    ontology_from_graph_store,
    records_to_bindings,
)
from infona_client.nlp.cypher_scope import (
    CrossTenantCypherError,
    CypherScopeError,
    confine_generated_cypher,
    scrub_cypher_error,
)
from infona_client.nlp.pipeline_helpers import (
    ACTIVE_TYPE_PROBE_CHUNK,
    ANSWER_ROW_CAP,
    MAX_ACTIVE_TYPE_PROBE_CONCURRENCY,
    MAX_ACTIVE_TYPE_PROBE_URIS,
    MAX_ENUM_DISCOVERY_CONCURRENCY,
    ONTOLOGY_CACHE_TTL,
    ONTOLOGY_EMPTY,
    MAX_REPORTED_REL_TYPES,
    ONTOLOGY_FETCH_ERROR,
    RDF_TYPE_URI,
    REL_TRAVERSAL_FEEDBACK,
    _GEO_WKT_URI,
    _active_types_cache,
    _active_types_cache_key,
    _alias_cache,
    _cypher_invented_rel_types,
    _cypher_uses_forbidden_shapes,
    _drop_internal_predicate_rows,
    _is_interpolatable_iri,
    _missing_template_params,
    _neptune_safe_duration,
    _ontology_cache,
    _parse_iso_dt,
    _parse_point_wkt,
    _prefer_attr_name_over_rdfs_label,
    _sanitize_sparql_literal,
    _store_active_types,
)
from infona_client.nlp.pipeline_llm import (
    CEREBRAS_LENGTH_RECOVERY_TOKENS,
    OPENROUTER_BASE,
    OPENROUTER_QUERY_TIMEOUT_S,
    OPENROUTER_REASONING_MAX_TOKENS,
    EmptyLLMResponse,
    _is_reasoning_query_model,
    _openrouter_base,
    _parse_sparql_gen_json,
    _require_message_content,
    get_embedding_service,
)
from infona_client.nlp.prompts import (
    CYPHER_GENERATION_SYSTEM,
    SPARQL_GENERATION_SYSTEM,
    build_cypher_generation_prompt,
    build_generation_prompt,
)
from infona_client.nlp.validator import normalize_sparql, validate_sparql
from infona_client.nlp.token_usage import (
    STAGE_REPHRASE,
    TokenUsageLedger,
    attach_usage,
    pop_attached_usage,
    stage_for_attempt,
)
from infona_client.offline import assert_online_url
from infona_client.resolver.llm_router import model_chain
from infona_client.spatiotemporal.routing import (
    SPATIAL_INTENT_SCHEMA,
    SPATIAL_INTENT_SYSTEM,
    filter_by_type,
    format_spatial_answer,
    looks_spatial,
    parse_spatial_intent,
)

logger = structlog.stdlib.get_logger("infona.nlp.pipeline")


def _cypher_is_assertion_shaped(cypher: str) -> bool:
    """True when free-form Cypher is an Assertion SUBJECT/PREDICATE plan.

    Template rescue stays for invented typed edges (``[:lead_sponsor]``). A
    schema-valid Assertion-shaped body must still ``execute_read`` so its
    ``RETURN`` aliases (person_name, date, …) are not discarded.
    """
    c = cypher or ""
    if ":Assertion" not in c:
        return False
    if "[:SUBJECT]" not in c or "[:PREDICATE]" not in c:
        return False
    return "[:OBJECT]" in c or "literal_value" in c


class PipelineCypherExecMixin:
    # ------------------------------------------------ generated-query confinement

    @staticmethod
    def _confine_generated(
        sparql: str, data_graph: str, layer_graph_uris: list[str] | None = None
    ) -> str:
        """Confine LLM-generated SPARQL to this request's graphs (ONTA-424).

        The single choke point every generated query passes through before it
        reaches Neptune. Returns the query to run, which is either ``sparql``
        unchanged or a repaired copy carrying ``FROM <data_graph>``; raises
        :class:`CrossTenantQueryError` when the generated text reaches outside
        the request's scope.

        The tenant is derived from ``data_graph`` rather than passed in, and that
        is deliberate. ``data_graph`` is resolved by the route from the
        AUTHENTICATED tenant (``/ask`` and the agent's ``QueryCapability`` both
        build it with ``kg_graph_uri(tenant.tenant_id, ...)``), so it is already
        the trusted boundary; deriving it means a future caller of ``ask()``
        cannot forget to thread a tenant and silently lose the guard. A
        ``data_graph`` outside the platform namespace (a self-hosted store) yields
        no tenant, and confinement then falls back to the graphs the request
        itself named, which is strictly tighter than tenant ownership.
        """
        return confine_generated_query(
            sparql,
            default_graphs=[data_graph],
            tenant_id=tenant_of_graph(data_graph),
            allowed_graphs=layer_graph_uris or (),
        )
    @staticmethod
    def _rewrite_cypher_alias_leaves(cypher: str, alias_map: dict[str, str]) -> str:
        """Rewrite aliased attribute leaf names inside Cypher property access.

        **Rewrite-only when a map is present.** An empty / missing map is a
        no-op (callers already gate on ``if alias_map:``); there is no ontology
        lookup, no registration, and no param mutation here — only textual leaf
        renames on the Cypher string the model (or fixture) produced.

        ``alias_map`` is old_uri → new_uri (from ``fetch_alias_map``). We rewrite
        only the leaf segment of ``attrs/<leaf>`` so ``e.phone_num`` and
        ``p.name = 'phone_num'`` pick up renames. Empty map ⇒ unchanged.
        """
        if not alias_map or not cypher:
            return cypher
        leaf_map: dict[str, str] = {}
        for old, new in alias_map.items():
            old_leaf = old.rsplit("/", 1)[-1]
            new_leaf = new.rsplit("/", 1)[-1]
            if old_leaf and new_leaf and old_leaf != new_leaf:
                leaf_map[old_leaf] = new_leaf
        if not leaf_map:
            return cypher
        # Longer leaves first so phone_num wins over phone.
        for old_leaf in sorted(leaf_map, key=len, reverse=True):
            new_leaf = leaf_map[old_leaf]
            cypher = re.sub(
                rf"(?<![A-Za-z0-9_]){re.escape(old_leaf)}(?![A-Za-z0-9_])",
                new_leaf,
                cypher,
            )
        return cypher

    async def _execute_confined_cypher(
        self,
        session: Any,
        gen: dict,
        cypher: str,
        forced_params: dict,
    ) -> tuple[list, str]:
        """Run confined Cypher: template rescue for invented rels, else execute_read.

        Session already forces tenant/kg; ``forced_params`` must come from
        :func:`confine_generated_cypher`. Never trusts model tenant/kg values.

        Allowlisted ``template`` still supersedes invented typed edges
        (``[:lead_sponsor]`` → template). Schema-valid Assertion-shaped
        Cypher with no invented rel types prefers ``execute_read`` only
        when ``gen.template`` is the generic ``related_entities`` dump so
        generated ``RETURN`` columns are not dropped. Constrained helpers
        (``related_entity_name_filter``, ``literal_values``, …) still
        supersede.
        """
        from infona_client.graph.schema_bootstrap import TEMPLATES
        from infona_client.graph.store import GraphQueryError

        template = gen.get("template")
        is_fixture = bool(gen.get("stub") or gen.get("fixture"))
        invented = _cypher_invented_rel_types(cypher)
        # Unconstrained related_entities drops generated RETURN aliases;
        # constrained helpers (name filter, literal eq/compare, …) must run.
        prefer_generated = (
            (not is_fixture)
            and (not invented)
            and _cypher_is_assertion_shaped(cypher)
            and template == "related_entities"
        )
        if (
            template
            and isinstance(template, str)
            and template in TEMPLATES
            and not TEMPLATES[template].writing
            and not prefer_generated
        ):
            tmpl_params = {
                k: v
                for k, v in forced_params.items()
                if k not in ("tenant_id", "kg")
            }
            for k, v in (gen.get("params") or {}).items():
                if k not in ("tenant_id", "kg") and k not in tmpl_params:
                    tmpl_params[k] = v
            cypher_text = TEMPLATES[template].cypher or ""
            if "$limit" in cypher_text and tmpl_params.get("limit") is None:
                tmpl_params["limit"] = 25
            if "$after_id" in cypher_text and "after_id" not in tmpl_params:
                tmpl_params["after_id"] = None
            missing = _missing_template_params(cypher_text, tmpl_params)
            if not missing:
                records = await session.execute_template(template, tmpl_params)
                return records, f"template:{template}"
            if is_fixture:
                raise GraphQueryError(
                    f"Fixture template {template!r} missing params: {sorted(missing)}"
                )

        if is_fixture and "count(*)" in cypher:
            if "primary_type" in forced_params:
                records = await session.execute_template(
                    "entity_count_by_type",
                    {"primary_type": forced_params["primary_type"]},
                )
                return records, "template:entity_count_by_type"
            records = await session.execute_template("entity_count_total", {})
            return records, "template:entity_count_total"

        # Last gate before free-form Cypher actually runs. Deliberately HERE and
        # not next to `_cypher_uses_forbidden_shapes` in the retry loop: an
        # allowlisted template above still rescues invented typed edges. The
        # invented check is skipped when that template already ran.
        if invented and not is_fixture:
            shown = invented[:MAX_REPORTED_REL_TYPES]
            more = "" if len(invented) == len(shown) else ", …"
            raise GraphQueryError(
                "generated Cypher traverses relationship type(s) "
                f"{', '.join(shown)}{more} that cannot exist. "
                + REL_TRAVERSAL_FEEDBACK
            )

        records = await session.execute_read(cypher, forced_params)
        return records, "execute_read"

    async def _try_llm_cypher(
        self,
        question: str,
        ontology: str,
        *,
        tenant_id: str,
        kg_name: str,
        examples_text: str = "",
        error_feedback: str = "",
        grounding_text: str = "",
        max_completion_tokens: int | None = None,
        prefer_fallback: bool = False,
    ) -> dict | None:
        """Best-effort LLM Cypher generation, with provider FAILOVER.

        Returns ``None`` without API keys. Re-raises :class:`EmptyLLMResponse`
        so the retry loop can apply length-truncation recovery (ONTA-530).

        Every OTHER generator failure is per-provider, not terminal: the
        configured providers are tried in preference order and only an
        all-providers-failed run returns ``None``. This used to wrap the whole
        chain in one ``try``, so the first provider raising made the later
        branches unreachable — when Cerebras started 400'ing our structured-output
        schema, every ``/ask`` and ``/agent`` question died with "no generator
        produced Cypher" even though OpenRouter and Anthropic were configured and
        healthy. A vendor tightening a validator must degrade to the next
        provider, not black out the feature.

        ``grounding_text`` is optional structured ontology-subgraph context
        (from :func:`~infona_client.nlp.ontology_subgraph_match.ground_ask_plan`)
        injected into the prompt — never a fixture short-circuit.
        """
        if not (
            self._openrouter_key
            or self._cerebras_key
            or getattr(self, "anthropic", None)
        ):
            return None
        if not self._openrouter_key and not self._cerebras_key:
            try:
                key = getattr(self.anthropic, "api_key", None) or ""
            except Exception:
                key = ""
            if not key:
                return None

        prompt = build_cypher_generation_prompt(
            question,
            ontology,
            tenant_id=tenant_id,
            kg_name=kg_name,
            examples_text=examples_text,
            error_feedback=error_feedback,
            grounding_text=grounding_text,
        )
        attempts = self._cypher_generator_chain(
            prompt,
            max_completion_tokens=max_completion_tokens,
            prefer_fallback=prefer_fallback,
        )
        for index, (provider, call) in enumerate(attempts):
            try:
                return await call()
            except EmptyLLMResponse:
                # Length truncation is RECOVERABLE by the retry loop (bigger
                # budget, then prefer_fallback) — it owns the escalation, so do
                # not silently burn a different provider here.
                raise
            except Exception:
                logger.warning(
                    "cypher_llm_generation_failed",
                    provider=provider,
                    attempt=index + 1,
                    of=len(attempts),
                    exc_info=True,
                )
        if attempts:
            logger.warning(
                "cypher_llm_all_providers_failed",
                providers=[provider for provider, _ in attempts],
            )
        return None

    def _cypher_generator_chain(
        self,
        prompt: str,
        *,
        max_completion_tokens: int | None = None,
        prefer_fallback: bool = False,
    ) -> list[tuple[str, Any]]:
        """Ordered ``(provider_label, zero-arg coroutine factory)`` failover chain.

        Preference order is unchanged from the single-shot version — configured
        provider first, then OpenRouter, then Cerebras, then Anthropic — but the
        later entries are now REACHED when an earlier one raises instead of being
        dead code behind the first ``return``. Each provider appears at most once.
        """
        # Happy path: do NOT pass max_completion_tokens so the call is
        # byte-identical when no length recovery is in play (tests pin this).
        cerebras_kw: dict[str, Any] = {}
        if max_completion_tokens is not None:
            cerebras_kw["max_completion_tokens"] = max_completion_tokens

        def cerebras() -> tuple[str, Any]:
            return (
                "cerebras",
                partial(self._generate_cypher_via_cerebras, prompt, **cerebras_kw),
            )

        def openrouter() -> tuple[str, Any]:
            return ("openrouter", partial(self._generate_cypher_via_openrouter, prompt))

        def anthropic() -> tuple[str, Any]:
            return ("anthropic", partial(self._generate_cypher_via_anthropic, prompt))

        anthropic_ready = self._cypher_anthropic_ready()
        chain: list[tuple[str, Any]] = []
        if prefer_fallback:
            # Tier-2 length recovery: leave the *reasoning* model for a
            # non-reasoning OpenRouter (or Anthropic) path so think-budget
            # exhaustion does not loop forever on gpt-oss. Distinct from the
            # plain "openrouter" rung below — different model.
            if self._openrouter_key:
                chain.append(
                    (
                        "openrouter(non-reasoning)",
                        partial(
                            self._generate_cypher_via_openrouter,
                            prompt,
                            prefer_non_reasoning=True,
                        ),
                    )
                )
            if anthropic_ready:
                chain.append(anthropic())
        if self._query_provider == "cerebras" and self._cerebras_key:
            chain.append(cerebras())
        if self._openrouter_key:
            chain.append(openrouter())
        if self._cerebras_key:
            chain.append(cerebras())
        if anthropic_ready:
            chain.append(anthropic())

        deduped: list[tuple[str, Any]] = []
        seen: set[str] = set()
        for provider, call in chain:
            if provider in seen:
                continue
            seen.add(provider)
            deduped.append((provider, call))
        return deduped

    def _cypher_anthropic_ready(self) -> bool:
        """True when the Anthropic client is worth a failover attempt.

        Skips only the provably-unusable case (a client carrying an EMPTY string
        key), so a real client, or a test double whose ``api_key`` is not a
        string, still gets its rung on the ladder.
        """
        client = getattr(self, "anthropic", None)
        if client is None:
            return False
        try:
            key = getattr(client, "api_key", None)
        except Exception:
            return True
        return bool(key.strip()) if isinstance(key, str) else True

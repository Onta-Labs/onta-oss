"""NL → Cypher query pipeline orchestrator.

Public type: :class:`NLQueryPipeline`. Implementation lives in sibling
``pipeline_*.py`` modules. Every previously importable name is re-exported here.

Invariants other agents must not break:
- Product /ask is always LLM Cypher (never fixture short-circuit).
- Money-leaf hard-bind is unique-resolve only.
- Never drop THIS-KG populated types from planning context.
- Exec timing key is ``cypher_exec_ms`` (not a Neptune exec label).
- No persona gold in this path.
"""
from __future__ import annotations

import os
from typing import Any

import anthropic
import httpx  # noqa: F401 — tests patch pipeline.httpx.AsyncClient
import structlog

from infona_client.graph.client import NeptuneClient
from infona_client.graph.iri import ENTITY_URI_PREFIX, IRI_BASE, TYPE_URI_PREFIX  # noqa: F401
from infona_client.graph.parser import parse_sparql_results, unbound_projection_vars  # noqa: F401
from infona_client.graph.queries import parse_kg_graph_uri, skip_invalid_type_name  # noqa: F401
from infona_client.graph.sparql_scope import (  # noqa: F401
    CrossTenantQueryError,
    confine_generated_query,
    tenant_of_graph,
)
from infona_client.models.query import NLResult
from infona_client.nlp.cypher_generate import (  # noqa: F401
    neo4j_ask_enabled,
    ontology_from_graph_store,
    records_to_bindings,
    try_deterministic_cypher,
)
from infona_client.nlp.cypher_scope import (  # noqa: F401
    CrossTenantCypherError,
    CypherScopeError,
    confine_generated_cypher,
    scrub_cypher_error,
)
from infona_client.nlp.pipeline_helpers import (  # noqa: F401
    ACTIVE_TYPE_PROBE_CHUNK,
    ANSWER_ROW_CAP,
    MAX_ACTIVE_TYPE_PROBE_CONCURRENCY,
    MAX_ACTIVE_TYPE_PROBE_URIS,
    MAX_ENUM_DISCOVERY_CONCURRENCY,
    ONTOLOGY_CACHE_TTL,
    ONTOLOGY_EMPTY,
    ONTOLOGY_FETCH_ERROR,
    RDF_TYPE_URI,
    _DURATION_DATATYPE_RE,
    _ENTITY_URI_PREFIX,
    _GEO_WKT_URI,
    _IRIREF_FORBIDDEN,
    _POINT_RE,
    _RDFS_LABEL_IRI,
    _TEMPLATE_PARAM_RE,
    _active_types_cache,
    _active_types_cache_key,
    _alias_cache,
    _cypher_uses_forbidden_shapes,
    _drop_internal_predicate_rows,
    _is_interpolatable_iri,
    _missing_template_params,
    _neptune_safe_duration,
    _ontology_cache,
    _parse_iso_dt,
    _parse_point_wkt,
    _prefer_attr_name_over_rdfs_label,
    _row_has_entity_object,
    _sanitize_sparql_literal,
    _store_active_types,
)
from infona_client.nlp.pipeline_llm import (  # noqa: F401
    CEREBRAS_LENGTH_RECOVERY_TOKENS,
    DEFAULT_QUERY_MODEL,
    DEFAULT_QUERY_PROVIDER,
    OPENROUTER_BASE,
    OPENROUTER_QUERY_TIMEOUT_S,
    OPENROUTER_REASONING_MAX_TOKENS,
    EmptyLLMResponse,
    _default_query_model,
    _default_query_provider,
    _is_reasoning_query_model,
    _openrouter_base,
    _parse_sparql_gen_json,
    _require_message_content,
    _resolve_openrouter_api_key,
    _embedding_service,
    _salvage_sparql_field,
    _strip_code_fences,
    get_embedding_service,
    reset_embedding_service_for_tests,
)
from infona_client.nlp.prompts import (  # noqa: F401
    CYPHER_GENERATION_SYSTEM,
    SPARQL_GENERATION_SYSTEM,
    build_cypher_generation_prompt,
    build_generation_prompt,
)
from infona_client.nlp.token_usage import (  # noqa: F401
    STAGE_REPHRASE,
    TokenUsageLedger,
    attach_usage,
    pop_attached_usage,
    stage_for_attempt,
)
from infona_client.nlp.validator import normalize_sparql, validate_sparql  # noqa: F401
from infona_client.offline import assert_online_url  # noqa: F401
from infona_client.pipeline.manifest import RunCoverage, RunManifest
from infona_client.resolver.llm_router import model_chain  # noqa: F401
from infona_client.spatiotemporal.routing import (  # noqa: F401
    SPATIAL_INTENT_SCHEMA,
    SPATIAL_INTENT_SYSTEM,
    filter_by_type,
    format_spatial_answer,
    looks_spatial,
    parse_spatial_intent,
)

logger = structlog.stdlib.get_logger("infona.nlp.pipeline")


class SparqlAskPathRetired(RuntimeError):
    """Raised when ``NLQueryPipeline.ask(..., use_cypher=False)`` is requested.

    The Neptune SPARQL NL path was removed with the cutover (ONTA-534). Product
    ``/ask`` always takes Cypher via :meth:`_ask_cypher`. The exception exists so
    eval/archive callers that still pass ``use_cypher=False`` fail closed with a
    clear message instead of POSTing SPARQL at a decommissioned endpoint.
    """


# Mixins after helpers so ``import pipeline as _pl`` inside them sees bindings.
from infona_client.nlp.pipeline_active_types import PipelineActiveTypesMixin  # noqa: E402
from infona_client.nlp.pipeline_ask import PipelineAskMixin  # noqa: E402
from infona_client.nlp.pipeline_ask_escalate import PipelineAskEscalateMixin  # noqa: E402
from infona_client.nlp.pipeline_ask_finish import PipelineAskFinishMixin  # noqa: E402
from infona_client.nlp.pipeline_ask_ground import PipelineAskGroundMixin  # noqa: E402
from infona_client.nlp.pipeline_ask_prep import PipelineAskPrepMixin  # noqa: E402
from infona_client.nlp.pipeline_ask_validate import PipelineAskValidateMixin  # noqa: E402
from infona_client.nlp.pipeline_ask_zero import PipelineAskZeroMixin  # noqa: E402
from infona_client.nlp.pipeline_cypher_exec import PipelineCypherExecMixin  # noqa: E402
from infona_client.nlp.pipeline_cypher_gen import PipelineCypherGenMixin  # noqa: E402
from infona_client.nlp.pipeline_entities import PipelineEntitiesMixin  # noqa: E402
from infona_client.nlp.pipeline_format import PipelineFormatMixin  # noqa: E402
from infona_client.nlp.pipeline_lookup import PipelineLookupMixin  # noqa: E402
from infona_client.nlp.pipeline_ontology import PipelineOntologyMixin  # noqa: E402
from infona_client.nlp.pipeline_ontology_fallback import (  # noqa: E402
    PipelineOntologyFallbackMixin,
)
from infona_client.nlp.pipeline_sparql_fix import PipelineSparqlFixMixin  # noqa: E402
from infona_client.nlp.pipeline_sparql_gen import PipelineSparqlGenMixin  # noqa: E402
from infona_client.nlp.pipeline_spatial import PipelineSpatialMixin  # noqa: E402


class NLQueryPipeline(
    PipelineAskMixin,
    PipelineAskPrepMixin,
    PipelineAskGroundMixin,
    PipelineAskEscalateMixin,
    PipelineAskValidateMixin,
    PipelineAskZeroMixin,
    PipelineAskFinishMixin,
    PipelineCypherExecMixin,
    PipelineCypherGenMixin,
    PipelineFormatMixin,
    PipelineLookupMixin,
    PipelineSpatialMixin,
    PipelineEntitiesMixin,
    PipelineActiveTypesMixin,
    PipelineOntologyMixin,
    PipelineOntologyFallbackMixin,
    PipelineSparqlFixMixin,
    PipelineSparqlGenMixin,
):
    """Natural-language question → confined Cypher → ``NLResult``."""

    def __init__(
        self,
        neptune: NeptuneClient,
        anthropic_key: str,
        *,
        graph_store: "object | None" = None,
    ):
        self.neptune = neptune
        self.anthropic = anthropic.AsyncAnthropic(api_key=anthropic_key)
        # Optional GraphStore for the Neo4j /ask path (E6). When None, the
        # Cypher path uses :func:`get_graph_store` under INFONA_GRAPH_BACKEND=neo4j.
        self._graph_store = graph_store
        from infona_client.config import settings
        self._openrouter_key = settings.openrouter_api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self._cerebras_key = os.environ.get("CEREBRAS_API_KEY", getattr(settings, "cerebras_api_key", ""))
        # Re-resolve at construct time so env set after import (tests, uvicorn
        # workers that load .env late) still get the smart OpenRouter default.
        self._query_provider = _default_query_provider()
        self._query_model = _default_query_model(self._query_provider)
        # Refresh base URL live so INFONA_LLM_BASE_URL is honored without reimport.
        import infona_client.nlp.pipeline_llm as _llm

        global OPENROUTER_BASE
        OPENROUTER_BASE = _openrouter_base()
        _llm.OPENROUTER_BASE = OPENROUTER_BASE
        # Attribute aliases (ADR 0002 §7): resolve renamed attribute IRIs in
        # generated SPARQL. Default OFF so the default call pattern
        # stays byte-identical (same gating pattern as INFONA_ER_ENABLED).
        self._aliases_enabled = os.environ.get("INFONA_ALIASES_ENABLED", "0") == "1"
        # Spatio-temporal read routing (ONTA-157 Phase 2 → ONTA-249): a
        # geo/proximity question is answered directly from the secondary index.
        # Defensively gated: the fast path returns None whenever the question
        # isn't spatial. Set INFONA_SPATIAL_ROUTING_ENABLED=0 to force it off.
        self._spatial_routing_enabled = (
            os.environ.get("INFONA_SPATIAL_ROUTING_ENABLED", "1") != "0"
        )
        # Honest-answer per-fact metadata (ONTA-280, P7). Default OFF.
        self._answer_citations_enabled = (
            os.environ.get("INFONA_ANSWER_CITATIONS_ENABLED", "0") == "1"
        )

    async def ask(
        self,
        question: str,
        graph_uri: str,
        instance_graph: str | None = None,
        exclude_questions: list[str] | None = None,
        layer_graph_uris: list[str] | None = None,
        run_manifest: "RunManifest | RunCoverage | None" = None,
        *,
        use_cypher: bool | None = None,
        conversation: list | None = None,
    ) -> NLResult:
        """Answer a natural-language question over the graph.

        layer_graph_uris (ADR 0002 §1, COG-37, opt-in): a LayerStack's
        visible_graph_uris(). Generated queries are graph-scoped (FROM the
        data graph), so without this the subclass-closure path can't see
        subClassOf edges living in other layer graphs; when provided, each
        generated query gains FROM clauses for every visible layer. When
        None (the default), behavior is exactly as before.

        run_manifest (A9, ONTA-374, opt-in): the run's :class:`RunManifest`
        (or its already-computed :class:`RunCoverage`). When threaded, the
        answer's coverage caveat composes the REAL A9 "answered from N of M
        items" fragment (the A9→A7 honest-answers contract) instead of the
        stale-count-only caveat. When None (the default — no caller threads one
        today), the answer + caveat are byte-identical to the prior behavior.

        use_cypher (ONTA-534): Cypher is the only product NL path. Default /
        ``True`` runs :meth:`_ask_cypher` via GraphStore. Explicit
        ``use_cypher=False`` raises :class:`SparqlAskPathRetired` — the Neptune
        SPARQL generator is no longer reachable from ``ask`` (ONTA-534).
        """
        # Ontology is always fetched from the base tenant graph for embeddings;
        # when layer_graph_uris is set (production ask route, ONTA-397) the full
        # fetch also unions visible global layers with shadowing so Public types
        # are visible to the planner. Instance data may be in a different graph.
        data_graph = instance_graph or graph_uri

        # ONTA-534: Cypher is the only NL path. ``use_cypher=False`` used to
        # force the SPARQL generator for eval/archive harnesses; that
        # store is gone, so the SPARQL branch is retired fail-closed rather
        # than quietly POSTing at a dead endpoint. The generator helpers
        # (``_generate_sparql`` etc.) remain for unit tests that call them
        # directly and for residual inventory. Do not delete NeptuneClient
        # in drive-by cleanup.
        if neo4j_ask_enabled(explicit=use_cypher):
            return await self._ask_cypher(
                question,
                graph_uri=graph_uri,
                data_graph=data_graph,
                exclude_questions=exclude_questions,
                layer_graph_uris=layer_graph_uris,
                run_manifest=run_manifest,
                conversation=conversation,
            )

        raise SparqlAskPathRetired(
            "NL→SPARQL /ask was retired with the Neptune cutover (ONTA-534). "
            "Neo4j Cypher is the only product query language; pass use_cypher=True "
            "or omit it (default). explicit use_cypher=False is no longer supported."
        )

    async def select_entity_uris(
        self,
        description: str,
        type_name: str,
        graph_uri: str,
        instance_graph: str | None = None,
        limit: int | None = None,
    ) -> list[str]:
        """Resolve an NL subset description to the IRIs of ``type_name`` entities.

        Turns a ranked/specific subset — e.g. "the 5 brokers with the most
        property listings" — into the concrete entity IRIs it names, so a caller
        (the agent's enrich planner) can enrich exactly those via ``entity_uris``
        instead of the whole type.

        **ONTA-534:** the NL→SPARQL execution path is retired. This method no
        longer POSTs SPARQL at a decommissioned endpoint (hang / silent-empty
        risk under Neo4j). When a GraphStore is available it runs a Cypher
        projection of the same subset question; otherwise it raises
        :class:`SparqlAskPathRetired`. Callers that treat any failure as ``[]``
        (e.g. enrich subset resolution) keep fail-closed semantics without
        enriching the whole type by accident.
        """
        data_graph = instance_graph or graph_uri
        store = self._graph_store
        if store is None:
            try:
                from infona_client.graph.store import get_graph_store

                store = get_graph_store()
            except Exception:
                store = None

        if store is None:
            raise SparqlAskPathRetired(
                "select_entity_uris NL→SPARQL was retired with the Neptune "
                "cutover (ONTA-534). Configure a GraphStore (Neo4j / Memory) "
                "for Cypher subset resolution, or pass entity_uris explicitly."
            )

        from infona_client.graph.queries import parse_kg_graph_uri
        from infona_client.graph.store import GraphScope
        from infona_client.nlp.cypher_generate import (
            ontology_from_graph_store,
            records_to_bindings,
            try_deterministic_cypher,
        )

        parsed = parse_kg_graph_uri(data_graph)
        if parsed:
            tenant_id, kg_name = parsed
        else:
            tenant_id = tenant_of_graph(data_graph) or ""
            kg_name = data_graph.rstrip("/").rsplit("/", 1)[-1] if data_graph else ""
        if not tenant_id or not kg_name:
            logger.warning(
                "select_entity_uris_bad_graph",
                data_graph=data_graph,
            )
            return []

        # 1) Deterministic Cypher fixtures for *internal* URI resolution only
        # (not user-facing /ask — that path is always LLM). Prefer a template
        # when the subset description matches a list/filter/hop shape.
        try:
            ontology, type_names = await ontology_from_graph_store(
                store, tenant_id=tenant_id, kg=kg_name
            )
            gen = try_deterministic_cypher(
                f"list {type_name}: {description}",
                ontology or "",
                type_names=type_names or [type_name],
            )
            if gen and gen.get("cypher"):
                params = dict(gen.get("params") or {})
                if limit is not None and "limit" in params:
                    params["limit"] = min(int(params["limit"]), int(limit))
                elif limit is not None:
                    params["limit"] = int(limit)
                cypher, forced = confine_generated_cypher(
                    gen["cypher"],
                    tenant_id=tenant_id,
                    kg=kg_name,
                    params=params,
                )
                session = store.session(GraphScope.for_instance(tenant_id, kg_name))
                records, _path = await self._execute_confined_cypher(
                    session, gen, cypher, forced
                )
                _vars, bindings = records_to_bindings(records)
                uris = self._entity_uris_from_bindings(bindings, limit)
                if uris:
                    return uris
        except Exception:
            logger.warning("select_entity_uris_deterministic_failed", exc_info=True)

        # 2) Full Cypher NL path — extract IRIs from the answer when present.
        cap = f" Return at most {int(limit)} rows." if limit else ""
        question = (
            f"Return ONLY the entity id/IRI of each {type_name} entity in this set: "
            f"{description}. Project a single identifier column (id or uri) for "
            f"each {type_name}. Apply any ranking/ordering and limit the set "
            f"describes; do not aggregate the id away or replace it with a label only."
            f"{cap}"
        )
        try:
            result = await self._ask_cypher(
                question,
                graph_uri=graph_uri,
                data_graph=data_graph,
            )
            uris = self._entity_uris_from_answer_text(result.answer, limit)
            if uris:
                return uris
        except Exception:
            logger.warning("select_entity_uris_cypher_failed", exc_info=True)

        # Fail closed: never hit residual SPARQL / dead HTTP.
        logger.warning(
            "select_entity_uris_unresolved",
            type_name=type_name,
            description=(description or "")[:120],
        )
        return []

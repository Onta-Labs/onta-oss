"""Entity-IRI extraction helpers for select_entity_uris."""
from __future__ import annotations

import json
import os
import re
import time
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
    ONTOLOGY_FETCH_ERROR,
    RDF_TYPE_URI,
    _GEO_WKT_URI,
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


class PipelineEntitiesMixin:
    @staticmethod
    def _entity_uris_from_answer_text(
        answer: str, limit: int | None = None
    ) -> list[str]:
        """Best-effort scrape of entity IRIs from an NL answer string."""
        if not answer:
            return []
        found = re.findall(r"https?://[^\s\"'<>]+", answer)
        out: list[str] = []
        seen: set[str] = set()
        for u in found:
            u = u.rstrip(".,);]")
            if u in seen:
                continue
            seen.add(u)
            out.append(u)
            if limit is not None and len(out) >= int(limit):
                break
        return out

    @staticmethod
    def _entity_uris_from_bindings(
        bindings: list[dict], limit: int | None = None
    ) -> list[str]:
        """Pull entity IRIs out of result bindings, order-preserving and deduped.

        Prefers the ``?uri`` / ``id`` column the resolver prompt asks for; if a
        row lacks it, falls back to the first http(s)-IRI value in that row.
        Caps at ``limit`` when given.
        """
        out: list[str] = []
        seen: set[str] = set()

        def _is_iri(v: object) -> bool:
            return isinstance(v, str) and v.startswith(("http://", "https://"))

        for row in bindings:
            val = row.get("uri") or row.get("id") or row.get("entity_id")
            if not isinstance(val, str) or not val:
                val = next((v for v in row.values() if _is_iri(v)), None)
            if not isinstance(val, str) or not val:
                continue
            if val in seen:
                continue
            seen.add(val)
            out.append(val)
            if limit is not None and len(out) >= int(limit):
                break
        return out

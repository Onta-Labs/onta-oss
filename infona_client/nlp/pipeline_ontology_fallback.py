"""Instance-graph ontology fallback when the schema graph is empty."""
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


class PipelineOntologyFallbackMixin:
    async def _instance_graph_ontology_fallback(
        self,
        graph_uri: str,
        instance_graph: str | None,
        active_types: set[str] | None,
    ) -> tuple[str, bool] | None:
        """Build a minimal ontology summary from INSTANCE data when the schema is missing.

        Called only when the base-graph schema query yields zero types. Probes
        the instance graph directly for the types actually present and the
        predicates used on them, so a freshly-ingested KG whose schema hasn't
        been written yet can still answer a basic "list all X" query instead of
        returning the misleading "No ontology defined yet."

        Returns:
          * ``(summary, True)``  — instances exist; `summary` is a minimal
            ontology built from instance types/predicates, prefixed with a
            diagnostic telling the caller the schema isn't available yet.
          * ``(None-sentinel, False)`` i.e. ``("", False)`` — no instances found;
            caller keeps the original "No ontology defined yet." message.
          * ``None`` — probing failed; caller falls back to the default message.

        Best-effort: any error returns ``None`` so /ask never breaks on it.
        """
        target_graph = instance_graph or graph_uri
        pass  # TYPE_URI_PREFIX imported
        from infona_client.graph.ontology_queries import type_uri, attr_uri

        try:
            # Reuse types already discovered upstream when available; otherwise
            # probe the instance graph now.
            type_leaves: set[str] = set(active_types) if active_types else set()
            if not type_leaves:
                type_query = (
                    f"SELECT DISTINCT ?type FROM <{target_graph}> "
                    f"WHERE {{ ?s <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> ?type }}"
                )
                _, type_bindings = parse_sparql_results(await self.neptune.query(type_query))
                for row in type_bindings:
                    t = row.get("type", "")
                    if t.startswith(TYPE_URI_PREFIX):
                        type_leaves.add(t[len(TYPE_URI_PREFIX):])

            if not type_leaves:
                # Genuinely empty — no instances either. Signal "no instances".
                return "", False

            # Collect the predicates actually used on each type's instances so
            # the LLM has concrete URIs to query, even without a schema. Bounded
            # per-type; failures per type are non-fatal.
            lines = [
                "NOTE: The ontology schema for this graph has not been written "
                "yet, but instance data is present. The types and predicates "
                "below were read directly from the instance data. For the full "
                "curated ontology once available, use view_ontology.",
                "",
            ]
            for leaf in sorted(type_leaves):
                lines.append(f"Type: {leaf} — URI: <{type_uri(leaf)}>")
                try:
                    pred_query = (
                        f"SELECT DISTINCT ?p FROM <{target_graph}> WHERE {{ "
                        f"?s <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> "
                        f"<{type_uri(leaf)}> . ?s ?p ?o }} LIMIT 100"
                    )
                    _, pred_bindings = parse_sparql_results(await self.neptune.query(pred_query))
                except Exception:
                    pred_bindings = []
                attrs: list[str] = []
                rels: list[str] = []
                for row in pred_bindings:
                    p = row.get("p", "")
                    if p.startswith(f"{TYPE_URI_PREFIX}{leaf}/attrs/"):
                        a_name = p.rsplit("/", 1)[-1]
                        attrs.append(f"{a_name} — URI: <{attr_uri(leaf, a_name)}>")
                    elif p.startswith(f"{IRI_BASE}/onto/"):
                        r_name = p.rsplit("/", 1)[-1]
                        rels.append(f"{r_name} — predicate URI: <{p}>")
                if attrs:
                    lines.append(f"  Attributes: {', '.join(sorted(set(attrs)))}")
                if rels:
                    lines.append(f"  Relationships: {', '.join(sorted(set(rels)))}")

            return "\n".join(lines), True
        except Exception:
            logger.warning("instance_graph_ontology_fallback_failed", exc_info=True)
            return None

"""Spatio-temporal fast path for geo/proximity questions."""
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


class PipelineSpatialMixin:
    # ------------------------------------------------------------- spatial path
    async def _try_spatial_fast_path(
        self,
        question: str,
        ontology: str,
        data_graph: str,
        timing: dict,
        t0: float,
    ) -> NLResult | None:
        """Answer a geo/proximity question directly from the spatio-temporal index.

        Returns an ``NLResult`` on success, or ``None`` to fall through to the
        normal SPARQL path — when the graph isn't a per-KG instance graph, the LLM
        doesn't return a servable spatial intent, the anchor can't be resolved, or
        anything errors. Never raises into :meth:`ask` (best-effort fast path).
        """
        scope = parse_kg_graph_uri(data_graph)
        if scope is None:
            return None  # index rows are scoped per (tenant, kg); can't route otherwise
        tenant_id, kg_name = scope
        try:
            ts = time.time()
            raw = await self._detect_spatial_intent(question, ontology)
            intent = parse_spatial_intent(raw) if raw else None
            timing["spatial_intent_ms"] = round((time.time() - ts) * 1000, 1)
            if intent is None:
                return None

            from infona_client.spatiotemporal.registry import get_spatiotemporal_index

            index = get_spatiotemporal_index()

            # Temporal predicate: a single instant (as_of) wins over a window.
            as_of = _parse_iso_dt(intent.as_of)
            window = None
            if as_of is None and (intent.time_from or intent.time_to):
                window = (_parse_iso_dt(intent.time_from), _parse_iso_dt(intent.time_to))

            tq = time.time()
            if intent.kind == "radius":
                coords = await self._resolve_anchor_coords(intent.anchor, data_graph)
                if coords is None:
                    return None  # "near X" but X didn't resolve → fall through
                lon, lat = coords
                hits = await index.query_radius(
                    tenant_id, lon, lat, intent.radius_m,
                    kg_name=kg_name, time_window=window, as_of=as_of,
                )
            else:  # bbox
                min_lon, min_lat, max_lon, max_lat = intent.bbox
                hits = await index.query_bbox(
                    tenant_id, min_lon, min_lat, max_lon, max_lat,
                    kg_name=kg_name, time_window=window, as_of=as_of,
                )
            timing["spatial_index_ms"] = round((time.time() - tq) * 1000, 1)

            hits = filter_by_type(hits, intent.target_type)
            answer = format_spatial_answer(hits, intent)
            timing["spatial_routed"] = "true"
            timing["total_ms"] = round((time.time() - t0) * 1000, 1)
            return NLResult(
                answer=answer,
                sparql="",
                explanation="Answered from the spatio-temporal index (no SPARQL).",
                ontology=ontology,
                narrative_answer=answer,
                functions_invoked=[],
                timing=timing,
            )
        except Exception:
            logger.warning("spatial_fast_path_failed", exc_info=True)
            return None

    async def _detect_spatial_intent(self, question: str, ontology: str) -> dict | None:
        """LLM classify: is this a servable spatial lookup, and with what params?
        Returns the raw JSON dict (caller parses) or None on error."""
        user = (
            f"Question: {question}\n\n"
            f"Knowledge-graph types/attributes (for the target type, if any):\n"
            f"{ontology[:2000]}"
        )
        try:
            return await self._structured_llm(
                SPATIAL_INTENT_SYSTEM, user, "spatial_intent", SPATIAL_INTENT_SCHEMA
            )
        except Exception:
            logger.warning("spatial_intent_detect_failed", exc_info=True)
            return None

    async def _resolve_anchor_coords(self, anchor, data_graph: str):
        """Resolve a radius anchor to ``(lon, lat)``.

        Resolution ladder (first hit wins):
          1. explicit coordinates on the intent;
          2. a KG entity whose label matches ``entity_description`` AND carries a
             ``geo:wktLiteral`` (one scoped Neptune lookup) — preferred, since it
             pins the anchor to the tenant's own data;
          3. the free-text GEOCODER seam (ONTA-249): turn a bare place name
             ("Irvine") into coords via the registered geocoder — the OSS default
             is a deterministic offline gazetteer; a premium geocoder registers
             over it. This is what lets a place name resolve when no KG entity for
             it exists.

        Returns ``None`` when nothing resolves — the caller then falls through to
        the normal SPARQL path (byte-stable pre-existing behavior)."""
        if anchor is None:
            return None
        if anchor.has_coords():
            return (anchor.lon, anchor.lat)
        if not anchor.entity_description:
            return None
        # 2. KG-entity geometry (preferred — anchored to the tenant's own data).
        via_kg = await self._resolve_anchor_via_neptune(
            anchor.entity_description, data_graph
        )
        if via_kg is not None:
            return via_kg
        # 3. Free-text geocoder seam.
        return await self._geocode_anchor(anchor.entity_description)

    async def _geocode_anchor(self, description: str):
        """Resolve a free-text place name to ``(lon, lat)`` via the geocoder seam.

        Best-effort: returns ``None`` (never raises) when the place is unknown or
        the geocoder errors, so the caller falls through to the SPARQL path."""
        if not description or not description.strip():
            return None
        try:
            from infona_client.spatiotemporal.geocoder import get_geocoder

            coords = await get_geocoder().geocode(description)
        except Exception:
            logger.warning("geocode_anchor_failed", exc_info=True)
            return None
        if (
            isinstance(coords, tuple)
            and len(coords) == 2
            and all(isinstance(c, (int, float)) for c in coords)
        ):
            lon, lat = float(coords[0]), float(coords[1])
            if -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0:
                return (lon, lat)
        return None

    async def _resolve_anchor_via_neptune(self, description: str, data_graph: str):
        """Find a KG entity whose label/text contains ``description`` AND that
        carries a ``geo:wktLiteral``; return that point's ``(lon, lat)`` or None.

        One scoped SELECT, LIMIT 1. The description is sanitized before it is
        interpolated into the FILTER literal."""
        desc = _sanitize_sparql_literal(description)
        for article in ("the ", "a ", "an "):
            if desc.startswith(article):
                desc = desc[len(article):]
        if not desc:
            return None
        anchor_query = (
            f"SELECT ?wkt FROM <{data_graph}> WHERE {{ "
            f"?e ?lp ?lbl . "
            f'FILTER(isLiteral(?lbl) && CONTAINS(LCASE(STR(?lbl)), "{desc}")) '
            f"?e ?gp ?wkt . "
            f"FILTER(datatype(?wkt) = <{_GEO_WKT_URI}>) "
            f"}} LIMIT 1"
        )
        try:
            raw = await self.neptune.query(anchor_query)
            _, rows = parse_sparql_results(raw)
        except Exception:
            logger.warning("anchor_resolve_failed", exc_info=True)
            return None
        if not rows:
            return None
        return _parse_point_wkt(rows[0].get("wkt", ""))

    async def _structured_llm(
        self, system: str, user: str, schema_name: str, schema: dict
    ) -> dict:
        """Provider-agnostic structured-JSON call for non-SPARQL classifiers (e.g.
        spatial-intent detection). Mirrors :meth:`_generate_sparql`'s provider
        selection but is a SEPARATE method on purpose — the SPARQL generators stay
        byte-identical so evals are unaffected."""
        if self._query_provider == "cerebras" and self._cerebras_key:
            endpoint = "https://api.cerebras.ai/v1/chat/completions"
            key, model = self._cerebras_key, self._query_model
        elif self._openrouter_key:
            endpoint = f"{OPENROUTER_BASE}/chat/completions"
            key, model = self._openrouter_key, self._query_model
        else:
            return await self._structured_via_anthropic(system, user, schema)
        assert_online_url(endpoint, purpose="query structured LLM")
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                endpoint,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {"name": schema_name, "strict": True, "schema": schema},
                    },
                },
            )
            res.raise_for_status()
            text = _require_message_content(res.json(), self._query_provider).strip()
            if text.startswith("```"):
                text = "\n".join(
                    l for l in text.split("\n") if not l.strip().startswith("```")
                )
            return json.loads(text)

    async def _structured_via_anthropic(self, system: str, user: str, schema: dict) -> dict:
        from infona_client.offline import assert_online_host
        assert_online_host("api.anthropic.com", purpose="Anthropic structured LLM call")
        message = await self.anthropic.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        return json.loads(message.content[0].text)

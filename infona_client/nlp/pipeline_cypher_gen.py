"""LLM Cypher generators (OpenRouter / Cerebras / Anthropic). Always-LLM for /ask."""
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
    _parse_cypher_gen_json,
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


class PipelineCypherGenMixin:
    def _openrouter_cypher_model_id(
        self, *, prefer_non_reasoning: bool = False
    ) -> str:
        """Return an OpenRouter-valid model slug for the Cypher path.

        Direct Cerebras uses bare ``gpt-oss-120b``; OpenRouter needs
        ``openai/gpt-oss-120b``. When this method is used as *tier-2*
        length recovery (``prefer_fallback``), prefer a non-reasoning
        OpenRouter model so think-budget exhaustion has an escape hatch.
        """
        if prefer_non_reasoning:
            return os.environ.get(
                "INFONA_QUERY_FALLBACK_MODEL", "google/gemini-2.5-flash"
            )
        model = (self._query_model or "").strip()
        if self._query_provider == "openrouter" and model:
            return model
        # Map bare Cerebras / short slugs onto OpenRouter ids.
        if model in ("gpt-oss-120b", "openai/gpt-oss-120b") or "gpt-oss-120b" in model:
            return "openai/gpt-oss-120b"
        if model.startswith("openai/") or model.startswith("google/") or "/" in model:
            return model
        if model:
            # Unknown bare slug — still try OpenRouter openai/ prefix for oss.
            return f"openai/{model}" if not model.startswith("openai/") else model
        return "openai/gpt-oss-120b"

    async def _generate_cypher_via_openrouter(
        self, prompt: str, *, prefer_non_reasoning: bool = False
    ) -> dict:
        openrouter_url = f"{OPENROUTER_BASE}/chat/completions"
        assert_online_url(openrouter_url, purpose="query Cypher LLM (openrouter)")
        model = self._openrouter_cypher_model_id(
            prefer_non_reasoning=prefer_non_reasoning
        )
        body: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": CYPHER_GENERATION_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        # Reasoning models (gpt-oss-120b etc.) need a large output budget so
        # chain-of-thought does not starve the JSON answer.
        if _is_reasoning_query_model(model):
            body["max_tokens"] = OPENROUTER_REASONING_MAX_TOKENS
            # Prefer Cerebras for openai/gpt-oss-* when OpenRouter hosts it
            # (thinking model + high throughput). Fallbacks allowed.
            if "gpt-oss" in model.lower():
                body["provider"] = {
                    "order": ["Cerebras"],
                    "allow_fallbacks": True,
                }
        timeout_s = (
            OPENROUTER_QUERY_TIMEOUT_S
            if _is_reasoning_query_model(model)
            else 60.0
        )
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            res = await client.post(
                openrouter_url,
                headers={
                    "Authorization": f"Bearer {self._openrouter_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            res.raise_for_status()
            data = res.json()
            content = _require_message_content(data, "openrouter")
            # Reasoning models sometimes wrap JSON in fences or prefix prose.
            parsed = _parse_cypher_gen_json(content)
            if "cypher" not in parsed and "sparql" in parsed:
                parsed["cypher"] = parsed["sparql"]
            return attach_usage(
                parsed,
                usage=data.get("usage") if isinstance(data, dict) else None,
                model=model,
                provider="openrouter",
                response_model=(data.get("model") if isinstance(data, dict) else None)
                or "",
            )

    async def _generate_cypher_via_cerebras(
        self, prompt: str, max_completion_tokens: int = 2048
    ) -> dict:
        """Generate Cypher via Cerebras in ``json_object`` mode.

        NOT ``json_schema``/``strict``. The Cypher contract carries a **free-form
        ``params`` bag** — the model picks the placeholder names (``$type_names``,
        ``$prop_key``, …) — and Cerebras strict structured output cannot express
        an object with arbitrary keys. Every shape was tried against the live API:

        * ``params: {"type": "object", "additionalProperties": true}`` (what we
          shipped) → **400** ``wrong_api_format``: *"Object fields require at
          least one of: 'properties' or 'anyOf' with a list of possible
          properties."* This 400'd every ``/ask`` in production.
        * ``params: {"type": "object", "properties": {}, "additionalProperties":
          true}`` → **400**, ``additionalProperties`` must be ``false``.
        * ``additionalProperties: false`` → a permanently EMPTY ``params``.
        * dropping ``params`` → root ``additionalProperties: false`` strips it
          from the response, silently killing parameterization.

        ``params`` is load-bearing (``pipeline_ask``, ``pipeline.py``, and
        ``_execute_confined_cypher`` all read ``gen["params"]``), so the schema
        had to go rather than the field. ``json_object`` also lets the model
        return ``template`` — an allowlisted-helper hint the system prompt
        documents and ``_execute_confined_cypher`` consumes, which the old root
        ``additionalProperties: false`` had been silently stripping. This matches
        what ``_generate_cypher_via_openrouter`` already sends: ONE
        ``response_format`` for the Cypher path, with the JSON contract stated in
        ``CYPHER_GENERATION_SYSTEM`` where both providers see it.
        """
        cerebras_url = "https://api.cerebras.ai/v1/chat/completions"
        assert_online_url(cerebras_url, purpose="query Cypher LLM (cerebras)")
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                cerebras_url,
                headers={
                    "Authorization": f"Bearer {self._cerebras_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._query_model,
                    "messages": [
                        {"role": "system", "content": CYPHER_GENERATION_SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    "max_completion_tokens": max_completion_tokens,
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                },
            )
            res.raise_for_status()
            data = res.json()
            content = _require_message_content(data, "cerebras")
            # No constrained decode any more, so tolerate fences / think-prose
            # exactly as the OpenRouter Cypher path does.
            parsed = _parse_cypher_gen_json(content)
            if "cypher" not in parsed and "sparql" in parsed:
                parsed["cypher"] = parsed["sparql"]
            return attach_usage(
                parsed,
                usage=data.get("usage") if isinstance(data, dict) else None,
                model=self._query_model,
                provider="cerebras",
                response_model=(data.get("model") if isinstance(data, dict) else None)
                or "",
            )

    async def _generate_cypher_via_anthropic(self, prompt: str) -> dict:
        msg = await self.anthropic.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=CYPHER_GENERATION_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text if msg.content else "{}"
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        if not text:
            raise EmptyLLMResponse("anthropic", finish_reason="stop")
        parsed = json.loads(text)
        if "cypher" not in parsed and "sparql" in parsed:
            parsed["cypher"] = parsed["sparql"]
        usage = None
        if getattr(msg, "usage", None) is not None:
            usage = {
                "prompt_tokens": getattr(msg.usage, "input_tokens", None),
                "completion_tokens": getattr(msg.usage, "output_tokens", None),
            }
        return attach_usage(
            parsed,
            usage=usage,
            model="claude-sonnet-4-6",
            provider="anthropic",
        )

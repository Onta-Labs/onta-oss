"""Residual SPARQL generators + narrative rephrase. /ask does not call SPARQL gen."""
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


class PipelineSparqlGenMixin:
    async def _rephrase_via_openrouter(self, question: str, bindings: list[dict], max_rows: int | None = None) -> str:
        """Generate a 2-3 sentence narrative summary of SPARQL result bindings.

        ``max_rows`` bounds how many rows are fed to the narrative LLM (a
        deliberate sample, not the full answer — the plain-text answer in
        ``_format_answer`` carries all rows up to ANSWER_ROW_CAP). Defaults to
        INFONA_REPHRASE_MAX_ROWS (30) so a wide result can't blow the summarizer's
        context; the truncation is already stated to the model. Now that
        generated SELECTs get a deterministic ORDER BY, this sample is stable
        across runs instead of an arbitrary slice.

        Uses Llama 3.1 8B on Cerebras (via OpenRouter) for fast, cheap rephrase.
        Fails open: returns "" on any error so the main response is never broken.
        """
        if not self._openrouter_key:
            return ""

        if max_rows is None:
            max_rows = int(os.environ.get("INFONA_REPHRASE_MAX_ROWS", "30"))

        # Same hygiene as _format_answer: never feed internal/housekeeping
        # predicate rows (er/*, onto/norm/*, onto/batch_id, …) to the narrative
        # summarizer, or it would describe ER plumbing as business facts.
        bindings = _drop_internal_predicate_rows(bindings)

        try:
            # Build a compact tabular string from bindings
            if not bindings:
                table_str = "(no results)"
                truncation_note = ""
            else:
                rows = bindings[:max_rows]
                if rows:
                    cols = list(rows[0].keys())
                    lines = ["\t".join(cols)]
                    for row in rows:
                        lines.append("\t".join(str(row.get(c, "")) for c in cols))
                    table_str = "\n".join(lines)
                else:
                    table_str = "(no results)"
                truncation_note = (
                    f"\n(Showing {len(rows)} of {len(bindings)} total rows.)"
                    if len(bindings) > max_rows else ""
                )

            system_prompt = (
                "You are an analyst summarizing a database query result. Rules:\n"
                "- Lead with the specific count (e.g. 'Eleven founders match.').\n"
                "- If multiple rows share similar values, find the ONE row that stands out — "
                "different company, different prior company, or different category. "
                "Use that outlier as your hero example with its exact column values.\n"
                "- Keep to 2-3 sentences, max 80 words.\n"
                "- ONLY state facts visible in the rows. Never mix values from different rows.\n"
                "- Trust the row values as literal, authoritative facts. If a column has a value, "
                "that IS the answer for that column — never describe a present value as "
                "'unknown' or 'incomplete' just because it's a short code.\n"
                "- SEC filing type codes are canonical form names (e.g. D means Form D, "
                "10-K means annual report, 10-Q means quarterly, 8-K means material event, "
                "S-1 means IPO registration). State the code as-is — prefixing with 'Form' "
                "is fine; calling it unknown is not.\n"
                "- If a column is source_url / source_name / url, cite it in the prose as a "
                "full http(s) link (markdown [name](url) is fine). That is the provenance.\n"
                "- Never print graph.infona.ai entity IRIs. Use human labels only.\n"
                "- Do NOT use chatbot phrases like 'Sure!', 'Here you go', 'Great question'.\n"
                "- If the result is empty, say 'No matches found.' and stop.\n"
                "- Speak in plain English, not technical jargon."
            )

            user_prompt = (
                f"Question: {question}\n\n"
                f"Result ({len(bindings)} row{'s' if len(bindings) != 1 else ''}):\n"
                f"{table_str}{truncation_note}\n\n"
                "Summarize this result in 2-3 sentences."
            )

            t_rephrase = time.time()
            rephrase_url = f"{OPENROUTER_BASE}/chat/completions"
            assert_online_url(rephrase_url, purpose="answer rephrase LLM")
            async with httpx.AsyncClient(timeout=10) as client:
                res = await client.post(
                    rephrase_url,
                    headers={
                        "Authorization": f"Bearer {self._openrouter_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "meta-llama/llama-3.1-8b-instruct",
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "max_tokens": 300,
                        "temperature": 0.2,
                        "provider": {
                            "order": ["Cerebras", "Groq", "Nebius"],
                            "allow_fallbacks": True,
                        },
                    },
                )
                res.raise_for_status()
                data = res.json()
                narrative = _require_message_content(data, "openrouter").strip()
                # Stash usage for the enclosing ask() ledger (one pipeline per
                # request; drained immediately after this call returns).
                usage = data.get("usage") if isinstance(data, dict) else None
                self._last_rephrase_usage = {
                    "prompt_tokens": (usage or {}).get("prompt_tokens"),
                    "completion_tokens": (usage or {}).get("completion_tokens"),
                    "total_tokens": (usage or {}).get("total_tokens"),
                    "model": (data.get("model") if isinstance(data, dict) else None)
                    or "meta-llama/llama-3.1-8b-instruct",
                    "provider": "openrouter",
                }
            rephrase_ms = round((time.time() - t_rephrase) * 1000, 1)
            logger.info("narrative_rephrase_ok", rephrase_ms=rephrase_ms, rows=len(bindings))
            return narrative
        except Exception:
            logger.warning("narrative_rephrase_failed", exc_info=True)
            self._last_rephrase_usage = None
            return ""

    async def _generate_sparql(
        self,
        question: str,
        ontology: str,
        graph_uri: str = "",
        error_feedback: str = "",
        examples_text: str = "",
        max_completion_tokens: int | None = None,
        prefer_fallback: bool = False,
    ) -> dict:
        # Name the target KG in the prompt (ONTA-417) so "[no instances]" reads as
        # "declared tenant-wide, absent from THIS graph" rather than "declared
        # here but empty". parse_kg_graph_uri returns None for a non-KG graph
        # (bare tenant/ontology graph), which leaves the prompt byte-identical.
        parsed_kg = parse_kg_graph_uri(graph_uri)
        tenant_id = (
            parsed_kg[0] if parsed_kg else None
        ) or tenant_of_graph(graph_uri) or ""
        from infona_client.skills.inject import ontology_with_skills, type_names_for_skills

        ontology = await ontology_with_skills(
            ontology,
            type_names_for_skills(ontology),
            tenant_id=tenant_id,
            tenant=getattr(self, "_tenant_ctx", None),
        )
        prompt = build_generation_prompt(
            question,
            ontology,
            graph_uri,
            examples_text=examples_text,
            kg_name=parsed_kg[1] if parsed_kg else "",
        )
        if error_feedback:
            prompt += f"\n\n{error_feedback}"

        # Reasoning-budget recovery (persona-eval RCA), retry path only. When the
        # Cerebras reasoning model exhausted its output budget on reasoning
        # (finish_reason="length"), `ask()` sets `prefer_fallback` to escalate OFF
        # the reasoning model to the non-reasoning OpenRouter/Anthropic JSON path,
        # which doesn't burn the budget reasoning before answering. Prefer
        # OpenRouter unless that's already the (truncating) provider, else Anthropic.
        if prefer_fallback:
            if self._openrouter_key and self._query_provider != "openrouter":
                return await self._generate_via_openrouter(prompt)
            return await self._generate_via_anthropic(prompt)

        if self._query_provider == "cerebras" and self._cerebras_key:
            # `max_completion_tokens` is threaded ONLY on the recovery retry (a
            # bigger budget so reasoning + the answer both fit). On the happy path
            # it is None and the call is byte-identical to before (default 2048).
            if max_completion_tokens is not None:
                return await self._generate_via_cerebras(prompt, max_completion_tokens=max_completion_tokens)
            return await self._generate_via_cerebras(prompt)
        if self._query_provider == "openrouter" and self._openrouter_key:
            return await self._generate_via_openrouter(prompt)
        if self._openrouter_key:
            return await self._generate_via_openrouter(prompt)
        return await self._generate_via_anthropic(prompt)

    async def _generate_via_cerebras(self, prompt: str, max_completion_tokens: int = 2048) -> dict:
        """Generate SPARQL via Cerebras with structured output.

        `max_completion_tokens` defaults to 2048 — the happy-path value, kept as a
        literal so a normal call is byte-identical to before. `ask()` passes a
        BIGGER budget only on the reasoning-budget recovery retry (see
        `CEREBRAS_LENGTH_RECOVERY_TOKENS`) after a finish_reason="length" truncation.
        """
        cerebras_url = "https://api.cerebras.ai/v1/chat/completions"
        assert_online_url(cerebras_url, purpose="query SPARQL LLM (cerebras)")
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
                        {"role": "system", "content": SPARQL_GENERATION_SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    # gpt-oss-120b is a reasoning model that spends output
                    # tokens on reasoning BEFORE emitting the answer. At 512 the
                    # JSON gets truncated mid-string and json.loads raises
                    # (empirically 0/3 at 512, 3/3 at 2048). Keep enough headroom
                    # for reasoning + a full SPARQL response. (OpenRouter/Anthropic
                    # caps are separate and unchanged.) The default is 2048; the
                    # reasoning-budget recovery retry passes a bigger value when a
                    # hard question still exhausts it (finish_reason="length").
                    "max_completion_tokens": max_completion_tokens,
                    "temperature": 0,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "sparql_response",
                            "strict": True,
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "sparql": {"type": "string"},
                                    "explanation": {"type": "string"},
                                    "functions_needed": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                },
                                "required": ["sparql", "explanation", "functions_needed"],
                                "additionalProperties": False,
                            },
                        },
                    },
                },
            )
            res.raise_for_status()
            data = res.json()
            # `_require_message_content` raises the typed, provider-named
            # EmptyLLMResponse (a ValueError) on a null/empty/ABSENT content —
            # including the finish_reason="length" reasoning-budget truncation where
            # the `content` key is missing entirely (which used to surface as a hard
            # KeyError('content') past the retry loop). It carries the finish_reason
            # so `ask()` can RECOVER a length truncation (bigger budget / fallback).
            # Separately, the JSON DECODE is tolerant: gpt-oss-120b sometimes wraps
            # its JSON in code fences or truncates it mid-string, which used to throw
            # an uncaught JSONDecodeError past the retry loop. Now a truncated-but-
            # usable query is salvaged, and an unrecoverable blob degrades to an
            # empty `sparql` that triggers the ask() escalation path.
            result = _parse_sparql_gen_json(_require_message_content(data, "cerebras"))
            return attach_usage(
                result,
                usage=data.get("usage") if isinstance(data, dict) else None,
                model=self._query_model,
                provider="cerebras",
                response_model=(data.get("model") if isinstance(data, dict) else None) or "",
            )

    async def _generate_via_openrouter(self, prompt: str) -> dict:
        """Generate SPARQL via OpenRouter (OpenAI-compatible API)."""
        openrouter_url = f"{OPENROUTER_BASE}/chat/completions"
        assert_online_url(openrouter_url, purpose="query SPARQL LLM")
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                openrouter_url,
                headers={
                    "Authorization": f"Bearer {self._openrouter_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._query_model,
                    "models": model_chain(self._query_model),
                    "messages": [
                        {"role": "system", "content": SPARQL_GENERATION_SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 1024,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "sparql_response",
                            "strict": True,
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "sparql": {"type": "string"},
                                    "explanation": {"type": "string"},
                                    "functions_needed": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                },
                                "required": ["sparql", "explanation", "functions_needed"],
                                "additionalProperties": False,
                            },
                        },
                    },
                },
            )
            res.raise_for_status()
            data = res.json()
            text = _require_message_content(data, "openrouter")
            # Strip code fences if present
            stripped = text.strip()
            if stripped.startswith("```"):
                lines = [l for l in stripped.split("\n") if not l.strip().startswith("```")]
                stripped = "\n".join(lines)
            result = json.loads(stripped)
            return attach_usage(
                result,
                usage=data.get("usage") if isinstance(data, dict) else None,
                model=self._query_model,
                provider="openrouter",
                response_model=(data.get("model") if isinstance(data, dict) else None) or "",
            )

    async def _generate_via_anthropic(self, prompt: str) -> dict:
        """Fallback: generate SPARQL via Anthropic API."""
        from infona_client.offline import assert_online_host
        assert_online_host("api.anthropic.com", purpose="Anthropic SPARQL generation")
        message = await self.anthropic.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SPARQL_GENERATION_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "sparql": {"type": "string", "description": "The SPARQL SELECT query"},
                            "explanation": {"type": "string", "description": "Brief explanation of what the query does"},
                            "functions_needed": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "List of function names if computation is needed",
                            },
                        },
                        "required": ["sparql", "explanation", "functions_needed"],
                        "additionalProperties": False,
                    },
                },
            },
        )
        result = json.loads(message.content[0].text)
        msg_usage = getattr(message, "usage", None)
        usage_dict = None
        if msg_usage is not None:
            usage_dict = {
                "input_tokens": getattr(msg_usage, "input_tokens", None),
                "output_tokens": getattr(msg_usage, "output_tokens", None),
            }
        return attach_usage(
            result,
            usage=usage_dict,
            model="claude-sonnet-4-6",
            provider="anthropic",
        )

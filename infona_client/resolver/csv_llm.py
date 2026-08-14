"""LLM call helpers for CSV schema inference.

``openrouter_chat`` is looked up on ``csv_resolver`` at call time so
tests that patch it keep working.
"""

from __future__ import annotations

import json
import os

import anthropic
import httpx

import structlog

from infona_client.resolver.csv_helpers import _strip_code_fences
from infona_client.resolver.csv_mapping import _V2_BASE_MAX_TOKENS
from infona_client.resolver.csv_prompts import CSV_SCHEMA_SYSTEM
from infona_client.resolver.models import ColumnMapping, ColumnRole, CSVSchemaMapping

logger = structlog.stdlib.get_logger("infona.resolver.csv")


def _host():
    from infona_client.resolver import csv_resolver as _mod

    return _mod


class CSVLlmMixin:
    """Anthropic / OpenRouter chat + mapping build."""

    async def _call_llm(self, user_content: str, temperature: float = 0.0) -> dict:
        if self.EXTRACT_PROVIDER == "openrouter" and self._openrouter_key:
            return await self._infer_via_openrouter(user_content, temperature)
        return await self._infer_via_anthropic(user_content, temperature)

    async def _call_llm_v2(
        self,
        system: str,
        user_content: str,
        temperature: float = 0.0,
        max_tokens: int = _V2_BASE_MAX_TOKENS,
    ) -> dict:
        """LLM seam for the v2 passes — like ``_call_llm`` but the system
        prompt varies per pass (REASON vs REFUTE). ``max_tokens`` is scaled to
        the column count by callers (COG-58) so a wide-table pass that must
        echo every column isn't truncated. Tests monkeypatch this."""
        if self.EXTRACT_PROVIDER == "openrouter" and self._openrouter_key:
            return await self._chat_openrouter(system, user_content, temperature, max_tokens=max_tokens)
        return await self._chat_anthropic(system, user_content, temperature, max_tokens=max_tokens)

    def _build_mapping(self, data: dict) -> CSVSchemaMapping:
        # Gemini Flash occasionally emits `datatype: null` for a column it
        # can't classify. Coerce to "string" so the pydantic model doesn't
        # reject the whole inference — callers can always retry the
        # downstream resolver pass if the string guess turns out wrong.
        for col in data.get("columns", []):
            if col.get("datatype") is None:
                col["datatype"] = "string"
            if col.get("role") is None:
                col["role"] = "attribute"
        # Multi-entity mode is opt-in: the model returns a non-empty `entities`
        # array only for wide CSVs that bundle several entities. Absent → legacy.
        entities = data.get("entities") or None
        relationships = data.get("relationships") or None
        # `entity_type` is required in single-entity mode — its absence signals a
        # malformed LLM response and (by raising KeyError) triggers the retry. In
        # multi-entity mode it's ignored, so a placeholder is fine.
        entity_type = data.get("entity_type")
        if entity_type is None:
            if entities is None:
                raise KeyError("entity_type")
            entity_type = "Entity"
        return CSVSchemaMapping(
            entity_type=entity_type,
            columns=[ColumnMapping(**col) for col in data["columns"]],
            entities=[EntitySpec(**e) for e in entities] if entities else None,
            relationships=(
                [EntityRelationSpec(**r) for r in relationships] if relationships else None
            ),
        )

    async def _infer_via_openrouter(self, user_content: str, temperature: float = 0.0) -> dict:
        return await self._chat_openrouter(CSV_SCHEMA_SYSTEM, user_content, temperature)

    async def _chat_openrouter(
        self,
        system: str,
        user_content: str,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> dict:
        text = await _host().openrouter_chat(
            self._openrouter_key,
            system,
            user_content,
            model=self.EXTRACT_MODEL,
            temperature=temperature,
            max_tokens=max_tokens,
            # Reasoning models need headroom for think + full schema JSON.
            timeout=float(os.environ.get("INFONA_CSV_SCHEMA_TIMEOUT_S", "180")),
        )
        return json.loads(_strip_code_fences(text))

    async def _chat_anthropic(
        self,
        system: str,
        user_content: str,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> dict:
        """Anthropic fallback for the v2 passes: free-form JSON (the pass
        output shapes differ, so no fixed output_config schema here)."""
        from infona_client.offline import assert_online_host
        assert_online_host("api.anthropic.com", purpose="Anthropic CSV schema chat")
        msg = await self._client.messages.create(
            model=self.INFER_MODEL,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user_content}],
        )
        return json.loads(_strip_code_fences(msg.content[0].text))

    async def _infer_via_anthropic(self, user_content: str, temperature: float = 0.0) -> dict:
        from infona_client.offline import assert_online_host
        assert_online_host("api.anthropic.com", purpose="Anthropic CSV schema infer")
        msg = await self._client.messages.create(
            model=self.INFER_MODEL,
            max_tokens=2048,
            temperature=temperature,
            system=CSV_SCHEMA_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "entity_type": {"type": "string"},
                            "columns": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "column_name": {"type": "string"},
                                        "role": {"type": "string", "enum": ["type_id", "attribute", "relationship"]},
                                        "target_type": {"type": ["string", "null"]},
                                        "datatype": {"type": "string"},
                                        "attribute_name": {"type": ["string", "null"]},
                                        "entity": {"type": ["string", "null"]},
                                    },
                                    "required": ["column_name", "role", "datatype"],
                                    "additionalProperties": False,
                                },
                            },
                            "entities": {
                                "type": ["array", "null"],
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "type_name": {"type": "string"},
                                        "id_column": {"type": ["string", "null"]},
                                        "id_from": {"type": ["array", "null"], "items": {"type": "string"}},
                                    },
                                    "required": ["name", "type_name"],
                                    "additionalProperties": False,
                                },
                            },
                            "relationships": {
                                "type": ["array", "null"],
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "subject": {"type": "string"},
                                        "predicate": {"type": "string"},
                                        "object": {"type": "string"},
                                    },
                                    "required": ["subject", "predicate", "object"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["entity_type", "columns"],
                        "additionalProperties": False,
                    },
                },
            },
        )
        return json.loads(msg.content[0].text)

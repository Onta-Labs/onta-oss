"""Pre-structured fast-path — the resolver seam (ONTA-272).

Pre-structured payloads (API-registry pulls with a known field mapping,
structured captures) already arrive as clean rows keyed by the confirmed
attribute set, so running the open-ended LLM extractor over them is a
non-deterministic detour. ``SchemaResolver.ingest_structured_rows`` commits them
through the SAME deterministic mapping seam CSV ingest uses — NO ``_extract`` —
after asserting the soft-typed, evidence-linked A2 contract.

Here we isolate the fast-path DECISION: drive structured rows through
``ingest_structured_rows``, assert it materializes a valid soft-typed A2, NEVER
calls ``_extract``, and delegates to the deterministic ``ingest_mapped_records``
seam with a correct fixed mapping (key attribute as type-id, source_url typed
``uri``). The deterministic write itself is covered end-to-end over a real store
by ``test_resolver_key_join``. Premium web-ingest routing of a structured
provider to this method lives in ``infona/web_ingest/tests``.
"""
from __future__ import annotations

import pathlib

import pytest

from infona_client.resolver.models import (
    ColumnRole,
    ExtractionResult,
    soft_a2_from_structured_rows,
    validate_soft_a2,
)
from infona_client.resolver.schema_resolver import IngestResult, SchemaResolver
from infona_client.resolver.verdict_cache import JsonVerdictCache


def _resolver() -> SchemaResolver:
    from unittest.mock import MagicMock

    return SchemaResolver(
        MagicMock(), "fake-key",
        JsonVerdictCache(pathlib.Path("/tmp/faststruct-verdict-cache.json")),
    )


@pytest.mark.asyncio
async def test_ingest_structured_rows_no_extract_valid_soft_a2(monkeypatch):
    r = _resolver()

    # Any LLM extraction on this path is a bug — make it fatal if reached.
    async def _boom(*a, **k):
        raise AssertionError("the LLM extractor must not run on the fast path")

    monkeypatch.setattr(SchemaResolver, "_extract", _boom)
    monkeypatch.setattr(SchemaResolver, "_extract_via_openrouter", _boom)

    # The deterministic seam is covered elsewhere over a real store; here we spy
    # on it to assert the fast path builds the right fixed mapping and delegates.
    captured: dict = {}

    async def fake_mapped(self, rows, mapping, tenant_id, source="",
                          instance_graph=None, key_join=None, run_id=None):
        captured.update(rows=rows, mapping=mapping, tenant_id=tenant_id,
                        source=source, instance_graph=instance_graph, run_id=run_id)
        return IngestResult(entities_extracted=len(rows), entities_resolved=len(rows))

    monkeypatch.setattr(SchemaResolver, "ingest_mapped_records", fake_mapped)

    rows = [
        {"name": "Dr X", "specialty": "Cardiology", "city": "Portland",
         "source_url": "https://reg.example/a"},
        {"name": "Dr Y", "specialty": "Neurology", "city": "Seattle",
         "source_url": "https://reg.example/b"},
    ]

    # The A2 the fast path materializes for these rows is valid soft-typed +
    # evidence-linked (the same witness the method asserts internally).
    a2 = soft_a2_from_structured_rows(rows, "Physician", key_field="name")
    assert isinstance(a2, ExtractionResult)
    assert validate_soft_a2(a2, require_evidence=True) == []

    result = await r.ingest_structured_rows(
        rows, "demo-tenant", "Physician",
        attributes=["name", "specialty", "city"],
        source="web:api:reg:q", instance_graph="g://kg", key_attribute="name",
    )
    assert result.entities_resolved == 2

    # Delegated to the deterministic seam with the confirmed rows + a fixed mapping.
    assert captured["rows"] is rows
    assert captured["source"] == "web:api:reg:q"
    assert captured["instance_graph"] == "g://kg"
    mapping = captured["mapping"]
    assert mapping.entity_type == "Physician"
    # The key attribute is the type-id; source_url is a uri-typed literal; the rest
    # are plain string literal attributes.
    key_cols = [c for c in mapping.columns if c.role == ColumnRole.TYPE_ID]
    assert [c.column_name for c in key_cols] == ["name"]
    su = next(c for c in mapping.columns if c.column_name == "source_url")
    assert su.role == ColumnRole.ATTRIBUTE and su.datatype == "uri"
    specialty = next(c for c in mapping.columns if c.column_name == "specialty")
    assert specialty.role == ColumnRole.ATTRIBUTE and specialty.datatype == "string"


@pytest.mark.asyncio
async def test_ingest_structured_rows_empty_is_noop(monkeypatch):
    r = _resolver()

    async def _boom(*a, **k):
        raise AssertionError("nothing should run on the empty fast path")

    monkeypatch.setattr(SchemaResolver, "_extract", _boom)
    monkeypatch.setattr(SchemaResolver, "ingest_mapped_records", _boom)
    result = await r.ingest_structured_rows([], "demo-tenant", "Physician")
    assert result.entities_extracted == 0
    assert result.rows_in == 0


@pytest.mark.asyncio
async def test_ingest_structured_rows_exhaustive_clips_extra_provider_columns(
    monkeypatch,
):
    """ONTA-382 structured ceiling: rich API payload must not invent attrs.

    Mirrors the TTS smoke: user confirmed name/provider/modality/input_price but
    OpenRouter rows also carry context_length/description/display_name.
    """
    r = _resolver()
    captured: dict = {}

    async def fake_mapped(self, rows, mapping, tenant_id, source="",
                          instance_graph=None, key_join=None, run_id=None):
        captured.update(rows=rows, mapping=mapping)
        return IngestResult(entities_extracted=len(rows), entities_resolved=len(rows))

    monkeypatch.setattr(SchemaResolver, "ingest_mapped_records", fake_mapped)

    rows = [
        {
            "name": "acme/tts-1",
            "provider": "acme",
            "modality": "text->speech",
            "input_price": "0.000015",
            "context_length": "0",  # must NOT be written under exhaustive
            "description": "A speech model",
            "display_name": "Acme TTS 1",
            "source_url": "https://api.example/models",
        },
    ]
    confirmed = ["name", "provider", "modality", "input_price"]
    await r.ingest_structured_rows(
        rows,
        "demo-tenant",
        "Model",
        attributes=confirmed,
        key_attribute="name",
        attributes_exhaustive=True,
    )

    written = captured["rows"][0]
    assert set(written.keys()) == {
        "name",
        "provider",
        "modality",
        "input_price",
        "source_url",
    }
    assert "context_length" not in written
    assert "description" not in written
    assert "display_name" not in written

    mapped_cols = {c.column_name for c in captured["mapping"].columns}
    assert mapped_cols == {
        "name",
        "provider",
        "modality",
        "input_price",
        "source_url",
    }


@pytest.mark.asyncio
async def test_ingest_structured_rows_non_exhaustive_keeps_extra_columns(
    monkeypatch,
):
    """Open attribute set: structured path still maps full provider rows."""
    r = _resolver()
    captured: dict = {}

    async def fake_mapped(self, rows, mapping, tenant_id, source="",
                          instance_graph=None, key_join=None, run_id=None):
        captured.update(rows=rows, mapping=mapping)
        return IngestResult(entities_extracted=len(rows), entities_resolved=len(rows))

    monkeypatch.setattr(SchemaResolver, "ingest_mapped_records", fake_mapped)

    rows = [
        {
            "name": "acme/tts-1",
            "provider": "acme",
            "context_length": "8192",
            "source_url": "https://api.example/models",
        },
    ]
    await r.ingest_structured_rows(
        rows,
        "demo-tenant",
        "Model",
        attributes=["name", "provider"],
        key_attribute="name",
        attributes_exhaustive=False,
    )
    assert "context_length" in captured["rows"][0]
    assert "context_length" in {c.column_name for c in captured["mapping"].columns}

"""ONTA-197 item 1 — JSONL extraction output with truncation salvage.

The extraction model now emits ONE self-contained JSON object PER LINE (JSONL)
instead of one big ``{"entities":[...],"relationships":[...]}`` document. The
``_extract`` parser reads line-by-line and DROPS the last partial/garbage line,
so a response truncated mid-last-line yields the N-1 complete records above it
instead of ZERO (the old single-document parse returned nothing for the whole
batch on any truncation).

These are UNIT tests over ``SchemaResolver._parse_extraction_jsonl`` and the
``_extract`` glue (mocked LLM, no network), proving:
  * a reply truncated mid-last-line yields ALL complete records, dropping only
    the partial,
  * a clean reply parses identically to the equivalent document,
  * malformed MIDDLE lines are skipped without sinking the batch,
  * entity/relationship discrimination via the ``kind`` field (and shape
    inference when the discriminator is absent).
"""

from __future__ import annotations

import json

import pytest

from cograph_client.resolver.schema_resolver import SchemaResolver
from cograph_client.resolver.verdict_cache import JsonVerdictCache


# --- parser unit tests (pure, no resolver instance needed) --------------------


def _entity_line(i: int, **extra) -> str:
    obj = {"kind": "entity", "type_name": "Model", "id": f"m{i}"}
    obj.update(extra)
    return json.dumps(obj)


def _rel_line(src: str, pred: str, tgt: str) -> str:
    return json.dumps(
        {"kind": "relationship", "source_id": src, "predicate": pred, "target_id": tgt}
    )


def test_clean_jsonl_parses_all_records():
    text = "\n".join([_entity_line(0), _entity_line(1), _rel_line("m0", "rel", "m1")])
    ents, rels, bad, dropped = SchemaResolver._parse_extraction_jsonl(text)
    assert [e.id for e in ents] == ["m0", "m1"]
    assert len(rels) == 1
    assert rels[0].source_id == "m0" and rels[0].target_id == "m1"
    assert bad == 0
    assert dropped is False


def test_truncated_last_line_is_dropped_rest_salvaged():
    """A response cut mid-last-line yields N-1 complete records, dropping only the
    partial — the whole point of JSONL over the single-document parse."""
    text = (
        _entity_line(0) + "\n"
        + _entity_line(1) + "\n"
        + _entity_line(2) + "\n"
        + '{"kind":"entity","type_name":"Model","id":"m3"'  # truncated, no close
    )
    ents, rels, bad, dropped = SchemaResolver._parse_extraction_jsonl(text)
    assert [e.id for e in ents] == ["m0", "m1", "m2"]
    assert dropped is True
    assert bad == 0  # the failure was the LAST line, not a middle one


def test_malformed_middle_line_skipped_not_fatal():
    """One malformed middle line is skipped (counted) without sinking the batch —
    the complete records before and after it still parse."""
    text = "\n".join(
        [
            _entity_line(0),
            "{ this is not valid json",
            _entity_line(1),
            _rel_line("m0", "rel", "m1"),
        ]
    )
    ents, rels, bad, dropped = SchemaResolver._parse_extraction_jsonl(text)
    assert [e.id for e in ents] == ["m0", "m1"]
    assert len(rels) == 1
    assert bad == 1
    assert dropped is False  # last line was the (valid) relationship


def test_kind_inferred_from_shape_when_absent():
    """A record missing the ``kind`` discriminator is classified by shape:
    source_id + target_id → relationship, else entity."""
    text = "\n".join(
        [
            json.dumps({"type_name": "Model", "id": "m0"}),  # no kind → entity
            json.dumps({"source_id": "m0", "predicate": "rel", "target_id": "m1"}),  # → rel
        ]
    )
    ents, rels, bad, dropped = SchemaResolver._parse_extraction_jsonl(text)
    assert [e.id for e in ents] == ["m0"]
    assert len(rels) == 1 and rels[0].predicate == "rel"
    assert bad == 0 and dropped is False


def test_code_fences_and_blank_lines_tolerated():
    """Stray code fences / blank lines around the JSONL are ignored, not fatal."""
    text = "```jsonl\n" + _entity_line(0) + "\n\n" + _entity_line(1) + "\n```"
    ents, rels, bad, dropped = SchemaResolver._parse_extraction_jsonl(text)
    assert [e.id for e in ents] == ["m0", "m1"]
    # The last non-blank line is the closing fence, which was stripped out; the
    # remaining lines all parse, so nothing is dropped as a partial.
    assert dropped is False


def test_empty_reply_yields_nothing():
    ents, rels, bad, dropped = SchemaResolver._parse_extraction_jsonl("")
    assert ents == [] and rels == []
    assert bad == 0 and dropped is False


def test_entity_attributes_survive_round_trip():
    """Attributes on an entity line are parsed into the model."""
    line = _entity_line(
        0,
        attributes=[{"name": "price", "value": "500000", "datatype": "integer"}],
        parent_chain=["Asset"],
    )
    ents, _, bad, dropped = SchemaResolver._parse_extraction_jsonl(line)
    assert len(ents) == 1
    assert ents[0].attributes[0].name == "price"
    assert ents[0].parent_chain == ["Asset"]
    assert bad == 0 and dropped is False


# --- _extract glue tests (mocked LLM) -----------------------------------------


@pytest.fixture
def resolver(tmp_path):
    import unittest.mock as um

    neptune = um.AsyncMock()
    cache = JsonVerdictCache(tmp_path / "cache.json")
    return SchemaResolver(neptune, "fake-key", cache)


@pytest.mark.asyncio
async def test_extract_salvages_truncated_reply(resolver, monkeypatch):
    """Through the full ``_extract`` path (OpenRouter branch), a truncated JSONL
    reply salvages the complete records and drops the partial last line."""
    monkeypatch.setattr(resolver, "EXTRACT_PROVIDER", "openrouter")
    monkeypatch.setattr(resolver, "_openrouter_key", "or-key")

    async def fake_or(user_content):
        body = (
            _entity_line(0) + "\n"
            + _entity_line(1) + "\n"
            + '{"kind":"entity","type_name":"Model","id":"m2"'  # truncated
        )
        return body, "length"

    monkeypatch.setattr(resolver, "_extract_via_openrouter", fake_or)
    result = await resolver._extract("[{}]", "json", {})
    assert [e.id for e in result.entities] == ["m0", "m1"]


@pytest.mark.asyncio
async def test_extract_clean_reply_matches_document_equivalent(resolver, monkeypatch):
    """A clean JSONL reply parses to the SAME entities+relationships the old
    single-document reply would have — behavior is unchanged on the happy path."""
    monkeypatch.setattr(resolver, "EXTRACT_PROVIDER", "openrouter")
    monkeypatch.setattr(resolver, "_openrouter_key", "or-key")

    async def fake_or(user_content):
        body = "\n".join(
            [
                _entity_line(0, attributes=[{"name": "n", "value": "a", "datatype": "string"}]),
                _entity_line(1),
                _rel_line("m0", "linked_to", "m1"),
            ]
        )
        return body, "stop"

    monkeypatch.setattr(resolver, "_extract_via_openrouter", fake_or)
    result = await resolver._extract("[{}]", "json", {})
    assert [e.id for e in result.entities] == ["m0", "m1"]
    assert result.entities[0].attributes[0].value == "a"
    assert len(result.relationships) == 1
    assert result.relationships[0].source_id == "m0"
    assert result.relationships[0].target_id == "m1"


@pytest.mark.asyncio
async def test_extract_malformed_middle_line_does_not_sink_batch(resolver, monkeypatch):
    """A malformed middle line is skipped; the surrounding complete records still
    land through the full ``_extract`` path."""
    monkeypatch.setattr(resolver, "EXTRACT_PROVIDER", "openrouter")
    monkeypatch.setattr(resolver, "_openrouter_key", "or-key")

    async def fake_or(user_content):
        body = "\n".join([_entity_line(0), "GARBAGE LINE {", _entity_line(1)])
        return body, "stop"

    monkeypatch.setattr(resolver, "_extract_via_openrouter", fake_or)
    result = await resolver._extract("[{}]", "json", {})
    assert [e.id for e in result.entities] == ["m0", "m1"]

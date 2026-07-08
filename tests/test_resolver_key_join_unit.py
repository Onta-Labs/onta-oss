"""CI-runnable (no pyoxigraph) unit tests for join-by-exact-key ingest (ONTA-250).

The companion ``test_resolver_key_join.py`` drives the ACTUAL resolver over a real
triplestore but skips where pyoxigraph is absent (as in CI). These tests cover the
same mechanism with a mock Neptune so the join's query shape + accounting/rebind
logic run in CI too — using INVENTED types/keys (Widget/``sku``) so nothing
overfits.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from cograph_client.graph.ontology_queries import (
    attr_uri,
    entities_by_key_value_query,
    type_uri,
)
from cograph_client.resolver.models import (
    ExtractedAttribute,
    ExtractedEntity,
    IngestResult,
    KeyJoin,
)
from cograph_client.resolver.schema_resolver import SchemaResolver
from cograph_client.resolver.verdict_cache import JsonVerdictCache

KGG = "https://cograph.tech/graphs/t/k"


def _cache() -> JsonVerdictCache:
    c = JsonVerdictCache.__new__(JsonVerdictCache)
    c._path = None
    c._cache = {}
    return c


def _resolver(neptune) -> SchemaResolver:
    r = SchemaResolver(neptune, "fake-key", _cache())
    r._er_enabled = False
    return r


def _widget(entity_id: str, sku: str, **attrs: str) -> ExtractedEntity:
    a = [ExtractedAttribute(name="sku", value=sku, datatype="string")]
    a += [ExtractedAttribute(name=k, value=v, datatype="string") for k, v in attrs.items()]
    return ExtractedEntity(type_name="Widget", id=entity_id, attributes=a)


# --- query builder ------------------------------------------------------------


def test_key_value_query_shape_and_predicate():
    q = entities_by_key_value_query(KGG, "Widget", "sku", ["W-1", "W-2"])
    # Keyed on the canonical attrs/<key> predicate + the type's rdf:type.
    assert f"<{attr_uri('Widget', 'sku')}>" in q
    assert f"<{type_uri('Widget')}>" in q
    # Datatype-agnostic lexical comparison.
    assert "FILTER(STR(?o) = STR(?v))" in q
    # Values batched via VALUES, scoped to the instance graph.
    assert f"FROM <{KGG}>" in q
    assert '("W-1")' in q and '("W-2")' in q


def test_key_value_query_escapes_injection():
    """A key value with a quote / newline is escaped, not injected."""
    q = entities_by_key_value_query(KGG, "Widget", "sku", ['bad" } INJECT', "a\nb"])
    assert 'bad\\" } INJECT' in q  # the inner quote is backslash-escaped
    assert "a\\nb" in q            # newline escaped
    # No bare unescaped quote that would close the VALUES literal early.
    assert '"bad" }' not in q


def test_key_value_query_escapes_cr_and_tab():
    """Interior carriage-return / tab in a CSV key value is escaped, not left raw
    (a raw \\r/\\t makes the SPARQL literal malformed — ONTA-250 review finding)."""
    q = entities_by_key_value_query(KGG, "Widget", "sku", ["a\rb", "c\td"])
    assert "a\\rb" in q and "c\\td" in q
    # No raw control char survives in the emitted query.
    assert "\r" not in q and "\t" not in q


# --- _resolve_key_join accounting + rebind ------------------------------------


def _bindings(pairs: list[tuple[str, str]]) -> dict:
    return {"results": {"bindings": [
        {"v": {"value": v}, "entity": {"value": e}} for v, e in pairs
    ]}}


@pytest.mark.asyncio
async def test_resolve_key_join_merges_matched_and_mints_unmatched():
    neptune = AsyncMock()
    # W-1 matches an existing node; W-2 matches nothing.
    neptune.query.return_value = _bindings([
        ("W-1", "https://cograph.tech/entities/Widget/existing-alpha"),
    ])
    r = _resolver(neptune)

    entities = [_widget("W-1", "W-1", region="west"), _widget("W-2", "W-2", region="east")]
    resolved_types = {"W-1": "Widget", "W-2": "Widget"}
    uri_map = {e.id: f"https://cograph.tech/entities/Widget/{e.id}" for e in entities}
    result = IngestResult()

    skip = await r._resolve_key_join(
        entities, resolved_types, uri_map, KGG,
        KeyJoin(key_attribute="sku"), result,
    )

    # W-1 rebound onto the existing node; W-2 keeps its minted URI.
    assert uri_map["W-1"] == "https://cograph.tech/entities/Widget/existing-alpha"
    assert uri_map["W-2"] == "https://cograph.tech/entities/Widget/W-2"
    assert result.rows_key_merged == 1
    assert result.rows_key_minted == 1
    assert result.rows_key_unmatched == 0
    assert skip == set()


@pytest.mark.asyncio
async def test_resolve_key_join_skips_unmatched_when_mint_disabled():
    neptune = AsyncMock()
    neptune.query.return_value = _bindings([
        ("W-1", "https://cograph.tech/entities/Widget/existing-alpha"),
    ])
    r = _resolver(neptune)

    entities = [_widget("W-1", "W-1"), _widget("W-9", "W-9")]
    resolved_types = {"W-1": "Widget", "W-9": "Widget"}
    uri_map = {e.id: f"https://cograph.tech/entities/Widget/{e.id}" for e in entities}
    result = IngestResult()

    skip = await r._resolve_key_join(
        entities, resolved_types, uri_map, KGG,
        KeyJoin(key_attribute="sku", mint_unmatched=False), result,
    )

    assert result.rows_key_merged == 1
    assert result.rows_key_unmatched == 1
    assert skip == {"W-9"}  # the unmatched-with-a-key row is skipped


@pytest.mark.asyncio
async def test_resolve_key_join_ambiguous_is_not_merged():
    """A key matching >1 existing entity is NOT merged onto an arbitrary one."""
    neptune = AsyncMock()
    neptune.query.return_value = _bindings([
        ("W-1", "https://cograph.tech/entities/Widget/a"),
        ("W-1", "https://cograph.tech/entities/Widget/b"),
    ])
    r = _resolver(neptune)

    entities = [_widget("W-1", "W-1")]
    resolved_types = {"W-1": "Widget"}
    uri_map = {"W-1": "https://cograph.tech/entities/Widget/W-1"}
    result = IngestResult()

    await r._resolve_key_join(
        entities, resolved_types, uri_map, KGG, KeyJoin(key_attribute="sku"), result,
    )
    # Left on its own minted URI (not rebound); counted as minted (mint default).
    assert uri_map["W-1"] == "https://cograph.tech/entities/Widget/W-1"
    assert result.rows_key_merged == 0
    assert result.rows_key_minted == 1


@pytest.mark.asyncio
async def test_resolve_key_join_entity_without_key_always_mints():
    """An entity with no value for the key attribute is never a join candidate —
    it always mints, even under mint_unmatched=false (never skipped)."""
    neptune = AsyncMock()
    neptune.query.return_value = _bindings([])
    r = _resolver(neptune)

    # A stub-like entity carrying no sku attribute at all.
    stub = ExtractedEntity(type_name="Maker", id="Acme", attributes=[])
    entities = [stub]
    resolved_types = {"Acme": "Maker"}
    uri_map = {"Acme": "https://cograph.tech/entities/Maker/Acme"}
    result = IngestResult()

    skip = await r._resolve_key_join(
        entities, resolved_types, uri_map, KGG,
        KeyJoin(key_attribute="sku", mint_unmatched=False), result,
    )
    assert skip == set()               # never skipped
    assert result.rows_key_unmatched == 0
    assert uri_map["Acme"] == "https://cograph.tech/entities/Maker/Acme"


@pytest.mark.asyncio
async def test_resolve_key_join_lookup_failure_degrades_to_mint():
    """A lookup error is best-effort — it degrades to ordinary minting, never raises."""
    neptune = AsyncMock()
    neptune.query.side_effect = RuntimeError("neptune boom")
    r = _resolver(neptune)

    entities = [_widget("W-1", "W-1")]
    resolved_types = {"W-1": "Widget"}
    uri_map = {"W-1": "https://cograph.tech/entities/Widget/W-1"}
    result = IngestResult()

    skip = await r._resolve_key_join(
        entities, resolved_types, uri_map, KGG, KeyJoin(key_attribute="sku"), result,
    )
    assert skip == set()
    assert uri_map["W-1"] == "https://cograph.tech/entities/Widget/W-1"  # minted, unmerged
    assert result.rows_key_merged == 0

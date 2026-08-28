"""CSV mapped ingest: no prefix-cluster types, no TypeMatcher subClassOf."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import inspect

from infona_client.api.routes.ingest import ingest_csv_rows
from infona_client.models.ontology import OntologyOpKind
from infona_client.resolver.csv_resolver import CSVResolver
from infona_client.resolver.models import (
    CSVSchemaMapping,
    ColumnMapping,
    ColumnRole,
    ExtractedAttribute,
    ExtractedEntity,
    IngestResult,
    MatchVerdict,
    TypeMatch,
)
from infona_client.resolver.schema_resolver import SchemaResolver
from infona_client.resolver.schema_type_resolve import coerce_subtype_match
from infona_client.resolver.verdict_cache import JsonVerdictCache

GRAPH = "https://graph.infona.ai/graphs/fid"


def _resolver(tmp_path) -> SchemaResolver:
    n = MagicMock()
    n.query = AsyncMock(return_value={"results": {"bindings": []}})
    n.update = AsyncMock()
    n.batch_exists = AsyncMock(return_value=set())
    r = SchemaResolver(n, "fake-key", JsonVerdictCache(tmp_path / "v.json"))
    r._er_enabled = False
    r._commit_ontology = AsyncMock()
    return r


def test_coerce_subtype_match_maps_subtype_to_different_when_off():
    sub = TypeMatch(
        proposed="Staff", resolved="Staff", verdict=MatchVerdict.SUBTYPE,
        confidence=0.9, is_new=True, parent_type="Group",
    )
    out = coerce_subtype_match(sub, allow_subtype_link=False, proposed="Staff")
    assert out.verdict == MatchVerdict.DIFFERENT
    assert out.parent_type is None
    assert out.resolved == "Staff"
    kept = coerce_subtype_match(sub, allow_subtype_link=True, proposed="Staff")
    assert kept.verdict == MatchVerdict.SUBTYPE
    same = TypeMatch(
        proposed="Staff", resolved="Group", verdict=MatchVerdict.SAME,
        confidence=1.0, is_new=False,
    )
    assert coerce_subtype_match(
        same, allow_subtype_link=False, proposed="Staff",
    ).verdict == MatchVerdict.SAME


@pytest.mark.asyncio
async def test_prefix_promotion_skipped_when_flag_off(tmp_path):
    resolver = _resolver(tmp_path)
    collected: list[tuple[str, str, str]] = []
    result = IngestResult()
    entity = ExtractedEntity(
        type_name="Property",
        id="p1",
        attributes=[
            ExtractedAttribute(name="address_street", value="1 Main", datatype="string"),
            ExtractedAttribute(name="address_city", value="Austin", datatype="string"),
            ExtractedAttribute(name="address_state", value="TX", datatype="string"),
        ],
    )
    await resolver._resolve_and_insert_entity(
        entity, "Property", "https://graph.infona.ai/entities/Property/p1",
        False, GRAPH, {"Property": ""}, {"Property": {}}, "csv", result,
        _collect_triples=collected,
        allow_prefix_promotion=False,
    )
    assert "Address" not in result.types_created
    assert not any("has_address" in p for _s, p, _o in collected)

    collected.clear()
    result_on = IngestResult()
    await resolver._resolve_and_insert_entity(
        entity, "Property", "https://graph.infona.ai/entities/Property/p1",
        False, GRAPH, {"Property": ""}, {"Property": {}}, "csv", result_on,
        _collect_triples=collected,
        allow_prefix_promotion=True,
    )
    assert "Address" in result_on.types_created
    assert any("has_address" in p for _s, p, _o in collected)


@pytest.mark.asyncio
async def test_subtype_match_does_not_write_subclassof_when_flag_off(tmp_path):
    resolver = _resolver(tmp_path)
    mutations: list = []

    async def capture(_graph, muts, **_kwargs):
        mutations.extend(muts)

    resolver._commit_ontology = capture  # type: ignore[method-assign]
    resolver._type_matcher.match = AsyncMock(return_value=TypeMatch(
        proposed="Staff", resolved="Staff", verdict=MatchVerdict.SUBTYPE,
        confidence=0.9, is_new=True, parent_type="Group",
    ))
    existing_types = {"Group": ""}
    existing_attrs = {"Group": {}}
    result = IngestResult()
    entity = ExtractedEntity(
        type_name="Staff", id="s1", parent_type="Group", parent_chain=["Group"],
    )
    resolved = await resolver._resolve_type(
        entity, GRAPH, existing_types, existing_attrs, result,
        allow_subtype_link=False,
    )
    assert resolved == "Staff"
    assert "Staff" in existing_types
    assert all(m.op != OntologyOpKind.SET_SUBCLASS for m in mutations)


@pytest.mark.asyncio
async def test_subtype_match_still_links_when_flag_on(tmp_path):
    resolver = _resolver(tmp_path)
    mutations: list = []

    async def capture(_graph, muts, **_kwargs):
        mutations.extend(muts)

    resolver._commit_ontology = capture  # type: ignore[method-assign]
    resolver._type_matcher.match = AsyncMock(return_value=TypeMatch(
        proposed="Staff", resolved="Staff", verdict=MatchVerdict.SUBTYPE,
        confidence=0.9, is_new=True, parent_type="Group",
    ))
    existing_types = {"Group": ""}
    existing_attrs = {"Group": {}}
    result = IngestResult()
    resolved = await resolver._resolve_type(
        ExtractedEntity(type_name="Staff", id="s1"),
        GRAPH, existing_types, existing_attrs, result,
        allow_subtype_link=True,
    )
    assert resolved == "Staff"
    assert any(m.op == OntologyOpKind.SET_SUBCLASS for m in mutations)


@pytest.mark.asyncio
async def test_ingest_csv_passes_fidelity_flags_into_ingest_mapped(tmp_path, monkeypatch):
    resolver = _resolver(tmp_path)
    captured: dict = {}

    async def fake_infer(self, *args, **kwargs):
        return CSVSchemaMapping(
            entity_type="Note",
            columns=[
                ColumnMapping(
                    column_name="Body",
                    role=ColumnRole.ATTRIBUTE,
                    attribute_name="body",
                    datatype="string",
                )
            ],
        )

    async def fake_mapped(self, *args, **kwargs):
        captured.update(kwargs)
        return IngestResult(rows_in=1)

    monkeypatch.setattr(CSVResolver, "infer_schema", fake_infer)
    monkeypatch.setattr(type(resolver), "_ingest_mapped", fake_mapped)
    result = await resolver._ingest_csv(
        "Body\nhello\n", GRAPH, {}, {}, "csv",
    )
    assert result.rows_in == 1
    assert captured.get("allow_prefix_promotion") is False
    assert captured.get("allow_subtype_link") is False


def test_csv_rows_route_passes_fidelity_flags():
    src = inspect.getsource(ingest_csv_rows)
    assert "allow_prefix_promotion=False" in src
    assert "allow_subtype_link=False" in src

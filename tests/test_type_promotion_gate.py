"""ONTA-383 — Gate attribute→type auto-promotion + focus consolidation + junk guard.

Acceptance (from the ticket brief):
  * Small clean type set under a proposed focus (Institution) — University /
    College land as subtypes, not free-standing peers.
  * No junk types (Colour, Online, InstructionMode class).
  * Fixture asserts type count ≤ K and junk set absent.

All mocked — no live Neptune, no LLM. A FakeTypeMatcher returns DIFFERENT for
any proposed type not already in existing_types so the focus-seed + parent-
injection path can be exercised end-to-end without a model call.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from cograph_client.graph.client import NeptuneClient
from cograph_client.graph.ontology_queries import type_uri
from cograph_client.resolver.attribute_resolver import is_junk_type_name
from cograph_client.resolver.models import (
    ExtractedAttribute,
    ExtractedEntity,
    ExtractedRelationship,
    ExtractionResult,
    IngestResult,
    MatchVerdict,
    TypeMatch,
)
from cograph_client.resolver.schema_resolver import (
    SchemaResolver,
    _primary_entity_ids,
)
from cograph_client.resolver.verdict_cache import JsonVerdictCache


# The junk set from the BC-universities symptom (ticket brief).
_JUNK_TYPES = frozenset({"Colour", "Color", "Online", "InstructionMode", "Mode", "Status"})

# Soft upper bound on types for a focused Institution discovery: focus + a
# handful of subtypes + a few dimension nodes (City/Province). Above this is
# the over-fragmentation the ticket guards against (17 types was the failure).
_TYPE_COUNT_BOUND = 8


class FakeTypeMatcher:
    """DIFFERENT for unknown names; SAME when the proposed name already exists.

    Mirrors the empty-ontology short-circuit so tests never hit embeddings/LLM.
    """

    def __init__(self):
        self._graph_uri = ""

    async def match(self, proposed_type, proposed_description, existing_types):
        for name in existing_types:
            if name.strip().lower() == proposed_type.strip().lower():
                return TypeMatch(
                    proposed=proposed_type,
                    resolved=name,
                    verdict=MatchVerdict.SAME,
                    confidence=1.0,
                    is_new=False,
                )
        return TypeMatch(
            proposed=proposed_type,
            resolved=proposed_type,
            verdict=MatchVerdict.DIFFERENT,
            confidence=1.0,
            is_new=True,
        )


@pytest.fixture
def mock_neptune():
    client = AsyncMock(spec=NeptuneClient)
    client.health.return_value = True
    client.query.return_value = {"head": {"vars": []}, "results": {"bindings": []}}
    client.update.return_value = None
    client.batch_exists.return_value = set()
    return client


@pytest.fixture
def resolver(mock_neptune):
    verdict_path = Path(tempfile.mkdtemp()) / "verdicts.json"
    r = SchemaResolver(
        neptune=mock_neptune,
        anthropic_key="test-key",
        verdict_cache=JsonVerdictCache(verdict_path),
    )
    r._type_matcher = FakeTypeMatcher()
    return r


def _update_sparql(mock_neptune) -> str:
    return " || ".join(c.args[0] for c in mock_neptune.update.call_args_list)


# --------------------------------------------------------------------------- #
# Helpers: primary vs dimension
# --------------------------------------------------------------------------- #


def test_primary_entity_ids_splits_sources_from_dimension_targets():
    extraction = ExtractionResult(
        entities=[
            ExtractedEntity(type_name="University", id="u1"),
            ExtractedEntity(type_name="City", id="c1"),
            ExtractedEntity(type_name="College", id="col1"),  # orphan = primary
        ],
        relationships=[
            ExtractedRelationship(source_id="u1", predicate="located_in", target_id="c1"),
        ],
    )
    primaries = _primary_entity_ids(extraction)
    assert "u1" in primaries
    assert "col1" in primaries
    assert "c1" not in primaries  # dimension-only target


# --------------------------------------------------------------------------- #
# Junk-type rejection on _resolve_type
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_junk_primary_retypes_to_focus(resolver, mock_neptune):
    """A primary record typed Colour under focus=Institution becomes Institution."""
    existing_types: dict[str, str] = {"Institution": ""}
    existing_attrs: dict[str, dict] = {"Institution": {}}
    result = IngestResult(entities_extracted=1)
    entity = ExtractedEntity(
        type_name="Colour",
        id="bad-1",
        attributes=[ExtractedAttribute(name="name", value="Red", datatype="string")],
    )
    resolved = await resolver._resolve_type(
        entity, "g", existing_types, existing_attrs, result,
        focus_types=["Institution"],
        is_primary=True,
    )
    assert resolved == "Institution"
    assert "Colour" not in result.types_created
    assert "Colour" not in existing_types


@pytest.mark.asyncio
async def test_junk_dimension_is_skipped(resolver, mock_neptune):
    """A dimension-only junk type (Online) is refused — no type minted."""
    existing_types: dict[str, str] = {"Institution": ""}
    existing_attrs: dict[str, dict] = {"Institution": {}}
    result = IngestResult(entities_extracted=1)
    entity = ExtractedEntity(type_name="Online", id="dim-1")
    resolved = await resolver._resolve_type(
        entity, "g", existing_types, existing_attrs, result,
        focus_types=["Institution"],
        is_primary=False,
    )
    assert resolved is None
    assert "Online" not in result.types_created


@pytest.mark.asyncio
async def test_instruction_mode_never_minted(resolver, mock_neptune):
    existing_types: dict[str, str] = {}
    existing_attrs: dict[str, dict] = {}
    result = IngestResult(entities_extracted=1)
    entity = ExtractedEntity(type_name="InstructionMode", id="im-1")
    resolved = await resolver._resolve_type(
        entity, "g", existing_types, existing_attrs, result,
        focus_types=None,
        is_primary=True,
    )
    assert resolved is None
    assert "InstructionMode" not in result.types_created


# --------------------------------------------------------------------------- #
# Focus consolidation — University / College under Institution
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_primary_without_parent_anchors_under_focus(resolver, mock_neptune):
    """University with no parent under focus=Institution becomes a subtype of it."""
    existing_types: dict[str, str] = {"Institution": ""}
    existing_attrs: dict[str, dict] = {"Institution": {}}
    parent_of: dict[str, str] = {}
    result = IngestResult(entities_extracted=1)
    entity = ExtractedEntity(
        type_name="University",
        id="ubc",
        attributes=[
            ExtractedAttribute(name="name", value="UBC", datatype="string"),
            ExtractedAttribute(name="website", value="https://ubc.ca", datatype="string"),
        ],
    )
    resolved = await resolver._resolve_type(
        entity, "g", existing_types, existing_attrs, result,
        focus_types=["Institution"],
        is_primary=True,
        parent_of=parent_of,
    )
    assert resolved == "University"
    assert "University" in result.types_created
    sparql = _update_sparql(mock_neptune)
    assert type_uri("University") in sparql
    assert type_uri("Institution") in sparql
    # Parent linkage recorded on the call-local map.
    assert parent_of.get("University") == "Institution"


@pytest.mark.asyncio
async def test_dimension_city_not_forced_under_focus(resolver, mock_neptune):
    """City as a dimension-only node is free-minted, not forced under Institution."""
    existing_types: dict[str, str] = {"Institution": ""}
    existing_attrs: dict[str, dict] = {"Institution": {}}
    parent_of: dict[str, str] = {}
    result = IngestResult(entities_extracted=1)
    entity = ExtractedEntity(
        type_name="City",
        id="vancouver",
        attributes=[ExtractedAttribute(name="name", value="Vancouver", datatype="string")],
    )
    resolved = await resolver._resolve_type(
        entity, "g", existing_types, existing_attrs, result,
        focus_types=["Institution"],
        is_primary=False,  # dimension
        parent_of=parent_of,
    )
    assert resolved == "City"
    assert parent_of.get("City") != "Institution"


@pytest.mark.asyncio
async def test_focus_types_seeded_before_resolve(resolver, mock_neptune):
    existing_types: dict[str, str] = {}
    existing_attrs: dict[str, dict] = {}
    result = IngestResult(entities_extracted=0)
    await resolver._ensure_focus_types(
        ["Institution"], "g", existing_types, existing_attrs, result,
    )
    assert "Institution" in existing_types
    assert "Institution" in result.types_created
    assert type_uri("Institution") in _update_sparql(mock_neptune)


# --------------------------------------------------------------------------- #
# End-to-end fixture: type count bound + junk absence under soft focus
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_institution_batch_type_count_bound_and_junk_absent(resolver, mock_neptune):
    """BC-universities-shaped extraction under proposed_type=Institution.

    Plants the failure modes from the ticket:
      * University / College / PublicInstitution as peer types (should consolidate)
      * Colour / Online / InstructionMode junk types (must be absent)
      * City as a legitimate dimension node

    Asserts:
      * no junk type in types_created / existing_types
      * total distinct types ≤ _TYPE_COUNT_BOUND
      * focus Institution exists; University & College exist as types
      * subtypes are anchored under Institution
    """
    extraction = ExtractionResult(
        entities=[
            ExtractedEntity(
                type_name="University",
                id="ubc",
                attributes=[
                    ExtractedAttribute(name="name", value="UBC", datatype="string"),
                    ExtractedAttribute(name="website", value="https://ubc.ca", datatype="string"),
                ],
            ),
            ExtractedEntity(
                type_name="College",
                id="langara",
                attributes=[
                    ExtractedAttribute(name="name", value="Langara", datatype="string"),
                    ExtractedAttribute(name="website", value="https://langara.ca", datatype="string"),
                ],
            ),
            ExtractedEntity(
                type_name="PublicInstitution",
                id="bcit",
                attributes=[
                    ExtractedAttribute(name="name", value="BCIT", datatype="string"),
                ],
            ),
            # Junk types the old auto-promotion / free mint would create.
            # Colour is a mis-typed PRIMARY (retyped to focus); Online /
            # InstructionMode are dimension-only targets (skipped entirely).
            ExtractedEntity(
                type_name="Colour",
                id="junk-colour",
                attributes=[
                    ExtractedAttribute(name="colour_primary", value="blue", datatype="string"),
                    ExtractedAttribute(name="colour_secondary", value="white", datatype="string"),
                    ExtractedAttribute(name="colour_accent", value="gold", datatype="string"),
                ],
            ),
            ExtractedEntity(type_name="Online", id="junk-online"),
            ExtractedEntity(type_name="InstructionMode", id="junk-im"),
            # Legitimate dimension:
            ExtractedEntity(
                type_name="City",
                id="vancouver",
                attributes=[
                    ExtractedAttribute(name="name", value="Vancouver", datatype="string"),
                ],
            ),
        ],
        relationships=[
            ExtractedRelationship(source_id="ubc", predicate="located_in", target_id="vancouver"),
            ExtractedRelationship(source_id="langara", predicate="located_in", target_id="vancouver"),
            ExtractedRelationship(source_id="bcit", predicate="located_in", target_id="vancouver"),
            # Make Online / InstructionMode dimension-only (targets only).
            ExtractedRelationship(source_id="ubc", predicate="offered_as", target_id="junk-online"),
            ExtractedRelationship(source_id="ubc", predicate="has_mode", target_id="junk-im"),
        ],
    )

    existing_types: dict[str, str] = {}
    existing_attrs: dict[str, dict] = {}
    result = IngestResult(entities_extracted=len(extraction.entities))
    parent_of: dict[str, str] = {}
    focus_types = ["Institution"]

    await resolver._ensure_focus_types(
        focus_types, "g", existing_types, existing_attrs, result,
    )
    primary_ids = _primary_entity_ids(extraction)

    resolved: dict[str, str | None] = {}
    for entity in extraction.entities:
        rt = await resolver._resolve_type(
            entity, "g", existing_types, existing_attrs, result,
            parent_of=parent_of,
            focus_types=focus_types,
            is_primary=entity.id in primary_ids,
        )
        resolved[entity.id] = rt

    # Junk primaries retyped to Institution; junk dimensions skipped.
    assert resolved["junk-colour"] == "Institution"
    assert resolved["junk-online"] is None
    assert resolved["junk-im"] is None

    # Focus + subtypes + City present.
    assert "Institution" in existing_types
    assert resolved["ubc"] == "University"
    assert resolved["langara"] == "College"
    assert resolved["bcit"] == "PublicInstitution"
    assert resolved["vancouver"] == "City"

    created = set(result.types_created) | set(existing_types)
    # Junk set absent.
    for junk in _JUNK_TYPES:
        assert junk not in created, f"junk type {junk} was minted"
        assert not any(is_junk_type_name(t) for t in created if t in _JUNK_TYPES)

    # Type count bound — focus + a few subtypes + City, not 17.
    assert len(created) <= _TYPE_COUNT_BOUND, (
        f"type count {len(created)} > bound {_TYPE_COUNT_BOUND}: {sorted(created)}"
    )
    # Expected set is a subset of {Institution, University, College,
    # PublicInstitution, City} — at most 5.
    assert created <= {
        "Institution", "University", "College", "PublicInstitution", "City",
    }

    # Subtypes anchored under Institution.
    for child in ("University", "College", "PublicInstitution"):
        assert parent_of.get(child) == "Institution", (
            f"{child} was not consolidated under Institution; parent_of={parent_of}"
        )

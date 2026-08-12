"""ONTA-383 — Gate attribute→type auto-promotion + focus consolidation + junk guard.

Acceptance (from the ticket brief):
  * Small clean type set under a proposed focus (Institution).
  * No junk types (Colour, Online, InstructionMode class).
  * Fixture asserts type count ≤ K and junk set absent.

ONTA-394 UPDATE: accidental subtypes now COLLAPSE into the confirmed focus by
default (``_DISCOVERY_COLLAPSE_SUBTYPES=1``) instead of being anchored as
subtypes — so University / College / PublicInstitution under an Institution focus
all resolve to Institution and NO unconfirmed subtype collection is minted. The
ONTA-383 anchor-as-subtype behavior is preserved behind
``INFONA_DISCOVERY_COLLAPSE_SUBTYPES=0`` and covered by the flag-off tests below.

All mocked — no live Neptune, no LLM. A FakeTypeMatcher returns DIFFERENT for
any proposed type not already in existing_types so the focus-seed + parent-
injection path can be exercised end-to-end without a model call.

**Ported by ONTA-527.** Two SPARQL-era assumptions came out:

* the graph URI was the placeholder ``"g"``. Type minting now runs through
  ``ontology_commit`` → ``ontology_catalog.upsert_type``, which derives the
  catalog scope from ``/graphs/<tenant>`` in that URI, so a placeholder raises
  ``GraphScopeError``. Every call passes the real tenant ontology graph now.
* "was the type minted?" was read off the SPARQL text
  ``mock_neptune.update`` was handed. No type write emits SPARQL any more, so
  that assertion had become vacuous — it would have passed had the mint stopped
  happening entirely. It is replaced by a read of the tenant ontology CATALOG
  (``ontology_catalog.list_types``), i.e. the thing the mint actually produces,
  plus ``mock_neptune.update.assert_not_called()`` to prove the catalog path is
  what ran.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from infona_client.graph.client import NeptuneClient
from infona_client.graph.iri import IRI_BASE
from infona_client.graph.ontology_catalog import list_types
from infona_client.resolver.attribute_resolver import is_junk_type_name
from infona_client.resolver.models import (
    ExtractedAttribute,
    ExtractedEntity,
    ExtractedRelationship,
    ExtractionResult,
    IngestResult,
    MatchVerdict,
    TypeMatch,
)
from infona_client.resolver import schema_resolver
from infona_client.resolver.schema_resolver import (
    SchemaResolver,
    _primary_entity_ids,
)
from infona_client.resolver.verdict_cache import JsonVerdictCache


TENANT = "t1"
# The tenant ontology graph. `_commit_ontology` recovers the tenant from
# `/graphs/<tenant>` here to scope the catalog session, so this must be a real
# tenant graph URI — the old `"g"` placeholder now raises GraphScopeError.
TENANT_GRAPH = f"{IRI_BASE}/graphs/{TENANT}"

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


async def _minted_types(mock_neptune) -> set[str]:
    """Type names actually present in the tenant ontology catalog.

    Replaces the old ``_update_sparql`` scan: a mint is a catalog row now, not a
    SPARQL string. ``assert_not_called`` is part of the assertion — it proves the
    rows below came from the GraphStore path rather than a surviving SPARQL one.
    """
    mock_neptune.update.assert_not_called()
    return {t.name for t in await list_types(tenant_id=TENANT)}


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
        entity, TENANT_GRAPH, existing_types, existing_attrs, result,
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
        entity, TENANT_GRAPH, existing_types, existing_attrs, result,
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
        entity, TENANT_GRAPH, existing_types, existing_attrs, result,
        focus_types=None,
        is_primary=True,
    )
    assert resolved is None
    assert "InstructionMode" not in result.types_created


# --------------------------------------------------------------------------- #
# Focus consolidation — University / College under Institution
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_primary_new_subtype_collapses_to_focus(resolver, mock_neptune):
    """ONTA-394 default: a NEW subtype (University) under focus=Institution
    collapses to Institution — no University collection is minted."""
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
        entity, TENANT_GRAPH, existing_types, existing_attrs, result,
        focus_types=["Institution"],
        is_primary=True,
        parent_of=parent_of,
    )
    assert resolved == "Institution"
    assert "University" not in result.types_created
    # Nothing was minted at all: collapse resolves to a type the caller already
    # had, so the catalog stays empty. (The old form asserted the University IRI
    # was absent from the emitted SPARQL, which no longer distinguishes "not
    # minted" from "minted through a path that emits no SPARQL".)
    assert "University" not in await _minted_types(mock_neptune)
    # No accidental subtype anchored.
    assert parent_of.get("University") is None


@pytest.mark.asyncio
async def test_primary_without_parent_anchors_under_focus_when_collapse_off(
    resolver, mock_neptune, monkeypatch
):
    """ONTA-383 fallback (INFONA_DISCOVERY_COLLAPSE_SUBTYPES=0): University with
    no parent under focus=Institution is anchored as a subtype of it."""
    monkeypatch.setattr(schema_resolver, "_DISCOVERY_COLLAPSE_SUBTYPES", False)
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
        entity, TENANT_GRAPH, existing_types, existing_attrs, result,
        focus_types=["Institution"],
        is_primary=True,
        parent_of=parent_of,
    )
    assert resolved == "University"
    assert "University" in result.types_created
    # Both the subtype and its focus parent exist in the tenant catalog, and the
    # SUBCLASS_OF edge is on the row rather than inferred from SPARQL text.
    minted = await _minted_types(mock_neptune)
    assert {"University", "Institution"} <= minted
    (university,) = [
        t for t in await list_types(tenant_id=TENANT) if t.name == "University"
    ]
    assert university.parent_type == "Institution"
    # Parent linkage recorded on the call-local map.
    assert parent_of.get("University") == "Institution"


@pytest.mark.asyncio
async def test_same_as_to_existing_type_is_preserved_over_collapse(resolver, mock_neptune):
    """ONTA-394: an explicit same_as to an EXISTING non-focus type is a de-dup, not
    an accidental subtype — collapse must NOT override it. The record resolves to
    the same_as target, not the focus."""

    class SameAsMatcher:
        async def match(self, proposed_type, proposed_description, existing_types):
            # Verifies the extractor's same_as claim: College == the existing
            # TradeSchool (SAME), resolved to that existing type.
            return TypeMatch(
                proposed=proposed_type,
                resolved="TradeSchool",
                verdict=MatchVerdict.SAME,
                confidence=1.0,
                is_new=False,
            )

    resolver._type_matcher = SameAsMatcher()
    existing_types: dict[str, str] = {"Institution": "", "TradeSchool": ""}
    existing_attrs: dict[str, dict] = {"Institution": {}, "TradeSchool": {}}
    entity = ExtractedEntity(
        type_name="College",
        id="x",
        same_as="TradeSchool",
        attributes=[ExtractedAttribute(name="name", value="X", datatype="string")],
    )
    resolved = await resolver._resolve_type(
        entity, TENANT_GRAPH, existing_types, existing_attrs, IngestResult(entities_extracted=1),
        focus_types=["Institution"],
        is_primary=True,
    )
    assert resolved == "TradeSchool"   # same_as respected, NOT collapsed to Institution


@pytest.mark.asyncio
async def test_existing_subtype_is_reused_not_collapsed(resolver, mock_neptune):
    """A subtype ALREADY in the ontology (a prior confirmed College) is REUSED,
    never collapsed — collapse only suppresses BRAND-NEW accidental subtypes."""
    existing_types: dict[str, str] = {"Institution": "", "College": ""}
    existing_attrs: dict[str, dict] = {"Institution": {}, "College": {}}
    result = IngestResult(entities_extracted=1)
    entity = ExtractedEntity(
        type_name="College",
        id="langara",
        attributes=[ExtractedAttribute(name="name", value="Langara", datatype="string")],
    )
    resolved = await resolver._resolve_type(
        entity, TENANT_GRAPH, existing_types, existing_attrs, result,
        focus_types=["Institution"],
        is_primary=True,
    )
    assert resolved == "College"


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
        entity, TENANT_GRAPH, existing_types, existing_attrs, result,
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
        ["Institution"], TENANT_GRAPH, existing_types, existing_attrs, result,
    )
    assert "Institution" in existing_types
    assert "Institution" in result.types_created
    # Seeded for real: the focus type is a row in the tenant ontology catalog,
    # not merely an entry in the caller's in-memory dict.
    assert "Institution" in await _minted_types(mock_neptune)


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

    Asserts (ONTA-394 collapse default):
      * no junk type in types_created / existing_types
      * total distinct types ≤ _TYPE_COUNT_BOUND
      * every accidental primary subtype collapses to Institution — no
        University / College / PublicInstitution collections are minted
      * City stays a legitimate dimension node
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
        focus_types, TENANT_GRAPH, existing_types, existing_attrs, result,
    )
    primary_ids = _primary_entity_ids(extraction)

    resolved: dict[str, str | None] = {}
    for entity in extraction.entities:
        rt = await resolver._resolve_type(
            entity, TENANT_GRAPH, existing_types, existing_attrs, result,
            parent_of=parent_of,
            focus_types=focus_types,
            is_primary=entity.id in primary_ids,
        )
        resolved[entity.id] = rt

    # Junk primaries retyped to Institution; junk dimensions skipped.
    assert resolved["junk-colour"] == "Institution"
    assert resolved["junk-online"] is None
    assert resolved["junk-im"] is None

    # ONTA-394: every accidental primary subtype collapses to the focus; City
    # (a dimension node) stays free-minted.
    assert "Institution" in existing_types
    assert resolved["ubc"] == "Institution"
    assert resolved["langara"] == "Institution"
    assert resolved["bcit"] == "Institution"
    assert resolved["vancouver"] == "City"

    created = set(result.types_created) | set(existing_types)
    # Junk set absent.
    for junk in _JUNK_TYPES:
        assert junk not in created, f"junk type {junk} was minted"
        assert not any(is_junk_type_name(t) for t in created if t in _JUNK_TYPES)

    # No unconfirmed subtype collections were minted.
    for accidental in ("University", "College", "PublicInstitution"):
        assert accidental not in created, (
            f"{accidental} was minted; should have collapsed to Institution"
        )

    # Type count bound — just focus + City after collapse.
    assert len(created) <= _TYPE_COUNT_BOUND, (
        f"type count {len(created)} > bound {_TYPE_COUNT_BOUND}: {sorted(created)}"
    )
    assert created == {"Institution", "City"}

    # …and the same bound holds for what actually LANDED in the tenant ontology
    # catalog, not just for the resolver's bookkeeping. This is the assertion the
    # ticket's "type count ≤ K" acceptance criterion is really about: the
    # in-memory dicts above would still look clean if a junk type were written to
    # the catalog behind them.
    assert await _minted_types(mock_neptune) == {"Institution", "City"}

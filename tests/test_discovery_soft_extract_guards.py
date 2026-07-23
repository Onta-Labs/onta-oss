"""ONTA-394 — P2 discovery soft-extract guards against amplifying skewed A1 rows.

The dogfood (job 7c6edadd): 44 keyed A1 rows → 91 entities, with type drift to a
``College`` collection, ``city ->`` relationship edges pointing at academic-calendar
junk nodes, and a fabricated ``website_city`` compound attribute. Even given a cell,
soft extract over-mints types, promotes garbage literals to relationship nodes, and
invents compound attributes. These tests pin the deterministic guards that stop each
amplification:

  * AC#2 — ``_drop_offplan_compound_attributes`` drops ``website_city`` (a merge of
    two requested plan attrs) even when the plan attrs are illustrative.
  * AC#3 — ``_is_implausible_node_label`` + the relationship-target branch keep a
    skewed ``city`` value (a year, a nav slug) as a LITERAL instead of minting a
    junk City node + ``city ->`` edge. A real place value still promotes.
  * AC#4 — ``_resolve_type`` collapses an accidental subtype (College under an
    Institution focus) into the focus so no unconfirmed collection is minted
    (covered by ``test_type_promotion_gate.py``; the collapse toggle is exercised
    there).

All mocked — no live Neptune, no LLM. Pure functions are called directly; the
promotion branch is driven through ``_resolve_and_insert_entity`` with a triple
collector (mirrors ``test_discovery_node_linking.py``).
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from cograph_client.graph.ontology_queries import entity_uri, type_uri
from cograph_client.resolver.attribute_resolver import AttributeSchema
from cograph_client.resolver.models import (
    CleanOutcome,
    ExtractedAttribute,
    ExtractedEntity,
    ExtractedRelationship,
    ExtractionConstraint,
    ExtractionResult,
    IngestResult,
)
from cograph_client.resolver.schema_resolver import (
    SchemaResolver,
    _drop_offplan_compound_attributes,
    _is_implausible_node_label,
)
from cograph_client.resolver.verdict_cache import JsonVerdictCache

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
PHYS_URI = entity_uri("Physician", "p1")
LOCATED_IN = "https://cograph.tech/onto/located_in"

# Plan attrs from the dogfood: {name, city, website, address}.
_PLAN = {"Institution": ["name", "city", "website", "address"]}


# --------------------------------------------------------------------------- #
# AC#3 — value-plausibility gate (pure function)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value",
    [
        "2020",                              # bare year
        "42",                                # bare number
        "1998-99",                           # numeric range
        "2020–2021",                         # en-dash range
        "UBC_Academic_Calendar",             # nav slug + "calendar"
        "WCC_-_Western_Community_Colle…",     # truncated nav breadcrumb
        "LaSalle_College_Vancouver_...",     # truncated + slug
        "https://ubc.ca/calendar",           # URL
        "www.langara.ca",                    # bare host
        "Read more",                         # nav chrome
        "",                                  # empty
        None,                                # missing
        "   ",                               # whitespace only
    ],
)
def test_implausible_node_labels_are_flagged(value):
    assert _is_implausible_node_label(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "San Francisco",
        "New York City",
        "Vancouver",
        "AcmeCorp",
        "Saint John's",       # apostrophe, real place
        "Coquitlam",
        "St. Louis",
    ],
)
def test_real_entity_labels_pass(value):
    assert _is_implausible_node_label(value) is False


# --------------------------------------------------------------------------- #
# AC#2 — compound-of-plan attribute drop (pure function)
# --------------------------------------------------------------------------- #


def _constraint(*, soft=True, exhaustive=False, attributes=None):
    return ExtractionConstraint(
        types=["Institution"],
        attributes=attributes if attributes is not None else _PLAN,
        soft=soft,
        attributes_exhaustive=exhaustive,
    )


def test_website_city_compound_dropped_even_when_illustrative():
    """The dogfood's ``website_city`` (website + city) is dropped — and it is
    dropped WITHOUT attributes_exhaustive (ONTA-382's ceiling would not fire)."""
    result = ExtractionResult(
        entities=[
            ExtractedEntity(
                type_name="Institution",
                id="ubc",
                attributes=[
                    ExtractedAttribute(name="name", value="UBC", datatype="string"),
                    ExtractedAttribute(name="city", value="Vancouver", datatype="string"),
                    ExtractedAttribute(name="website", value="https://ubc.ca", datatype="string"),
                    ExtractedAttribute(name="website_city", value="ubc.ca Vancouver", datatype="string"),
                    ExtractedAttribute(name="address_city", value="X, Vancouver", datatype="string"),
                ],
            ),
        ],
        relationships=[],
    )
    out = _drop_offplan_compound_attributes(result, _constraint(exhaustive=False))
    kept = {a.name for a in out.entities[0].attributes}
    assert kept == {"name", "city", "website"}
    # Both compounds ledgered as DROPPED with the ONTA-394 reason.
    dropped = {d.attribute for d in out.ceiling_drops}
    assert dropped == {"website_city", "address_city"}
    assert all(d.outcome == CleanOutcome.DROPPED for d in out.ceiling_drops)
    assert all(d.reason == "compound_plan_attribute" for d in out.ceiling_drops)


def test_real_multiword_attr_and_offtype_dimension_untouched():
    """A real multi-word attr whose tokens are NOT separate plan attrs survives on
    the subject; a lifted DIMENSION node (a relationship TARGET, not a primary) is
    unrestricted even for a plan-compound name."""
    result = ExtractionResult(
        entities=[
            ExtractedEntity(
                type_name="Institution",
                id="ubc",
                attributes=[
                    ExtractedAttribute(name="name", value="UBC", datatype="string"),
                    # tokens {postal, code} — neither is a plan attr → kept.
                    ExtractedAttribute(name="postal_code", value="V6T", datatype="string"),
                    # tokens {address, line} — only "address" is a plan attr → kept.
                    ExtractedAttribute(name="address_line", value="2329 West Mall", datatype="string"),
                ],
            ),
            ExtractedEntity(
                type_name="City",  # lifted DIMENSION (relationship target below)
                id="van",
                attributes=[
                    # a plan-compound name is UNTOUCHED on an off-type dimension node.
                    ExtractedAttribute(name="website_city", value="x", datatype="string"),
                ],
            ),
        ],
        relationships=[
            ExtractedRelationship(source_id="ubc", predicate="located_in", target_id="van"),
        ],
    )
    out = _drop_offplan_compound_attributes(result, _constraint())
    inst = next(e for e in out.entities if e.id == "ubc")
    city = next(e for e in out.entities if e.id == "van")
    assert {a.name for a in inst.attributes} == {"name", "postal_code", "address_line"}
    assert {a.name for a in city.attributes} == {"website_city"}  # dimension untouched
    assert not out.ceiling_drops


def test_evidence_free_subtype_compound_dropped():
    """Dogfood shape (ONTA-394 review gap): a compound attr on an EVIDENCE-FREE
    near-synonym subtype — ``College``, empty parent_chain, orphan primary — is
    still dropped. This is the record AC#4's collapse folds into the focus, so the
    AC#2 backstop must reach it even though it is NOT focus-typed at drop time."""
    result = ExtractionResult(
        entities=[
            ExtractedEntity(
                type_name="College",  # NOT the focus; no parent_chain / same_as
                id="langara",
                attributes=[
                    ExtractedAttribute(name="name", value="Langara", datatype="string"),
                    ExtractedAttribute(name="website_city", value="langara.ca Vancouver", datatype="string"),
                ],
            ),
        ],
        relationships=[],  # orphan ⇒ primary record
    )
    out = _drop_offplan_compound_attributes(result, _constraint(exhaustive=False))
    assert {a.name for a in out.entities[0].attributes} == {"name"}
    assert [d.attribute for d in out.ceiling_drops] == ["website_city"]


def test_compound_drop_noop_for_hard_or_inactive_constraint():
    ent = ExtractedEntity(
        type_name="Institution",
        id="ubc",
        attributes=[ExtractedAttribute(name="website_city", value="x", datatype="string")],
    )
    result = ExtractionResult(entities=[ent], relationships=[])
    # HARD mode already ceilings — this guard is a no-op there.
    hard = _drop_offplan_compound_attributes(result, _constraint(soft=True, attributes=_PLAN))
    assert len(hard.entities[0].attributes) == 0  # soft: dropped
    hard2 = _drop_offplan_compound_attributes(
        result, ExtractionConstraint(types=["Institution"], attributes=_PLAN, soft=False)
    )
    assert {a.name for a in hard2.entities[0].attributes} == {"website_city"}  # untouched
    # Inactive constraint (no types) → unchanged.
    inactive = _drop_offplan_compound_attributes(result, ExtractionConstraint())
    assert inactive is result


# --------------------------------------------------------------------------- #
# AC#3 — end-to-end: skewed city value stays literal, real value still promotes
# --------------------------------------------------------------------------- #


async def _drive_promotion(tmp_path, value: str, *, city_exists: bool):
    """Fire the promotion branch for ``Physician.located_in = value`` (range City).

    ``city_exists`` toggles the WARM path (City + located_in->City already
    declared) vs COLD start (City must be minted). Returns (collected, result).
    """
    resolver = SchemaResolver(AsyncMock(), "fake-key", JsonVerdictCache(tmp_path / "c.json"))
    result = IngestResult()
    collected: list[tuple[str, str, str]] = []
    existing_types = {"Physician": ""}
    existing_attrs: dict[str, dict] = {"Physician": {}}
    if city_exists:
        existing_types["City"] = ""
        existing_attrs["Physician"] = {"located_in": AttributeSchema("located_in", "City")}
        existing_attrs["City"] = {}
    await resolver._resolve_and_insert_entity(
        entity=ExtractedEntity(
            type_name="Physician",
            id="p1",
            attributes=[ExtractedAttribute(name="located_in", value=value, datatype="City")],
        ),
        resolved_type="Physician",
        entity_uri=PHYS_URI,
        is_duplicate=False,
        graph_uri="https://omnix.dev/graphs/test",
        existing_types=existing_types,
        existing_attrs=existing_attrs,
        source="test",
        result=result,
        _collect_triples=collected,
    )
    return collected, result


@pytest.mark.asyncio
async def test_skewed_year_city_value_kept_literal(tmp_path):
    """A ``city`` value of "2020" is NOT minted as a City node — no edge, no node,
    no City type; the value survives as a literal."""
    collected, result = await _drive_promotion(tmp_path, "2020", city_exists=True)
    bad_target = entity_uri("City", "2020")
    # No relationship edge, no minted/typed junk node.
    assert (PHYS_URI, LOCATED_IN, bad_target) not in collected
    assert not any(p == LOCATED_IN for _, p, _ in collected)
    assert not any(p == RDF_TYPE and o == type_uri("City") for _, p, o in collected)
    assert "City" not in result.node_target_types
    # The value is still recorded as a literal fact (nothing silently dropped).
    assert result.triples_inserted >= 1


@pytest.mark.asyncio
async def test_navjunk_city_value_kept_literal_cold_start(tmp_path):
    """A nav-slug ``city`` value never mints a City type on the cold-start path."""
    collected, result = await _drive_promotion(
        tmp_path, "UBC_Academic_Calendar", city_exists=False
    )
    assert not any(p == LOCATED_IN for _, p, _ in collected)
    assert "City" not in result.types_created
    assert "City" not in result.node_target_types


@pytest.mark.asyncio
async def test_real_city_value_still_promotes(tmp_path):
    """Regression: a legitimate place value still becomes a first-class City node
    (the value gate is conservative — it must not block real entities)."""
    collected, result = await _drive_promotion(tmp_path, "San Francisco", city_exists=True)
    target = entity_uri("City", "San Francisco")
    assert (PHYS_URI, LOCATED_IN, target) in collected          # edge -> node
    assert (target, RDF_TYPE, type_uri("City")) in collected    # typed
    assert (target, RDFS_LABEL, "San Francisco") in collected   # labelled
    assert "City" in result.node_target_types

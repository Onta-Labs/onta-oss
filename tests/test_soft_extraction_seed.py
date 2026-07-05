"""Soft (seed) extraction mode — the discovery decomposition fix.

The HARD ExtractionConstraint (ONTA-199) flattens discovery to one literal-only
type: it drops off-type entities, strips lineage, and deletes relationships, which
mis-typed subtypes and demoted real-world values (city, specialty) to literals.
SOFT mode keeps the confirmed type + attributes as a PRIOR that orients extraction
(focused + compact) while letting the extractor decompose faithfully; the
post-extraction guard becomes a no-op.

These are pure-function tests (no LLM): the model field, prompt-template
selection, and the apply-guard branch.
"""
from __future__ import annotations

from cograph_client.resolver import schema_resolver as sr
from cograph_client.resolver.models import (
    ExtractedAttribute,
    ExtractedEntity,
    ExtractedRelationship,
    ExtractionConstraint,
    ExtractionResult,
)


def test_constraint_soft_field_defaults_false():
    c = ExtractionConstraint(types=["Physician"], attributes={"Physician": ["name"]})
    assert c.soft is False
    assert c.is_active is True
    assert ExtractionConstraint(types=["X"], attributes={}, soft=True).soft is True


def test_build_block_selects_template_by_mode():
    hard = ExtractionConstraint(
        types=["Physician"], attributes={"Physician": ["specialty", "city"]}
    )
    soft = ExtractionConstraint(
        types=["Physician"], attributes={"Physician": ["specialty", "city"]}, soft=True
    )
    hard_block = sr._build_constraint_user_block(hard)
    soft_block = sr._build_constraint_user_block(soft)

    # hard = the flat CONSTRAINT cage; soft = the FOCUS/seed prior.
    assert "CONSTRAINT" in hard_block
    assert "ONLY" in hard_block
    assert "FOCUS" in soft_block
    assert "subtypes" in soft_block.lower()
    # both still enumerate the per-type attribute lines
    assert "Physician: specialty, city" in hard_block
    assert "Physician: specialty, city" in soft_block


def test_inactive_constraint_builds_empty_block():
    assert sr._build_constraint_user_block(None) == ""
    assert sr._build_constraint_user_block(
        ExtractionConstraint(types=[], attributes={})
    ) == ""


def _sample_result() -> ExtractionResult:
    # an on-type entity carrying an extra (non-confirmed) attribute + lineage,
    # an OFF-type real-world entity (City) with a parent chain, and an edge.
    physician = ExtractedEntity(
        type_name="Physician",
        id="p1",
        parent_type="HealthcareProvider",
        parent_chain=["HealthcareProvider", "Person"],
        subtype_description="a doctor",
        attributes=[
            ExtractedAttribute(name="name", value="Dr X"),
            ExtractedAttribute(name="extra", value="unwanted"),
        ],
    )
    city = ExtractedEntity(
        type_name="City", id="c1", parent_chain=["Place"], subtype_description="a city"
    )
    rel = ExtractedRelationship(source_id="p1", predicate="located_in", target_id="c1")
    return ExtractionResult(entities=[physician, city], relationships=[rel])


def test_soft_apply_is_a_noop():
    """SOFT mode must not drop off-type entities, strip lineage, filter
    attributes, or delete relationships — the decomposition IS the output."""
    soft = ExtractionConstraint(
        types=["Physician"], attributes={"Physician": ["name"]}, soft=True
    )
    out = sr._apply_extraction_constraint(_sample_result(), soft)

    assert {e.type_name for e in out.entities} == {"Physician", "City"}  # City kept
    assert len(out.relationships) == 1                                    # edge kept
    phys = next(e for e in out.entities if e.type_name == "Physician")
    assert {a.name for a in phys.attributes} == {"name", "extra"}         # attrs kept
    assert phys.parent_chain == ["HealthcareProvider", "Person"]          # lineage kept
    city = next(e for e in out.entities if e.type_name == "City")
    assert city.parent_chain == ["Place"]


def test_hard_apply_still_flattens():
    """HARD mode (the ONTA-199 behavior) must still drop the off-type City, its
    edge, the non-confirmed attribute, and strip lineage — unchanged."""
    hard = ExtractionConstraint(
        types=["Physician"], attributes={"Physician": ["name"]}, soft=False
    )
    out = sr._apply_extraction_constraint(_sample_result(), hard)

    assert {e.type_name for e in out.entities} == {"Physician"}   # City dropped
    assert out.relationships == []                                # edge dropped (target gone)
    phys = out.entities[0]
    assert {a.name for a in phys.attributes} == {"name"}          # 'extra' filtered
    assert phys.parent_chain == []                                # lineage stripped
    assert phys.parent_type is None
    assert phys.subtype_description is None

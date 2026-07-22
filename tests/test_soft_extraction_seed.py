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


# ---------------------------------------------------------------------------
# ONTA-382 — exhaustive attribute set is a CEILING (even under soft extract)
# ---------------------------------------------------------------------------


def test_constraint_attributes_exhaustive_defaults_false():
    c = ExtractionConstraint(types=["X"], attributes={"X": ["name"]})
    assert c.attributes_exhaustive is False
    assert ExtractionConstraint(
        types=["X"], attributes={"X": ["name"]}, attributes_exhaustive=True
    ).attributes_exhaustive is True


def test_soft_illustrative_keeps_extra_attrs_open_mode_regression():
    """Open / illustrative soft mode (attributes_exhaustive=False, the default)
    is unchanged: extra attributes the extractor emits survive the soft guard.
    This is the open-mode regression fixture for ONTA-382."""
    soft = ExtractionConstraint(
        types=["Physician"],
        attributes={"Physician": ["name"]},
        soft=True,
        attributes_exhaustive=False,
    )
    # Per-chunk soft path does not ceiling; the full-batch ceiling also no-ops
    # when exhaustive is False.
    out = sr._apply_extraction_constraint(_sample_result(), soft)
    out = sr._apply_attribute_ceiling(out, soft)
    phys = next(e for e in out.entities if e.type_name == "Physician")
    assert {a.name for a in phys.attributes} == {"name", "extra"}
    assert {e.type_name for e in out.entities} == {"Physician", "City"}
    assert out.ceiling_drops == []


def test_soft_exhaustive_ceiling_filters_focus_attrs_only():
    """Exhaustive soft: focus-type attributes ⊆ requested (± name/label/title);
    off-type City + relationship + lineage survive (soft decomposition intact)."""
    soft_ceiling = ExtractionConstraint(
        types=["Physician"],
        attributes={"Physician": ["name", "website", "type"]},
        soft=True,
        attributes_exhaustive=True,
    )
    bloated = ExtractionResult(
        entities=[
            ExtractedEntity(
                type_name="Physician",
                id="p1",
                parent_chain=["HealthcareProvider"],
                attributes=[
                    ExtractedAttribute(name="name", value="Dr X"),
                    ExtractedAttribute(name="website", value="https://x.example"),
                    ExtractedAttribute(name="type", value="MD"),
                    # Unrequested extras the soft model loves to invent:
                    ExtractedAttribute(name="phone", value="555-0100"),
                    ExtractedAttribute(name="npi", value="1234567890"),
                    ExtractedAttribute(name="specialty", value="Cards"),
                    ExtractedAttribute(name="rating", value="4.8"),
                ],
            ),
            ExtractedEntity(
                type_name="City",
                id="c1",
                attributes=[ExtractedAttribute(name="name", value="Boston")],
            ),
        ],
        relationships=[
            ExtractedRelationship(
                source_id="p1", predicate="located_in", target_id="c1"
            )
        ],
    )
    # Simulate the full-batch soft path: focus floor then attribute ceiling.
    out = sr._apply_soft_focus_floor(bloated, soft_ceiling, allow_strip=True)
    out = sr._apply_attribute_ceiling(out, soft_ceiling)

    assert {e.type_name for e in out.entities} == {"Physician", "City"}
    assert len(out.relationships) == 1
    phys = next(e for e in out.entities if e.type_name == "Physician")
    # CEILING: only the requested set (⊆). name/website/type kept; extras gone.
    assert {a.name for a in phys.attributes} <= {"name", "website", "type", "label", "title"}
    assert {a.name for a in phys.attributes} == {"name", "website", "type"}
    # Soft lineage on the focus type is preserved (ceiling is attr-only).
    assert phys.parent_chain == ["HealthcareProvider"]
    # Off-type City kept its identity attr.
    city = next(e for e in out.entities if e.type_name == "City")
    assert {a.name for a in city.attributes} == {"name"}
    # Drops are ledgered, not silent.
    assert len(out.ceiling_drops) == 4
    dropped_names = {d.attribute for d in out.ceiling_drops}
    assert dropped_names == {"phone", "npi", "specialty", "rating"}
    assert all(d.reason == "attribute_ceiling" for d in out.ceiling_drops)
    assert all(d.outcome.value == "dropped" for d in out.ceiling_drops)


def test_soft_exhaustive_ceiling_keeps_identity_attrs():
    """name/label/title always survive the ceiling so a record stays identifiable
    even when the requested set happened to omit them."""
    soft_ceiling = ExtractionConstraint(
        types=["Org"],
        attributes={"Org": ["website"]},
        soft=True,
        attributes_exhaustive=True,
    )
    result = ExtractionResult(
        entities=[
            ExtractedEntity(
                type_name="Org",
                id="o1",
                attributes=[
                    ExtractedAttribute(name="name", value="Acme"),
                    ExtractedAttribute(name="website", value="https://acme.example"),
                    ExtractedAttribute(name="founded", value="1999"),
                ],
            )
        ]
    )
    out = sr._apply_attribute_ceiling(result, soft_ceiling)
    kept = {a.name for a in out.entities[0].attributes}
    assert kept == {"name", "website"}
    assert [d.attribute for d in out.ceiling_drops] == ["founded"]


def test_build_block_selects_ceiling_template_when_exhaustive():
    """Exhaustive soft uses the ceiling user-prompt template; illustrative soft
    keeps the open 'guide, not a limit' wording."""
    illustrative = ExtractionConstraint(
        types=["Widget"],
        attributes={"Widget": ["name", "sku"]},
        soft=True,
        attributes_exhaustive=False,
    )
    exhaustive = ExtractionConstraint(
        types=["Widget"],
        attributes={"Widget": ["name", "sku"]},
        soft=True,
        attributes_exhaustive=True,
    )
    ill_block = sr._build_constraint_user_block(illustrative)
    exh_block = sr._build_constraint_user_block(exhaustive)
    assert "not a limit" in ill_block
    assert "CEILING" in exh_block
    assert "not a limit" not in exh_block
    assert "Widget: name, sku" in exh_block

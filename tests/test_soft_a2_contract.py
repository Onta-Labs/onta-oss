"""A2 zero-ontology-commitment contract (ONTA-272).

A2 (``ExtractionResult``) is the CANDIDATE-FACTS tier of the P2/P5 seam: the
extractor PROPOSES soft-typed, evidence-linked candidates; the placement layer
(P5) decides their final ontology home. These pure-function tests pin the
explicit contract: an A2 payload validates SOFT-TYPED-ONLY (no committed
ontology reference smuggled into a type slot) and — opt-in — EVIDENCE-LINKED,
while soft lineage (parent_chain / also_types / subtypes) is PRESERVED and never
flagged (re-caging extraction is the bug ONTA-199 fixed and must not return).
"""
from __future__ import annotations

import json

import pytest

from cograph_client.qc.boundary import BOUNDARY_SPECS, default_fixtures_dir
from cograph_client.resolver.models import (
    ExtractedAttribute,
    ExtractedEntity,
    ExtractionResult,
    SoftContractViolation,
    assert_soft_a2,
    soft_a2_from_structured_rows,
    validate_soft_a2,
)


# --------------------------------------------------------------------------- #
# evidence field — additive + back-compat
# --------------------------------------------------------------------------- #
def test_evidence_field_defaults_empty_and_is_optional():
    # Existing extraction never sets evidence — the models parse unchanged.
    a = ExtractedAttribute(name="specialty", value="Cardiology")
    e = ExtractedEntity(type_name="Physician", id="p1", attributes=[a])
    assert a.evidence == ""
    assert e.evidence == ""
    # And it round-trips when set.
    a2 = ExtractedAttribute(name="specialty", value="Cardiology", evidence="https://ex/a")
    e2 = ExtractedEntity(type_name="Physician", id="p1", evidence="https://ex/a")
    assert a2.evidence == "https://ex/a"
    assert e2.evidence == "https://ex/a"


# --------------------------------------------------------------------------- #
# validate_soft_a2 — soft-typed-only
# --------------------------------------------------------------------------- #
def test_valid_soft_a2_passes_and_keeps_lineage():
    """A soft-typed candidate WITH lineage (parent_chain / also_types / subtype)
    is VALID — the placement layer consumes those suggestions; flagging them
    would be re-caging."""
    e = ExtractedEntity(
        type_name="NursePractitioner",
        id="np1",
        parent_type="HealthcareProvider",
        parent_chain=["HealthcareProvider", "Person"],
        also_types=["Guest"],
        subtype_description="a nurse practitioner",
        attributes=[ExtractedAttribute(name="city", value="Portland")],
    )
    result = ExtractionResult(entities=[e])
    assert validate_soft_a2(result) == []


def test_committed_uri_in_any_type_slot_is_flagged():
    """A resolved ontology IRI in ANY type slot is a HARD commitment that
    pre-empts P5 — the one thing 'zero ontology commitment' forbids."""
    base = "https://cograph.tech/types/Physician"
    for kwargs in (
        {"type_name": base},
        {"type_name": "Physician", "same_as": base},
        {"type_name": "Physician", "parent_type": base},
        {"type_name": "Physician", "parent_chain": [base]},
        {"type_name": "Physician", "also_types": [base]},
    ):
        e = ExtractedEntity(id="p1", **kwargs)
        violations = validate_soft_a2(ExtractionResult(entities=[e]))
        assert violations, f"expected a violation for {kwargs}"
        assert "committed ontology reference" in violations[0]


def test_missing_candidate_type_is_flagged():
    e = ExtractedEntity(type_name="", id="p1")
    violations = validate_soft_a2(ExtractionResult(entities=[e]))
    assert any("no candidate type_name" in v for v in violations)


def test_non_extraction_result_payload_is_flagged():
    assert validate_soft_a2({"entities": []}) == [
        "A2 payload is not an ExtractionResult (got dict)"
    ]


# --------------------------------------------------------------------------- #
# validate_soft_a2 — evidence-linked (opt-in)
# --------------------------------------------------------------------------- #
def test_require_evidence_flags_unlinked_entity():
    e = ExtractedEntity(
        type_name="Physician", id="p1",
        attributes=[ExtractedAttribute(name="city", value="Portland")],
    )
    result = ExtractionResult(entities=[e])
    # Default (evidence not required) passes; require_evidence flags the gap.
    assert validate_soft_a2(result) == []
    violations = validate_soft_a2(result, require_evidence=True)
    assert any("not evidence-linked" in v for v in violations)


def test_require_evidence_satisfied_by_entity_or_attribute_span():
    on_entity = ExtractedEntity(type_name="Physician", id="p1", evidence="https://ex/a")
    on_attr = ExtractedEntity(
        type_name="Physician", id="p2",
        attributes=[ExtractedAttribute(name="city", value="Portland", evidence="https://ex/b")],
    )
    result = ExtractionResult(entities=[on_entity, on_attr])
    assert validate_soft_a2(result, require_evidence=True) == []


# --------------------------------------------------------------------------- #
# assert_soft_a2 — the fatal enforcement seam
# --------------------------------------------------------------------------- #
def test_assert_soft_a2_raises_on_leak():
    bad = ExtractionResult(
        entities=[ExtractedEntity(type_name="https://cograph.tech/types/X", id="x1")]
    )
    with pytest.raises(SoftContractViolation):
        assert_soft_a2(bad)
    # A clean soft payload does not raise.
    assert_soft_a2(ExtractionResult(entities=[ExtractedEntity(type_name="X", id="x1")]))


# --------------------------------------------------------------------------- #
# soft_a2_from_structured_rows — the deterministic builder
# --------------------------------------------------------------------------- #
def test_builder_produces_valid_soft_evidence_linked_a2():
    rows = [
        {"name": "Dr X", "specialty": "Cardiology", "city": "Portland",
         "source_url": "https://ex/a"},
        {"name": "Dr Y", "specialty": "Neurology", "city": "Seattle",
         "source_url": "https://ex/b"},
    ]
    a2 = soft_a2_from_structured_rows(rows, "Physician", key_field="name")
    assert [e.type_name for e in a2.entities] == ["Physician", "Physician"]
    assert [e.id for e in a2.entities] == ["Dr X", "Dr Y"]
    # source_url is carried as EVIDENCE, not as an attribute.
    assert a2.entities[0].evidence == "https://ex/a"
    assert all(a.name != "source_url" for a in a2.entities[0].attributes)
    assert {a.name for a in a2.entities[0].attributes} == {"name", "specialty", "city"}
    # Valid soft-typed-only AND evidence-linked.
    assert validate_soft_a2(a2, require_evidence=True) == []


def test_builder_id_falls_back_to_name_then_index():
    rows = [{"specialty": "Cardiology"}, {"name": "Dr Z", "specialty": "Neuro"}]
    a2 = soft_a2_from_structured_rows(rows, "Physician", key_field="npi")
    # row 0 has neither the key (npi) nor a name → positional index "0".
    assert a2.entities[0].id == "0"
    assert a2.entities[1].id == "Dr Z"
    # No provenance → still soft-typed-valid, just not evidence-linked.
    assert validate_soft_a2(a2) == []


# --------------------------------------------------------------------------- #
# real extraction output honors the contract
# --------------------------------------------------------------------------- #
def test_reference_extraction_validates_soft_typed_only():
    """The boundary harness's FROZEN reference A2 (the deterministic stand-in for
    the real extractor, WITH subtype lineage across all 4 domains) is
    soft-typed-only — proving ordinary extraction output already honors the
    contract without stripping lineage."""
    fixtures = default_fixtures_dir()
    for name in BOUNDARY_SPECS:
        a2_json = json.loads((fixtures / f"{name}.a2.json").read_text())
        result = ExtractionResult(
            entities=[
                ExtractedEntity(
                    type_name=e["type_name"],
                    id=e["id"],
                    parent_chain=e.get("parent_chain", []),
                    also_types=e.get("also_types", []),
                    attributes=[
                        ExtractedAttribute(name=a["name"], value=a["value"],
                                           datatype=a.get("datatype", "string"))
                        for a in e.get("attributes", [])
                    ],
                )
                for e in a2_json["entities"]
            ]
        )
        assert validate_soft_a2(result) == [], f"{name} A2 should be soft-typed-only"

"""Supplier inheritance: Entity → Organization → Company → Supplier."""

from __future__ import annotations

from ontology_skills.compiler import compile_flat, compile_routed
from ontology_skills.dataset import load_ontology
from ontology_skills.models import Neighborhood, Ontology, Skill


def _supplier_nb() -> Neighborhood:
    return Neighborhood(type_ids=("Supplier",))


def test_supplier_lineage_is_specific_first() -> None:
    compiled = compile_routed(load_ontology(), _supplier_nb())
    assert compiled.type_lineage == (
        "Supplier",
        "Company",
        "Organization",
        "Entity",
    )


def test_supplier_inherits_ancestor_type_skills() -> None:
    compiled = compile_routed(load_ontology(), _supplier_nb())
    assert compiled.skill_ids == (
        "vendor-reconciliation",
        "registration-id",
        "legal-name-normalization",
        "identity-hygiene",
        "quantity-validation",
        "temporal-window",
    )


def test_supplier_does_not_pull_far_side_person_skill() -> None:
    """Incident EMPLOYS does not compile Person skills."""
    compiled = compile_routed(load_ontology(), _supplier_nb())
    assert "person-not-org" not in compiled.skill_ids
    assert "Person" not in compiled.type_lineage


def test_supplies_to_relation_skills_come_from_incident_edge() -> None:
    compiled = compile_routed(load_ontology(), _supplier_nb())
    assert "SUPPLIES_TO" in compiled.relation_ids
    assert "temporal-window" in compiled.skill_ids
    assert "quantity-validation" in compiled.skill_ids


def test_more_specific_skill_id_shadows_ancestor() -> None:
    onto = load_ontology()
    override = Skill(
        skill_id="identity-hygiene",
        title="Supplier-specific identity",
        body="Prefer vendorId over name matching for Supplier rows.",
        attached_to="Supplier",
        kind="type",
        provenance="curated",
    )
    extended = Ontology(
        types=onto.types,
        relations=onto.relations,
        skills=onto.skills + (override,),
    )
    compiled = compile_routed(extended, _supplier_nb())
    bodies = {s.skill_id: s.body for s in compiled.skills}
    assert bodies["identity-hygiene"].startswith("Prefer vendorId")
    assert compiled.skill_ids.count("identity-hygiene") == 1
    # Still present, but attached to Supplier (first wins), not Entity.
    ident = next(s for s in compiled.skills if s.skill_id == "identity-hygiene")
    assert ident.attached_to == "Supplier"


def test_disabled_more_specific_skill_suppresses_ancestor() -> None:
    onto = load_ontology()
    disabled = Skill(
        skill_id="identity-hygiene",
        title="Disabled override",
        body="Do not use generic identity hygiene for Supplier.",
        attached_to="Supplier",
        kind="type",
        enabled=False,
    )
    extended = Ontology(
        types=onto.types,
        relations=onto.relations,
        skills=onto.skills + (disabled,),
    )
    compiled = compile_routed(extended, _supplier_nb())
    assert "identity-hygiene" not in compiled.skill_ids
    assert "identity-hygiene" in compiled.suppressed_skill_ids


def test_flat_dump_includes_unrelated_person_skill() -> None:
    routed = compile_routed(load_ontology(), _supplier_nb())
    flat = compile_flat(load_ontology())
    assert "person-not-org" in flat.skill_ids
    assert "person-not-org" not in routed.skill_ids
    assert set(routed.skill_ids) < set(flat.skill_ids)

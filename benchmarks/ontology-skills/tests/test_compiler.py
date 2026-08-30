"""Same neighborhood → same compiled skill set (order and fingerprint)."""

from __future__ import annotations

import random

import pytest

from ontology_skills.compiler import compile_none, compile_routed
from ontology_skills.dataset import load_ontology
from ontology_skills.models import (
    EntityType,
    Neighborhood,
    Ontology,
    OntologyError,
)


def test_repeated_compile_is_byte_identical() -> None:
    onto = load_ontology()
    nb = Neighborhood(type_ids=("Supplier",), relation_ids=("SUPPLIES_TO",))
    first = compile_routed(onto, nb)
    second = compile_routed(onto, nb)
    assert first == second
    assert first.fingerprint() == second.fingerprint()
    assert first.skill_ids == second.skill_ids


def test_shuffled_skill_tuple_does_not_change_output() -> None:
    onto = load_ontology()
    nb = Neighborhood(type_ids=("Supplier",))
    expected = compile_routed(onto, nb)
    skills = list(onto.skills)
    rng = random.Random(0)
    rng.shuffle(skills)
    shuffled = Ontology(
        types=onto.types, relations=onto.relations, skills=tuple(skills)
    )
    got = compile_routed(shuffled, nb)
    assert got.skill_ids == expected.skill_ids
    assert got.fingerprint() == expected.fingerprint()
    assert got.skills == expected.skills


def test_same_type_set_seed_order_preserves_skill_id_set() -> None:
    onto = load_ontology()
    a = compile_routed(onto, Neighborhood(type_ids=("Supplier", "Customer")))
    b = compile_routed(onto, Neighborhood(type_ids=("Customer", "Supplier")))
    assert set(a.skill_ids) == set(b.skill_ids)
    # Seed order is part of the neighborhood; lineage order may differ.
    assert a.type_lineage[0] == "Supplier"
    assert b.type_lineage[0] == "Customer"


def test_unknown_type_raises() -> None:
    onto = load_ontology()
    with pytest.raises(OntologyError, match="unknown type"):
        compile_routed(onto, Neighborhood(type_ids=("NotAType",)))


def test_cycle_raises_at_ontology_load() -> None:
    onto = load_ontology()
    types = dict(onto.types)
    types["Entity"] = EntityType("Entity", "Entity", ("Organization",))
    with pytest.raises(OntologyError, match="cycle"):
        Ontology(types=types, relations=onto.relations, skills=onto.skills)


def test_compile_none_keeps_lineage_drops_skills() -> None:
    onto = load_ontology()
    nb = Neighborhood(type_ids=("Supplier",))
    compiled = compile_none(onto, nb)
    assert compiled.mode == "none"
    assert compiled.skills == ()
    assert compiled.type_lineage[0] == "Supplier"


def test_without_ancestors_does_not_inherit() -> None:
    onto = load_ontology()
    nb = Neighborhood(
        type_ids=("Supplier",),
        include_ancestors=False,
        include_incident_relations=False,
    )
    compiled = compile_routed(onto, nb)
    assert compiled.type_lineage == ("Supplier",)
    assert "vendor-reconciliation" in compiled.skill_ids
    assert "registration-id" not in compiled.skill_ids
    assert "identity-hygiene" not in compiled.skill_ids
    assert all(skill.attached_to == "Supplier" for skill in compiled.skills)

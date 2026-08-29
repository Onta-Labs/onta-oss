"""Dataset volume, splits, and no-leakage guards (INF-608)."""

from __future__ import annotations

from ontology_skills.compiler import compile_routed
from ontology_skills.dataset import (
    SPLITS,
    TASK_FAMILIES,
    load_fixture_bundle,
    load_ontology,
    load_tasks,
)

MIN_PER_FAMILY = 8
MAX_PER_FAMILY = 12
MIN_PER_SPLIT = 8


def test_fixture_ontology_has_supplier_chain() -> None:
    onto = load_ontology()
    assert onto.types["Supplier"].parent_ids == ("Company",)
    assert onto.types["Company"].parent_ids == ("Organization",)
    assert onto.types["Organization"].parent_ids == ("Entity",)
    assert onto.types["Entity"].parent_ids == ()
    assert onto.relations["SUPPLIES_TO"].source_type == "Supplier"
    assert onto.relations["SUPPLIES_TO"].target_type == "Customer"


def test_volume_per_family_and_split() -> None:
    bundle = load_fixture_bundle()
    assert {t.family for t in bundle.tasks} == set(TASK_FAMILIES)
    for family in TASK_FAMILIES:
        n = len(bundle.tasks_for(family=family))
        assert MIN_PER_FAMILY <= n <= MAX_PER_FAMILY, family
    used = {t.split for t in bundle.tasks}
    assert used == set(SPLITS)
    for split in SPLITS:
        n = len(bundle.tasks_for(split=split))
        assert n >= MIN_PER_SPLIT, split


def test_gold_deltas_are_non_empty_graph_deltas() -> None:
    onto = load_ontology()
    for task in load_tasks():
        ops = task.gold.canonical_ops()
        assert ops, f"{task.task_id} gold delta is empty"
        assert task.neighborhood.type_ids, f"{task.task_id} missing neighborhood"
        for tid in task.neighborhood.type_ids:
            assert tid in onto.types, f"{task.task_id} seeds unknown type {tid}"
        for rid in task.neighborhood.relation_ids:
            assert rid in onto.relations, f"{task.task_id} seeds unknown rel {rid}"


def test_unseen_extensions_are_not_in_the_snapshot() -> None:
    onto = load_ontology()
    unseen = [
        t
        for t in load_tasks()
        if t.split == "unseen_ontology_branches" and t.gold.type_extensions
    ]
    assert unseen, "unseen split needs type_extensions"
    for task in unseen:
        for ext in task.gold.type_extensions:
            assert ext.type_id not in onto.types, task.task_id
            assert ext.parent_id in onto.types, task.task_id


def test_no_gold_key_in_input() -> None:
    for task in load_tasks():
        assert "gold" not in task.input, task.task_id


def test_every_neighborhood_compiles() -> None:
    bundle = load_fixture_bundle()
    for task in bundle.tasks:
        compiled = compile_routed(bundle.ontology, task.neighborhood)
        assert compiled.mode == "routed"


def test_bundle_filter() -> None:
    bundle = load_fixture_bundle()
    ext = bundle.tasks_for(family="ontology_extension")
    assert {t.task_id for t in ext} >= {"ext-001"}
    adv = bundle.tasks_for(split="adversarial_conflicting")
    assert "cvr-001" in {t.task_id for t in adv}
    assert "conf-001" in {t.task_id for t in adv}

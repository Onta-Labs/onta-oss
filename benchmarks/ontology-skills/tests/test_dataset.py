"""Dataset loader stubs and gold graph-delta fixtures."""

from __future__ import annotations

from ontology_skills.dataset import (
    SPLITS,
    TASK_FAMILIES,
    load_fixture_bundle,
    load_ontology,
    load_tasks,
)


def test_fixture_ontology_has_supplier_chain() -> None:
    onto = load_ontology()
    assert onto.types["Supplier"].parent_ids == ("Company",)
    assert onto.types["Company"].parent_ids == ("Organization",)
    assert onto.types["Organization"].parent_ids == ("Entity",)
    assert onto.types["Entity"].parent_ids == ()
    assert onto.relations["SUPPLIES_TO"].source_type == "Supplier"
    assert onto.relations["SUPPLIES_TO"].target_type == "Customer"


def test_one_gold_task_per_family() -> None:
    tasks = load_tasks()
    families = {t.family for t in tasks}
    assert families == set(TASK_FAMILIES)
    assert len(tasks) == len(TASK_FAMILIES)


def test_splits_are_from_the_locked_set() -> None:
    tasks = load_tasks()
    assert {t.split for t in tasks} <= set(SPLITS)
    used = {t.split for t in tasks}
    assert "known_ontology_unseen_instances" in used
    assert "unseen_ontology_branches" in used
    assert "adversarial_conflicting" in used


def test_gold_deltas_are_non_empty_and_canonical() -> None:
    for task in load_tasks():
        ops = task.gold.canonical_ops()
        assert ops, f"{task.task_id} gold delta is empty"
        assert task.neighborhood.type_ids, f"{task.task_id} missing neighborhood"


def test_bundle_filter() -> None:
    bundle = load_fixture_bundle()
    ext = bundle.tasks_for(family="ontology_extension")
    assert len(ext) == 1
    assert ext[0].task_id == "ext-001"
    adv = bundle.tasks_for(split="adversarial_conflicting")
    assert {t.task_id for t in adv} == {"cvr-001", "conf-001"}

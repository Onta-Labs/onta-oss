"""Holdout fixtures: schema, no id overlap, no copied skill bodies."""

from __future__ import annotations

import json
import re

from ontology_skills.compiler import compile_routed
from ontology_skills.dataset import (
    FIXTURES_DIR,
    SPLITS,
    TASK_FAMILIES,
    load_fixture_bundle,
    load_ontology,
    load_tasks,
)
from ontology_skills.graph_delta import GraphDelta

HOLDOUT_DIR = FIXTURES_DIR / "holdout"
ENT_PREFIX = "https://graph.infona.ai/bench/ent/"
ONTO_PREFIX = "https://graph.infona.ai/bench/onto/"
CAMEL = re.compile(r"^[a-z][a-zA-Z0-9]*$")
SHORT_ID = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")


def _holdout() -> tuple:
    return load_fixture_bundle(
        HOLDOUT_DIR / "ontology.json", HOLDOUT_DIR / "tasks.jsonl"
    )


def test_holdout_volume_families_and_splits() -> None:
    bundle = _holdout()
    assert len(bundle.tasks) == 24
    assert {t.family for t in bundle.tasks} == set(TASK_FAMILIES)
    assert {t.split for t in bundle.tasks} == set(SPLITS)
    for family in TASK_FAMILIES:
        rows = bundle.tasks_for(family=family)
        assert len(rows) == 3, family
        assert {t.split for t in rows} <= set(SPLITS), family


def test_holdout_task_ids_do_not_overlap_main() -> None:
    main_ids = {t.task_id for t in load_tasks()}
    hold_ids = {t.task_id for t in _holdout().tasks}
    assert hold_ids.isdisjoint(main_ids)
    assert len(hold_ids) == 24


def test_holdout_skill_bodies_are_not_copied() -> None:
    main = {s.body.strip() for s in load_ontology().skills}
    hold = {s.body.strip() for s in _holdout().ontology.skills}
    assert hold
    assert main.isdisjoint(hold)


def test_holdout_tree_adds_carrier_leaf() -> None:
    onto = _holdout().ontology
    assert onto.types["Organization"].parent_ids == ("Entity",)
    assert onto.types["Company"].parent_ids == ("Organization",)
    assert onto.types["Carrier"].parent_ids == ("Company",)
    assert "Carrier" not in load_ontology().types
    assert onto.relations["HAULS_FOR"].source_type == "Carrier"
    assert onto.relations["HAULS_FOR"].target_type == "Consignee"


def test_holdout_gold_is_a_graph_delta() -> None:
    onto = _holdout().ontology
    for task in _holdout().tasks:
        gold = GraphDelta.from_dict(task.gold.to_dict())
        assert gold.canonical_ops(), task.task_id
        assert gold == task.gold
        assert "gold" not in task.input
        for tid in task.neighborhood.type_ids:
            assert tid in onto.types, task.task_id
        for rid in task.neighborhood.relation_ids:
            assert rid in onto.relations, task.task_id


def test_holdout_input_has_no_entity_uri_or_mint_as() -> None:
    prefix = ENT_PREFIX

    def walk(obj, found: set[str]) -> None:
        if isinstance(obj, dict):
            for value in obj.values():
                walk(value, found)
        elif isinstance(obj, list):
            for value in obj:
                walk(value, found)
        elif isinstance(obj, str) and obj.startswith(prefix):
            found.add(obj)

    def gold_uris(task) -> set[str]:
        found: set[str] = set()
        g = task.gold
        for item in g.type_assertions:
            found.add(item.entity)
        for item in g.literals:
            found.add(item.entity)
        for item in (*g.adds, *g.deletes):
            found.add(item.subject)
            found.add(item.object)
        for item in g.merges:
            found.add(item.absorbed)
            found.add(item.survivor)
        return {u for u in found if u.startswith(prefix)}

    for task in _holdout().tasks:
        assert "entity_uri" not in task.input, task.task_id
        assert "mint_as" not in task.input, task.task_id
        known: set[str] = set()
        walk(task.input, known)
        missing = gold_uris(task) - known
        assert not missing, (task.task_id, missing)


def test_holdout_identifier_contract() -> None:
    for task in _holdout().tasks:
        for item in task.gold.type_assertions:
            assert SHORT_ID.match(item.type_id), (task.task_id, item.type_id)
            assert item.entity.startswith(ENT_PREFIX)
        for item in task.gold.literals:
            assert CAMEL.match(item.attr), (task.task_id, item.attr)
            assert item.entity.startswith(ENT_PREFIX)
        for item in (*task.gold.adds, *task.gold.deletes):
            assert item.predicate.startswith(ONTO_PREFIX), task.task_id
            assert "/" not in item.predicate.removeprefix(ONTO_PREFIX)
        for item in task.gold.type_extensions:
            assert SHORT_ID.match(item.type_id), task.task_id
            assert SHORT_ID.match(item.parent_id), task.task_id


def test_holdout_no_gold_type_names_in_prompt_hints() -> None:
    for task in _holdout().tasks:
        names = {item.type_id for item in task.gold.type_assertions}
        names.update(item.type_id for item in task.gold.type_extensions)
        blob = json.dumps({"notes": task.notes, "input": dict(task.input)})
        leaked = [name for name in names if name in blob]
        assert not leaked, (task.task_id, leaked)


def test_holdout_vat_is_not_paired_to_registration_id() -> None:
    for task in _holdout().tasks:
        blob = json.dumps(dict(task.input))
        if "VAT" not in blob:
            continue
        for item in task.gold.literals:
            assert item.attr != "registrationId", task.task_id


def test_holdout_unseen_extensions_are_absent_from_snapshot() -> None:
    onto = _holdout().ontology
    unseen = [
        t
        for t in _holdout().tasks
        if t.split == "unseen_ontology_branches" and t.gold.type_extensions
    ]
    assert unseen
    for task in unseen:
        for item in task.gold.type_extensions:
            assert item.type_id not in onto.types, task.task_id
            assert item.parent_id in onto.types, task.task_id


def test_holdout_neighborhoods_compile() -> None:
    bundle = _holdout()
    for task in bundle.tasks:
        compiled = compile_routed(bundle.ontology, task.neighborhood)
        assert compiled.mode == "routed"

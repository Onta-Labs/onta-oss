"""Dataset volume, splits, and no-leakage guards (INF-608)."""

from __future__ import annotations

import hashlib
import json

from ontology_skills.compiler import compile_routed
from ontology_skills.dataset import (
    FIXTURES_DIR,
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


GOLD_OPS_SHA256 = (
    "9b1c641deb4f486d2c81408fa6e4a89e1067fcf36dd7a61cc079383fd44257ce"
)


def test_gold_ops_unchanged_after_mint_id_input() -> None:
    digest = hashlib.sha256()
    path = FIXTURES_DIR / "tasks.jsonl"
    for line in path.read_text(encoding="utf-8").splitlines():
        obj = json.loads(line)
        blob = json.dumps(obj["gold"], sort_keys=True, separators=(",", ":"))
        digest.update(blob.encode("utf-8"))
    assert digest.hexdigest() == GOLD_OPS_SHA256


def test_minted_gold_uris_are_blank_nodes_in_input() -> None:
    prefix = "https://graph.infona.ai/bench/ent/"

    def walk(obj, found: set[str]) -> None:
        if isinstance(obj, dict):
            for value in obj.values():
                walk(value, found)
        elif isinstance(obj, list):
            for value in obj:
                walk(value, found)
        elif isinstance(obj, str) and obj.startswith(prefix):
            found.add(obj)

    def gold_uris(task) -> list[str]:
        g = task.gold
        ordered: list[str] = []
        seen: set[str] = set()

        def add(uri: str) -> None:
            if uri.startswith(prefix) and uri not in seen:
                seen.add(uri)
                ordered.append(uri)

        for item in g.type_assertions:
            add(item.entity)
        for item in g.literals:
            add(item.entity)
        for item in (*g.adds, *g.deletes):
            add(item.subject)
            add(item.object)
        for item in g.merges:
            add(item.absorbed)
            add(item.survivor)
        return ordered

    for task in load_tasks():
        inp = dict(task.input)
        known: set[str] = set()
        rest = {k: v for k, v in inp.items() if k not in ("entity_uri", "mint_as")}
        walk(rest, known)
        minted = [uri for uri in gold_uris(task) if uri not in known]
        if len(minted) == 1:
            assert inp.get("entity_uri") == minted[0], task.task_id
            assert "mint_as" not in inp, task.task_id
        elif len(minted) > 1:
            assert inp.get("mint_as") == minted, task.task_id
            assert "entity_uri" not in inp, task.task_id
        else:
            assert "entity_uri" not in inp, task.task_id
            assert "mint_as" not in inp, task.task_id
        assert "type_id" not in inp, task.task_id
        assert "literals" not in inp, task.task_id


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

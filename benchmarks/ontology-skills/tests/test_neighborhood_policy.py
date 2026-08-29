"""Family-aware compile: et-001 routed must not staple SUPPLIES_TO skills."""

from __future__ import annotations

import hashlib
import json

import pytest

from ontology_skills.conditions import condition_by_id
from ontology_skills.dataset import FIXTURES_DIR, load_fixture_bundle
from ontology_skills.harness import compile_for_condition
from ontology_skills.neighborhood_policy import compile_for_task, neighborhood_for_task
from ontology_skills.policy_compile import compile_for_execute

# Same pin as tests/test_dataset.py. This PR must not rewrite gold.
GOLD_OPS_SHA256 = (
    "9b1c641deb4f486d2c81408fa6e4a89e1067fcf36dd7a61cc079383fd44257ce"
)

INCIDENT_SUPPLIES_TO_SKILLS = ("temporal-window", "quantity-validation")


def _task(task_id: str):
    bundle = load_fixture_bundle()
    task = next(t for t in bundle.tasks if t.task_id == task_id)
    return bundle, task


def test_et001_routed_omits_incident_relation_skills() -> None:
    bundle, task = _task("et-001")
    cond = condition_by_id("4b_ontology_routed")
    compiled = compile_for_task(bundle.ontology, task, cond)
    assert compiled.mode == "routed"
    for skill_id in INCIDENT_SUPPLIES_TO_SKILLS:
        assert skill_id not in compiled.skill_ids
    assert "SUPPLIES_TO" not in compiled.relation_ids
    assert "vendor-reconciliation" in compiled.skill_ids
    assert compiled.skill_ids == (
        "vendor-reconciliation",
        "registration-id",
        "legal-name-normalization",
        "identity-hygiene",
    )


def test_et001_two_arg_compile_for_task_is_primary_routed() -> None:
    bundle, task = _task("et-001")
    compiled = compile_for_task(bundle.ontology, task)
    assert compiled.mode == "routed"
    assert "temporal-window" not in compiled.skill_ids
    assert "quantity-validation" not in compiled.skill_ids


def test_raw_neighborhood_still_leaks_without_policy() -> None:
    """compiler.py is unchanged: fixture incident flag still pulls SUPPLIES_TO."""
    bundle, task = _task("et-001")
    cond = condition_by_id("4b_ontology_routed")
    raw = compile_for_condition(bundle.ontology, task.neighborhood, cond)
    assert "temporal-window" in raw.skill_ids
    assert "quantity-validation" in raw.skill_ids


def test_relation_inference_still_gets_relation_skills() -> None:
    bundle, task = _task("rel-001")
    cond = condition_by_id("4b_ontology_routed")
    compiled = compile_for_task(bundle.ontology, task, cond)
    assert "temporal-window" in compiled.skill_ids
    assert "quantity-validation" in compiled.skill_ids
    assert "SUPPLIES_TO" in compiled.relation_ids


def test_multi_step_ingest_keeps_incident_relations() -> None:
    bundle, task = _task("ms-001")
    assert task.neighborhood.relation_ids == ()
    nb = neighborhood_for_task(task)
    assert nb.include_incident_relations is True
    compiled = compile_for_task(
        bundle.ontology, task, condition_by_id("4b_ontology_routed")
    )
    assert "temporal-window" in compiled.skill_ids
    assert "quantity-validation" in compiled.skill_ids


def test_property_schema_mapping_does_not_seed_relations() -> None:
    bundle, task = _task("map-001")
    nb = neighborhood_for_task(task)
    assert nb.relation_ids == ()
    assert nb.include_incident_relations is False
    compiled = compile_for_task(
        bundle.ontology, task, condition_by_id("4b_ontology_routed")
    )
    for skill_id in INCIDENT_SUPPLIES_TO_SKILLS:
        assert skill_id not in compiled.skill_ids


def test_entity_typing_strips_fixture_relation_seeds() -> None:
    _, task = _task("et-003")
    assert task.neighborhood.relation_ids == ("LOCATED_IN",)
    nb = neighborhood_for_task(task)
    assert nb.relation_ids == ()
    assert nb.include_incident_relations is False


def test_other_family_keeps_seeded_relations_drops_incident() -> None:
    _, task = _task("cvr-001")
    assert "SUPPLIES_TO" in task.neighborhood.relation_ids
    assert task.neighborhood.include_incident_relations is True
    nb = neighborhood_for_task(task)
    assert nb.relation_ids == task.neighborhood.relation_ids
    assert nb.include_incident_relations is False


def test_policy_does_not_mutate_task_or_gold() -> None:
    _, task = _task("et-001")
    before_nb = task.neighborhood
    before_gold = task.gold.canonical_ops()
    neighborhood_for_task(task)
    compile_for_task(load_fixture_bundle().ontology, task)
    assert task.neighborhood is before_nb
    assert task.neighborhood.include_incident_relations is True
    assert task.gold.canonical_ops() == before_gold


def test_gold_ops_sha_unchanged() -> None:
    digest = hashlib.sha256()
    path = FIXTURES_DIR / "tasks.jsonl"
    for line in path.read_text(encoding="utf-8").splitlines():
        obj = json.loads(line)
        blob = json.dumps(obj["gold"], sort_keys=True, separators=(",", ":"))
        digest.update(blob.encode("utf-8"))
    assert digest.hexdigest() == GOLD_OPS_SHA256


def test_flat_and_none_follow_condition() -> None:
    bundle, task = _task("et-001")
    none = compile_for_task(bundle.ontology, task, condition_by_id("4b_vanilla"))
    assert none.mode == "none"
    assert none.skills == ()
    assert "SUPPLIES_TO" not in none.relation_ids
    flat = compile_for_task(
        bundle.ontology, task, condition_by_id("4b_flat_skills")
    )
    assert flat.mode == "flat"
    assert "temporal-window" in flat.skill_ids


def test_fine_tune_stays_blocked() -> None:
    bundle, task = _task("et-001")
    with pytest.raises(RuntimeError, match="blocked"):
        compile_for_task(
            bundle.ontology, task, condition_by_id("4b_ft_ontology_routed")
        )


def test_executor_helper_matches_compile_for_task() -> None:
    bundle, task = _task("et-001")
    cond = condition_by_id("4b_ontology_routed")
    assert compile_for_execute(bundle.ontology, task, cond) == compile_for_task(
        bundle.ontology, task, cond
    )


def test_compile_for_execute_rag_matches_rewritten_k_and_can_disagree() -> None:
    from ontology_skills.embedder import MockEmbedder

    bundle, et = _task("et-001")
    embedder = MockEmbedder()
    routed = compile_for_execute(
        bundle.ontology, et, condition_by_id("4b_ontology_routed")
    )
    rag = compile_for_execute(
        bundle.ontology,
        et,
        condition_by_id("4b_rag_skills"),
        embedder=embedder,
    )
    assert rag.mode == "rag"
    assert len(rag.skills) == len(routed.skills)
    assert "temporal-window" not in routed.skill_ids
    assert "quantity-validation" not in routed.skill_ids
    disagreed = set(rag.skill_ids) != set(routed.skill_ids)
    if not disagreed:
        for task in bundle.tasks:
            r = compile_for_execute(
                bundle.ontology, task, condition_by_id("4b_ontology_routed")
            )
            g = compile_for_execute(
                bundle.ontology,
                task,
                condition_by_id("4b_rag_skills"),
                embedder=embedder,
            )
            if set(g.skill_ids) != set(r.skill_ids):
                disagreed = True
                break
    assert disagreed, "RAG vs routed should differ on at least one task"


def test_conflict_and_cvr_skills_are_on_seeded_types_not_wrong_family() -> None:
    """Vanilla beating Infona on those families is extra ops, not a mis-route."""
    bundle, conf = _task("conf-001")
    compiled = compile_for_task(bundle.ontology, conf)
    assert conf.neighborhood.type_ids == ("Supplier",)
    assert "legal-name-normalization" in compiled.skill_ids
    assert "vendor-reconciliation" in compiled.skill_ids
    assert "temporal-window" not in compiled.skill_ids
    assert "quantity-validation" not in compiled.skill_ids
    assert "person-not-org" not in compiled.skill_ids

    bundle, cvr = _task("cvr-001")
    compiled = compile_for_task(bundle.ontology, cvr)
    assert "Person" in cvr.neighborhood.type_ids
    assert "SUPPLIES_TO" in cvr.neighborhood.relation_ids
    assert "person-not-org" in compiled.skill_ids
    assert "temporal-window" in compiled.skill_ids
    assert "SUPPLIES_TO" in compiled.relation_ids

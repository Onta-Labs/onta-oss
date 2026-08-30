"""Distractor cabinet: dump-all / RAG control. Routed et-001 stays neighborhood-legal."""

from __future__ import annotations

import json
import re

from ontology_skills.compiler import compile_flat, compile_routed
from ontology_skills.conditions import condition_by_id
from ontology_skills.dataset import load_fixture_bundle, load_tasks
from ontology_skills.embedder import TABLE_EMBEDDER_ID, TableEmbedder
from ontology_skills.harness import compile_for_condition
from ontology_skills.neighborhood_policy import compile_for_task
from ontology_skills.prompts import build_prompt
from ontology_skills.rag import retrieve_skills

_ROUTED_CORE = (
    "registration-id",
    "legal-name-normalization",
    "identity-hygiene",
)
_DISTRACTOR_RELATION = "SUBSIDIARY_OF"
_BANNED_TYPE_NAMES = (
    "Supplier",
    "Company",
    "Organization",
    "Entity",
    "Customer",
    "Person",
    "Location",
    "Product",
    "ThirdPartyWarehouse",
    "BondedWarehouse",
)
_TYPE_NAME_RE = re.compile(
    r"\b(" + "|".join(re.escape(n) for n in _BANNED_TYPE_NAMES) + r")\b",
    re.I,
)
_LEAF_SEED_FAMILIES = frozenset({"entity_typing", "multi_step_ingest"})


def _et001():
    bundle = load_fixture_bundle()
    task = next(t for t in bundle.tasks if t.task_id == "et-001")
    return bundle, task


def _distractor_ids(ontology) -> frozenset[str]:
    return frozenset(
        s.skill_id for s in ontology.skills if s.provenance == "distractor"
    )


def test_distractors_attach_to_subsidiary_of_not_a_type() -> None:
    bundle, _ = _et001()
    ids = _distractor_ids(bundle.ontology)
    assert 20 <= len(ids) <= 40
    assert _DISTRACTOR_RELATION in bundle.ontology.relations
    typing_rel_seeds = {
        rid
        for task in bundle.tasks
        if task.family == "entity_typing"
        for rid in task.neighborhood.relation_ids
    }
    assert _DISTRACTOR_RELATION not in typing_rel_seeds
    for skill in bundle.ontology.skills:
        if skill.provenance != "distractor":
            continue
        assert skill.kind == "relation", skill.skill_id
        assert skill.attached_to == _DISTRACTOR_RELATION, skill.skill_id
        assert skill.attached_to in bundle.ontology.relations
        assert skill.attached_to not in bundle.ontology.types


def test_distractor_bodies_omit_gold_type_names() -> None:
    """Lowercase 'supplier' is bait. Any other type-id token is a leak."""
    bundle, _ = _et001()
    for skill in bundle.ontology.skills:
        if skill.provenance != "distractor":
            continue
        assert "type_id" not in skill.body, skill.skill_id
        assert "type_assertions" not in skill.body, skill.skill_id
        for match in _TYPE_NAME_RE.finditer(skill.body):
            token = match.group(0)
            assert token == "supplier", f"{skill.skill_id} names type {token!r}"


def test_original_four_skills_still_attached() -> None:
    bundle, _ = _et001()
    by_id = {s.skill_id: s for s in bundle.ontology.skills}
    assert by_id["identity-hygiene"].attached_to == "Entity"
    assert by_id["legal-name-normalization"].attached_to == "Organization"
    assert by_id["registration-id"].attached_to == "Company"
    assert by_id["vendor-reconciliation"].attached_to == "Supplier"


def test_et001_routed_excludes_distractors_and_vendor_reconciliation() -> None:
    bundle, task = _et001()
    compiled = compile_for_task(bundle.ontology, task)
    distractors = _distractor_ids(bundle.ontology)
    assert compiled.skill_ids == _ROUTED_CORE
    assert "vendor-reconciliation" not in compiled.skill_ids
    assert set(compiled.skill_ids).isdisjoint(distractors)
    assert all(skill.attached_to != "Supplier" for skill in compiled.skills)


def test_et001_types_only_prompt_has_no_supplier_token() -> None:
    bundle, task = _et001()
    cond = condition_by_id("4b_ontology_context")
    compiled = compile_for_condition(bundle.ontology, task.neighborhood, cond)
    prompt = build_prompt(task, bundle.ontology, compiled, cond)
    assert "Supplier" not in prompt.text
    assert "type Company parents=Organization" in prompt.text


def test_et001_flat_is_much_larger_than_routed() -> None:
    bundle, task = _et001()
    routed = compile_routed(bundle.ontology, task.neighborhood)
    flat = compile_flat(bundle.ontology)
    assert len(flat.skills) >= len(routed.skills) + 15
    assert set(_ROUTED_CORE) <= set(flat.skill_ids)


def test_et001_dump_all_distractor_headers_are_not_type_supplier() -> None:
    bundle, task = _et001()
    cond = condition_by_id("4b_flat_skills")
    flat = compile_flat(bundle.ontology)
    prompt = build_prompt(task, bundle.ontology, flat, cond)
    distractors = _distractor_ids(bundle.ontology)
    for skill_id in distractors:
        header = f"### {skill_id} [relation:{_DISTRACTOR_RELATION}]"
        assert header in prompt.text, skill_id
        assert f"### {skill_id} [type:Supplier]" not in prompt.text
    assert "### vendor-reconciliation [type:Supplier]" in prompt.text


def test_et001_rag_top_k_includes_a_distractor() -> None:
    """Hash mock is not lexical. Pin one distractor body next to the query."""
    bundle, task = _et001()
    distractors = _distractor_ids(bundle.ontology)
    bait = next(s for s in bundle.ontology.skills if s.skill_id in distractors)
    query = json.dumps(dict(task.input), sort_keys=True, ensure_ascii=False)
    aligned = (1.0, 0.0, 0.0, 0.0)
    embedder = TableEmbedder(table={query: aligned, bait.body: aligned})
    routed = compile_routed(bundle.ontology, task.neighborhood)
    rag = retrieve_skills(bundle.ontology, task, embedder)
    assert embedder.embedder_id == TABLE_EMBEDDER_ID
    assert rag.k == len(routed.skills)
    assert rag.k == 3
    assert bait.skill_id in rag.compiled.skill_ids
    assert set(rag.compiled.skill_ids) & distractors


def test_typing_and_ingest_still_do_not_seed_gold_leaf() -> None:
    for task in load_tasks():
        if task.family not in _LEAF_SEED_FAMILIES:
            continue
        gold = {item.type_id for item in task.gold.type_assertions}
        leak = set(task.neighborhood.type_ids) & gold
        assert not leak, f"{task.task_id} seeds gold leaf {sorted(leak)}"

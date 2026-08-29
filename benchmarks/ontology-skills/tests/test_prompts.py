"""Prompt builders: four skill-injection modes, no gold leakage."""

from __future__ import annotations

from ontology_skills.conditions import condition_by_id
from ontology_skills.dataset import load_fixture_bundle
from ontology_skills.harness import compile_for_condition
from ontology_skills.prompts import build_prompt


def _built(condition_id: str, task_id: str = "et-001"):
    bundle = load_fixture_bundle()
    task = next(t for t in bundle.tasks if t.task_id == task_id)
    cond = condition_by_id(condition_id)
    compiled = compile_for_condition(bundle.ontology, task.neighborhood, cond)
    return build_prompt(task, bundle.ontology, compiled, cond), compiled


def test_vanilla_has_no_skills_or_ontology_block() -> None:
    prompt, compiled = _built("4b_vanilla")
    assert prompt.skill_injection == "none"
    assert compiled.skills == ()
    assert "Mint one entity URI" not in prompt.text
    assert "skills: none" in prompt.text
    assert "ontology: none" in prompt.text
    assert "### vendor-reconciliation" not in prompt.text


def test_ontology_context_lists_lineage_without_skill_bodies() -> None:
    prompt, compiled = _built("4b_ontology_context")
    assert prompt.skill_injection == "ontology_context"
    assert compiled.skills == ()
    assert "type Supplier parents=Company" in prompt.text
    assert "relation SUPPLIES_TO" in prompt.text
    assert "skills: none" in prompt.text
    assert "Mint one entity URI" not in prompt.text


def test_flat_includes_unrelated_person_skill() -> None:
    prompt, compiled = _built("4b_flat_skills")
    assert prompt.skill_injection == "flat"
    assert "person-not-org" in compiled.skill_ids
    assert "### person-not-org" in prompt.text


def test_routed_omits_person_skill_for_supplier_neighborhood() -> None:
    prompt, compiled = _built("4b_ontology_routed")
    assert prompt.skill_injection == "routed"
    assert "vendor-reconciliation" in compiled.skill_ids
    assert "### vendor-reconciliation" in prompt.text
    assert "Mint one entity URI" in prompt.text
    assert "person-not-org" not in compiled.skill_ids
    assert "### person-not-org" not in prompt.text


def test_vanilla_conditions_share_prompt_text() -> None:
    a, _ = _built("4b_vanilla")
    b, _ = _built("9b_vanilla")
    c, _ = _built("27b_or_frontier_vanilla")
    assert a.text == b.text == c.text


def test_routed_and_teacher_share_prompt_on_v1_fixture() -> None:
    a, _ = _built("4b_ontology_routed")
    b, _ = _built("teacher_skills_4b")
    assert a.text == b.text
    assert a.sha256 == b.sha256


def test_prompt_is_deterministic_and_omits_gold_key() -> None:
    first, _ = _built("4b_ontology_routed")
    second, _ = _built("4b_ontology_routed")
    assert first.text == second.text
    assert first.sha256 == second.sha256
    assert len(first.sha256) == 64
    assert first.template_id == "ontology_skills.prompt.v5"
    assert '"gold"' not in first.text
    assert '"entity_uri"' not in first.text
    assert '"mint_as"' not in first.text


def test_v5_schema_hint_does_not_name_a_gold_type() -> None:
    from ontology_skills.prompts import SCHEMA_HINT, TEMPLATE_ID

    assert TEMPLATE_ID == "ontology_skills.prompt.v5"
    assert "Supplier" not in SCHEMA_HINT
    assert "Company" not in SCHEMA_HINT
    assert "Organization" not in SCHEMA_HINT
    assert "leaf type only, short local id, never an IRI" in SCHEMA_HINT
    assert "No prose" in SCHEMA_HINT
    assert "No code fences" in SCHEMA_HINT
    assert "entity_uri" not in SCHEMA_HINT
    assert "mint_as" not in SCHEMA_HINT
    assert "acme-components" not in SCHEMA_HINT
    prompt, _ = _built("4b_vanilla", "et-001")
    assert prompt.template_id == TEMPLATE_ID
    assert SCHEMA_HINT in prompt.text
    assert "https://graph.infona.ai/bench/ent/acme-components" not in prompt.text

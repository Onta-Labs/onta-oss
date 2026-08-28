"""INF-575 / INF-577 — Clinical Trials install is idempotent and reversible."""

from __future__ import annotations

import pytest

from infona_client.blueprint import (
    inspect_blueprint,
    install_blueprint,
    uninstall_blueprint,
)
from infona_client.blueprint.install import (
    BlueprintNotInstalled,
    BlueprintUninstallRefused,
    manifest_content_hash,
)
from infona_client.blueprint.catalog import reset_blueprint_package_store
from infona_client.blueprint.lock import reset_blueprint_lock_store
from infona_client.blueprint.seeds import CLINICAL_TRIALS
from infona_client.graph.facts import Fact
from infona_client.graph.kg_writer import insert_facts, refresh_after_write
from infona_client.graph.ontology_catalog import list_types
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.queries import kg_graph_uri
from infona_client.skills.store import make_type_skill_store, reset_type_skill_store

TENANT = "bp-install-tenant"
PEER = "bp-peer-tenant"
KG = "clinical-trials"
SEED_TYPES = {
    "ClinicalTrial",
    "Organization",
    "MedicalCondition",
    "Intervention",
    "Investigator",
    "Facility",
}


@pytest.fixture(autouse=True)
def _reset_blueprint_state():
    reset_blueprint_lock_store()
    reset_blueprint_package_store()
    reset_type_skill_store()
    yield
    reset_blueprint_lock_store()
    reset_blueprint_package_store()
    reset_type_skill_store()


async def _type_names(tenant_id: str = TENANT) -> set[str]:
    return {t.name for t in await list_types(tenant_id=tenant_id)}


@pytest.mark.asyncio
async def test_clinical_trials_install_is_idempotent_and_reversible():
    first = await install_blueprint(CLINICAL_TRIALS, tenant_id=TENANT, kg=KG)
    assert first.status == "installed"
    assert first.blueprint_id == "infona/clinical-trials"
    assert first.version == "0.1.0"
    assert first.sample_included is True
    assert first.sample_is_current is False
    assert first.sample_captured_at == "2026-06-01"
    assert set(first.types) == SEED_TYPES
    assert len(first.sample_subjects) == 25
    assert SEED_TYPES <= await _type_names()

    skills = await make_type_skill_store().list_for_tenant(TENANT)
    assert {s.slug for s in skills} >= {"sample-is-not-current", "cite-nct"}

    second = await install_blueprint(CLINICAL_TRIALS, tenant_id=TENANT, kg=KG)
    assert second.status == "already_installed"
    assert second.content_hash == first.content_hash
    assert second.sample_subjects == first.sample_subjects
    assert len(second.sample_subjects) == 25

    card = await inspect_blueprint(TENANT, "infona/clinical-trials")
    assert card["blueprint_id"] == "infona/clinical-trials"
    assert card["sample_is_current"] is False
    assert card["sample_subject_count"] == 25
    assert card["content_hash"] == first.content_hash

    removed = await uninstall_blueprint(TENANT, "infona/clinical-trials")
    assert removed["status"] == "uninstalled"
    assert set(removed["removed_types"]) == SEED_TYPES
    assert len(removed["removed_sample"]) == 25
    assert not (SEED_TYPES & await _type_names())
    assert await make_type_skill_store().list_for_tenant(TENANT) == []

    with pytest.raises(BlueprintNotInstalled):
        await inspect_blueprint(TENANT, "infona/clinical-trials")


@pytest.mark.asyncio
async def test_reinstall_after_uninstall_is_a_fresh_install():
    await install_blueprint(CLINICAL_TRIALS, tenant_id=TENANT, kg=KG)
    await uninstall_blueprint(TENANT, "infona/clinical-trials")
    again = await install_blueprint(CLINICAL_TRIALS, tenant_id=TENANT, kg=KG)
    assert again.status == "installed"
    assert len(again.sample_subjects) == 25
    await uninstall_blueprint(TENANT, "infona/clinical-trials")


@pytest.mark.asyncio
async def test_user_authored_type_survives_uninstall():
    from infona_client.graph.ontology_commit import commit_ontology
    from infona_client.graph.queries import tenant_graph_uri
    from infona_client.models.ontology import OntologyMutation, OntologyOpKind

    await commit_ontology(
        None,
        tenant_graph_uri(TENANT),
        [OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="UserNote")],
    )
    await install_blueprint(CLINICAL_TRIALS, tenant_id=TENANT, kg=KG)
    await uninstall_blueprint(TENANT, "infona/clinical-trials")
    names = await _type_names()
    assert "UserNote" in names
    assert "ClinicalTrial" not in names


@pytest.mark.asyncio
async def test_uninstall_refuses_when_another_kg_holds_typed_data():
    await install_blueprint(CLINICAL_TRIALS, tenant_id=TENANT, kg=KG)
    other = "acquired"
    subject = entity_uri("ClinicalTrial", "USER-ROW")
    await insert_facts(
        None,
        kg_graph_uri(TENANT, other),
        facts=[
            Fact(subject_id=subject, kind="type", key="ClinicalTrial"),
            Fact(subject_id=subject, kind="literal", key="nct_id", value="USER-ROW"),
        ],
    )
    await refresh_after_write(
        None, tenant_id=TENANT, kg_name=other, affected_types=["ClinicalTrial"]
    )
    with pytest.raises(BlueprintUninstallRefused) as exc:
        await uninstall_blueprint(TENANT, "infona/clinical-trials")
    assert exc.value.status_code == 409
    assert any(d["kg"] == other for d in exc.value.details["dependents"])
    # Sample + types still present — refuse did not partial-apply.
    card = await inspect_blueprint(TENANT, "infona/clinical-trials")
    assert card["sample_subject_count"] == 25
    assert "ClinicalTrial" in await _type_names()


@pytest.mark.asyncio
async def test_install_is_confined_to_the_installing_tenant():
    await install_blueprint(CLINICAL_TRIALS, tenant_id=TENANT, kg=KG)
    with pytest.raises(BlueprintNotInstalled):
        await inspect_blueprint(PEER, "infona/clinical-trials")
    assert "ClinicalTrial" not in await _type_names(PEER)


@pytest.mark.asyncio
async def test_install_without_sample_is_an_empty_graph():
    result = await install_blueprint(
        CLINICAL_TRIALS, tenant_id=TENANT, kg=KG, include_sample=False
    )
    assert result.sample_included is False
    assert result.sample_subjects == []
    assert result.sample_is_current is False
    assert set(result.types) == SEED_TYPES
    await uninstall_blueprint(TENANT, "infona/clinical-trials")


@pytest.mark.asyncio
async def test_sample_flag_flip_keeps_ownership_for_uninstall():
    first = await install_blueprint(CLINICAL_TRIALS, tenant_id=TENANT, kg=KG)
    assert first.status == "installed"
    flipped = await install_blueprint(
        CLINICAL_TRIALS, tenant_id=TENANT, kg=KG, include_sample=False
    )
    assert flipped.status == "updated"
    assert flipped.sample_included is False
    removed = await uninstall_blueprint(TENANT, "infona/clinical-trials")
    assert set(removed["removed_types"]) == SEED_TYPES
    assert not (SEED_TYPES & await _type_names())


@pytest.mark.asyncio
async def test_uninstall_refuses_when_kg_scan_fails(monkeypatch):
    await install_blueprint(CLINICAL_TRIALS, tenant_id=TENANT, kg=KG)

    async def boom(_tenant_id: str):
        raise RuntimeError("registry down")

    monkeypatch.setattr(
        "infona_client.graph.kg_registry.list_registered_kgs", boom
    )
    with pytest.raises(BlueprintUninstallRefused) as exc:
        await uninstall_blueprint(TENANT, "infona/clinical-trials")
    assert exc.value.status_code == 409
    assert "ClinicalTrial" in await _type_names()
    card = await inspect_blueprint(TENANT, "infona/clinical-trials")
    assert card["sample_subject_count"] == 25


def test_content_hash_is_stable():
    from infona_client.blueprint import load_blueprint_package

    manifest = load_blueprint_package(CLINICAL_TRIALS)
    assert manifest_content_hash(manifest) == manifest_content_hash(manifest)
    assert len(manifest_content_hash(manifest)) == 64


def test_load_and_validate_parses_yaml_document_not_as_a_path():
    from infona_client.blueprint.install import load_and_validate

    yaml_text = (CLINICAL_TRIALS / "blueprint.yaml").read_text(encoding="utf-8")
    manifest = load_and_validate(yaml_text)
    assert manifest.id == "infona/clinical-trials"
    assert load_and_validate(CLINICAL_TRIALS).id == manifest.id


def test_sample_relationship_facts_use_rel_kind():
    """INF-576 — a type-ranged sample slot is a rel Fact, not a literal."""
    from infona_client.blueprint import load_blueprint_package
    from infona_client.blueprint.plan import facts_for_sample
    from infona_client.graph.ontology_queries import entity_uri

    manifest = load_blueprint_package(CLINICAL_TRIALS)
    assert manifest.sample is not None
    trial = next(e for e in manifest.sample.entities if e.type == "ClinicalTrial")
    trial.attributes["lead_sponsor"] = "Example Pharma A"
    facts, _ = facts_for_sample(manifest)
    rels = [f for f in facts if f.kind == "rel"]
    assert len(rels) == 1
    assert rels[0].key == "lead_sponsor"
    assert rels[0].value == entity_uri("Organization", "Example Pharma A")
    assert not any(f.kind == "literal" and f.key == "lead_sponsor" for f in facts)

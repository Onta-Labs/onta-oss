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
    instance_edge_predicate,
    manifest_content_hash,
)
from infona_client.blueprint.catalog import reset_blueprint_package_store
from infona_client.blueprint.lock import (
    GraphStoreBlueprintLockStore,
    make_blueprint_lock_store,
    reset_blueprint_lock_store,
)
from infona_client.blueprint.overlay import reset_blueprint_overlay_store
from infona_client.blueprint.seeds import CLINICAL_TRIALS
from infona_client.graph.facts import Fact
from infona_client.graph.kg_writer import insert_facts, refresh_after_write
from infona_client.graph.ontology_catalog import list_types
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.queries import kg_graph_uri
from infona_client.graph.store import get_graph_store
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
    reset_blueprint_overlay_store()
    reset_type_skill_store()
    yield
    reset_blueprint_lock_store()
    reset_blueprint_package_store()
    reset_blueprint_overlay_store()
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

    graph = get_graph_store()
    raw = await graph.blueprint_lock_get(TENANT, "infona/clinical-trials")
    assert raw is not None
    assert raw["content_hash"] == first.content_hash
    assert raw["version"] == first.version

    removed = await uninstall_blueprint(TENANT, "infona/clinical-trials")
    assert removed["status"] == "uninstalled"
    assert set(removed["removed_types"]) == SEED_TYPES
    assert len(removed["removed_sample"]) == 25
    assert not (SEED_TYPES & await _type_names())
    assert await make_type_skill_store().list_for_tenant(TENANT) == []

    with pytest.raises(BlueprintNotInstalled):
        await inspect_blueprint(TENANT, "infona/clinical-trials")
    assert await graph.blueprint_overlay_get(TENANT, "infona/clinical-trials") is None
    assert await graph.blueprint_package_get(TENANT, "infona/clinical-trials") is None


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


@pytest.mark.asyncio
async def test_install_pin_survives_lock_store_reload():
    """Process bounce / other ECS task: new lock wrapper, same GraphStore."""
    first = await install_blueprint(CLINICAL_TRIALS, tenant_id=TENANT, kg=KG)
    assert first.status == "installed"
    assert isinstance(make_blueprint_lock_store(), GraphStoreBlueprintLockStore)

    reset_blueprint_lock_store()
    wrapper = GraphStoreBlueprintLockStore()
    pin = await wrapper.get(TENANT, "infona/clinical-trials")
    assert pin is not None
    assert pin.content_hash == first.content_hash
    assert pin.sample_subjects == first.sample_subjects

    card = await inspect_blueprint(TENANT, "infona/clinical-trials")
    assert card["blueprint_id"] == "infona/clinical-trials"
    assert card["version"] == first.version
    assert card["content_hash"] == first.content_hash
    assert card["sample_subject_count"] == 25
    assert card["installed"] is True

    again = await install_blueprint(CLINICAL_TRIALS, tenant_id=TENANT, kg=KG)
    assert again.status == "already_installed"

    reset_blueprint_lock_store()
    with pytest.raises(BlueprintNotInstalled):
        await inspect_blueprint(PEER, "infona/clinical-trials")

    reset_blueprint_lock_store()
    removed = await uninstall_blueprint(TENANT, "infona/clinical-trials")
    assert removed["status"] == "uninstalled"
    assert set(removed["removed_types"]) == SEED_TYPES
    assert len(removed["removed_sample"]) == 25
    assert not (SEED_TYPES & await _type_names())

    reset_blueprint_lock_store()
    with pytest.raises(BlueprintNotInstalled):
        await inspect_blueprint(TENANT, "infona/clinical-trials")
    assert await get_graph_store().blueprint_lock_get(
        TENANT, "infona/clinical-trials"
    ) is None


def test_sample_relationship_facts_use_rel_kind():
    """INF-576 — a type-ranged sample slot is a rel Fact, not a literal."""
    from infona_client.blueprint import load_blueprint_package
    from infona_client.blueprint.plan import facts_for_sample
    from infona_client.graph.iri import ONTO_PRED_PREFIX
    from infona_client.graph.ontology_queries import attr_uri, entity_uri

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
    pred = instance_edge_predicate("lead_sponsor")
    assert pred == f"{ONTO_PRED_PREFIX}lead_sponsor"
    assert pred != attr_uri("ClinicalTrial", "lead_sponsor")
    assert "/attrs/" not in pred


@pytest.mark.asyncio
async def test_clinical_trials_install_writes_through_shared_path(monkeypatch):
    """INF-576 — sample insert + refresh go through kg_writer, not a bespoke write."""
    insert_calls: list[tuple] = []
    refresh_calls: list[tuple] = []
    real_insert = insert_facts
    real_refresh = refresh_after_write

    async def spy_insert(*args, **kwargs):
        insert_calls.append((args, kwargs))
        return await real_insert(*args, **kwargs)

    async def spy_refresh(*args, **kwargs):
        refresh_calls.append((args, kwargs))
        return await real_refresh(*args, **kwargs)

    monkeypatch.setattr("infona_client.blueprint.install.insert_facts", spy_insert)
    monkeypatch.setattr(
        "infona_client.blueprint.install.refresh_after_write", spy_refresh
    )

    await install_blueprint(CLINICAL_TRIALS, tenant_id=TENANT, kg=KG)
    assert len(insert_calls) == 1
    facts = insert_calls[0][1]["facts"]
    assert facts
    assert all(isinstance(f, Fact) for f in facts)
    assert {f.kind for f in facts} <= {"type", "literal", "rel"}
    assert any(f.kind == "type" for f in facts)
    assert any(f.kind == "literal" for f in facts)
    # Shipped seed sample is still literals-only. Rel edges are proven
    # by test_install_relationship_lands_on_onto_not_attrs.
    assert not any(f.kind == "rel" for f in facts)
    assert len(refresh_calls) == 1
    assert refresh_calls[0][1]["tenant_id"] == TENANT
    assert refresh_calls[0][1]["kg_name"] == KG
    assert "ClinicalTrial" in refresh_calls[0][1]["affected_types"]
    await uninstall_blueprint(TENANT, "infona/clinical-trials")


@pytest.mark.asyncio
async def test_install_relationship_lands_on_onto_not_attrs():
    """INF-576 — a sample rel is an object Assertion, not an attrs/ literal."""
    from infona_client.blueprint import load_blueprint_package
    from infona_client.graph.assertion_model import property_uri
    from infona_client.graph.iri import ONTO_PRED_PREFIX
    from infona_client.graph.ontology_queries import attr_uri
    from infona_client.graph.rdfs_helpers import (
        session_literal_values,
        session_object_values,
    )
    from infona_client.graph.scope import GraphScope
    from infona_client.graph.store import get_graph_store

    manifest = load_blueprint_package(CLINICAL_TRIALS)
    assert manifest.sample is not None
    trial = next(
        e
        for e in manifest.sample.entities
        if e.type == "ClinicalTrial" and e.attributes.get("nct_id") == "SAMPLE-001"
    )
    trial.attributes["lead_sponsor"] = "Example Pharma A"
    await install_blueprint(manifest, tenant_id=TENANT, kg=KG)

    trial_uri = entity_uri("ClinicalTrial", "SAMPLE-001")
    org_uri = entity_uri("Organization", "Example Pharma A")
    pred = instance_edge_predicate("lead_sponsor")
    assert pred == f"{ONTO_PRED_PREFIX}lead_sponsor"
    assert pred != attr_uri("ClinicalTrial", "lead_sponsor")
    assert "/attrs/lead_sponsor" in attr_uri("ClinicalTrial", "lead_sponsor")

    session = get_graph_store().session(GraphScope.for_instance(TENANT, KG))
    objs = await session_object_values(
        session, trial_uri, property_uri("lead_sponsor")
    )
    assert objs == [org_uri]
    lits = await session_literal_values(
        session, trial_uri, property_uri("lead_sponsor")
    )
    assert lits == []
    await uninstall_blueprint(TENANT, "infona/clinical-trials")


@pytest.mark.asyncio
async def test_install_composite_rel_uses_sample_subject_not_a_partial_key():
    """INF-576 — conducted_at lands on the Facility sample_subject IRI."""
    from infona_client.blueprint import load_blueprint_package
    from infona_client.blueprint.models import SampleEntity
    from infona_client.blueprint.plan import (
        BlueprintValidationError,
        facts_for_sample,
        sample_subject,
    )
    from infona_client.graph.assertion_model import property_uri
    from infona_client.graph.rdfs_helpers import session_object_values
    from infona_client.graph.scope import GraphScope
    from infona_client.graph.store import get_graph_store

    manifest = load_blueprint_package(CLINICAL_TRIALS)
    assert manifest.sample is not None
    # Seed is at the 25-entity cap. Drop interventions so a Facility fits.
    manifest.sample.entities = [
        e for e in manifest.sample.entities if e.type != "Intervention"
    ]
    facility = next(c for c in manifest.concepts if c.name == "Facility")
    site = SampleEntity(
        type="Facility",
        attributes={"facility_name": "MGH", "country": "USA"},
    )
    manifest.sample.entities.append(site)
    trial = next(
        e
        for e in manifest.sample.entities
        if e.type == "ClinicalTrial" and e.attributes.get("nct_id") == "SAMPLE-001"
    )
    trial.attributes["conducted_at"] = "MGH"
    with pytest.raises(BlueprintValidationError, match="does not resolve"):
        facts_for_sample(manifest)

    trial.attributes["conducted_at"] = "MGH_USA"
    await install_blueprint(manifest, tenant_id=TENANT, kg=KG)
    expected = sample_subject(facility, site)
    session = get_graph_store().session(GraphScope.for_instance(TENANT, KG))
    objs = await session_object_values(
        session,
        entity_uri("ClinicalTrial", "SAMPLE-001"),
        property_uri("conducted_at"),
    )
    assert objs == [expected]
    assert expected == entity_uri("Facility", "MGH_USA")
    assert entity_uri("Facility", "MGH") not in objs
    await uninstall_blueprint(TENANT, "infona/clinical-trials")


def test_blueprint_install_module_uses_shared_write_primitives():
    """INF-576 structural: install.py must not grow a bespoke instance write."""
    import inspect
    import re

    import infona_client.blueprint.install as mod

    src = inspect.getsource(mod)
    assert re.search(r"(?<![\w.])insert_facts\(", src)
    assert re.search(r"(?<![\w.])delete_facts\(", src)
    assert re.search(r"(?<![\w.])refresh_after_write\(", src)
    assert re.search(r"(?<![\w.])insert_triples\(", src) is None
    assert re.search(r"DELETE\s*\{|DELETE\s+WHERE|DELETE\s+DATA", src) is None

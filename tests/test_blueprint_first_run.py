"""INF-593 — post-install first run: credentials → acquire → first answer."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from infona_client.api_registry.executor import RegistryApiSource
from infona_client.blueprint import (
    inspect_blueprint,
    install_blueprint,
    run_first_run,
    uninstall_blueprint,
)
from infona_client.blueprint.catalog import reset_blueprint_package_store
from infona_client.blueprint.first_run import (
    FIRST_RUN_MAX_ROWS,
    facts_from_registry_rows,
    missing_credentials,
    required_credentials,
)
from infona_client.blueprint.lock import reset_blueprint_lock_store
from infona_client.blueprint.overlay import reset_blueprint_overlay_store
from infona_client.blueprint.plan import (
    BlueprintAcquisitionFailed,
    BlueprintCredentialsMissing,
    BlueprintNotInstalled,
    BlueprintPaidBinding,
    BlueprintUninstallRefused,
    load_and_validate,
)
from infona_client.blueprint.seeds import CLINICAL_TRIALS
from infona_client.graph.facts import Fact
from infona_client.graph.iri import ONTO_PRED_PREFIX
from infona_client.graph.ontology_queries import entity_uri
from infona_client.retrieval import safety as safety_mod
from infona_client.skills.store import reset_type_skill_store

KG = "clinical-trials"
NCT = "NCT09990001"

_CT_PAYLOAD = {
    "studies": [
        {
            "protocolSection": {
                "identificationModule": {
                    "nctId": NCT,
                    "briefTitle": "Phase 3 obesity recruiting study",
                    "officialTitle": "A Phase 3 study of obesity",
                },
                "statusModule": {"overallStatus": "RECRUITING"},
                "designModule": {
                    "phases": ["PHASE3"],
                    "studyType": "INTERVENTIONAL",
                    "enrollmentInfo": {"count": 400},
                },
                "sponsorCollaboratorsModule": {
                    "leadSponsor": {"name": "Live Example Sponsor", "class": "INDUSTRY"}
                },
                "conditionsModule": {"conditions": ["Obesity"]},
            }
        }
    ],
    "nextPageToken": None,
    "totalCount": 1,
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


@pytest.fixture(autouse=True)
def _offline_dns(monkeypatch):
    monkeypatch.setattr(safety_mod, "_resolve_ips", lambda host: ["93.184.216.34"])


def _executor() -> RegistryApiSource:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "clinicaltrials.gov" in str(request.url)
        return httpx.Response(200, json=_CT_PAYLOAD)

    return RegistryApiSource(transport=httpx.MockTransport(handler))


def _min_manifest() -> dict:
    path = (
        Path(__file__).resolve().parents[1]
        / "infona_client/blueprint/data/clinical_trials_min.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _byok_manifest() -> dict:
    body = _min_manifest()
    body["sources"].append(
        {
            "id": "private-tracker",
            "title": "Private trial tracker",
            "kind": "licensed_api",
            "publisher": "Example",
            "description": "Keyed source used to prove first-run fail-closed.",
            "license": "Apache-2.0",
            "url": "https://example.test/api/trials",
            "credential": "byok",
            "key_env": "INFONA_TEST_TRACKER_KEY",
            "declared_cadence": "weekly",
            "mappings": [
                {
                    "source_field": "id",
                    "lands_on": "ClinicalTrial.nct_id",
                    "kind": "literal",
                }
            ],
        }
    )
    return body


@pytest.mark.asyncio
async def test_clinical_trials_first_run_acquires_and_answers():
    tenant = "bp-fr-acquire"
    installed = await install_blueprint(CLINICAL_TRIALS, tenant_id=tenant, kg=KG)
    assert installed.sample_is_current is False
    assert installed.sample_included is True

    result = await run_first_run(
        tenant,
        "infona/clinical-trials",
        executor=_executor(),
        max_rows=FIRST_RUN_MAX_ROWS,
    )
    assert result.status == "answered"
    assert result.task == "acquire_condition_set"
    assert result.acquired_rows == 1
    assert result.sample_is_current is False
    assert result.sample_used is False
    assert NCT in result.citations
    assert NCT in result.answer
    assert "SAMPLE-" not in result.answer
    assert "not current" not in result.answer.lower() or NCT in result.answer
    assert entity_uri("ClinicalTrial", NCT) in result.acquired_subjects
    assert "ctgov" in result.sources

    card = await inspect_blueprint(tenant, "infona/clinical-trials")
    assert card["sample_is_current"] is False
    # Acquired rows are live instance data, not sample — uninstall must refuse.
    with pytest.raises(BlueprintUninstallRefused):
        await uninstall_blueprint(tenant, "infona/clinical-trials")


@pytest.mark.asyncio
async def test_missing_byok_credentials_fail_closed():
    tenant = "bp-fr-missing-creds"
    await install_blueprint(_byok_manifest(), tenant_id=tenant, kg=KG)
    assert required_credentials(load_and_validate(_byok_manifest()))
    missing = missing_credentials(
        load_and_validate(_byok_manifest()),
        provided={},
        environ={},
    )
    assert [m.source_id for m in missing] == ["private-tracker"]

    with pytest.raises(BlueprintCredentialsMissing) as exc:
        await run_first_run(
            tenant,
            "infona/clinical-trials",
            executor=_executor(),
            environ={},
        )
    assert exc.value.status_code == 400
    assert exc.value.details["fail_closed"] is True
    assert exc.value.details["missing"][0]["key_env"] == "INFONA_TEST_TRACKER_KEY"

    card = await inspect_blueprint(tenant, "infona/clinical-trials")
    assert card["sample_is_current"] is False
    await uninstall_blueprint(tenant, "infona/clinical-trials")


@pytest.mark.asyncio
async def test_supplied_byok_credentials_then_acquire_and_answer():
    tenant = "bp-fr-supplied-creds"
    await install_blueprint(_byok_manifest(), tenant_id=tenant, kg=KG)
    result = await run_first_run(
        tenant,
        "infona/clinical-trials",
        credentials={"INFONA_TEST_TRACKER_KEY": "user-owned-test-key"},
        executor=_executor(),
        environ={},
    )
    assert result.status == "answered"
    assert result.sample_is_current is False
    assert NCT in result.citations
    assert "user-owned-test-key" not in result.answer
    with pytest.raises(BlueprintUninstallRefused):
        await uninstall_blueprint(tenant, "infona/clinical-trials")


@pytest.mark.asyncio
async def test_first_run_requires_install():
    with pytest.raises(BlueprintNotInstalled):
        await run_first_run("bp-fr-missing-pin", "infona/clinical-trials", executor=_executor())


@pytest.mark.asyncio
async def test_empty_acquire_answers_from_sample_without_claiming_current():
    tenant = "bp-fr-sample-only"
    await install_blueprint(CLINICAL_TRIALS, tenant_id=tenant, kg=KG)

    def empty_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"studies": [], "totalCount": 0})

    empty = RegistryApiSource(transport=httpx.MockTransport(empty_handler))
    result = await run_first_run(tenant, "infona/clinical-trials", executor=empty)
    assert result.status == "answered"
    assert result.acquired_rows == 0
    assert result.sample_is_current is False
    assert result.sample_used is True
    assert "sample" in result.answer.lower()
    assert "2026-06-01" in result.answer
    assert "not current" in result.answer.lower()
    await uninstall_blueprint(tenant, "infona/clinical-trials")


@pytest.mark.asyncio
async def test_paid_catalog_binding_fails_closed(monkeypatch):
    tenant = "bp-fr-paid"
    await install_blueprint(CLINICAL_TRIALS, tenant_id=tenant, kg=KG)

    class _Paid:
        is_paid = True
        slug = "clinicaltrials_gov"

    monkeypatch.setattr(
        "infona_client.blueprint.first_run.make_api_source_catalog",
        lambda: type("C", (), {"get": staticmethod(lambda slug: _Paid())})(),
    )
    with pytest.raises(BlueprintPaidBinding) as exc:
        await run_first_run(tenant, "infona/clinical-trials", executor=_executor())
    assert exc.value.status_code == 403
    assert exc.value.details["fail_closed"] is True
    await uninstall_blueprint(tenant, "infona/clinical-trials")


def test_acquired_facts_use_shared_rel_kind():
    facts, subjects = facts_from_registry_rows(
        [
            {
                "nct_id": NCT,
                "title": "Phase 3 obesity",
                "status": "RECRUITING",
                "phase": "PHASE3",
                "lead_sponsor": "Live Example Sponsor",
                "conditions": ["Obesity"],
            }
        ],
        source_mark="api:clinicaltrials_gov",
    )
    assert subjects == [entity_uri("ClinicalTrial", NCT)]
    assert {f.kind for f in facts} <= {"type", "literal", "rel"}
    rels = [f for f in facts if f.kind == "rel"]
    assert any(f.key == "lead_sponsor" for f in rels)
    assert any(f.key == "studies_condition" for f in rels)
    assert all(isinstance(f, Fact) for f in facts)
    assert all(f.key != "lead_sponsor" or f.kind == "rel" for f in facts)
    assert f"{ONTO_PRED_PREFIX}lead_sponsor".endswith("lead_sponsor")


@pytest.mark.asyncio
async def test_first_run_writes_through_insert_facts(monkeypatch):
    insert_calls: list[tuple] = []
    from infona_client.graph.kg_writer import insert_facts as real_insert

    async def spy_insert(*args, **kwargs):
        insert_calls.append((args, kwargs))
        return await real_insert(*args, **kwargs)

    monkeypatch.setattr("infona_client.blueprint.first_run.insert_facts", spy_insert)
    tenant = "bp-fr-write-path"
    await install_blueprint(CLINICAL_TRIALS, tenant_id=tenant, kg=KG)
    await run_first_run(tenant, "infona/clinical-trials", executor=_executor())
    assert insert_calls
    facts = insert_calls[0][1]["facts"]
    assert facts
    assert all(isinstance(f, Fact) for f in facts)
    with pytest.raises(BlueprintUninstallRefused):
        await uninstall_blueprint(tenant, "infona/clinical-trials")


@pytest.mark.asyncio
async def test_acquire_error_fails_closed():
    tenant = "bp-fr-acquire-error"
    await install_blueprint(CLINICAL_TRIALS, tenant_id=tenant, kg=KG)

    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "nope"})

    bad = RegistryApiSource(transport=httpx.MockTransport(boom))
    with pytest.raises((BlueprintAcquisitionFailed, BlueprintCredentialsMissing)):
        await run_first_run(tenant, "infona/clinical-trials", executor=bad)
    await uninstall_blueprint(tenant, "infona/clinical-trials")

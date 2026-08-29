"""INF-575 / INF-579 / INF-578 — one /graphs/{tenant}/blueprints family."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from infona_client.blueprint import load_blueprint_package
from infona_client.blueprint.catalog import reset_blueprint_package_store
from infona_client.blueprint.lock import reset_blueprint_lock_store
from infona_client.blueprint.overlay import reset_blueprint_overlay_store
from infona_client.blueprint.seeds import CLINICAL_TRIALS
from infona_client.skills.store import reset_type_skill_store

TENANT = "test-tenant"
KG = "clinical-trials"


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


def _manifest() -> dict:
    return load_blueprint_package(CLINICAL_TRIALS).model_dump(mode="json")


def test_install_inspect_uninstall_via_canonical_routes(client, auth_headers):
    installed = client.post(
        f"/graphs/{TENANT}/blueprints/install",
        json={"kg": KG, "manifest": _manifest()},
        headers=auth_headers,
    )
    assert installed.status_code == 200, installed.text
    body = installed.json()
    assert body["status"] == "installed"
    assert body["blueprint_id"] == "infona/clinical-trials"
    assert body["sample_is_current"] is False

    again = client.post(
        f"/graphs/{TENANT}/blueprints/install",
        json={"kg": KG, "manifest": _manifest()},
        headers=auth_headers,
    )
    assert again.status_code == 200
    assert again.json()["status"] == "already_installed"

    listed = client.get(f"/graphs/{TENANT}/blueprints", headers=auth_headers)
    assert listed.status_code == 200
    ids = [row["blueprint_id"] for row in listed.json()["blueprints"]]
    assert "infona/clinical-trials" in ids

    card = client.get(
        f"/graphs/{TENANT}/blueprints/infona/clinical-trials",
        headers=auth_headers,
    )
    assert card.status_code == 200
    assert card.json()["sample_is_current"] is False
    assert card.json()["version"] == "0.1.0"

    gone = client.delete(
        f"/graphs/{TENANT}/blueprints/infona/clinical-trials",
        headers=auth_headers,
    )
    assert gone.status_code == 200
    assert gone.json()["status"] == "uninstalled"

    missing = client.get(
        f"/graphs/{TENANT}/blueprints/infona/clinical-trials",
        headers=auth_headers,
    )
    assert missing.status_code == 404


def test_fork_copies_the_seed_with_lineage_on_the_same_route_family(
    client, auth_headers
):
    resp = client.post(
        f"/graphs/{TENANT}/blueprints/infona/clinical-trials/fork",
        json={"as": "acme/clinical-trials"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "forked"
    assert body["blueprint_id"] == "acme/clinical-trials"
    assert body["parent_id"] == "infona/clinical-trials"
    assert body["parent_version"] == "0.1.0"
    assert body["lineage"]["parent"] == {
        "id": "infona/clinical-trials",
        "version": "0.1.0",
    }
    assert "Infona" in body["attribution"]
    assert body["sample_is_current"] is False

    card = client.get(
        f"/graphs/{TENANT}/blueprints/acme/clinical-trials",
        headers=auth_headers,
    )
    assert card.status_code == 200, card.text
    assert card.json()["lineage"]["parent"]["id"] == "infona/clinical-trials"
    assert card.json()["sample_is_current"] is False

    listed = client.get(f"/graphs/{TENANT}/blueprints", headers=auth_headers)
    assert listed.json()["blueprints"] == []


def test_install_of_fork_is_a_separate_pin(client, auth_headers):
    parent = client.post(
        f"/graphs/{TENANT}/blueprints/install",
        json={"kg": KG, "manifest": _manifest()},
        headers=auth_headers,
    )
    assert parent.status_code == 200, parent.text
    forked = client.post(
        f"/graphs/{TENANT}/blueprints/infona/clinical-trials/fork",
        json={"as": "acme/clinical-trials"},
        headers=auth_headers,
    )
    assert forked.status_code == 200, forked.text
    child = client.post(
        f"/graphs/{TENANT}/blueprints/install",
        json={"kg": "fork-kg", "manifest": forked.json()["manifest"]},
        headers=auth_headers,
    )
    assert child.status_code == 200, child.text
    assert child.json()["status"] == "installed"
    assert child.json()["blueprint_id"] == "acme/clinical-trials"
    assert child.json()["blueprint_id"] != parent.json()["blueprint_id"]

    listed = client.get(f"/graphs/{TENANT}/blueprints", headers=auth_headers)
    ids = [row["blueprint_id"] for row in listed.json()["blueprints"]]
    assert "infona/clinical-trials" in ids
    assert "acme/clinical-trials" in ids

    again_parent = client.post(
        f"/graphs/{TENANT}/blueprints/install",
        json={"kg": KG, "manifest": _manifest()},
        headers=auth_headers,
    )
    assert again_parent.json()["status"] == "already_installed"

    gone = client.delete(
        f"/graphs/{TENANT}/blueprints/acme/clinical-trials",
        headers=auth_headers,
    )
    assert gone.status_code == 200
    still = client.get(
        f"/graphs/{TENANT}/blueprints/infona/clinical-trials",
        headers=auth_headers,
    )
    assert still.status_code == 200
    assert still.json()["blueprint_id"] == "infona/clinical-trials"


def test_fork_is_confined_to_path_tenant(client, auth_headers):
    resp = client.post(
        "/graphs/someone-else/blueprints/infona/clinical-trials/fork",
        json={"as": "acme/clinical-trials"},
        headers=auth_headers,
    )
    assert resp.status_code == 403


def test_install_is_confined_to_path_tenant(client, auth_headers):
    resp = client.post(
        "/graphs/someone-else/blueprints/install",
        json={"kg": KG, "manifest": _manifest()},
        headers=auth_headers,
    )
    assert resp.status_code == 403


def test_install_accepts_manifest_yaml_like_the_cli(client, auth_headers):
    yaml_text = (CLINICAL_TRIALS / "blueprint.yaml").read_text(encoding="utf-8")
    installed = client.post(
        f"/graphs/{TENANT}/blueprints/install",
        json={"kg": KG, "manifest_yaml": yaml_text},
        headers=auth_headers,
    )
    assert installed.status_code == 200, installed.text
    assert installed.json()["status"] == "installed"
    assert installed.json()["sample_is_current"] is False
    validated = client.post(
        f"/graphs/{TENANT}/blueprints/validate",
        json={"manifest_yaml": yaml_text},
        headers=auth_headers,
    )
    assert validated.status_code == 200
    assert validated.json()["valid"] is True


def test_validate_writes_nothing(client, auth_headers):
    resp = client.post(
        f"/graphs/{TENANT}/blueprints/validate",
        json={"manifest": _manifest()},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["valid"] is True
    listed = client.get(f"/graphs/{TENANT}/blueprints", headers=auth_headers)
    assert listed.json()["blueprints"] == []


def _min_manifest() -> dict:
    path = (
        Path(__file__).resolve().parents[1]
        / "infona_client/blueprint/data/clinical_trials_min.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_extend_and_update_on_the_same_route_family(client, auth_headers):
    body = _min_manifest()
    installed = client.post(
        f"/graphs/{TENANT}/blueprints/install",
        json={"kg": KG, "include_sample": False, "manifest": body},
        headers=auth_headers,
    )
    assert installed.status_code == 200, installed.text
    extended = client.post(
        f"/graphs/{TENANT}/blueprints/infona/clinical-trials/extend",
        json={
            "overlay": {
                "concepts": [
                    {
                        "name": "ClinicalTrial",
                        "attributes": [
                            {
                                "name": "internal_priority",
                                "kind": "literal",
                                "datatype": "string",
                                "optional": True,
                            }
                        ],
                    }
                ]
            }
        },
        headers=auth_headers,
    )
    assert extended.status_code == 200, extended.text
    assert extended.json()["status"] == "extended"
    nxt = dict(body)
    nxt["version"] = "0.2.0"
    updated = client.post(
        f"/graphs/{TENANT}/blueprints/infona/clinical-trials/update",
        json={"manifest": nxt, "include_sample": False},
        headers=auth_headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["status"] == "updated"
    assert updated.json()["conflicts"] == []
    card = client.get(
        f"/graphs/{TENANT}/blueprints/infona/clinical-trials",
        headers=auth_headers,
    )
    assert card.json()["overlay"]["concepts"][0]["name"] == "ClinicalTrial"


def test_first_run_via_canonical_route(client, auth_headers, monkeypatch):
    """Route runs acquire → answer. Fetch is stubbed; the orchestrator is not."""
    from infona_client.api_registry.executor import ApiCallResult, RegistryApiSource

    installed = client.post(
        f"/graphs/{TENANT}/blueprints/install",
        json={"kg": KG, "manifest": _manifest()},
        headers=auth_headers,
    )
    assert installed.status_code == 200, installed.text

    async def fake_execute(self, spec, bindings=None, **kwargs):
        return ApiCallResult(
            slug=spec.slug,
            rows=[
                {
                    "nct_id": "NCT09990001",
                    "title": "Phase 3 obesity recruiting study",
                    "status": "RECRUITING",
                    "phase": "PHASE3",
                    "lead_sponsor": "Live Example Sponsor",
                    "conditions": ["Obesity"],
                }
            ],
        )

    monkeypatch.setattr(RegistryApiSource, "execute", fake_execute)
    resp = client.post(
        f"/graphs/{TENANT}/blueprints/infona/clinical-trials/first-run",
        json={},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "answered"
    assert body["task"] == "acquire_condition_set"
    assert body["sample_is_current"] is False
    assert "NCT09990001" in body["citations"]
    assert "NCT09990001" in body["answer"]
    assert body["sample_used"] is False


def test_first_run_missing_credentials_fail_closed(client, auth_headers):
    body = _min_manifest()
    body["sources"].append(
        {
            "id": "private-tracker",
            "title": "Private trial tracker",
            "kind": "licensed_api",
            "publisher": "Example",
            "description": "Keyed source.",
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
    installed = client.post(
        f"/graphs/{TENANT}/blueprints/install",
        json={"kg": KG, "include_sample": False, "manifest": body},
        headers=auth_headers,
    )
    assert installed.status_code == 200, installed.text
    resp = client.post(
        f"/graphs/{TENANT}/blueprints/infona/clinical-trials/first-run",
        json={},
        headers=auth_headers,
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["fail_closed"] is True
    assert detail["missing"][0]["key_env"] == "INFONA_TEST_TRACKER_KEY"


def test_first_run_is_confined_to_path_tenant(client, auth_headers):
    resp = client.post(
        "/graphs/someone-else/blueprints/infona/clinical-trials/first-run",
        json={},
        headers=auth_headers,
    )
    assert resp.status_code == 403


def test_extend_and_update_are_confined_to_path_tenant(client, auth_headers):
    ext = client.post(
        "/graphs/someone-else/blueprints/infona/clinical-trials/extend",
        json={"overlay": {"concepts": []}},
        headers=auth_headers,
    )
    assert ext.status_code == 403
    upd = client.post(
        "/graphs/someone-else/blueprints/infona/clinical-trials/update",
        json={"manifest": _manifest()},
        headers=auth_headers,
    )
    assert upd.status_code == 403

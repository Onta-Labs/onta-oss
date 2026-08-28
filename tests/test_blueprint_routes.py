"""INF-575 — one /graphs/{tenant}/blueprints family; fork is 501."""

from __future__ import annotations

import pytest

from infona_client.blueprint import load_blueprint_package
from infona_client.blueprint.lock import reset_blueprint_lock_store
from infona_client.blueprint.seeds import CLINICAL_TRIALS
from infona_client.skills.store import reset_type_skill_store

TENANT = "test-tenant"
KG = "clinical-trials"


@pytest.fixture(autouse=True)
def _reset_blueprint_state():
    reset_blueprint_lock_store()
    reset_type_skill_store()
    yield
    reset_blueprint_lock_store()
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


def test_fork_is_501_on_the_same_route_family(client, auth_headers):
    resp = client.post(
        f"/graphs/{TENANT}/blueprints/infona/clinical-trials/fork",
        headers=auth_headers,
    )
    assert resp.status_code == 501
    detail = resp.json()["detail"]
    assert "INF-579" in str(detail)


def test_install_is_confined_to_path_tenant(client, auth_headers):
    resp = client.post(
        "/graphs/someone-else/blueprints/install",
        json={"kg": KG, "manifest": _manifest()},
        headers=auth_headers,
    )
    assert resp.status_code == 403


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

"""INF-605 — public install target is a new workspace; first-run is kicked."""

from __future__ import annotations

import pytest

from infona_client.api_registry.executor import ApiCallResult, RegistryApiSource
from infona_client.auth.tenant_directory import (
    Tenant,
    TenantProviderError,
    register_tenant_provider,
)
from infona_client.blueprint.catalog import reset_blueprint_package_store
from infona_client.blueprint.install import inspect_blueprint, list_installed_blueprints
from infona_client.blueprint.lock import reset_blueprint_lock_store
from infona_client.blueprint.overlay import reset_blueprint_overlay_store
from infona_client.blueprint.public_path import (
    TARGET_NEW_WORKSPACE,
    resolve_shipped_seed,
    tenant_holds_graph,
)
from infona_client.graph.kg_registry import list_registered_kgs, upsert_registered_kg
from infona_client.skills.store import reset_type_skill_store

TENANT = "test-tenant"
UNRELATED_KG = "unrelated-movies-inf605"


class _FakeDirectory:
    def __init__(self) -> None:
        self.store: dict[str, list[Tenant]] = {
            "test-key": [Tenant(id=TENANT, label="Test")],
        }

    def _user(self, api_key: str) -> list[Tenant]:
        if api_key not in self.store:
            raise TenantProviderError(401, "Invalid API key")
        return self.store[api_key]

    def list_tenants(self, api_key: str) -> list[Tenant]:
        return list(self._user(api_key))

    def add_tenant(self, api_key: str, tenant_id: str, label: str) -> Tenant:
        owned = self._user(api_key)
        if any(t.id == tenant_id for t in owned):
            raise TenantProviderError(409, f'Tenant "{tenant_id}" already exists.')
        row = Tenant(id=tenant_id, label=label)
        owned.append(row)
        return row

    def remove_tenant(self, api_key: str, tenant_id: str) -> None:
        owned = self._user(api_key)
        self.store[api_key] = [t for t in owned if t.id != tenant_id]


@pytest.fixture(autouse=True)
def _reset_blueprint_state():
    reset_blueprint_lock_store()
    reset_blueprint_package_store()
    reset_blueprint_overlay_store()
    reset_type_skill_store()
    register_tenant_provider(_FakeDirectory())
    yield
    register_tenant_provider(None)
    reset_blueprint_lock_store()
    reset_blueprint_package_store()
    reset_blueprint_overlay_store()
    reset_type_skill_store()


def _stub_first_run(monkeypatch) -> None:
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


@pytest.mark.asyncio
async def test_resolve_shipped_seed_accepts_catalog_leaf():
    path = resolve_shipped_seed("clinical-trials")
    assert path.name == "clinical-trials"
    assert resolve_shipped_seed("infona/clinical-trials") == path


def test_public_install_seed_one_shot_reuses_empty_path_tenant(
    client, auth_headers, monkeypatch
):
    """Just-minted empty workspace is the new workspace — no second mint."""
    _stub_first_run(monkeypatch)
    resp = client.post(
        f"/graphs/{TENANT}/blueprints/install",
        json={
            "seed": "infona/clinical-trials",
            "target": TARGET_NEW_WORKSPACE,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "installed"
    assert body["tenant_id"] == TENANT
    assert body["kg"] == "clinical-trials"
    assert body["target"] == TARGET_NEW_WORKSPACE
    assert body["first_run"]["status"] == "answered"
    assert body["first_run"]["sample_is_current"] is False
    assert "NCT09990001" in body["first_run"]["answer"]


@pytest.mark.asyncio
async def test_public_install_does_not_contaminate_tenant_with_unrelated_kg(
    client, auth_headers, monkeypatch
):
    """INF-605: default new-workspace path must not write into a dirty tenant."""
    _stub_first_run(monkeypatch)
    await upsert_registered_kg(TENANT, UNRELATED_KG, description="pre-existing")
    assert await tenant_holds_graph(TENANT) is True

    resp = client.post(
        f"/graphs/{TENANT}/blueprints/install",
        json={
            "seed": "clinical-trials",
            "include_sample": True,
            "target": "new_workspace",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    dest = body["tenant_id"]
    assert dest != TENANT
    assert dest.startswith("untitled-workspace")
    assert body["kg"] == "clinical-trials"
    assert body["first_run"]["status"] == "answered"

    dirty_kgs = {row["name"] for row in await list_registered_kgs(TENANT)}
    assert UNRELATED_KG in dirty_kgs
    assert "clinical-trials" not in dirty_kgs
    assert await list_installed_blueprints(TENANT) == []

    dest_kgs = {row["name"] for row in await list_registered_kgs(dest)}
    assert "clinical-trials" in dest_kgs
    card = await inspect_blueprint(dest, "infona/clinical-trials")
    assert card["kg"] == "clinical-trials"
    assert card["sample_is_current"] is False


def test_existing_target_still_writes_the_path_tenant_when_dirty(
    client, auth_headers
):
    """CLI / explicit existing-workspace install is unchanged."""
    resp = client.post(
        f"/graphs/{TENANT}/blueprints/install",
        json={"seed": "infona/clinical-trials", "kg": "clinical-trials"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == TENANT
    assert "first_run" not in body
    assert body.get("target") == "existing"


def test_public_fork_uses_new_workspace_and_keeps_lineage(
    client, auth_headers
):
    """Public Fork is fork-with-lineage, not install. No first-run."""
    # Dirty the path tenant so target=new_workspace must mint.
    client.post(
        f"/graphs/{TENANT}/blueprints/install",
        json={"seed": "infona/clinical-trials", "include_sample": False},
        headers=auth_headers,
    )
    resp = client.post(
        f"/graphs/{TENANT}/blueprints/infona/clinical-trials/fork",
        json={"target": "new_workspace"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "forked"
    assert body["tenant_id"] != TENANT
    assert body["parent_id"] == "infona/clinical-trials"
    assert body["lineage"]["parent"]["id"] == "infona/clinical-trials"
    assert "first_run" not in body
    listed = client.get(f"/graphs/{TENANT}/blueprints", headers=auth_headers)
    ids = [row["blueprint_id"] for row in listed.json()["blueprints"]]
    assert "infona/clinical-trials" in ids
    # The fork identity is not installed on the dirty tenant.
    assert body["blueprint_id"] not in ids


def test_public_install_first_run_fail_closed(client, auth_headers):
    """Missing BYOK on the public path is an error, not a skipped first-run."""
    import json
    from pathlib import Path

    body = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "infona_client/blueprint/data/clinical_trials_min.json"
        ).read_text(encoding="utf-8")
    )
    body["sources"].append(
        {
            "id": "private-tracker",
            "title": "Private trial tracker",
            "kind": "licensed_api",
            "publisher": "Example",
            "description": "Keyed source.",
            "license": "Apache-2.0",
            "url": "https://clinicaltrials.gov/api/v2/studies",
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
    ctgov = next(row for row in body["acquisition"] if row["source"] == "ctgov")
    body["acquisition"] = [{**ctgov, "source": "private-tracker"}]
    resp = client.post(
        f"/graphs/{TENANT}/blueprints/install",
        json={
            "kg": "clinical-trials",
            "include_sample": False,
            "manifest": body,
            "target": "new_workspace",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["fail_closed"] is True
    assert detail["missing"][0]["key_env"] == "INFONA_TEST_TRACKER_KEY"


def test_public_install_without_directory_refuses_dirty_tenant(
    client, auth_headers
):
    register_tenant_provider(None)
    # Seed a pin so the path tenant is not empty.
    seeded = client.post(
        f"/graphs/{TENANT}/blueprints/install",
        json={"seed": "infona/clinical-trials", "include_sample": False},
        headers=auth_headers,
    )
    assert seeded.status_code == 200, seeded.text
    resp = client.post(
        f"/graphs/{TENANT}/blueprints/install",
        json={
            "seed": "infona/clinical-trials",
            "target": "new_workspace",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 409, resp.text
    assert "new workspace" in resp.text.lower()

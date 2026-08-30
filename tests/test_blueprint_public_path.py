"""INF-605 — public install target is a new workspace; install does not acquire."""

from __future__ import annotations

import pytest

from infona_client.api_registry.executor import RegistryApiSource
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
from infona_client.enrichment.job_store import InMemoryJobStore
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


def _forbid_acquisition(monkeypatch) -> dict[str, int]:
    """Install must not call first-run, hit CT.gov, or create an enrich job."""
    hits = {"first_run": 0, "registry_execute": 0, "enrich_jobs": 0}

    async def boom_first_run(*args, **kwargs):
        hits["first_run"] += 1
        raise AssertionError("install must not call run_first_run")

    async def boom_execute(self, spec, bindings=None, **kwargs):
        hits["registry_execute"] += 1
        raise AssertionError("install must not hit ClinicalTrials.gov")

    async def boom_job(self, job):
        hits["enrich_jobs"] += 1
        raise AssertionError("install must not create an enrichment job")

    monkeypatch.setattr(
        "infona_client.blueprint.first_run.run_first_run", boom_first_run
    )
    monkeypatch.setattr(
        "infona_client.blueprint.public_path.run_first_run",
        boom_first_run,
        raising=False,
    )
    monkeypatch.setattr(RegistryApiSource, "execute", boom_execute)
    monkeypatch.setattr(InMemoryJobStore, "create", boom_job)
    return hits


@pytest.mark.asyncio
async def test_resolve_shipped_seed_accepts_catalog_leaf():
    path = resolve_shipped_seed("clinical-trials")
    assert path.name == "clinical-trials"
    assert resolve_shipped_seed("infona/clinical-trials") == path


def test_public_install_seed_one_shot_reuses_empty_path_tenant(
    client, auth_headers, monkeypatch
):
    """Just-minted empty workspace is the new workspace — no second mint."""
    hits = _forbid_acquisition(monkeypatch)
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
    assert body["sample_included"] is False
    assert body["sample_is_current"] is False
    assert body["types"]
    assert "first_run" not in body
    assert hits == {"first_run": 0, "registry_execute": 0, "enrich_jobs": 0}
    jobs = client.get(f"/graphs/{TENANT}/jobs", headers=auth_headers)
    assert jobs.status_code == 200
    assert jobs.json() == []


@pytest.mark.asyncio
async def test_public_install_does_not_contaminate_tenant_with_unrelated_kg(
    client, auth_headers, monkeypatch
):
    """INF-605: default new-workspace path must not write into a dirty tenant."""
    hits = _forbid_acquisition(monkeypatch)
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
    assert "first_run" not in body
    assert hits == {"first_run": 0, "registry_execute": 0, "enrich_jobs": 0}

    dirty_kgs = {row["name"] for row in await list_registered_kgs(TENANT)}
    assert UNRELATED_KG in dirty_kgs
    assert "clinical-trials" not in dirty_kgs
    assert await list_installed_blueprints(TENANT) == []

    dest_kgs = {row["name"] for row in await list_registered_kgs(dest)}
    assert "clinical-trials" in dest_kgs
    card = await inspect_blueprint(dest, "infona/clinical-trials")
    assert card["kg"] == "clinical-trials"
    assert card["sample_is_current"] is False


def test_public_install_ignores_first_run_body_and_does_not_hit_ctgov(
    client, auth_headers, monkeypatch
):
    """A leftover first_run=true on POST /install must not acquire."""
    hits = _forbid_acquisition(monkeypatch)
    resp = client.post(
        f"/graphs/{TENANT}/blueprints/install",
        json={
            "seed": "infona/clinical-trials",
            "target": "new_workspace",
            "first_run": True,
            "credentials": {"INFONA_UNUSED": "nope"},
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "installed"
    assert "first_run" not in body
    assert hits["first_run"] == 0
    assert hits["registry_execute"] == 0
    assert hits["enrich_jobs"] == 0


@pytest.mark.asyncio
async def test_existing_target_still_writes_the_path_tenant_when_dirty(
    client, auth_headers, monkeypatch
):
    """CLI / explicit existing-workspace install writes the path tenant."""
    hits = _forbid_acquisition(monkeypatch)
    await upsert_registered_kg(TENANT, UNRELATED_KG, description="pre-existing")
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
    assert hits["first_run"] == 0
    assert hits["registry_execute"] == 0


def test_public_fork_uses_new_workspace_and_keeps_lineage(
    client, auth_headers, monkeypatch
):
    """Public Fork is fork-with-lineage, not install. No first-run."""
    hits = _forbid_acquisition(monkeypatch)
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
    assert hits["first_run"] == 0
    listed = client.get(f"/graphs/{TENANT}/blueprints", headers=auth_headers)
    ids = [row["blueprint_id"] for row in listed.json()["blueprints"]]
    assert "infona/clinical-trials" in ids
    # The fork identity is not installed on the dirty tenant.
    assert body["blueprint_id"] not in ids


def test_public_install_retries_same_workspace_on_same_pin(
    client, auth_headers, monkeypatch
):
    """Re-install of the same seed stays on the leftover pin; no second mint."""
    hits = _forbid_acquisition(monkeypatch)
    first = client.post(
        f"/graphs/{TENANT}/blueprints/install",
        json={"seed": "infona/clinical-trials", "target": "new_workspace"},
        headers=auth_headers,
    )
    assert first.status_code == 200, first.text
    listed = client.get(f"/graphs/{TENANT}/blueprints", headers=auth_headers)
    assert listed.json()["blueprints"][0]["blueprint_id"] == "infona/clinical-trials"

    retry = client.post(
        f"/graphs/{TENANT}/blueprints/install",
        json={"seed": "infona/clinical-trials", "target": "new_workspace"},
        headers=auth_headers,
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["tenant_id"] == TENANT
    assert retry.json()["status"] == "already_installed"
    assert "first_run" not in retry.json()
    assert hits["first_run"] == 0
    assert hits["registry_execute"] == 0


@pytest.mark.asyncio
async def test_public_install_without_directory_refuses_dirty_tenant(
    client, auth_headers
):
    register_tenant_provider(None)
    await upsert_registered_kg(TENANT, UNRELATED_KG, description="pre-existing")
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

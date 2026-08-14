"""User-scoped API sources — store isolation, catalog merge, /v1/me/api-sources.

A user registers a source once; it is visible in every workspace they can
access. tenant_custom still shadows a same-slug user_custom for that one
workspace. Static/anonymous keys cannot register user sources (401).
"""

from __future__ import annotations

import pytest

from infona_client.api_registry import (
    LAYER_TENANT_CUSTOM,
    LAYER_USER_CUSTOM,
    InMemoryUserApiSourceStore,
    UserApiSource,
    get_api_source_catalog,
    load_user_custom_catalog,
    make_user_api_source_store,
    reset_api_source_catalog,
    reset_tenant_api_source_store,
    reset_user_api_source_store,
    set_tenant_custom_specs,
    set_user_custom_specs,
)
from infona_client.api_registry.catalog import _LAYER_RANK
from infona_client.api_registry.spec import ApiSourceSpec
from infona_client.auth.api_keys import AuthVerdict, register_external_verifier

_ME = "/v1/me/api-sources"
_TENANT_LIST = "/graphs/test-tenant/api-sources"


@pytest.fixture(autouse=True)
def _clean():
    reset_api_source_catalog()
    reset_user_api_source_store()
    reset_tenant_api_source_store()
    register_external_verifier(None)
    yield
    reset_api_source_catalog()
    reset_user_api_source_store()
    reset_tenant_api_source_store()
    register_external_verifier(None)


def _spec(slug: str, *, title: str = "", enabled: bool = True) -> ApiSourceSpec:
    return ApiSourceSpec.from_dict(
        {
            "slug": slug,
            "title": title or slug,
            "base_url": "https://api.example.com",
            "enabled": enabled,
            "endpoints": [
                {
                    "name": "default",
                    "method": "GET",
                    "path": "/search",
                    "params": [{"name": "q", "location": "query"}],
                    "result_path": "results",
                    "field_mappings": {"name": "name"},
                }
            ],
        }
    )


def _record(subject: str, slug: str, *, enabled: bool = True, title: str = "") -> UserApiSource:
    return UserApiSource(
        owner_subject=subject, slug=slug, spec=_spec(slug, title=title), enabled=enabled
    )


def _spec_body(slug: str = "my_user_api") -> dict:
    return {
        "slug": slug,
        "title": "My User API",
        "base_url": "https://api.acme.example",
        "auth": {"mode": "none"},
        "endpoints": [{
            "name": "default", "method": "GET", "path": "/search",
            "params": [{"name": "q", "location": "query"}],
            "result_path": "results",
            "field_mappings": {"name": "name"},
        }],
    }


@pytest.fixture
def user_keys():
    """Two signed-in users, both granted test-tenant. Cleared after the test."""

    def verifier(api_key: str):
        if api_key == "key-user-a":
            return AuthVerdict(tenants=["test-tenant"], subject="user_a")
        if api_key == "key-user-b":
            return AuthVerdict(tenants=["test-tenant"], subject="user_b")
        return None

    register_external_verifier(verifier)
    yield {
        "a": {"X-API-Key": "key-user-a"},
        "b": {"X-API-Key": "key-user-b"},
    }
    register_external_verifier(None)


# --------------------------------------------------------------------------- #
# Layer rank
# --------------------------------------------------------------------------- #
def test_user_custom_rank_sits_between_global_and_tenant():
    assert _LAYER_RANK["user_custom"] == 15
    assert (
        _LAYER_RANK["tenant_custom"]
        > _LAYER_RANK["user_custom"]
        > _LAYER_RANK["global_enhanced"]
        > _LAYER_RANK["global_public"]
    )


# --------------------------------------------------------------------------- #
# Store upsert / list / get / delete isolated by subject
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_store_upsert_list_get_delete_isolated_by_subject():
    store = InMemoryUserApiSourceStore()
    await store.upsert(_record("user_a", "alpha", title="Alpha"))
    await store.upsert(_record("user_a", "beta", title="Beta"))
    await store.upsert(_record("user_b", "gamma", title="Gamma"))

    a_rows = await store.list_for_subject("user_a")
    b_rows = await store.list_for_subject("user_b")
    assert {r.slug for r in a_rows} == {"alpha", "beta"}
    assert {r.slug for r in b_rows} == {"gamma"}

    got = await store.get("user_a", "alpha")
    assert got is not None
    assert got.spec.title == "Alpha"
    assert got.owner_subject == "user_a"
    assert got.created_at is not None and got.updated_at is not None

    assert await store.delete("user_a", "alpha") is True
    assert await store.get("user_a", "alpha") is None
    assert await store.delete("user_a", "alpha") is False
    # leftover rows are untouched
    assert await store.get("user_a", "beta") is not None
    assert await store.get("user_b", "gamma") is not None


@pytest.mark.asyncio
async def test_subject_a_cannot_read_subject_b():
    store = InMemoryUserApiSourceStore()
    await store.upsert(_record("user_a", "a_only"))
    await store.upsert(_record("user_b", "b_only"))

    assert await store.get("user_a", "b_only") is None
    assert await store.get("user_b", "a_only") is None
    # Deleting another subject's slug is a no-op, not a cross-user delete.
    assert await store.delete("user_a", "b_only") is False
    assert await store.get("user_b", "b_only") is not None


@pytest.mark.asyncio
async def test_load_user_custom_catalog_materializes_layer():
    store = InMemoryUserApiSourceStore()
    await store.upsert(_record("user_a", "acme_internal", title="Acme"))
    cat = await load_user_custom_catalog("user_a", store)
    entry = cat.get("acme_internal")
    assert entry is not None
    assert entry.layer == LAYER_USER_CUSTOM
    assert entry.title == "Acme"
    # Isolation: not in the global / operator view, nor another subject's merge.
    assert get_api_source_catalog().get("acme_internal") is None
    assert get_api_source_catalog(subject="user_b").get("acme_internal") is None


# --------------------------------------------------------------------------- #
# Catalog merge: user source appears for that subject; tenant shadows same slug
# --------------------------------------------------------------------------- #
def test_catalog_merge_user_source_appears_for_that_subject():
    set_user_custom_specs("user_a", [_spec("user_pms", title="User PMS")])
    cat = get_api_source_catalog(subject="user_a")
    assert cat.get("user_pms") is not None
    assert cat.get("user_pms").layer == LAYER_USER_CUSTOM
    assert cat.get("user_pms").title == "User PMS"
    # Other subject / global view do not see it.
    assert get_api_source_catalog(subject="user_b").get("user_pms") is None
    assert get_api_source_catalog().get("user_pms") is None


def test_tenant_custom_shadows_same_slug_user_custom():
    set_user_custom_specs("user_a", [_spec("shared", title="User Shared")])
    set_tenant_custom_specs("ws-1", [_spec("shared", title="Tenant Shared")])

    both = get_api_source_catalog("ws-1", subject="user_a")
    assert both.get("shared").title == "Tenant Shared"
    assert both.get("shared").layer == LAYER_TENANT_CUSTOM

    # Other workspace: user source is still visible (nothing to shadow it).
    other_ws = get_api_source_catalog("ws-2", subject="user_a")
    assert other_ws.get("shared").title == "User Shared"
    assert other_ws.get("shared").layer == LAYER_USER_CUSTOM

    # Tenant-only view (no subject) still has the tenant overlay.
    tenant_only = get_api_source_catalog("ws-1")
    assert tenant_only.get("shared").layer == LAYER_TENANT_CUSTOM


def test_merged_catalog_does_not_leak_into_global_singleton():
    before = set(get_api_source_catalog().slugs())
    set_user_custom_specs("user_a", [_spec("user_pms")])
    _ = get_api_source_catalog(subject="user_a")
    after = set(get_api_source_catalog().slugs())
    assert before == after


# --------------------------------------------------------------------------- #
# HTTP: POST 401 without subject; POST + GET list happy path
# --------------------------------------------------------------------------- #
def test_post_me_api_sources_401_without_key(client):
    resp = client.post(_ME, json={"spec": _spec_body()})
    assert resp.status_code == 401


def test_static_key_can_register_user_source(client):
    """A static API key has no Clerk subject — fingerprint it so local / legacy
    keys can still register user-scoped sources (same key → same bucket)."""
    headers = {"X-API-Key": "test-key"}
    resp = client.post(_ME, json={"spec": _spec_body("static_src")}, headers=headers)
    assert resp.status_code == 201, resp.text
    listed = client.get(_ME, headers=headers).json()
    assert any(s["slug"] == "static_src" for s in listed)


def test_post_and_get_list_happy_path(client, user_keys):
    resp = client.post(_ME, json={"spec": _spec_body("my_user_api")}, headers=user_keys["a"])
    assert resp.status_code == 201, resp.text
    summary = resp.json()
    assert summary["slug"] == "my_user_api"
    assert summary["layer"] == "user_custom"
    assert summary["editable"] is True

    listed = client.get(_ME, headers=user_keys["a"]).json()
    by_slug = {s["slug"]: s for s in listed}
    assert "my_user_api" in by_slug
    assert by_slug["my_user_api"]["layer"] == "user_custom"
    assert by_slug["my_user_api"]["editable"] is True
    # No global seed leaks onto the user-scoped list.
    assert "nppes" not in by_slug
    assert all(s["layer"] == "user_custom" for s in listed)

    got = client.get(f"{_ME}/my_user_api", headers=user_keys["a"])
    assert got.status_code == 200
    assert got.json()["slug"] == "my_user_api"
    assert got.json()["editable"] is True

    # Subject B cannot read A's source via the user-scoped routes.
    assert client.get(_ME, headers=user_keys["b"]).json() == []
    assert client.get(f"{_ME}/my_user_api", headers=user_keys["b"]).status_code == 404


# --------------------------------------------------------------------------- #
# Workspace catalog / tenant list includes the caller's user source
# --------------------------------------------------------------------------- #
def test_workspace_list_includes_caller_user_source(client, user_keys):
    created = client.post(_ME, json={"spec": _spec_body("user_pms")}, headers=user_keys["a"])
    assert created.status_code == 201, created.text

    listed = client.get(_TENANT_LIST, headers=user_keys["a"]).json()
    by_slug = {s["slug"]: s for s in listed}
    assert "user_pms" in by_slug
    assert by_slug["user_pms"]["layer"] == "user_custom"
    assert by_slug["user_pms"]["editable"] is True

    # Same workspace, different user: A's source is not visible.
    listed_b = client.get(_TENANT_LIST, headers=user_keys["b"]).json()
    assert "user_pms" not in {s["slug"] for s in listed_b}

    got = client.get(f"{_TENANT_LIST}/user_pms", headers=user_keys["a"])
    assert got.status_code == 200
    assert got.json()["layer"] == "user_custom"
    assert got.json()["editable"] is True
    assert client.get(f"{_TENANT_LIST}/user_pms", headers=user_keys["b"]).status_code == 404


@pytest.mark.asyncio
async def test_workspace_catalog_includes_user_source():
    from infona_client.api.routes.ontology import _workspace_catalog

    store = make_user_api_source_store()
    await store.upsert(_record("user_a", "user_pms", title="User PMS"))

    cat = await _workspace_catalog("test-tenant", "user_a")
    assert cat is not None
    entry = cat.get("user_pms")
    assert entry is not None
    assert entry.layer == LAYER_USER_CUSTOM

    # No subject → operator/global-style workspace read: no user layer.
    anon = await _workspace_catalog("test-tenant", None)
    assert anon is not None
    assert anon.get("user_pms") is None
    # Global operator ontology must never include user_custom.
    assert get_api_source_catalog().get("user_pms") is None

"""ONTA-2xx Child 3 — canonical CRUD + validate + test-call routes.

Covers authz (cross-tenant 403, global-edit 403), the create→get→list→patch→
delete lifecycle, validate happy/error, the collection-level test-call happy/
error, and — critically — that a secret VALUE is never returned by get/list or
echoed by the test call (only ``has_secret``).

Uses the app TestClient from conftest (key ``test-key`` → tenant ``test-tenant``).
"""

from __future__ import annotations

import httpx
import pytest

from cograph_client.api_registry import (
    RegistryApiSource,
    reset_api_source_catalog,
    reset_secret_cipher,
    reset_tenant_api_source_store,
    reset_tenant_secret_store,
)

_HDR = {"X-API-Key": "test-key"}
_BASE = "/graphs/test-tenant/api-sources"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # Fresh stores + a configured cipher so secret storage is enabled.
    monkeypatch.setenv("OMNIX_SECRETS_KEY", "cGFzc3BocmFzZS1mb3ItdGVzdHM=")
    reset_api_source_catalog()
    reset_tenant_api_source_store()
    reset_tenant_secret_store()
    reset_secret_cipher()
    yield
    reset_api_source_catalog()
    reset_tenant_api_source_store()
    reset_tenant_secret_store()
    reset_secret_cipher()


def _spec_body(slug="acme_internal", *, secret_ref=None):
    auth = {"mode": "none"}
    if secret_ref:
        auth = {"mode": "api_key_header", "secret_ref": secret_ref, "header_name": "X-Acme-Key"}
    return {
        "slug": slug,
        "title": "Acme Internal API",
        "base_url": "https://api.acme.example",
        "auth": auth,
        "endpoints": [{
            "name": "default", "method": "GET", "path": "/search",
            "params": [{"name": "q", "location": "query"}],
            "result_path": "results",
            "field_mappings": {"name": "name"},
        }],
    }


# --------------------------------------------------------------------------- #
# Authz
# --------------------------------------------------------------------------- #
@pytest.fixture
def multi_tenant_key():
    """Register a user-scoped (multi-tenant) verifier: the key `multi-key` grants
    ONLY `test-tenant`. A request for any other tenant path is a 403 (the key is
    valid; the tenant grant is not) — the mechanism `get_tenant` enforces and the
    api-sources routes rely on. Cleared after the test.

    (A legacy static single-tenant key like `test-key` routes to ITS tenant
    regardless of the path — documented behavior — so it can never reach another
    tenant's data but doesn't 403; the 403 path is the user-scoped-key path.)"""
    from cograph_client.auth.api_keys import register_external_verifier

    def verifier(api_key: str):
        if api_key == "multi-key":
            return ["test-tenant"]  # a sequence => user-scoped key
        return None

    register_external_verifier(verifier)
    yield {"X-API-Key": "multi-key"}
    register_external_verifier(None)


def test_cross_tenant_list_is_403(client, multi_tenant_key):
    # multi-key grants test-tenant only; another tenant path => 403.
    resp = client.get("/graphs/other-tenant/api-sources", headers=multi_tenant_key)
    assert resp.status_code == 403


def test_cross_tenant_create_is_403(client, multi_tenant_key):
    resp = client.post(
        "/graphs/other-tenant/api-sources",
        json={"spec": _spec_body()}, headers=multi_tenant_key,
    )
    assert resp.status_code == 403


def test_owned_tenant_allowed_for_multi_tenant_key(client, multi_tenant_key):
    # The same key CAN list its granted tenant.
    resp = client.get(_BASE, headers=multi_tenant_key)
    assert resp.status_code == 200


def test_no_auth_is_rejected(client):
    resp = client.get(_BASE)
    assert resp.status_code in (401, 403)


# --------------------------------------------------------------------------- #
# Lifecycle: create → get → list → patch → delete
# --------------------------------------------------------------------------- #
def test_create_lists_and_reads_back(client):
    resp = client.post(_BASE, json={"spec": _spec_body()}, headers=_HDR)
    assert resp.status_code == 201, resp.text
    summary = resp.json()
    assert summary["slug"] == "acme_internal"
    assert summary["layer"] == "tenant_custom"
    assert summary["editable"] is True
    assert summary["has_secret"] is False

    # List includes the tenant's own custom entry. A regular (non-operator)
    # caller — the static `test-key` is non-operator — sees ONLY its own
    # tenant_custom sources; the global seed catalog is hidden (ONTA-234).
    listed = client.get(_BASE, headers=_HDR).json()
    by_slug = {s["slug"]: s for s in listed}
    assert "acme_internal" in by_slug
    assert by_slug["acme_internal"]["editable"] is True
    # No global seed entry (e.g. nppes) leaks to a regular caller.
    assert "nppes" not in by_slug
    assert all(s["layer"] == "tenant_custom" for s in listed)

    # Read one.
    got = client.get(f"{_BASE}/acme_internal", headers=_HDR)
    assert got.status_code == 200
    assert got.json()["slug"] == "acme_internal"
    assert got.json()["editable"] is True


def test_duplicate_create_is_409(client):
    client.post(_BASE, json={"spec": _spec_body()}, headers=_HDR)
    resp = client.post(_BASE, json={"spec": _spec_body()}, headers=_HDR)
    assert resp.status_code == 409


def test_patch_updates_and_toggles_enabled(client):
    client.post(_BASE, json={"spec": _spec_body()}, headers=_HDR)
    # Disable via PATCH {enabled:false}.
    resp = client.patch(f"{_BASE}/acme_internal", json={"enabled": False}, headers=_HDR)
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    # Reflected in get.
    assert client.get(f"{_BASE}/acme_internal", headers=_HDR).json()["enabled"] is False


def test_patch_missing_is_404(client):
    resp = client.patch(f"{_BASE}/nonexistent", json={"enabled": False}, headers=_HDR)
    assert resp.status_code == 404


def test_delete_removes(client):
    client.post(_BASE, json={"spec": _spec_body()}, headers=_HDR)
    assert client.delete(f"{_BASE}/acme_internal", headers=_HDR).status_code == 200
    assert client.get(f"{_BASE}/acme_internal", headers=_HDR).status_code == 404
    # Deleting again is 404.
    assert client.delete(f"{_BASE}/acme_internal", headers=_HDR).status_code == 404


# --------------------------------------------------------------------------- #
# Global entries are read-only (403 on edit/delete)
# --------------------------------------------------------------------------- #
def test_patch_global_slug_is_403(client):
    # nppes is a global_public seed entry.
    resp = client.patch(f"{_BASE}/nppes", json={"enabled": False}, headers=_HDR)
    assert resp.status_code == 403


def test_delete_global_slug_is_403(client):
    resp = client.delete(f"{_BASE}/nppes", headers=_HDR)
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Operator gate (ONTA-234): global visibility is operator-only, server-side.
#
# The static `test-key` has no identity → non-operator. `operator-key` falls
# through the static map to an external verifier returning an AuthVerdict with
# is_operator=True (that is exactly how premium Clerk populates the seam — the
# DETERMINATION stays premium; here we assert the OSS route honors the bit).
# --------------------------------------------------------------------------- #
@pytest.fixture
def operator_key():
    from cograph_client.auth.api_keys import AuthVerdict, register_external_verifier

    def verifier(api_key: str):
        if api_key == "operator-key":
            return AuthVerdict(tenants=["test-tenant"], subject="op_1", is_operator=True)
        if api_key == "regular-key":  # verified identity, but NOT an operator
            return AuthVerdict(tenants=["test-tenant"], subject="usr_1", is_operator=False)
        return None

    register_external_verifier(verifier)
    yield {"operator": {"X-API-Key": "operator-key"}, "regular": {"X-API-Key": "regular-key"}}
    register_external_verifier(None)


def test_non_operator_list_hides_global(client):
    """A regular caller's list is tenant_custom-only — zero global entries."""
    listed = client.get(_BASE, headers=_HDR).json()
    assert all(s["layer"] == "tenant_custom" for s in listed)
    assert not any(s["slug"] == "nppes" for s in listed)


def test_operator_list_includes_global(client, operator_key):
    """An operator additionally sees the global catalog, read-only."""
    listed = client.get(_BASE, headers=operator_key["operator"]).json()
    by_slug = {s["slug"]: s for s in listed}
    assert "nppes" in by_slug  # a global_public seed entry is visible
    assert by_slug["nppes"]["layer"] == "global_public"
    assert by_slug["nppes"]["editable"] is False


def test_verified_non_operator_still_hides_global(client, operator_key):
    """A VERIFIED (Clerk) identity that is not an operator is treated like any
    regular user — global stays hidden. Operator status, not merely being
    verified, is the gate."""
    listed = client.get(_BASE, headers=operator_key["regular"]).json()
    assert not any(s["slug"] == "nppes" for s in listed)
    assert all(s["layer"] == "tenant_custom" for s in listed)


def test_non_operator_get_global_slug_is_404(client):
    """GET of a global slug by a non-operator is 404 — never leaks existence."""
    resp = client.get(f"{_BASE}/nppes", headers=_HDR)
    assert resp.status_code == 404


def test_operator_get_global_slug_is_200(client, operator_key):
    """An operator can read a global source's spec (the authoring aid)."""
    resp = client.get(f"{_BASE}/nppes", headers=operator_key["operator"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["slug"] == "nppes"
    assert body["editable"] is False


def test_non_operator_test_global_slug_is_404(client):
    """Smoke-testing a global slug by a non-operator is 404, matching GET —
    the test route never confirms a global slug exists to a regular caller."""
    resp = client.post(f"{_BASE}/test", json={"slug": "nppes"}, headers=_HDR)
    assert resp.status_code == 404


def test_operator_cannot_mutate_global_slug(client, operator_key):
    """Even an operator cannot WRITE a global slug through the tenant route —
    globals stay file-based + PR-reviewed. Visibility ≠ write access."""
    assert (
        client.patch(f"{_BASE}/nppes", json={"enabled": False}, headers=operator_key["operator"]).status_code
        == 403
    )
    assert (
        client.delete(f"{_BASE}/nppes", headers=operator_key["operator"]).status_code == 403
    )


# --------------------------------------------------------------------------- #
# Validate
# --------------------------------------------------------------------------- #
def test_validate_happy(client):
    resp = client.post(f"{_BASE}/validate", json={"spec": _spec_body()}, headers=_HDR)
    assert resp.status_code == 200
    assert resp.json() == {"valid": True, "errors": []}


def test_validate_reports_errors(client):
    bad = _spec_body()
    bad["base_url"] = "http://insecure.example"  # not https → lint error
    resp = client.post(f"{_BASE}/validate", json={"spec": bad}, headers=_HDR)
    body = resp.json()
    assert body["valid"] is False
    assert body["errors"]
    assert any("https" in e["message"] for e in body["errors"])


def test_create_invalid_spec_is_422(client):
    bad = _spec_body()
    bad["endpoints"] = []  # no endpoints → invalid
    resp = client.post(_BASE, json={"spec": bad}, headers=_HDR)
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Test-call (collection-level; slug OR inline spec)
# --------------------------------------------------------------------------- #
def test_test_call_with_inline_spec(client, monkeypatch):
    canned_rows = [{"name": "Acme Row"}]

    async def fake_execute(self, spec, bindings=None, **kw):
        from cograph_client.api_registry.executor import ApiCallResult
        return ApiCallResult(slug=spec.slug, rows=canned_rows, source=f"api:{spec.slug}")

    monkeypatch.setattr(RegistryApiSource, "execute", fake_execute)
    resp = client.post(
        f"{_BASE}/test",
        json={"spec": _spec_body(), "sample_params": {"q": "test"}}, headers=_HDR,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["rows"] == canned_rows


def test_test_call_requires_slug_or_spec(client):
    resp = client.post(f"{_BASE}/test", json={"sample_params": {}}, headers=_HDR)
    assert resp.status_code == 422


def test_test_call_invalid_spec_returns_error(client):
    bad = _spec_body()
    bad["base_url"] = "http://insecure.example"
    resp = client.post(f"{_BASE}/test", json={"spec": bad}, headers=_HDR)
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert resp.json()["error"]


def test_test_call_unknown_slug_is_404(client):
    resp = client.post(f"{_BASE}/test", json={"slug": "does_not_exist"}, headers=_HDR)
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Secrets: stored, flagged, NEVER returned
# --------------------------------------------------------------------------- #
_SECRET = "sk-tenant-secret-abc123"


def test_create_with_secret_flags_has_secret_but_never_returns_it(client):
    body = {"spec": _spec_body(secret_ref="api_key"), "secrets": {"api_key": _SECRET}}
    resp = client.post(_BASE, json=body, headers=_HDR)
    assert resp.status_code == 201
    summary = resp.json()
    assert summary["has_secret"] is True
    # The secret value must appear NOWHERE in the create response.
    assert _SECRET not in resp.text

    # get: has_secret true, no value.
    got = client.get(f"{_BASE}/acme_internal", headers=_HDR)
    assert got.json()["has_secret"] is True
    assert _SECRET not in got.text

    # list: no value.
    listed = client.get(_BASE, headers=_HDR)
    assert _SECRET not in listed.text
    acme = next(s for s in listed.json() if s["slug"] == "acme_internal")
    assert acme["has_secret"] is True


def test_test_call_never_echoes_the_secret(client, monkeypatch):
    # Store a source + secret.
    client.post(
        _BASE,
        json={"spec": _spec_body(secret_ref="api_key"), "secrets": {"api_key": _SECRET}},
        headers=_HDR,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        # The secret reaches the upstream header (correct) but must not come back.
        return httpx.Response(200, json={"results": [{"name": "row1"}]})

    # Patch the executor's transport so the smoke call hits our mock.
    orig_init = RegistryApiSource.__init__

    def patched_init(self, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        orig_init(self, **kw)

    monkeypatch.setattr(RegistryApiSource, "__init__", patched_init)

    resp = client.post(f"{_BASE}/test", json={"slug": "acme_internal", "sample_params": {"q": "x"}}, headers=_HDR)
    assert resp.status_code == 200
    assert _SECRET not in resp.text


def test_create_with_secret_but_no_cipher_is_503(client, monkeypatch):
    monkeypatch.delenv("OMNIX_SECRETS_KEY", raising=False)
    reset_secret_cipher()
    body = {"spec": _spec_body(secret_ref="api_key"), "secrets": {"api_key": _SECRET}}
    resp = client.post(_BASE, json=body, headers=_HDR)
    assert resp.status_code == 503

"""The /v1/me/tenants routes and the tenant-provider plugin protocol.

Exercises the OSS surface only: a fake in-memory provider stands in for the
premium Clerk integration, the same way the auth tests use a fake verifier.
"""

import pytest
from fastapi.testclient import TestClient

from infona_client.api.app import create_app
from infona_client.auth.tenant_directory import (
    TENANT_ID_RE,
    Tenant,
    TenantProvider,
    TenantProviderError,
    ensure_label_available,
    label_key,
    mint_untitled_tenant_id,
    next_untitled_label,
    register_tenant_provider,
    validate_new_tenant,
)


class FakeProvider:
    """In-memory tenant directory keyed by api_key → list[Tenant]."""

    def __init__(self):
        self.store: dict[str, list[Tenant]] = {"good-key": []}

    def _user(self, api_key: str) -> list[Tenant]:
        if api_key not in self.store:
            raise TenantProviderError(401, "Invalid API key")
        return self.store[api_key]

    def list_tenants(self, api_key):
        return list(self._user(api_key))

    def add_tenant(self, api_key, tenant_id, label):
        owned = self._user(api_key)
        if any(t.id == tenant_id for t in owned):
            raise TenantProviderError(409, f'Tenant "{tenant_id}" already exists.')
        t = Tenant(id=tenant_id, label=label)
        owned.append(t)
        return t

    def remove_tenant(self, api_key, tenant_id):
        owned = self._user(api_key)
        if not any(t.id == tenant_id for t in owned):
            raise TenantProviderError(404, f'Tenant "{tenant_id}" not found.')
        self.store[api_key] = [t for t in owned if t.id != tenant_id]

    def rename_tenant(self, api_key, tenant_id, label):
        owned = self._user(api_key)
        if not any(t.id == tenant_id for t in owned):
            raise TenantProviderError(404, f'Tenant "{tenant_id}" not found.')
        renamed = Tenant(id=tenant_id, label=label)
        self.store[api_key] = [renamed if t.id == tenant_id else t for t in owned]
        return renamed


class LegacyProvider(FakeProvider):
    """A provider written before renaming existed — no ``rename_tenant``.

    The attribute is genuinely ABSENT here (see ``NullRenameProvider`` and
    ``NominalProvider`` for the other two "doesn't implement it" shapes).
    """

    def __getattribute__(self, name):
        if name == "rename_tenant":
            raise AttributeError(name)
        return object.__getattribute__(self, name)


class NullRenameProvider(FakeProvider):
    """Explicit opt-out sentinel."""

    rename_tenant = None


class NominalProvider(TenantProvider):
    """Subclasses the Protocol NOMINALLY and implements only the original three
    methods, so ``rename_tenant`` resolves to the Protocol's ``...`` stub — which
    is callable and returns None. Must be 501, not a 500 from ``_out(None)``.

    (It delegates rather than inheriting FakeProvider, because inheriting one
    that HAS rename_tenant would put the real method ahead of the stub in the
    MRO and defeat the point of this fixture.)
    """

    def __init__(self):
        self._inner = FakeProvider()

    def list_tenants(self, api_key):
        return self._inner.list_tenants(api_key)

    def add_tenant(self, api_key, tenant_id, label):
        return self._inner.add_tenant(api_key, tenant_id, label)

    def remove_tenant(self, api_key, tenant_id):
        return self._inner.remove_tenant(api_key, tenant_id)


@pytest.fixture
def app():
    # Open access (no static keys) so get_tenant isn't in play; these routes
    # authenticate via the provider, not the path-tenant dependency.
    import os

    os.environ["OMNIX_API_KEYS"] = "{}"
    return create_app()


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_provider():
    yield
    register_tenant_provider(None)


# --- routes with a provider registered ---------------------------------------


def test_add_list_remove_roundtrip(client):
    register_tenant_provider(FakeProvider())
    h = {"X-API-Key": "good-key"}

    assert client.get("/v1/me/tenants", headers=h).json() == []

    r = client.post(
        "/v1/me/tenants", headers=h, json={"id": "acme-co", "label": "Acme"}
    )
    assert r.status_code == 201
    # Creator is owner (write capability); TenantOut always includes role + capability.
    assert r.json() == {
        "id": "acme-co",
        "label": "Acme",
        "role": "owner",
        "capability": "write",
    }

    # List attaches role via resolve_member_role; static keys have no subject →
    # default writer (still write capability). POST hardcodes owner for the
    # create response because the caller just claimed the workspace.
    assert client.get("/v1/me/tenants", headers=h).json() == [
        {
            "id": "acme-co",
            "label": "Acme",
            "role": "writer",
            "capability": "write",
        }
    ]

    r = client.delete("/v1/me/tenants/acme-co", headers=h)
    assert r.status_code == 200
    assert r.json() == {"removed": "acme-co"}
    assert client.get("/v1/me/tenants", headers=h).json() == []


def test_duplicate_add_is_409(client):
    register_tenant_provider(FakeProvider())
    h = {"X-API-Key": "good-key"}
    client.post("/v1/me/tenants", headers=h, json={"id": "acme-co", "label": "A"})
    r = client.post("/v1/me/tenants", headers=h, json={"id": "acme-co", "label": "A"})
    assert r.status_code == 409


def test_remove_unknown_is_404(client):
    register_tenant_provider(FakeProvider())
    r = client.delete("/v1/me/tenants/nope", headers={"X-API-Key": "good-key"})
    assert r.status_code == 404


def test_invalid_key_is_401(client):
    register_tenant_provider(FakeProvider())
    r = client.get("/v1/me/tenants", headers={"X-API-Key": "bogus"})
    assert r.status_code == 401


def test_missing_key_is_401(client):
    register_tenant_provider(FakeProvider())
    assert client.get("/v1/me/tenants").status_code == 401


@pytest.mark.parametrize(
    "tid,label",
    [
        ("UPPER", "x"),  # uppercase
        ("ab", "x"),  # too short
        ("a" * 41, "x"),  # too long
        ("demo-tenant", "x"),  # reserved
        ("acme-co", ""),  # empty label
        ("acme-co", "y" * 65),  # label too long
    ],
)
def test_invalid_input_is_400_before_provider(client, tid, label):
    register_tenant_provider(FakeProvider())
    r = client.post(
        "/v1/me/tenants",
        headers={"X-API-Key": "good-key"},
        json={"id": tid, "label": label},
    )
    assert r.status_code == 400


def test_present_but_empty_id_is_still_400(client):
    """Omitting a field means "pick one for me"; sending "" is a caller bug."""
    register_tenant_provider(FakeProvider())
    r = client.post(
        "/v1/me/tenants",
        headers={"X-API-Key": "good-key"},
        json={"id": "", "label": "A"},
    )
    assert r.status_code == 400


# --- one-click create (auto-named) -------------------------------------------


def test_empty_body_mints_untitled_workspace(client):
    register_tenant_provider(FakeProvider())
    h = {"X-API-Key": "good-key"}

    first = client.post("/v1/me/tenants", headers=h, json={})
    assert first.status_code == 201
    assert first.json()["label"] == "Untitled workspace 1"

    # Counts up over what the caller already has.
    second = client.post("/v1/me/tenants", headers=h, json={})
    assert second.json()["label"] == "Untitled workspace 2"

    # Ids are minted, distinct, and NOT derived from the label — a derived
    # "untitled-workspace-1" would be contended in the global registry.
    ids = {first.json()["id"], second.json()["id"]}
    assert len(ids) == 2
    assert all(i.startswith("untitled-workspace-") for i in ids)


def test_no_body_at_all_mints_untitled_workspace(client):
    """The Explorer posts `{}`, but a bare POST must work the same way."""
    register_tenant_provider(FakeProvider())
    r = client.post("/v1/me/tenants", headers={"X-API-Key": "good-key"})
    assert r.status_code == 201
    assert r.json()["label"] == "Untitled workspace 1"


def test_untitled_counter_skips_interior_gaps(client):
    """N is highest-plus-one, so renaming one in the middle doesn't make the
    next create land on a number the user just walked past."""
    register_tenant_provider(FakeProvider())
    h = {"X-API-Key": "good-key"}
    client.post("/v1/me/tenants", headers=h, json={})
    second = client.post("/v1/me/tenants", headers=h, json={})
    client.post("/v1/me/tenants", headers=h, json={})  # → Untitled workspace 3
    client.patch(
        f"/v1/me/tenants/{second.json()['id']}", headers=h, json={"label": "Sales"}
    )
    assert client.post("/v1/me/tenants", headers=h, json={}).json()["label"] == (
        "Untitled workspace 4"
    )


def test_untitled_counter_reuses_the_highest_after_a_delete(client):
    """Deleting the last one frees its number — same as "New Folder" anywhere."""
    register_tenant_provider(FakeProvider())
    h = {"X-API-Key": "good-key"}
    client.post("/v1/me/tenants", headers=h, json={})
    second = client.post("/v1/me/tenants", headers=h, json={})
    client.delete(f"/v1/me/tenants/{second.json()['id']}", headers=h)
    assert client.post("/v1/me/tenants", headers=h, json={}).json()["label"] == (
        "Untitled workspace 2"
    )


def test_explicit_id_without_label_gets_auto_label(client):
    register_tenant_provider(FakeProvider())
    r = client.post(
        "/v1/me/tenants", headers={"X-API-Key": "good-key"}, json={"id": "acme-co"}
    )
    assert r.status_code == 201
    assert r.json() == {
        "id": "acme-co",
        "label": "Untitled workspace 1",
        "role": "owner",
        "capability": "write",
    }


def test_minted_id_collision_is_re_minted_never_joined(client, monkeypatch):
    """A minted id that is ALREADY REGISTERED can only be a collision, so it
    must be thrown away and re-drawn.

    The alternative — falling through to _claim_or_check_ownership, which
    allow-and-logs a foreign id while ownership enforcement is off — would put
    a STRANGER'S tenant into the caller's profile and grant them its KG.
    """
    from infona_client.api.routes import tenants as tenants_routes
    from infona_client.auth.workspace_store import make_workspace_store

    register_tenant_provider(FakeProvider())
    store = make_workspace_store()
    # Someone else already owns the first id we will draw.
    victim_id = "untitled-workspace-aaaaaa"
    import asyncio

    asyncio.run(store.claim_workspace(victim_id, "someone-else", "Their workspace"))

    drawn = iter([victim_id, "untitled-workspace-bbbbbb"])
    monkeypatch.setattr(tenants_routes, "mint_untitled_tenant_id", lambda: next(drawn))
    # A subject is required for the registry to engage at all.
    monkeypatch.setattr(tenants_routes, "resolve_subject", lambda key: "me")

    r = client.post("/v1/me/tenants", headers={"X-API-Key": "good-key"}, json={})
    assert r.status_code == 201
    assert r.json()["id"] == "untitled-workspace-bbbbbb"  # NOT the victim's


def test_minted_id_gives_up_rather_than_joining_a_stranger(client, monkeypatch):
    """If every draw collides, fail loudly — never fall back to joining one."""
    from infona_client.api.routes import tenants as tenants_routes
    from infona_client.auth.workspace_store import make_workspace_store

    register_tenant_provider(FakeProvider())
    store = make_workspace_store()
    taken = "untitled-workspace-cccccc"
    import asyncio

    asyncio.run(store.claim_workspace(taken, "someone-else", "Theirs"))
    monkeypatch.setattr(tenants_routes, "mint_untitled_tenant_id", lambda: taken)
    monkeypatch.setattr(tenants_routes, "resolve_subject", lambda key: "me")

    r = client.post("/v1/me/tenants", headers={"X-API-Key": "good-key"}, json={})
    assert r.status_code == 500
    # And nothing was added to the caller's profile.
    assert client.get("/v1/me/tenants", headers={"X-API-Key": "good-key"}).json() == []


def test_auto_label_stays_within_max_len(client):
    """A hand-typed 64-char "Untitled workspace <45 digits>" must not make the
    one-click button 400 with an over-long successor."""
    register_tenant_provider(FakeProvider())
    h = {"X-API-Key": "good-key"}
    pathological = "Untitled workspace " + "9" * 45
    assert len(pathological) == 64
    client.post(
        "/v1/me/tenants", headers=h, json={"id": "acme-co", "label": pathological}
    )

    r = client.post("/v1/me/tenants", headers=h, json={})
    assert r.status_code == 201
    assert len(r.json()["label"]) <= 64
    assert r.json()["label"] == "Untitled workspace 1"


# --- per-user name uniqueness ------------------------------------------------


def test_duplicate_label_on_create_is_409(client):
    register_tenant_provider(FakeProvider())
    h = {"X-API-Key": "good-key"}
    client.post("/v1/me/tenants", headers=h, json={"id": "acme-co", "label": "Acme"})
    # Different id, same name — and the comparison ignores case + extra spaces.
    r = client.post(
        "/v1/me/tenants", headers=h, json={"id": "acme-two", "label": " acme "}
    )
    assert r.status_code == 409
    assert "already have a workspace named" in r.json()["detail"]


def test_same_label_for_a_different_user_is_fine(client):
    """Uniqueness is per-user: labels live on each user's own profile."""
    provider = FakeProvider()
    provider.store["other-key"] = []
    register_tenant_provider(provider)
    client.post(
        "/v1/me/tenants",
        headers={"X-API-Key": "good-key"},
        json={"id": "acme-co", "label": "Acme"},
    )
    r = client.post(
        "/v1/me/tenants",
        headers={"X-API-Key": "other-key"},
        json={"id": "acme-two", "label": "Acme"},
    )
    assert r.status_code == 201


# --- rename ------------------------------------------------------------------


def test_rename_changes_label_only(client):
    register_tenant_provider(FakeProvider())
    h = {"X-API-Key": "good-key"}
    client.post("/v1/me/tenants", headers=h, json={"id": "acme-co", "label": "Acme"})

    r = client.patch(
        "/v1/me/tenants/acme-co", headers=h, json={"label": "  Acme Inc  "}
    )
    assert r.status_code == 200
    assert r.json()["id"] == "acme-co"
    assert r.json()["label"] == "Acme Inc"
    assert client.get("/v1/me/tenants", headers=h).json()[0]["label"] == "Acme Inc"


def test_rename_to_a_name_i_already_use_is_409(client):
    register_tenant_provider(FakeProvider())
    h = {"X-API-Key": "good-key"}
    client.post("/v1/me/tenants", headers=h, json={"id": "acme-co", "label": "Acme"})
    client.post("/v1/me/tenants", headers=h, json={"id": "beta-co", "label": "Beta"})
    r = client.patch("/v1/me/tenants/beta-co", headers=h, json={"label": "acme"})
    assert r.status_code == 409
    # Rejected write leaves the label untouched.
    labels = {
        t["id"]: t["label"] for t in client.get("/v1/me/tenants", headers=h).json()
    }
    assert labels["beta-co"] == "Beta"


def test_rename_to_its_own_name_is_allowed(client):
    """Re-submitting the same name (or just its casing) must not self-collide."""
    register_tenant_provider(FakeProvider())
    h = {"X-API-Key": "good-key"}
    client.post("/v1/me/tenants", headers=h, json={"id": "acme-co", "label": "Acme"})
    r = client.patch("/v1/me/tenants/acme-co", headers=h, json={"label": "ACME"})
    assert r.status_code == 200
    assert r.json()["label"] == "ACME"


def test_rename_unknown_tenant_is_404(client):
    register_tenant_provider(FakeProvider())
    r = client.patch(
        "/v1/me/tenants/nope", headers={"X-API-Key": "good-key"}, json={"label": "X"}
    )
    assert r.status_code == 404


def test_rename_empty_label_is_400(client):
    register_tenant_provider(FakeProvider())
    h = {"X-API-Key": "good-key"}
    client.post("/v1/me/tenants", headers=h, json={"id": "acme-co", "label": "Acme"})
    assert (
        client.patch(
            "/v1/me/tenants/acme-co", headers=h, json={"label": "   "}
        ).status_code
        == 400
    )


@pytest.mark.parametrize("cls", [LegacyProvider, NullRenameProvider, NominalProvider])
def test_rename_on_a_provider_without_it_is_501(client, cls):
    """All three "doesn't implement rename" shapes report 501, never 500:
    attribute absent, explicit None, and the inherited Protocol stub."""
    register_tenant_provider(cls())
    h = {"X-API-Key": "good-key"}
    client.post("/v1/me/tenants", headers=h, json={"id": "acme-co", "label": "Acme"})
    r = client.patch("/v1/me/tenants/acme-co", headers=h, json={"label": "X"})
    assert r.status_code == 501


def test_rename_without_a_key_is_401_not_501(client):
    """Auth comes first, so an unauthenticated caller can't probe whether this
    deployment has a rename provider."""
    register_tenant_provider(NullRenameProvider())
    assert (
        client.patch("/v1/me/tenants/acme-co", json={"label": "X"}).status_code == 401
    )


# --- no provider registered (OSS-only deployment) ----------------------------


def test_no_provider_is_501(client):
    r = client.get("/v1/me/tenants", headers={"X-API-Key": "good-key"})
    assert r.status_code == 501


# --- shared validation helper ------------------------------------------------


def test_validate_new_tenant_trims_and_returns():
    assert validate_new_tenant("  acme-co ", "  Acme  ") == ("acme-co", "Acme")


def test_validate_new_tenant_rejects_reserved():
    with pytest.raises(TenantProviderError) as exc:
        validate_new_tenant("spider-bench", "x")
    assert exc.value.status_code == 400


@pytest.mark.parametrize(
    "a,b",
    [("Acme", "acme"), ("My  Space", "my space"), (" Acme ", "Acme")],
)
def test_label_key_collapses_case_and_whitespace(a, b):
    assert label_key(a) == label_key(b)


def test_ensure_label_available_ignores_the_excluded_tenant():
    owned = [Tenant("a", "Acme"), Tenant("b", "Beta")]
    ensure_label_available(owned, "Acme", exclude_id="a")  # renaming itself
    with pytest.raises(TenantProviderError) as exc:
        ensure_label_available(owned, "Acme", exclude_id="b")
    assert exc.value.status_code == 409


def test_next_untitled_label_ignores_unrelated_names():
    owned = [Tenant("a", "Untitled workspace 7"), Tenant("b", "Workspace 99")]
    assert next_untitled_label(owned) == "Untitled workspace 8"


def test_minted_ids_satisfy_the_slug_rule():
    for _ in range(50):
        assert TENANT_ID_RE.match(mint_untitled_tenant_id())

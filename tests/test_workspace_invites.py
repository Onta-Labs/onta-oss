"""Workspace registry, membership, and invite routes (ONTA-227).

Exercises the OSS surface only: fake in-memory providers stand in for the
premium identity integration (verifier → subject, tenant grants, invite
delivery), the same way the tenant-directory tests use a fake provider. The
in-memory workspace store backs everything; the Postgres store shares the
exact semantics by construction (same protocol, constraint-guard behavior
mirrored) and is exercised against a real database by the premium deployment.
"""

import asyncio
import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from cograph_client.auth.api_keys import AuthVerdict, register_external_verifier
from cograph_client.auth.tenant_directory import (
    Tenant,
    TenantProviderError,
    register_tenant_provider,
)
from cograph_client.auth.workspace_store import (
    DuplicatePendingInviteError,
    InMemoryWorkspaceStore,
    WorkspaceInvite,
    effective_status,
    make_workspace_store,
    register_invite_delivery_provider,
    register_tenant_grant_provider,
    reset_workspace_store,
)
from cograph_client.config import settings

_run = asyncio.run

# --- fakes --------------------------------------------------------------------

_SUBJECTS = {"key-a": "user_a", "key-b": "user_b", "key-c": "user_c"}

HA = {"X-API-Key": "key-a"}
HB = {"X-API-Key": "key-b"}
HC = {"X-API-Key": "key-c"}


def _fake_verifier(key):
    if key in _SUBJECTS:
        return AuthVerdict(tenants=[], subject=_SUBJECTS[key])
    if key == "key-legacy":
        return ["legacy-tenant"]  # legacy verdict shape: valid key, no subject
    return None


class FakeTenantProvider:
    """In-memory tenant directory keyed by api_key → list[Tenant]."""

    def __init__(self):
        self.store = {"key-a": [], "key-b": [], "key-c": [], "test-key": []}

    def _user(self, api_key):
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
        self.store[api_key] = [t for t in self._user(api_key) if t.id != tenant_id]


class FakeGrantProvider:
    def __init__(self, fail=False):
        self.grants = []
        self.revokes = []
        self.fail = fail

    def grant(self, subject, tenant_id, label):
        if self.fail:
            raise RuntimeError("identity provider down")
        self.grants.append((subject, tenant_id, label))

    def revoke(self, subject, tenant_id):
        if self.fail:
            raise RuntimeError("identity provider down")
        self.revokes.append((subject, tenant_id))


class FakeDeliveryProvider:
    def __init__(self, subjects_by_email=None, emails_by_subject=None):
        self.subjects_by_email = subjects_by_email or {}
        self.emails_by_subject = emails_by_subject or {}
        self.sent = []
        self.revoked = []

    def lookup_subject_by_email(self, email):
        return self.subjects_by_email.get(email)

    def emails_for_subject(self, subject):
        return list(self.emails_by_subject.get(subject, []))

    def display_profile(self, subject):
        emails = self.emails_by_subject.get(subject)
        if not emails:
            return None
        return {"email": emails[0], "name": subject.replace("_", " ").title()}

    def send_signup_invitation(self, email, redirect_url, metadata):
        self.sent.append((email, redirect_url, metadata))
        return f"sinv_{len(self.sent)}"

    def revoke_signup_invitation(self, invitation_id):
        self.revoked.append(invitation_id)
        return True


# --- fixtures -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    reset_workspace_store()
    monkeypatch.delenv("COGRAPH_WORKSPACE_ENFORCE_OWNERSHIP", raising=False)
    yield
    register_external_verifier(None)
    register_tenant_provider(None)
    register_tenant_grant_provider(None)
    register_invite_delivery_provider(None)
    reset_workspace_store()


@pytest.fixture
def verifier():
    register_external_verifier(_fake_verifier)
    return _fake_verifier


@pytest.fixture
def grants(verifier):
    provider = FakeGrantProvider()
    register_tenant_grant_provider(provider)
    return provider


def _seed_workspace(tenant="acme-co", owner="user_a", label="Acme"):
    return _run(make_workspace_store().claim_workspace(tenant, owner, label))


def _seed_invite(
    tenant="acme-co",
    email="bob@example.com",
    token="tok",
    invited_by="user_a",
    expires_delta_days=30,
):
    store = make_workspace_store()
    invite = WorkspaceInvite(
        id=str(uuid.uuid4()),
        tenant_id=tenant,
        email=email,
        role="member",
        status="pending",
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        invited_by=invited_by,
        expires_at=datetime.now(timezone.utc) + timedelta(days=expires_delta_days),
    )
    return _run(store.create_invite(invite))


def _create_invite(client, email="bob@example.com", tenant="acme-co", headers=HA):
    return client.post(
        f"/v1/me/tenants/{tenant}/invites", headers=headers, json={"email": email}
    )


# --- store semantics ----------------------------------------------------------


def test_claim_workspace_first_wins():
    store = InMemoryWorkspaceStore()
    first = _run(store.claim_workspace("acme-co", "user_a", "Acme"))
    assert first is not None and first.owner_subject == "user_a"
    # Second claimant loses: returned-row check, owner unchanged.
    assert _run(store.claim_workspace("acme-co", "user_b", "Evil")) is None
    ws = _run(store.get_workspace("acme-co"))
    assert ws.owner_subject == "user_a" and ws.label == "Acme"
    # The role='owner' membership projection was written by the claim.
    member = _run(store.get_member("acme-co", "user_a"))
    assert member is not None and member.role == "owner"


def test_mark_accepted_is_single_use():
    store = InMemoryWorkspaceStore()
    inv = WorkspaceInvite(
        id=str(uuid.uuid4()),
        tenant_id="acme-co",
        email="bob@example.com",
        role="member",
        status="pending",
        token_hash="h",
        invited_by="user_a",
    )
    _run(store.create_invite(inv))
    assert _run(store.mark_accepted(inv.id, "user_b")) is True
    assert _run(store.mark_accepted(inv.id, "user_c")) is False
    assert _run(store.get_invite(inv.id)).accepted_by == "user_b"


def test_duplicate_pending_raises_with_existing_id():
    store = InMemoryWorkspaceStore()
    first = WorkspaceInvite(
        id=str(uuid.uuid4()),
        tenant_id="acme-co",
        email="bob@example.com",
        role="member",
        status="pending",
        token_hash="h1",
        invited_by="user_a",
    )
    _run(store.create_invite(first))
    dup = WorkspaceInvite(
        id=str(uuid.uuid4()),
        tenant_id="acme-co",
        email="bob@example.com",
        role="member",
        status="pending",
        token_hash="h2",
        invited_by="user_a",
    )
    with pytest.raises(DuplicatePendingInviteError) as exc:
        _run(store.create_invite(dup))
    assert exc.value.invite_id == first.id


def test_effective_status_computes_expiry():
    inv = WorkspaceInvite(
        id=str(uuid.uuid4()),
        tenant_id="acme-co",
        email="bob@example.com",
        role="member",
        status="pending",
        token_hash="h",
        invited_by="user_a",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    assert effective_status(inv) == "expired"
    inv.status = "accepted"  # settled statuses are never re-mapped
    assert effective_status(inv) == "accepted"


# --- subject resolution -------------------------------------------------------


def test_static_key_gets_403(client, verifier):
    # conftest's OMNIX_API_KEYS maps "test-key" — valid but subject-less.
    r = client.get("/v1/me/tenants/acme-co/invites", headers={"X-API-Key": "test-key"})
    assert r.status_code == 403
    assert "user-scoped" in r.json()["detail"]


def test_subjectless_verdict_gets_403(client, verifier):
    r = client.get(
        "/v1/me/tenants/acme-co/invites", headers={"X-API-Key": "key-legacy"}
    )
    assert r.status_code == 403


def test_no_verifier_at_all_gets_403(client):
    r = client.get("/v1/me/tenants/acme-co/invites", headers=HA)
    assert r.status_code == 403


def test_unknown_key_gets_401(client, verifier):
    r = client.get("/v1/me/tenants/acme-co/invites", headers={"X-API-Key": "bogus"})
    assert r.status_code == 401


def test_missing_key_gets_401(client, verifier):
    assert client.get("/v1/me/tenants/acme-co/invites").status_code == 401


# --- invite creation ----------------------------------------------------------


def test_create_invite_link_only_without_delivery(client, verifier):
    _seed_workspace()
    r = _create_invite(client, email="  Bob@Example.com ")
    assert r.status_code == 201
    body = r.json()
    assert body["delivery"] == "link_only"
    assert body["accept_url"] is None
    assert body["accept_token"]
    assert body["invite"]["email"] == "bob@example.com"  # lowercased + trimmed
    assert body["invite"]["status"] == "pending"
    assert body["invite"]["email_sent"] is False
    # The raw token is never stored — only its hash.
    inv = _run(make_workspace_store().get_invite(body["invite"]["id"]))
    assert inv.token_hash == hashlib.sha256(
        body["accept_token"].encode()
    ).hexdigest()


def test_create_invite_unregistered_workspace_404(client, verifier):
    assert _create_invite(client).status_code == 404


def test_create_invite_owner_only(client, verifier):
    _seed_workspace()
    _run(make_workspace_store().add_member("acme-co", "user_b", "member"))
    assert _create_invite(client, headers=HB).status_code == 403  # member
    assert _create_invite(client, headers=HC).status_code == 403  # stranger


def test_create_invite_validates_email_and_role(client, verifier):
    _seed_workspace()
    assert _create_invite(client, email="not-an-email").status_code == 400
    r = client.post(
        "/v1/me/tenants/acme-co/invites",
        headers=HA,
        json={"email": "bob@example.com", "role": "owner"},
    )
    assert r.status_code == 400


def test_duplicate_pending_invite_409_carries_existing_id(client, verifier):
    _seed_workspace()
    first = _create_invite(client).json()["invite"]["id"]
    r = _create_invite(client)
    assert r.status_code == 409
    assert r.json()["detail"]["invite_id"] == first


def test_expired_pending_slot_is_freed_on_reinvite(client, verifier):
    _seed_workspace()
    old = _seed_invite(expires_delta_days=-1)  # stored pending, past expiry
    r = _create_invite(client)
    assert r.status_code == 201  # computed expiry persisted, slot freed
    assert _run(make_workspace_store().get_invite(old.id)).status == "expired"


def test_pending_invite_cap(client, verifier):
    _seed_workspace()
    for i in range(50):
        _seed_invite(email=f"user{i}@example.com", token=f"t{i}")
    assert _create_invite(client, email="one-more@example.com").status_code == 429


def test_create_invite_email_sent_for_new_user(client, verifier, monkeypatch):
    monkeypatch.setattr(
        settings, "invite_accept_url_base", "https://app.example.test/invite"
    )
    delivery = FakeDeliveryProvider()  # knows no users → everyone is new
    register_invite_delivery_provider(delivery)
    _seed_workspace()
    r = _create_invite(client)
    assert r.status_code == 201
    body = r.json()
    assert body["delivery"] == "email_sent"
    assert body["accept_url"] == (
        "https://app.example.test/invite/" + body["accept_token"]
    )
    (email, redirect_url, metadata) = delivery.sent[0]
    assert email == "bob@example.com"
    assert redirect_url == body["accept_url"]
    assert metadata["invite_id"] == body["invite"]["id"]
    assert metadata["workspace_label"] == "Acme"
    assert body["invite"]["email_sent"] is True


def test_create_invite_in_app_for_existing_user(client, verifier):
    delivery = FakeDeliveryProvider(
        subjects_by_email={"bob@example.com": "user_b"},
        emails_by_subject={"user_b": ["bob@example.com"]},
    )
    register_invite_delivery_provider(delivery)
    _seed_workspace()
    r = _create_invite(client)
    assert r.status_code == 201
    assert r.json()["delivery"] == "in_app"
    assert delivery.sent == []  # no sign-up email for an existing account


def test_reserved_id_invites_only_after_manual_seed(client, verifier):
    # No registry row → 404: reserved ids are never self-serve created, so
    # this is what blocks invites INTO an unseeded reserved workspace.
    r = _create_invite(client, tenant="hotel-design-partner")
    assert r.status_code == 404
    # Manual seeding (the sanctioned ops path) creates the row; invites work.
    _seed_workspace(tenant="hotel-design-partner", owner="user_a", label="Hotel")
    r = _create_invite(client, tenant="hotel-design-partner")
    assert r.status_code == 201


# --- invite listing + revoke --------------------------------------------------


def test_list_invites_owner_only_and_computed_expiry(client, verifier):
    _seed_workspace()
    _seed_invite(email="fresh@example.com", token="t1")
    _seed_invite(email="stale@example.com", token="t2", expires_delta_days=-1)
    r = client.get("/v1/me/tenants/acme-co/invites", headers=HA)
    assert r.status_code == 200
    by_email = {i["email"]: i["status"] for i in r.json()}
    assert by_email == {"fresh@example.com": "pending", "stale@example.com": "expired"}
    # Non-owner (even a member) cannot see pending invites (v1: owner only).
    _run(make_workspace_store().add_member("acme-co", "user_b", "member"))
    assert (
        client.get("/v1/me/tenants/acme-co/invites", headers=HB).status_code == 403
    )


def test_revoke_invite(client, verifier, grants):
    delivery = FakeDeliveryProvider()
    register_invite_delivery_provider(delivery)
    _seed_workspace()
    inv = _seed_invite(token="tok-revoke")
    _run(make_workspace_store().set_signup_invitation_id(inv.id, "sinv_9"))
    r = client.delete(f"/v1/me/tenants/acme-co/invites/{inv.id}", headers=HA)
    assert r.status_code == 200
    assert _run(make_workspace_store().get_invite(inv.id)).status == "revoked"
    # Best-effort revoke of the provider's sign-up invitation rode along.
    assert delivery.revoked == ["sinv_9"]
    # Idempotent re-revoke.
    assert (
        client.delete(f"/v1/me/tenants/acme-co/invites/{inv.id}", headers=HA)
        .status_code
        == 200
    )
    # A revoked invite's token no longer accepts.
    r = client.post("/v1/invites/accept", headers=HB, json={"token": "tok-revoke"})
    assert r.status_code == 410


def test_revoke_invite_owner_only_and_404s(client, verifier):
    _seed_workspace()
    inv = _seed_invite()
    assert (
        client.delete(f"/v1/me/tenants/acme-co/invites/{inv.id}", headers=HB)
        .status_code
        == 403
    )
    assert (
        client.delete(
            f"/v1/me/tenants/acme-co/invites/{uuid.uuid4()}", headers=HA
        ).status_code
        == 404
    )
    # An invite id from ANOTHER workspace is a 404 here, not a cross-tenant revoke.
    _seed_workspace(tenant="other-co", owner="user_a", label="Other")
    other = _seed_invite(tenant="other-co", email="x@example.com", token="t9")
    assert (
        client.delete(f"/v1/me/tenants/acme-co/invites/{other.id}", headers=HA)
        .status_code
        == 404
    )


# --- accept (token) -----------------------------------------------------------


def test_token_accept_grants_then_membership_then_status(client, verifier, grants):
    _seed_workspace()
    token = _create_invite(client).json()["accept_token"]
    r = client.post("/v1/invites/accept", headers=HB, json={"token": token})
    assert r.status_code == 200
    assert r.json() == {
        "tenant_id": "acme-co",
        "label": "Acme",
        "role": "member",
        "status": "accepted",
    }
    assert grants.grants == [("user_b", "acme-co", "Acme")]
    store = make_workspace_store()
    member = _run(store.get_member("acme-co", "user_b"))
    assert member is not None and member.role == "member"
    # Idempotent re-accept by the same subject: 200 no-op, no second grant.
    r2 = client.post("/v1/invites/accept", headers=HB, json={"token": token})
    assert r2.status_code == 200
    assert len(grants.grants) == 1
    # Single-use across subjects: a different user is refused.
    r3 = client.post("/v1/invites/accept", headers=HC, json={"token": token})
    assert r3.status_code == 409


def test_token_accept_invalid_and_expired(client, verifier, grants):
    _seed_workspace()
    assert (
        client.post("/v1/invites/accept", headers=HB, json={"token": "nope"})
        .status_code
        == 404
    )
    _seed_invite(token="tok-old", expires_delta_days=-1)
    r = client.post("/v1/invites/accept", headers=HB, json={"token": "tok-old"})
    assert r.status_code == 410
    assert grants.grants == []  # expired invite never touched the grant path


def test_token_accept_email_match_when_delivery_registered(client, verifier, grants):
    register_invite_delivery_provider(
        FakeDeliveryProvider(
            emails_by_subject={
                "user_b": ["other@example.com"],
                "user_c": ["bob@example.com"],
            }
        )
    )
    _seed_workspace()
    _seed_invite(token="tok-match")
    # Wrong verified email → 403 naming the invited address.
    r = client.post("/v1/invites/accept", headers=HB, json={"token": "tok-match"})
    assert r.status_code == 403
    assert "bob@example.com" in r.json()["detail"]
    # Matching verified email → accepted.
    r = client.post("/v1/invites/accept", headers=HC, json={"token": "tok-match"})
    assert r.status_code == 200


def test_accept_without_grant_provider_501(client, verifier):
    _seed_workspace()
    _seed_invite(token="tok-nogrant")
    r = client.post("/v1/invites/accept", headers=HB, json={"token": "tok-nogrant"})
    assert r.status_code == 501


def test_accept_grant_failure_is_retryable_5xx(client, verifier):
    register_external_verifier(_fake_verifier)
    register_tenant_grant_provider(FakeGrantProvider(fail=True))
    _seed_workspace()
    _seed_invite(token="tok-fail")
    r = client.post("/v1/invites/accept", headers=HB, json={"token": "tok-fail"})
    assert r.status_code == 502
    # Invite stays pending — the retry is the repair path.
    ok = FakeGrantProvider()
    register_tenant_grant_provider(ok)
    r = client.post("/v1/invites/accept", headers=HB, json={"token": "tok-fail"})
    assert r.status_code == 200
    assert ok.grants == [("user_b", "acme-co", "Acme")]


# --- in-app invites (delivery provider required) --------------------------------


def test_my_invites_and_in_app_accept(client, verifier, grants):
    register_invite_delivery_provider(
        FakeDeliveryProvider(emails_by_subject={"user_b": ["bob@example.com"]})
    )
    _seed_workspace()
    inv = _seed_invite()
    r = client.get("/v1/me/invites", headers=HB)
    assert r.status_code == 200
    assert [i["id"] for i in r.json()] == [inv.id]
    assert r.json()[0]["workspace_label"] == "Acme"
    # A user whose emails don't match sees nothing.
    assert client.get("/v1/me/invites", headers=HC).json() == []
    r = client.post(f"/v1/me/invites/{inv.id}/accept", headers=HB)
    assert r.status_code == 200
    assert grants.grants == [("user_b", "acme-co", "Acme")]
    # Accepted → gone from the pending list.
    assert client.get("/v1/me/invites", headers=HB).json() == []


def test_in_app_accept_requires_email_match(client, verifier, grants):
    register_invite_delivery_provider(
        FakeDeliveryProvider(emails_by_subject={"user_c": ["carol@example.com"]})
    )
    _seed_workspace()
    inv = _seed_invite()  # addressed to bob@example.com
    r = client.post(f"/v1/me/invites/{inv.id}/accept", headers=HC)
    assert r.status_code == 403


def test_decline_requires_same_email_authorization(client, verifier):
    register_invite_delivery_provider(
        FakeDeliveryProvider(
            emails_by_subject={
                "user_b": ["bob@example.com"],
                "user_c": ["carol@example.com"],
            }
        )
    )
    _seed_workspace()
    inv = _seed_invite()
    # A stranger with the invite id cannot decline someone else's invite.
    assert (
        client.post(f"/v1/me/invites/{inv.id}/decline", headers=HC).status_code == 403
    )
    r = client.post(f"/v1/me/invites/{inv.id}/decline", headers=HB)
    assert r.status_code == 200
    assert _run(make_workspace_store().get_invite(inv.id)).status == "declined"
    # Idempotent re-decline; owner's pending list reflects the decline.
    assert (
        client.post(f"/v1/me/invites/{inv.id}/decline", headers=HB).status_code == 200
    )
    assert client.get("/v1/me/tenants/acme-co/invites", headers=HA).json() == []


def test_in_app_routes_501_without_delivery_provider(client, verifier, grants):
    _seed_workspace()
    inv = _seed_invite()
    assert client.get("/v1/me/invites", headers=HB).status_code == 501
    assert (
        client.post(f"/v1/me/invites/{inv.id}/accept", headers=HB).status_code == 501
    )
    assert (
        client.post(f"/v1/me/invites/{inv.id}/decline", headers=HB).status_code == 501
    )


# --- members ------------------------------------------------------------------


def test_list_members_any_member_with_decoration(client, verifier):
    register_invite_delivery_provider(
        FakeDeliveryProvider(emails_by_subject={"user_a": ["alice@example.com"]})
    )
    _seed_workspace()
    store = make_workspace_store()
    _run(store.add_member("acme-co", "user_b", "member"))
    r = client.get("/v1/me/tenants/acme-co/members", headers=HB)  # member can list
    assert r.status_code == 200
    by_subject = {m["subject"]: m for m in r.json()}
    assert by_subject["user_a"]["role"] == "owner"
    assert by_subject["user_a"]["email"] == "alice@example.com"
    assert by_subject["user_b"]["role"] == "member"
    # Non-members cannot.
    assert (
        client.get("/v1/me/tenants/acme-co/members", headers=HC).status_code == 403
    )


def test_remove_member_owner_only_tolerates_missing_row(client, verifier, grants):
    _seed_workspace()
    store = make_workspace_store()
    _run(store.add_member("acme-co", "user_b", "member"))
    # Non-owner cannot remove.
    r = client.delete("/v1/me/tenants/acme-co/members/user_a", headers=HB)
    assert r.status_code == 403
    # Owner cannot remove self.
    r = client.delete("/v1/me/tenants/acme-co/members/user_a", headers=HA)
    assert r.status_code == 400
    # Normal removal: grant revoked first, row deleted.
    r = client.delete("/v1/me/tenants/acme-co/members/user_b", headers=HA)
    assert r.status_code == 200
    assert grants.revokes == [("user_b", "acme-co")]
    assert _run(store.get_member("acme-co", "user_b")) is None
    # Accept-limbo repair: NO membership row, removal still revokes + 200s.
    r = client.delete("/v1/me/tenants/acme-co/members/user_c", headers=HA)
    assert r.status_code == 200
    assert grants.revokes[-1] == ("user_c", "acme-co")


def test_remove_member_without_grant_provider_501(client, verifier):
    _seed_workspace()
    _run(make_workspace_store().add_member("acme-co", "user_b", "member"))
    r = client.delete("/v1/me/tenants/acme-co/members/user_b", headers=HA)
    assert r.status_code == 501
    # Fails closed: the membership row was NOT deleted without the grant revoke.
    assert _run(make_workspace_store().get_member("acme-co", "user_b")) is not None


# --- create-route ownership check (the closed self-add hole) --------------------


def _enforce(monkeypatch):
    """Enable enforcement: env flag + a durable-marked store (the in-memory
    singleton is monkeypatched durable so the gate can be exercised without
    Postgres — production durability comes from database_url selection)."""
    monkeypatch.setenv("COGRAPH_WORKSPACE_ENFORCE_OWNERSHIP", "1")
    store = make_workspace_store()
    store.durable = True
    return store


def test_create_claims_ownership_and_403s_second_claimant(
    client, verifier, monkeypatch
):
    register_tenant_provider(FakeTenantProvider())
    _enforce(monkeypatch)
    r = client.post(
        "/v1/me/tenants", headers=HA, json={"id": "acme-co", "label": "Acme"}
    )
    assert r.status_code == 201
    ws = _run(make_workspace_store().get_workspace("acme-co"))
    assert ws.owner_subject == "user_a"
    # THE regression: another user self-adding the same workspace id.
    r = client.post(
        "/v1/me/tenants", headers=HB, json={"id": "acme-co", "label": "Acme"}
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "workspace id is taken"
    # Ownership unchanged, no membership granted.
    ws = _run(make_workspace_store().get_workspace("acme-co"))
    assert ws.owner_subject == "user_a"


def test_create_allows_owner_and_member_readd(client, verifier, monkeypatch):
    provider = FakeTenantProvider()
    register_tenant_provider(provider)
    _enforce(monkeypatch)
    assert (
        client.post(
            "/v1/me/tenants", headers=HA, json={"id": "acme-co", "label": "Acme"}
        ).status_code
        == 201
    )
    # Owner re-add (e.g. after removing from their own switcher).
    provider.store["key-a"] = []
    assert (
        client.post(
            "/v1/me/tenants", headers=HA, json={"id": "acme-co", "label": "Acme"}
        ).status_code
        == 201
    )
    # A member (row present) may re-add too.
    _run(make_workspace_store().add_member("acme-co", "user_b", "member"))
    assert (
        client.post(
            "/v1/me/tenants", headers=HB, json={"id": "acme-co", "label": "Acme"}
        ).status_code
        == 201
    )


def test_create_enforcement_off_allows_but_still_claims(client, verifier):
    register_tenant_provider(FakeTenantProvider())
    # No env flag: registry writes happen (rollout step 1), 403 stays off.
    assert (
        client.post(
            "/v1/me/tenants", headers=HA, json={"id": "acme-co", "label": "Acme"}
        ).status_code
        == 201
    )
    r = client.post(
        "/v1/me/tenants", headers=HB, json={"id": "acme-co", "label": "Acme"}
    )
    assert r.status_code == 201  # allowed — enforcement is off
    # But the registry row still records the FIRST claimant as owner.
    assert _run(make_workspace_store().get_workspace("acme-co")).owner_subject == (
        "user_a"
    )


def test_create_flag_without_durable_store_does_not_enforce(
    client, verifier, monkeypatch
):
    register_tenant_provider(FakeTenantProvider())
    monkeypatch.setenv("COGRAPH_WORKSPACE_ENFORCE_OWNERSHIP", "1")
    # In-memory store stays durable=False → the 403 must NOT fire (an
    # in-memory registry forgetting owners on restart would re-run
    # first-claim-wins, worse than not pretending).
    client.post("/v1/me/tenants", headers=HA, json={"id": "acme-co", "label": "A"})
    r = client.post(
        "/v1/me/tenants", headers=HB, json={"id": "acme-co", "label": "A"}
    )
    assert r.status_code == 201


def test_create_with_subjectless_key_behaves_as_today(client, verifier):
    register_tenant_provider(FakeTenantProvider())
    r = client.post(
        "/v1/me/tenants",
        headers={"X-API-Key": "test-key"},  # static key → no subject
        json={"id": "acme-co", "label": "Acme"},
    )
    assert r.status_code == 201
    # No registry row was minted for an anonymous key.
    assert _run(make_workspace_store().get_workspace("acme-co")) is None

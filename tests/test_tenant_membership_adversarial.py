"""Adversarial membership tests: only owners/members reach workspace data.

The graph-isolation suite (ONTA-402b) asks "given two authorized tenants,
does A's read ever see B's triples?". This file asks the prior question:
can a caller who is NOT an owner or member of workspace V even enter V?

Gate under test: ``get_tenant`` (path tenant ∩ key grant list). A valid
API key for workspace S must 403 on every ``/graphs/{V}/…`` route. Auth
runs before the handler, so missing query params / bodies must not skip
the 403.

Actors
------
* **owner**  — grant list contains V; registry owner of V
* **member** — grant list contains V; membership row (reader)
* **stranger** — valid key, grant list is a different workspace S
* **nobody** — valid key, empty grant list
* **anon** — no key
* **forged** — unrecognized key

Adversarial attempts (must not disclose V's data): path-tenant mismatch,
header spoof, body ``tenant`` override, encoded / traversal ids, mixed
case, query ``?tenant=`` on a path-scoped route.
"""

from __future__ import annotations

import re
from urllib.parse import quote

import pytest
from infona_client.auth.api_keys import AuthVerdict, register_external_verifier
from infona_client.auth.workspace_store import (
    make_workspace_store,
    reset_workspace_store,
)

VICTIM = "victim-ws"
STRANGER_WS = "stranger-ws"
MARKER = "UNIQUE_VICTIM_SECRET_TOKEN_7f3a"

OWNER_KEY = {"X-API-Key": "key-owner"}
MEMBER_KEY = {"X-API-Key": "key-member"}
STRANGER_KEY = {"X-API-Key": "key-stranger"}
NOBODY_KEY = {"X-API-Key": "key-nobody"}
FORGED_KEY = {"X-API-Key": "key-forged"}

_SUBJECTS = {
    "key-owner": ("user_owner", [VICTIM]),
    "key-member": ("user_member", [VICTIM]),
    "key-stranger": ("user_stranger", [STRANGER_WS]),
    "key-nobody": ("user_nobody", []),
}


def _verifier(key):
    row = _SUBJECTS.get(key)
    if row is None:
        return None
    subject, tenants = row
    return AuthVerdict(tenants=tenants, subject=subject)


@pytest.fixture
def app_client(client):
    """Reuse the suite client (Neptune mock on app.state) + membership seed."""
    register_external_verifier(_verifier)
    reset_workspace_store()
    store = make_workspace_store()
    import asyncio

    async def _seed():
        await store.claim_workspace(VICTIM, "user_owner", "Victim")
        await store.add_member(VICTIM, "user_member", "reader")
        await store.claim_workspace(STRANGER_WS, "user_stranger", "Stranger")

    asyncio.run(_seed())
    yield client
    register_external_verifier(None)
    reset_workspace_store()


def _walk_routes(routes):
    for route in routes:
        nested = getattr(getattr(route, "original_router", None), "routes", None)
        if nested:
            yield from _walk_routes(nested)
            continue
        path = getattr(route, "path", None) or getattr(route, "path_format", None)
        methods = getattr(route, "methods", None)
        if path and methods:
            yield path, methods


def _tenant_routes(app) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for path, methods in _walk_routes(app.routes):
        if "{tenant}" not in path:
            continue
        for method in sorted(methods):
            if method in {"HEAD", "OPTIONS"}:
                continue
            out.append((method, path))
    assert out, "create_app() exposed no /graphs/{tenant} routes"
    return out


def _fill(path: str, tenant: str = VICTIM) -> str:
    filled = path.replace("{tenant}", tenant)
    return re.sub(r"\{[^}]+\}", "x", filled)


def _call(client, method: str, url: str, headers: dict, **kw):
    mutating = method in {"POST", "PUT", "PATCH", "DELETE"}
    if mutating and "json" not in kw and "content" not in kw:
        kw["json"] = {}
    return client.request(method, url, headers=headers, **kw)


def test_stranger_is_403_on_every_tenant_scoped_route(app_client):
    """A valid key for S must not enter V on ANY /graphs/{tenant} route."""
    offenders: list[str] = []
    for method, path in _tenant_routes(app_client.app):
        url = _fill(path, VICTIM)
        r = _call(app_client, method, url, STRANGER_KEY)
        if r.status_code != 403:
            offenders.append(f"{method} {path} -> {r.status_code} {r.text[:160]}")
        elif MARKER.lower() in (r.text or "").lower():
            offenders.append(f"{method} {path} leaked victim marker")
    assert not offenders, "stranger entered a foreign workspace:\n" + "\n".join(
        offenders
    )


# Representative reads — a 403 here means the grant fixture is broken and
# the stranger-403 sweep would prove nothing. Not every GET: some need query
# params (422) or are entitlement-walled for other reasons.
_POSITIVE_READS = (
    f"/graphs/{VICTIM}/kgs",
    f"/graphs/{VICTIM}/ontology",
    f"/graphs/{VICTIM}/jobs",
    f"/graphs/{VICTIM}/skills",
    f"/graphs/{VICTIM}/functions",
    f"/graphs/{VICTIM}/conversations",
    f"/graphs/{VICTIM}/usage",
)


def test_owner_and_member_are_not_403_on_victim_reads(app_client):
    """Positive control: owner and member grants actually open the door."""
    for headers, who in ((OWNER_KEY, "owner"), (MEMBER_KEY, "member")):
        blocked = []
        for url in _POSITIVE_READS:
            r = app_client.get(url, headers=headers)
            if r.status_code == 403:
                blocked.append(f"GET {url} -> 403 {r.text[:120]}")
        assert not blocked, f"{who} was refused their own workspace:\n" + "\n".join(
            blocked
        )


def test_anon_and_forged_and_empty_grant_never_enter_victim(app_client):
    url = f"/graphs/{VICTIM}/kgs"
    assert app_client.get(url).status_code == 401
    assert app_client.get(url, headers=FORGED_KEY).status_code == 401
    # Empty grant list is "invalid key" at the allow-list resolver (401),
    # not a 403-with-hint that would confirm the tenant id exists.
    assert app_client.get(url, headers=NOBODY_KEY).status_code == 401


def test_stranger_own_workspace_is_not_confused_with_victim(app_client):
    r = app_client.get(f"/graphs/{STRANGER_WS}/kgs", headers=STRANGER_KEY)
    assert r.status_code != 403
    assert MARKER not in (r.text or "")
    r = app_client.get(f"/graphs/{VICTIM}/kgs", headers=STRANGER_KEY)
    assert r.status_code == 403


def test_spoofed_headers_cannot_select_victim_workspace(app_client):
    """X-Tenant / X-Tenant-Id / Host tricks must not override the path."""
    headers = {
        **STRANGER_KEY,
        "X-Tenant": VICTIM,
        "X-Tenant-Id": VICTIM,
        "X-Infona-Tenant": VICTIM,
        "X-Forwarded-Tenant": VICTIM,
        "Infona-Tenant": VICTIM,
    }
    r = app_client.get(f"/graphs/{STRANGER_WS}/kgs", headers=headers)
    assert r.status_code != 403
    r = app_client.get(f"/graphs/{VICTIM}/kgs", headers=headers)
    assert r.status_code == 403


def test_body_tenant_cannot_override_path_tenant(app_client):
    r = app_client.post(
        f"/graphs/{STRANGER_WS}/ask",
        headers=STRANGER_KEY,
        json={"question": "leak", "tenant": VICTIM, "tenant_id": VICTIM},
    )
    # Stranger is allowed on S; they must not be rewritten onto V.
    assert r.status_code != 403
    assert MARKER not in (r.text or "")
    r = app_client.post(
        f"/graphs/{VICTIM}/ask",
        headers=STRANGER_KEY,
        json={"question": "leak"},
    )
    assert r.status_code == 403


def test_query_tenant_cannot_override_path_tenant(app_client):
    r = app_client.get(
        f"/graphs/{STRANGER_WS}/kgs?tenant={VICTIM}",
        headers=STRANGER_KEY,
    )
    assert r.status_code != 403
    r = app_client.get(
        f"/graphs/{VICTIM}/kgs?tenant={STRANGER_WS}",
        headers=STRANGER_KEY,
    )
    assert r.status_code == 403


@pytest.mark.parametrize(
    "tenant_id",
    [
        f"../{VICTIM}",
        f"..%2f{VICTIM}",
        f"{VICTIM}%2f..%2f{VICTIM}",
        f"{VICTIM}/kgs",
        "victim-ws ",
        "VICTIM-WS",
        "victim‑ws",  # non-breaking hyphen
        "victim-ws\x00",
        quote(VICTIM),
        f"{VICTIM}?kg=1",
        "graphs/" + VICTIM,
        "",
        ".",
        "..",
        "*",
        "{tenant}",
        "%s" % VICTIM,
    ],
)
def test_adversarial_tenant_ids_do_not_enter_victim(app_client, tenant_id):
    """Crafted path segments must 4xx, never 200 on the victim graph.

    Mixed-case / lookalike / traversal ids are different workspaces (or
    invalid ids), not aliases of V. Only the exact grant `victim-ws`
    opens V, and the stranger does not have it.
    """
    # Starlette rejects NUL in the path before the app sees it.
    if "\x00" in tenant_id:
        try:
            r = app_client.get(f"/graphs/{tenant_id}/kgs", headers=STRANGER_KEY)
        except Exception:
            return
        assert r.status_code >= 400
        return
    r = app_client.get(f"/graphs/{tenant_id}/kgs", headers=STRANGER_KEY)
    assert r.status_code != 200
    assert MARKER not in (r.text or "")
    # The one id that IS the victim must stay 403 for the stranger.
    if tenant_id == VICTIM:
        assert r.status_code == 403


def test_member_without_grant_is_rejected(app_client):
    """Registry membership without a key grant is fail-closed.

    Dual-write: grant (Clerk metadata) is auth truth. A leftover membership
    row must not open the workspace when the key's allow-list no longer
    includes it (revoked member, stale row).
    """
    import asyncio
    from infona_client.auth.workspace_store import make_workspace_store

    asyncio.run(make_workspace_store().add_member(VICTIM, "user_stranger", "writer"))
    r = app_client.get(f"/graphs/{VICTIM}/kgs", headers=STRANGER_KEY)
    assert r.status_code == 403


def test_static_key_cannot_be_pointed_at_another_tenant(monkeypatch):
    """Static INFONA_API_KEYS entries are 1:1; path mismatch is 403."""
    from fastapi import HTTPException
    from infona_client.auth.api_keys import get_tenant

    monkeypatch.setattr(
        "infona_client.auth.api_keys.settings.api_keys",
        '{"static-key": "static-tenant"}',
    )
    with pytest.raises(HTTPException) as exc:
        get_tenant(tenant=VICTIM, api_key="static-key")
    assert exc.value.status_code == 403


def test_legacy_str_verdict_does_not_honor_path_as_grant():
    """A legacy single-tenant str verdict routes to ITS tenant, not the path.

    That is back-compat (claims.tenant keys), not an escalation onto V:
    the resolved context is the key's tenant, so a path naming V still
    operates on the key's own workspace — it must never become V.
    """
    from infona_client.auth.api_keys import get_tenant, register_external_verifier

    register_external_verifier(lambda key: STRANGER_WS)
    try:
        ctx = get_tenant(tenant=VICTIM, api_key="legacy-key")
        assert ctx.tenant_id == STRANGER_WS
        assert ctx.tenant_id != VICTIM
    finally:
        register_external_verifier(None)


def test_members_list_rejects_stranger(app_client):
    r = app_client.get(f"/v1/me/tenants/{VICTIM}/members", headers=STRANGER_KEY)
    assert r.status_code == 403
    r = app_client.get(f"/v1/me/tenants/{VICTIM}/members", headers=MEMBER_KEY)
    assert r.status_code == 200
    r = app_client.get(f"/v1/me/tenants/{VICTIM}/invites", headers=STRANGER_KEY)
    assert r.status_code == 403
    r = app_client.get(f"/v1/me/tenants/{VICTIM}/invites", headers=MEMBER_KEY)
    assert r.status_code == 403  # owner-only
    r = app_client.post(
        f"/v1/me/tenants/{VICTIM}/invites",
        headers=STRANGER_KEY,
        json={"email": "x@example.com"},
    )
    assert r.status_code == 403

"""API-key auth on an ordinary authenticated read.

**Ported by ONTA-527.** These three cases used ``GET /graphs/{tenant}/triples``
purely as "some authenticated route" — raw triple SPO is now a 410 tombstone
(``api/routes/triples.py``), so a valid key there proves only that the caller
reached a route that refuses everyone. They now run against ``GET
/graphs/{tenant}/kgs``, a live read on the production (property-graph) path, so
"a valid key is accepted" is again a 200 rather than a 410.

The last case keeps the tombstone honest from the auth side: auth resolves
BEFORE the handler, so an unauthenticated caller must still get 401 there — a
route that answered 410 to anonymous callers would be leaking the fact that the
tenant exists.
"""


def test_valid_api_key(client, auth_headers):
    response = client.get("/graphs/test-tenant/kgs", headers=auth_headers)
    assert response.status_code == 200


def test_missing_api_key(client):
    response = client.get("/graphs/test-tenant/kgs")
    assert response.status_code in (401, 403)


def test_invalid_api_key(client):
    response = client.get(
        "/graphs/test-tenant/kgs",
        headers={"X-API-Key": "bad-key"},
    )
    assert response.status_code == 401


def test_foreign_tenant_is_403_not_a_listing(client, auth_headers):
    """The key resolves a tenant; naming a different one in the path is refused."""
    response = client.get("/graphs/other-tenant/kgs", headers=auth_headers)
    assert response.status_code == 403


def test_auth_still_runs_before_the_triples_tombstone(client, auth_headers):
    """410 is reached only by an authenticated caller (ONTA-527)."""
    assert client.get("/graphs/test-tenant/triples").status_code == 401
    assert (
        client.get("/graphs/test-tenant/triples", headers=auth_headers).status_code
        == 410
    )

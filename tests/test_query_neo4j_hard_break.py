"""Public SPARQL surfaces are gone (ONTA-527).

``POST /graphs/{tenant}/query`` and ``/update`` return 410 Gone unconditionally.
They used to 410 only under ``INFONA_GRAPH_BACKEND=neo4j`` and otherwise execute
scoped SPARQL against Neptune; that backend and its execution path are deleted,
so the gate is no longer conditional and there is nothing left to fall through
to. The routes stay mounted so an old client reads "gone", not "wrong URL".
"""

import inspect

import pytest

from infona_client.auth.api_keys import TenantContext, get_tenant

TENANT = "test-tenant"
TENANT_GRAPH = f"https://graph.infona.ai/graphs/{TENANT}"
SCOPED_SELECT = f"SELECT ?s FROM <{TENANT_GRAPH}> WHERE {{ ?s ?p ?o }}"
UNSCOPED_SELECT = "SELECT ?s WHERE { ?s ?p ?o }"


@pytest.mark.parametrize("query", [SCOPED_SELECT, UNSCOPED_SELECT])
def test_query_returns_410(client, auth_headers, mock_neptune, query):
    res = client.post(
        f"/graphs/{TENANT}/query",
        headers=auth_headers,
        json={"query": query},
    )
    assert res.status_code == 410, res.text
    detail = res.json()["detail"]
    assert "/ask" in detail or "agent" in detail.lower() or "SDK" in detail
    mock_neptune.query.assert_not_called()


def test_query_returns_410_even_with_legacy_backend_env(
    client, auth_headers, mock_neptune, monkeypatch
):
    """A leftover env value cannot re-open the SPARQL surface."""
    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neptune")
    res = client.post(
        f"/graphs/{TENANT}/query",
        headers=auth_headers,
        json={"query": SCOPED_SELECT},
    )
    assert res.status_code == 410, res.text
    mock_neptune.query.assert_not_called()


def test_update_returns_410_for_operator(app, client, auth_headers, mock_neptune):
    app.dependency_overrides[get_tenant] = lambda: TenantContext(
        tenant_id=TENANT, api_key="k", is_operator=True
    )
    try:
        res = client.post(
            f"/graphs/{TENANT}/update",
            headers=auth_headers,
            json={"update": f"DROP SILENT GRAPH <{TENANT_GRAPH}/kg/x>"},
        )
        assert res.status_code == 410, res.text
        mock_neptune.update.assert_not_called()
    finally:
        app.dependency_overrides.pop(get_tenant, None)


def test_update_still_403s_a_non_operator_before_the_410(
    app, client, auth_headers, mock_neptune
):
    """The operator gate must not silently loosen while the route is a tombstone."""
    app.dependency_overrides[get_tenant] = lambda: TenantContext(
        tenant_id=TENANT, api_key="k", is_operator=False
    )
    try:
        res = client.post(
            f"/graphs/{TENANT}/update",
            headers=auth_headers,
            json={"update": f"DROP SILENT GRAPH <{TENANT_GRAPH}/kg/x>"},
        )
        assert res.status_code == 403, res.text
        mock_neptune.update.assert_not_called()
    finally:
        app.dependency_overrides.pop(get_tenant, None)


def test_passthrough_routes_execute_no_sparql():
    """Structural: every public SPARQL route rejects, and none reaches a store."""
    from infona_client.api.routes import query as query_routes

    module_src = inspect.getsource(query_routes)
    assert "get_neptune_client" not in module_src
    assert "parse_sparql_results" not in module_src

    for route in query_routes.router.routes:
        source = inspect.getsource(route.endpoint)
        assert "reject_raw_sparql()" in source, f"{route.path} does not reject"
        assert "client." not in source, f"{route.path} still touches a store client"

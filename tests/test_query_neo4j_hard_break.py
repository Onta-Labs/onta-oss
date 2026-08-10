"""E9 partial: public SPARQL surfaces hard-break under neo4j backend.

When ``COGRAPH_GRAPH_BACKEND=neo4j``, ``POST /graphs/{tenant}/query`` and
``/update`` return 410 Gone (ADR 0012 L2). Default / Neptune backend keeps the
existing scoped SPARQL behaviour (mocked Neptune client).
"""

from infona_client.auth.api_keys import TenantContext, get_tenant

TENANT = "test-tenant"
TENANT_GRAPH = f"https://graph.onta.sh/graphs/{TENANT}"
SCOPED_SELECT = f"SELECT ?s FROM <{TENANT_GRAPH}> WHERE {{ ?s ?p ?o }}"


def test_query_returns_410_when_neo4j_backend(
    client, auth_headers, mock_neptune, monkeypatch
):
    monkeypatch.setenv("COGRAPH_GRAPH_BACKEND", "neo4j")
    res = client.post(
        f"/graphs/{TENANT}/query",
        headers=auth_headers,
        json={"query": SCOPED_SELECT},
    )
    assert res.status_code == 410, res.text
    detail = res.json()["detail"]
    assert "neo4j" in detail.lower()
    assert "/ask" in detail or "agent" in detail.lower() or "SDK" in detail
    mock_neptune.query.assert_not_called()


def test_update_returns_410_when_neo4j_backend(
    app, client, auth_headers, mock_neptune, monkeypatch
):
    monkeypatch.setenv("COGRAPH_GRAPH_BACKEND", "neo4j")
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
        assert "neo4j" in res.json()["detail"].lower()
        mock_neptune.update.assert_not_called()
    finally:
        app.dependency_overrides.pop(get_tenant, None)


def test_query_still_accepts_when_backend_default(
    client, auth_headers, mock_neptune, monkeypatch
):
    monkeypatch.delenv("COGRAPH_GRAPH_BACKEND", raising=False)
    mock_neptune.query.return_value = {
        "head": {"vars": ["s"]},
        "results": {"bindings": []},
    }
    res = client.post(
        f"/graphs/{TENANT}/query",
        headers=auth_headers,
        json={"query": SCOPED_SELECT},
    )
    assert res.status_code == 200, res.text
    mock_neptune.query.assert_awaited_once()


def test_query_still_accepts_when_backend_neptune(
    client, auth_headers, mock_neptune, monkeypatch
):
    monkeypatch.setenv("COGRAPH_GRAPH_BACKEND", "neptune")
    mock_neptune.query.return_value = {
        "head": {"vars": ["s"]},
        "results": {"bindings": []},
    }
    res = client.post(
        f"/graphs/{TENANT}/query",
        headers=auth_headers,
        json={"query": SCOPED_SELECT},
    )
    assert res.status_code == 200, res.text
    mock_neptune.query.assert_awaited_once()


def test_update_still_accepts_operator_when_backend_default(
    app, client, auth_headers, mock_neptune, monkeypatch
):
    monkeypatch.delenv("COGRAPH_GRAPH_BACKEND", raising=False)
    app.dependency_overrides[get_tenant] = lambda: TenantContext(
        tenant_id=TENANT, api_key="k", is_operator=True
    )
    try:
        res = client.post(
            f"/graphs/{TENANT}/update",
            headers=auth_headers,
            json={"update": f"DROP SILENT GRAPH <{TENANT_GRAPH}/kg/x>"},
        )
        assert res.status_code == 200, res.text
        mock_neptune.update.assert_awaited_once()
    finally:
        app.dependency_overrides.pop(get_tenant, None)


def test_passthrough_routes_gate_neo4j():
    """Structural: every public SPARQL route must call the neo4j hard-break."""
    import inspect

    from infona_client.api.routes import query as query_routes

    for route in query_routes.router.routes:
        source = inspect.getsource(route.endpoint)
        assert "reject_raw_sparql_if_neo4j" in source, (
            f"{route.path} missing neo4j SPARQL hard-break"
        )

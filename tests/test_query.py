"""Raw SPARQL passthrough, happy path.

The queries here carry an explicit ``FROM <tenant graph>`` because
``/graphs/{tenant}/query`` now requires one (ONTA-412): without a dataset clause
Neptune reads the union of every named graph, i.e. every workspace. The
confinement rules themselves live in ``tests/test_query_tenant_scoping.py``.
"""

TENANT_GRAPH = "https://cograph.tech/graphs/test-tenant"


def test_execute_sparql(client, auth_headers, mock_neptune):
    mock_neptune.query.return_value = {
        "head": {"vars": ["name"]},
        "results": {
            "bindings": [
                {"name": {"type": "literal", "value": "Central Park"}},
            ]
        },
    }
    response = client.post(
        "/graphs/test-tenant/query",
        headers=auth_headers,
        json={
            "query": (
                f"SELECT ?name FROM <{TENANT_GRAPH}> "
                "WHERE { ?s <https://schema.org/name> ?name }"
            )
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["vars"] == ["name"]
    assert len(data["bindings"]) == 1
    assert data["bindings"][0]["name"] == "Central Park"


def test_execute_sparql_empty_result(client, auth_headers, mock_neptune):
    response = client.post(
        "/graphs/test-tenant/query",
        headers=auth_headers,
        json={"query": f"SELECT ?s FROM <{TENANT_GRAPH}> WHERE {{ ?s ?p ?o }}"},
    )
    assert response.status_code == 200
    assert response.json()["bindings"] == []

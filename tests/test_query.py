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
        json={"query": "SELECT ?name WHERE { ?s <https://schema.org/name> ?name }"},
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
        json={"query": "SELECT ?s WHERE { ?s ?p ?o }"},
    )
    assert response.status_code == 200
    assert response.json()["bindings"] == []

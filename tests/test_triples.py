def test_create_triples(client, auth_headers, mock_neptune):
    response = client.post(
        "/graphs/test-tenant/triples",
        headers=auth_headers,
        json={
            "triples": [
                {
                    "subject": "https://example.com/place/1",
                    "predicate": "https://schema.org/name",
                    "object": "Central Park",
                }
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["inserted"] == 1
    mock_neptune.update.assert_called_once()


def test_create_triples_empty(client, auth_headers):
    response = client.post(
        "/graphs/test-tenant/triples",
        headers=auth_headers,
        json={"triples": []},
    )
    assert response.status_code == 422


def test_get_triples(client, auth_headers, mock_neptune):
    mock_neptune.query.return_value = {
        "head": {"vars": ["s", "p", "o"]},
        "results": {
            "bindings": [
                {
                    "s": {"type": "uri", "value": "https://example.com/place/1"},
                    "p": {"type": "uri", "value": "https://schema.org/name"},
                    "o": {"type": "literal", "value": "Central Park"},
                }
            ]
        },
    }
    response = client.get("/graphs/test-tenant/triples", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert len(data["bindings"]) == 1
    assert data["bindings"][0]["o"] == "Central Park"


def test_delete_triples(client, auth_headers, mock_neptune):
    response = client.request(
        "DELETE",
        "/graphs/test-tenant/triples",
        headers=auth_headers,
        json={
            "triples": [
                {
                    "subject": "https://example.com/place/1",
                    "predicate": "https://schema.org/name",
                    "object": "Central Park",
                }
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["deleted"] == 1
    mock_neptune.update.assert_called_once()

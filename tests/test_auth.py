def test_valid_api_key(client, auth_headers):
    response = client.get("/graphs/test-tenant/triples", headers=auth_headers)
    assert response.status_code == 200


def test_missing_api_key(client):
    response = client.get("/graphs/test-tenant/triples")
    assert response.status_code in (401, 403)


def test_invalid_api_key(client):
    response = client.get(
        "/graphs/test-tenant/triples",
        headers={"X-API-Key": "bad-key"},
    )
    assert response.status_code == 401

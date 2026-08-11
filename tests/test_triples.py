"""Raw triple SPO routes are gone (ONTA-527).

They were a SPARQL surface over the RDF named-graph model; the property graph
has no faithful equivalent (no kg scope, no 1:1 predicate→property mapping), so
all three verbs return 410 Gone and touch no store. Request-body validation
still runs first — an empty batch is a 422, because FastAPI validates before the
handler — which is worth pinning so the tombstone does not accidentally become
the repo's answer to "is this payload well-formed".
"""

import pytest

VALID_TRIPLE = {
    "subject": "https://example.com/place/1",
    "predicate": "https://schema.org/name",
    "object": "Central Park",
}


def test_create_triples_returns_410(client, auth_headers, mock_neptune):
    response = client.post(
        "/graphs/test-tenant/triples",
        headers=auth_headers,
        json={"triples": [VALID_TRIPLE]},
    )
    assert response.status_code == 410, response.text
    assert "ingest" in response.json()["detail"]
    mock_neptune.update.assert_not_called()


def test_create_triples_empty_still_422(client, auth_headers):
    """Body validation precedes the tombstone."""
    response = client.post(
        "/graphs/test-tenant/triples",
        headers=auth_headers,
        json={"triples": []},
    )
    assert response.status_code == 422


def test_get_triples_returns_410(client, auth_headers, mock_neptune):
    response = client.get("/graphs/test-tenant/triples", headers=auth_headers)
    assert response.status_code == 410, response.text
    mock_neptune.query.assert_not_called()


def test_delete_triples_returns_410(client, auth_headers, mock_neptune):
    response = client.request(
        "DELETE",
        "/graphs/test-tenant/triples",
        headers=auth_headers,
        json={"triples": [VALID_TRIPLE]},
    )
    assert response.status_code == 410, response.text
    mock_neptune.update.assert_not_called()


@pytest.mark.parametrize("legacy", ["neptune", "fuseki"])
def test_legacy_backend_env_cannot_reopen_the_routes(
    client, auth_headers, mock_neptune, monkeypatch, legacy
):
    monkeypatch.setenv("INFONA_GRAPH_BACKEND", legacy)
    response = client.post(
        "/graphs/test-tenant/triples",
        headers=auth_headers,
        json={"triples": [VALID_TRIPLE]},
    )
    assert response.status_code == 410, response.text
    mock_neptune.update.assert_not_called()

def test_health_ok(client, mock_neptune):
    # Default backend is neo4j; hermetic suite injects MemoryGraphStore (health=True).
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "backend": "neo4j",
        "neo4j": True,
    }


def test_health_degraded(client, mock_neptune, monkeypatch):
    # Legacy SPARQL path: explicit neptune backend still probes NeptuneClient.
    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neptune")
    mock_neptune.health.return_value = False
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["backend"] == "neptune"
    assert response.json()["neptune"] is False


def test_health_neo4j_degraded(client, monkeypatch):
    from infona_client.graph.store import configure_graph_store

    class _DownStore:
        async def health(self):
            return False

    configure_graph_store(_DownStore())  # type: ignore[arg-type]
    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neo4j")
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["backend"] == "neo4j"
    assert body["neo4j"] is False
    assert body["status"] == "degraded"

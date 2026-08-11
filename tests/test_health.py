def test_health_ok(client, mock_neptune):
    # Hermetic suite pins INFONA_GRAPH_BACKEND=neptune + mock_neptune.
    mock_neptune.health.return_value = True
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body.get("backend", "neptune") == "neptune"
    assert body.get("neptune") is True


def test_health_degraded(client, mock_neptune):
    mock_neptune.health.return_value = False
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json().get("backend", "neptune") == "neptune"
    assert response.json().get("neptune") is False


def test_health_neo4j_ok(client, monkeypatch):
    from infona_client.graph.memory_store import MemoryGraphStore
    from infona_client.graph.store import configure_graph_store

    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neo4j")
    configure_graph_store(MemoryGraphStore())
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["backend"] == "neo4j"
    assert body["neo4j"] is True
    assert body["status"] == "healthy"


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

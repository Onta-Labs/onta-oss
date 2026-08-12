def test_health_ok(client):
    # Neo4j is the only backend; hermetic suite injects MemoryGraphStore (health=True).
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "backend": "neo4j",
        "neo4j": True,
    }


def test_health_degraded_when_store_is_down(client):
    from infona_client.graph.store import configure_graph_store

    class _DownStore:
        async def health(self):
            return False

    configure_graph_store(_DownStore())  # type: ignore[arg-type]
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["backend"] == "neo4j"
    assert body["neo4j"] is False
    assert body["status"] == "degraded"


def test_health_degraded_when_no_store_configured(client):
    """The probe must answer 'degraded', never 500 (ONTA-527 fail-closed reads).

    With no process store and no NEO4J_* env, get_graph_store raises
    GraphConfigError; health swallows it so a misconfigured task still reports
    rather than crashing the container's health check into a restart loop.
    """
    from infona_client.graph.store import reset_graph_store_for_tests

    reset_graph_store_for_tests()
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "degraded", "backend": "neo4j", "neo4j": False}

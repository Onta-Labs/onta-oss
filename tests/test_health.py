def test_health_ok(client):
    # Neo4j is the only backend; hermetic suite injects MemoryGraphStore (health=True).
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["backend"] == "neo4j"
    assert body["neo4j"] is True
    assert "neo4j_uri_kind" in body


def test_health_degraded_when_store_is_down(client):
    from infona_client.graph.store import configure_graph_store

    class _DownStore:
        async def health(self):
            return False

    configure_graph_store(_DownStore())  # type: ignore[arg-type]
    response = client.get("/health")
    # 503 so an ALB/ECS health check stops routing Explorer at a task
    # whose Bolt target is dead (200 + degraded used to keep sending).
    assert response.status_code == 503
    body = response.json()
    assert body["backend"] == "neo4j"
    assert body["neo4j"] is False
    assert body["status"] == "degraded"


def test_health_degraded_when_no_store_configured(client):
    """The probe must answer 503 'degraded', never 500 (ONTA-527 fail-closed).

    With no process store and no NEO4J_* env, get_graph_store raises
    GraphConfigError; health swallows it so a misconfigured task still
    reports rather than crashing the container.
    """
    from infona_client.graph.store import reset_graph_store_for_tests

    reset_graph_store_for_tests()
    response = client.get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["backend"] == "neo4j"
    assert body["neo4j"] is False
    assert body["neo4j_uri_kind"] == "missing"


def test_health_reports_private_ip_kind_without_echoing_the_host(client, monkeypatch):
    monkeypatch.setenv("NEO4J_URI", "bolt://10.0.10.176:7687")
    response = client.get("/health")
    assert response.status_code == 200  # MemoryGraphStore is up
    body = response.json()
    assert body["neo4j_uri_kind"] == "private_ip"
    dumped = response.text
    assert "10.0.10.176" not in dumped


def test_health_malformed_uri_never_500(client, monkeypatch):
    monkeypatch.setenv("NEO4J_URI", "bolt://[")
    response = client.get("/health")
    assert response.status_code != 500
    body = response.json()
    assert body["neo4j_uri_kind"] == "missing"

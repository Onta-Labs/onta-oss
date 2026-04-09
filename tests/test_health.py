def test_health_ok(client, mock_neptune):
    mock_neptune.health.return_value = True
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy", "neptune": True}


def test_health_degraded(client, mock_neptune):
    mock_neptune.health.return_value = False
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"

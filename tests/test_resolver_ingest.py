"""Integration test for the ingest API endpoint."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def mock_anthropic_response():
    """Mock Anthropic response with extracted entities."""
    def make_response(text: str):
        mock = AsyncMock()
        content_block = MagicMock()
        content_block.text = text
        mock.content = [content_block]
        return mock
    return make_response


def test_ingest_endpoint_exists(client, auth_headers):
    """Verify the endpoint is registered and requires auth."""
    response = client.post("/graphs/test-tenant/ingest")
    assert response.status_code != 404


def test_ingest_requires_auth(client):
    response = client.post(
        "/graphs/test-tenant/ingest",
        json={"content": "test"},
    )
    assert response.status_code == 401


def test_ingest_requires_content(client, auth_headers):
    response = client.post(
        "/graphs/test-tenant/ingest",
        json={},
        headers=auth_headers,
    )
    assert response.status_code == 422


@patch("cograph.api.routes.ingest.SchemaResolver")
def test_ingest_returns_result(mock_resolver_cls, client, auth_headers):
    """Test that ingest endpoint calls resolver and returns result."""
    from cograph.resolver.models import IngestResult
    mock_instance = AsyncMock()
    mock_instance.ingest.return_value = IngestResult(
        entities_extracted=2,
        entities_resolved=2,
        triples_inserted=10,
        types_created=["Property"],
        attributes_added=["Property.price"],
    )
    mock_resolver_cls.return_value = mock_instance

    response = client.post(
        "/graphs/test-tenant/ingest",
        json={"content": "A 3-bedroom house at 123 Main St for $500,000", "source": "test"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["entities_extracted"] == 2
    assert data["triples_inserted"] == 10
    assert "Property" in data["types_created"]

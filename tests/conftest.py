import os
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

os.environ["OMNIX_API_KEYS"] = '{"test-key": "test-tenant"}'
os.environ["OMNIX_NEPTUNE_ENDPOINT"] = "http://fake-neptune:8182"

from cograph_client.api.app import create_app
from cograph_client.graph.client import NeptuneClient


@pytest.fixture(autouse=True)
def _reset_enrichment_chain_state():
    """Keep enrichment chain resolution isolated across tests.

    Building the app + hitting an enrichment route registers the API-source
    registry's chain-prefix provider globally (ONTA-194 phase 3), which perturbs
    ``get_chain``. Resetting tiers (which also clears prefix providers) before and
    after every test makes exact-chain assertions robust-by-construction rather
    than relying on each test file to remember to reset.
    """
    from cograph_client.enrichment.tiers import reset_tiers

    reset_tiers()
    yield
    reset_tiers()


@pytest.fixture
def mock_neptune():
    client = AsyncMock(spec=NeptuneClient)
    client.health.return_value = True
    client.query.return_value = {
        "head": {"vars": []},
        "results": {"bindings": []},
    }
    client.update.return_value = None
    return client


@pytest.fixture
def app(mock_neptune):
    application = create_app()
    application.state.neptune_client = mock_neptune
    return application


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def auth_headers():
    return {"X-API-Key": "test-key"}

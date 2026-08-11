import os
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from tests._hermetic import (
    HERMETIC_SENTINEL_VAR,
    LIVE_PROVIDER_KEY_VARS,
    live_llm_opted_in,
)

os.environ["INFONA_API_KEYS"] = '{"test-key": "test-tenant"}'
os.environ["INFONA_NEPTUNE_ENDPOINT"] = "http://fake-neptune:8182"
# Production default is Neo4j; hermetic suite pins it and injects MemoryGraphStore
# (see _hermetic_graph_store) so tests never open a live Bolt connection.
os.environ.setdefault("INFONA_GRAPH_BACKEND", "neo4j")

# Hermetic-by-default LLM credentials.
#
# The SPARQL-gen and schema-inference paths reach a LIVE provider whenever a key
# happens to sit in the ambient environment. `nlp/pipeline.py`'s dispatch takes the
# Cerebras branch first (INFONA_QUERY_PROVIDER defaults to "cerebras") and then ends
# with a bare `if self._openrouter_key: return await
# self._generate_via_openrouter(...)`, while `resolver/schema_resolver.py`,
# `resolver/csv_resolver.py` and `resolver/llm_router.py` read OPENROUTER_API_KEY /
# CEREBRAS_API_KEY straight off os.environ. Tests that mock only
# `pipeline.anthropic.messages.create` therefore sail PAST their mock and call
# openrouter.ai or api.cerebras.ai for real on any machine with a key exported —
# the normal state of a dev shell.
#
# That made the suite ~10 tests redder on a developer's machine than in CI, and
# meant the CI gate was green only because the runner exports no provider key:
# adding one to the workflow env would have turned `test` red with no product
# change. Clearing the keys here makes the default run hermetic and reproducible —
# the dispatch falls through to the Anthropic path the tests actually mock.
#
# This has to happen at MODULE scope, before infona_client is imported, because
# `config.settings` is a module-level pydantic-settings singleton that snapshots
# the environment at import time; a fixture would run too late to affect it.
#
# Tests that need a key PRESENT set a fake one with `monkeypatch.setenv` (see
# test_multityping_saas.py, test_llm_router.py) — that still works, since
# monkeypatch applies after this and undoes itself afterwards.
#
# Escape hatch: export INFONA_TEST_ALLOW_LIVE_LLM=1 for a deliberate live-provider
# run — the opt-in live tests in test_csv_resolver.py need it.
#
# tests/test_hermetic_llm_env.py asserts the outcome, so this stays honest. It
# checks HERMETIC_SENTINEL_VAR rather than only "is a key absent": absence is
# unfalsifiable on a machine that never had a key (CI), so the sentinel is what
# lets the guard fail in CI if this block is ever deleted.
if not live_llm_opted_in():
    for _live_provider_key in LIVE_PROVIDER_KEY_VARS:
        os.environ.pop(_live_provider_key, None)
    os.environ[HERMETIC_SENTINEL_VAR] = "1"

from infona_client.api.app import create_app
from infona_client.graph.client import NeptuneClient
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.store import configure_graph_store, reset_graph_store_for_tests


@pytest.fixture(autouse=True)
def _hermetic_graph_store():
    """Every test gets a fresh MemoryGraphStore as the process GraphStore.

    With INFONA_GRAPH_BACKEND=neo4j as the production default, routes and write
    rails call get_graph_store() unless they explicitly opt into legacy SPARQL.
    Injecting MemoryGraphStore keeps the suite hermetic (no live Neo4j).
    """
    configure_graph_store(MemoryGraphStore())
    yield
    reset_graph_store_for_tests()


@pytest.fixture(autouse=True)
def _reset_enrichment_chain_state():
    """Keep enrichment chain resolution isolated across tests.

    Building the app + hitting an enrichment route registers the API-source
    registry's chain-prefix provider globally (ONTA-194 phase 3), which perturbs
    ``get_chain``. Resetting tiers (which also clears prefix providers) before and
    after every test makes exact-chain assertions robust-by-construction rather
    than relying on each test file to remember to reset.
    """
    from infona_client.enrichment.tiers import reset_tiers

    reset_tiers()
    yield
    reset_tiers()


@pytest.fixture(autouse=True)
def _reset_active_types_cache():
    """Isolate the per-instance-graph active-type cache across tests (ONTA-411).

    The cache is keyed by instance-graph URI with a 60s TTL, and the fixture
    graphs in this suite are shared constants, so without a reset one test's
    "only Widget has instances" probe result silently scopes the NEXT test that
    reuses the same KG URI with a different active set. Cleared here rather than
    per test file so a new test cannot fall into the same trap.
    """
    from infona_client.nlp.pipeline import _active_types_cache

    _active_types_cache.clear()
    yield
    _active_types_cache.clear()


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

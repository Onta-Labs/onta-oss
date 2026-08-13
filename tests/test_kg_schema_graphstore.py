"""P0 inspect residual: whole-KG /schema + type-edges on GraphStore (no SPARQL).

MCP ``inspect_graph_schema`` hits ``GET …/explore/kgs/{kg}/schema``. Under Neo4j
that path was SPARQL-only and raised SparqlClientRetired → 500. Compose from
type_counts + type_summary + ontology catalog instead.

Anti-overfit: synthetic type/attr names only.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from infona_client.api.app import create_app
from infona_client.graph.client import NeptuneClient
from infona_client.graph.iri import IRI_BASE
from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_catalog import upsert_attribute, upsert_type
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.store import configure_graph_store, reset_graph_store_for_tests
from infona_client.normalization.inference import list_type_schema

TENANT = "test-tenant"
KG = "inspect-schema-kg"
GRAPH = f"{IRI_BASE}/graphs/{TENANT}/kg/{KG}"
AUTH = {"X-API-Key": "test-key"}
SCHEMA_URL = f"/graphs/{TENANT}/explore/kgs/{KG}/schema"
EDGES_URL = f"/graphs/{TENANT}/explore/kgs/{KG}/type-edges"

TYPE_WIDGET = "InspWidget"
TYPE_BIN = "InspBin"
ATTR_CODE = "insp_code"
REL_STORED_IN = "insp_stored_in"


@pytest.fixture
def mock_neptune():
    """AsyncMock dual-arm client — production uses real NeptuneClient type check."""
    client = AsyncMock(spec=NeptuneClient)
    client.health.return_value = True
    client.query.return_value = {
        "head": {"vars": []},
        "results": {"bindings": []},
    }
    client.update.return_value = None
    return client


@pytest.fixture
def real_neptune():
    """Real NeptuneClient instance (no HTTP) for fail-closed type(client) is checks."""
    c = NeptuneClient.__new__(NeptuneClient)
    c._endpoint = "http://unused.invalid"
    c._allow_http = False
    return c


@pytest.fixture
def store():
    s = MemoryGraphStore()
    yield s
    asyncio.run(s.close())
    reset_graph_store_for_tests()


@pytest.fixture
def client(mock_neptune):
    app = create_app()
    app.state.neptune_client = mock_neptune
    return TestClient(app)


async def _seed(store: MemoryGraphStore) -> None:
    w1 = entity_uri(TYPE_WIDGET, "w1")
    w2 = entity_uri(TYPE_WIDGET, "w2")
    b1 = entity_uri(TYPE_BIN, "b1")
    triples = [
        (w1, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/{TYPE_WIDGET}"),
        (w1, "http://www.w3.org/2000/01/rdf-schema#label", "W1"),
        (w1, f"{IRI_BASE}/types/{TYPE_WIDGET}/attrs/{ATTR_CODE}", "A"),
        (w2, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/{TYPE_WIDGET}"),
        (w2, "http://www.w3.org/2000/01/rdf-schema#label", "W2"),
        (b1, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/{TYPE_BIN}"),
        (b1, "http://www.w3.org/2000/01/rdf-schema#label", "B1"),
        (w1, f"{IRI_BASE}/onto/{REL_STORED_IN}", b1),
    ]
    await insert_facts(None, GRAPH, triples, store=store)
    await upsert_type(
        store=store, name=TYPE_WIDGET, description="inspect widget",
        layer="tenant", tenant_id=TENANT,
    )
    await upsert_type(
        store=store, name=TYPE_BIN, description="inspect bin",
        layer="tenant", tenant_id=TENANT,
    )
    await upsert_attribute(
        store=store, type_name=TYPE_WIDGET, attr_name=ATTR_CODE,
        datatype="string", layer="tenant", tenant_id=TENANT,
    )
    await upsert_attribute(
        store=store, type_name=TYPE_WIDGET, attr_name=REL_STORED_IN,
        datatype=TYPE_BIN, layer="tenant", tenant_id=TENANT,
    )


def test_schema_graphstore_200_with_populated_types(store, client, mock_neptune):
    async def run():
        await _seed(store)
        configure_graph_store(store)

    asyncio.run(run())
    r = client.get(SCHEMA_URL, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kg"] == KG
    assert body["stats_source"] == "graph_store"
    by_name = {t["name"]: t for t in body["types"]}
    assert TYPE_WIDGET in by_name
    assert by_name[TYPE_WIDGET]["entity_count"] == 2
    assert by_name[TYPE_WIDGET]["populated"] is True
    attr_names = {a["name"] for a in by_name[TYPE_WIDGET]["attributes"]}
    assert ATTR_CODE in attr_names
    rel_names = {rel["name"] for rel in by_name[TYPE_WIDGET]["relationships"]}
    assert REL_STORED_IN in rel_names
    # SPARQL dual-arm must not run when GraphStore answers.
    assert mock_neptune.query.await_count == 0


def test_schema_graphstore_with_real_neptune_client_no_500(store, real_neptune):
    """Production shape: real NeptuneClient + Memory store → schema 200 not 500."""
    app = create_app()
    app.state.neptune_client = real_neptune
    tc = TestClient(app)

    async def run():
        await _seed(store)
        configure_graph_store(store)

    asyncio.run(run())
    r = tc.get(SCHEMA_URL, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["stats_source"] == "graph_store"
    assert any(t["name"] == TYPE_WIDGET for t in r.json()["types"])


def test_type_edges_graphstore_200(store, client):
    async def run():
        await _seed(store)
        configure_graph_store(store)

    asyncio.run(run())
    r = client.get(EDGES_URL, headers=AUTH)
    assert r.status_code == 200, r.text
    edges = r.json()
    assert isinstance(edges, list)
    # Undirected Widget—Bin from relationship target.
    pairs = {(e["source"], e["target"]) for e in edges}
    assert (TYPE_BIN, TYPE_WIDGET) in pairs or (TYPE_WIDGET, TYPE_BIN) in pairs


def test_list_type_schema_uses_catalog_when_store_configured(store):
    async def run():
        await _seed(store)
        configure_graph_store(store)
        # Real client would raise on SPARQL; catalog path must not call it.
        real = NeptuneClient.__new__(NeptuneClient)
        real._endpoint = "http://unused.invalid"
        real._allow_http = False

        async def boom(*_a, **_k):
            raise AssertionError("list_type_schema must not call SPARQL under GraphStore")

        real.query = boom  # type: ignore[method-assign]
        schema = await list_type_schema(real, TENANT, TYPE_WIDGET)
        assert ATTR_CODE in schema["attributes"]
        rels = {r["name"]: r.get("target_type") for r in schema["relationships"]}
        assert REL_STORED_IN in rels
        assert rels[REL_STORED_IN] == TYPE_BIN

    asyncio.run(run())


def test_schema_503_when_no_store_and_real_neptune():
    reset_graph_store_for_tests()
    app = create_app()
    real = NeptuneClient.__new__(NeptuneClient)
    real._endpoint = "http://unused.invalid"
    real._allow_http = False
    app.state.neptune_client = real
    tc = TestClient(app)
    r = tc.get(SCHEMA_URL, headers=AUTH)
    assert r.status_code == 503, r.text

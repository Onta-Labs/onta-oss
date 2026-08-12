"""Hermetic GraphStore coverage for major API routes.

With an injected ``MemoryGraphStore``: explore records / entity detail /
type-counts, grep, and ontology list+upsert run on GraphStore; raw triples are
410; value history reads Assertion provenance.

This file was `test_api_dual_backend_routes.py` when there were two backends to
choose between. ONTA-527 removed the choice, so the "legacy neptune still uses
SPARQL" half is gone and what remains is the assertion that these routes reach
the store and never a SPARQL client — the mocked ``NeptuneClient`` stays only as
a tripwire (`assert_not_called`).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from infona_client.api.app import create_app
from infona_client.graph.client import NeptuneClient
from infona_client.graph.facts import Fact
from infona_client.graph.iri import IRI_BASE
from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_catalog import upsert_attribute, upsert_type
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.store import configure_graph_store, reset_graph_store_for_tests

TENANT = "test-tenant"
KG = "bookstore"
GRAPH = f"{IRI_BASE}/graphs/{TENANT}/kg/{KG}"
AUTH = {"X-API-Key": "test-key"}


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


async def _seed_kg(store: MemoryGraphStore) -> dict[str, str]:
    alice = entity_uri("Person", "alice")
    bob = entity_uri("Person", "bob")
    acme = entity_uri("Organization", "acme")
    triples = [
        (alice, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/Person"),
        (alice, "http://www.w3.org/2000/01/rdf-schema#label", "Alice"),
        (alice, f"{IRI_BASE}/types/Person/attrs/email", "alice@example.com"),
        (bob, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/Person"),
        (bob, "http://www.w3.org/2000/01/rdf-schema#label", "Bob"),
        (bob, f"{IRI_BASE}/types/Person/attrs/email", "bob@example.com"),
        (acme, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/Organization"),
        (acme, "http://www.w3.org/2000/01/rdf-schema#label", "Acme Corp"),
        (alice, f"{IRI_BASE}/onto/works_at", acme),
    ]
    await insert_facts(None, GRAPH, triples, store=store)
    await upsert_type(
        store=store,
        name="Person",
        description="A person",
        layer="tenant",
        tenant_id=TENANT,
    )
    await upsert_attribute(
        store=store,
        type_name="Person",
        attr_name="email",
        datatype="string",
        layer="tenant",
        tenant_id=TENANT,
    )
    return {"alice": alice, "bob": bob, "acme": acme}


# ---------------------------------------------------------------------------
# Explore
# ---------------------------------------------------------------------------


def test_explore_records_neo4j_store_path(client, mock_neptune, store, monkeypatch):
    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neo4j")
    configure_graph_store(store)
    ids = asyncio.run(_seed_kg(store))

    res = client.get(
        f"/graphs/{TENANT}/explore/kgs/{KG}/types/Person/records",
        headers=AUTH,
        params={"limit": 10},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total"] == 2
    assert len(body["rows"]) == 2
    assert "name" in body["columns"]
    row_ids = {r["id"] for r in body["rows"]}
    assert ids["alice"] in row_ids
    assert ids["bob"] in row_ids
    # GraphStore path must not hit SPARQL.
    mock_neptune.query.assert_not_called()


def test_explore_entity_detail_neo4j_store_path(client, mock_neptune, store, monkeypatch):
    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neo4j")
    configure_graph_store(store)
    ids = asyncio.run(_seed_kg(store))

    res = client.get(
        f"/graphs/{TENANT}/explore/kgs/{KG}/entities/{ids['alice']}",
        headers=AUTH,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["id"] == ids["alice"]
    assert body["name"] == "Alice"
    assert body["primary_type"] == "Person"
    assert body["properties"].get("email") == "alice@example.com"
    assert any(r["other_id"] == ids["acme"] for r in body["outgoing"])
    mock_neptune.query.assert_not_called()


def test_type_counts_neo4j_store_path(client, mock_neptune, store, monkeypatch):
    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neo4j")
    configure_graph_store(store)
    asyncio.run(_seed_kg(store))

    res = client.get(
        f"/graphs/{TENANT}/kgs/{KG}/type-counts",
        headers=AUTH,
    )
    assert res.status_code == 200, res.text
    by_name = {r["name"]: r["entity_count"] for r in res.json()}
    assert by_name.get("Person") == 2
    assert by_name.get("Organization") == 1
    mock_neptune.query.assert_not_called()


# ---------------------------------------------------------------------------
# Grep
# ---------------------------------------------------------------------------


def test_grep_neo4j_property_scan(client, mock_neptune, store, monkeypatch):
    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neo4j")
    configure_graph_store(store)
    asyncio.run(_seed_kg(store))

    res = client.post(
        f"/graphs/{TENANT}/grep",
        headers=AUTH,
        json={"q": "alice@example", "kg_name": KG, "limit": 20},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["count"] >= 1
    assert any("alice@example" in m["value"] for m in body["matches"])
    mock_neptune.query.assert_not_called()


# ---------------------------------------------------------------------------
# Ontology
# ---------------------------------------------------------------------------


def test_ontology_list_and_create_neo4j(client, mock_neptune, store, monkeypatch):
    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neo4j")
    configure_graph_store(store)

    create = client.post(
        f"/graphs/{TENANT}/ontology/types",
        headers=AUTH,
        json={
            "name": "Book",
            "description": "A book",
            "attributes": [{"name": "title", "datatype": "string"}],
        },
    )
    assert create.status_code == 201, create.text
    assert create.json()["created"] == "Book"
    mock_neptune.update.assert_not_called()

    listed = client.get(f"/graphs/{TENANT}/ontology/types", headers=AUTH)
    assert listed.status_code == 200, listed.text
    names = [t["name"] for t in listed.json()]
    assert "Book" in names
    book = next(t for t in listed.json() if t["name"] == "Book")
    assert any(a["name"] == "title" for a in book["attributes"])
    mock_neptune.query.assert_not_called()


# ---------------------------------------------------------------------------
# Triples + history gates
# ---------------------------------------------------------------------------


def test_triples_410_on_neo4j(client, mock_neptune, monkeypatch):
    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neo4j")
    res = client.post(
        f"/graphs/{TENANT}/triples",
        headers=AUTH,
        json={
            "triples": [
                {
                    "subject": "https://example.com/s",
                    "predicate": "https://example.com/p",
                    "object": "o",
                }
            ]
        },
    )
    assert res.status_code == 410, res.text
    assert "neo4j" in res.json()["detail"].lower()
    mock_neptune.update.assert_not_called()

    get = client.get(f"/graphs/{TENANT}/triples", headers=AUTH)
    assert get.status_code == 410


def test_history_assertion_provenance_on_neo4j(
    client, mock_neptune, store, monkeypatch
):
    """Neo4j path: Assertion provenance as history (not 501)."""
    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neo4j")
    configure_graph_store(store)
    alice = entity_uri("Person", "alice")
    email_prop = f"{IRI_BASE}/properties/email"

    async def _seed():
        await insert_facts(
            None,
            GRAPH,
            facts=[
                Fact(subject_id=alice, kind="type", key="Person"),
                Fact(
                    subject_id=alice,
                    kind="literal",
                    key="email",
                    value="alice@example.com",
                    source_url="https://example.com/alice",
                    verified_at="2026-08-01T12:00:00Z",
                    provenance="enrichment",
                ),
            ],
            store=store,
        )

    asyncio.run(_seed())

    res = client.get(
        f"/graphs/{TENANT}/history",
        headers=AUTH,
        params={"kg_name": KG, "subject": alice},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["kg_name"] == KG
    assert body["count"] >= 1
    # Prefer the email fact we stamped with verified_at.
    email_rows = [
        c
        for c in body["changes"]
        if c["subject"] == alice
        and (c["predicate"] == email_prop or c["new_value"] == "alice@example.com")
    ]
    assert email_rows, body["changes"]
    row = email_rows[0]
    assert row["new_value"] == "alice@example.com"
    assert row["old_value"] == ""
    assert row["changed_at"] == "2026-08-01T12:00:00Z"
    mock_neptune.query.assert_not_called()



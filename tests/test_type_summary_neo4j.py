"""P-A1a — per-KG vis drill-in type summary on GraphStore (Neo4j / Memory).

Overview (``type-counts``) and drill-in (``types/{Type}/summary``) must agree
on entity_count for the same synthetic type. Anti-overfit: synthetic type /
attribute / entity names only (no warehouse or persona CSVs).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from infona_client.api.app import create_app
from infona_client.graph.client import NeptuneClient
from infona_client.graph.explore_store import type_counts, type_summary
from infona_client.graph.iri import IRI_BASE
from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_catalog import upsert_attribute, upsert_type
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.queries import InvalidTypeName
from infona_client.graph.schema_bootstrap import TEMPLATES
from infona_client.graph.store import configure_graph_store, reset_graph_store_for_tests

# Path tenant must match the static-key map in tests/conftest.py (test-key → test-tenant).
TENANT = "test-tenant"
KG = "synth-kg"
GRAPH = f"{IRI_BASE}/graphs/{TENANT}/kg/{KG}"
AUTH = {"X-API-Key": "test-key"}

# Synthetic names only — no real-world domain vocabulary.
TYPE_WIDGET = "SynthWidget"
TYPE_BIN = "SynthBin"
ATTR_CODE = "synth_code"
ATTR_MASS = "synth_mass"
REL_STORED_IN = "synth_stored_in"


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


async def _seed_synth(store: MemoryGraphStore, *, with_ontology: bool = True) -> dict[str, str]:
    """Three SynthWidget + one SynthBin with literals and a relationship."""
    w1 = entity_uri(TYPE_WIDGET, "w1")
    w2 = entity_uri(TYPE_WIDGET, "w2")
    w3 = entity_uri(TYPE_WIDGET, "w3")
    b1 = entity_uri(TYPE_BIN, "b1")
    triples = [
        (w1, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/{TYPE_WIDGET}"),
        (w1, "http://www.w3.org/2000/01/rdf-schema#label", "Widget One"),
        (w1, f"{IRI_BASE}/types/{TYPE_WIDGET}/attrs/{ATTR_CODE}", "C-1"),
        (w1, f"{IRI_BASE}/types/{TYPE_WIDGET}/attrs/{ATTR_MASS}", "1.5"),
        (w2, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/{TYPE_WIDGET}"),
        (w2, "http://www.w3.org/2000/01/rdf-schema#label", "Widget Two"),
        (w2, f"{IRI_BASE}/types/{TYPE_WIDGET}/attrs/{ATTR_CODE}", "C-2"),
        (w3, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/{TYPE_WIDGET}"),
        (w3, "http://www.w3.org/2000/01/rdf-schema#label", "Widget Three"),
        (b1, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/{TYPE_BIN}"),
        (b1, "http://www.w3.org/2000/01/rdf-schema#label", "Bin Alpha"),
        (w1, f"{IRI_BASE}/onto/{REL_STORED_IN}", b1),
        (w2, f"{IRI_BASE}/onto/{REL_STORED_IN}", b1),
    ]
    await insert_facts(None, GRAPH, triples, store=store)
    if with_ontology:
        await upsert_type(
            store=store,
            name=TYPE_WIDGET,
            description="A synthetic widget for P-A1a tests",
            layer="tenant",
            tenant_id=TENANT,
        )
        await upsert_attribute(
            store=store,
            type_name=TYPE_WIDGET,
            attr_name=ATTR_CODE,
            datatype="string",
            layer="tenant",
            tenant_id=TENANT,
        )
        await upsert_attribute(
            store=store,
            type_name=TYPE_WIDGET,
            attr_name=ATTR_MASS,
            datatype="float",
            layer="tenant",
            tenant_id=TENANT,
        )
        await upsert_type(
            store=store,
            name=TYPE_BIN,
            description="A synthetic bin",
            layer="tenant",
            tenant_id=TENANT,
        )
        await upsert_attribute(
            store=store,
            type_name=TYPE_WIDGET,
            attr_name=REL_STORED_IN,
            datatype=TYPE_BIN,
            layer="tenant",
            tenant_id=TENANT,
        )
    return {"w1": w1, "w2": w2, "w3": w3, "b1": b1}


def test_type_summary_templates_registered():
    for name in ("entity_type_attr_coverage", "entity_type_rel_coverage"):
        assert name in TEMPLATES
        assert "$tenant_id" in TEMPLATES[name].cypher
        assert "$kg" in TEMPLATES[name].cypher
        assert "$primary_type" in TEMPLATES[name].cypher
        assert TEMPLATES[name].writing is False


def test_type_counts_and_summary_agree_on_entity_count(store):
    """Core P-A1a contract: overview count == drill-in entity_count."""

    async def run():
        await _seed_synth(store)
        counts = await type_counts(store=store, tenant_id=TENANT, kg=KG)
        assert counts is not None
        by_name = {c.name: c.entity_count for c in counts}
        assert by_name[TYPE_WIDGET] == 3
        assert by_name[TYPE_BIN] == 1

        summary = await type_summary(
            store=store, tenant_id=TENANT, kg=KG, type_name=TYPE_WIDGET
        )
        assert summary is not None
        assert summary.entity_count == by_name[TYPE_WIDGET]
        assert summary.name == TYPE_WIDGET
        assert "synthetic widget" in summary.description.lower()

        # Attribute coverage: synth_code on 2/3, synth_mass on 1/3.
        attrs = {a.name: a for a in summary.attributes}
        assert ATTR_CODE in attrs
        assert attrs[ATTR_CODE].count == 2
        assert attrs[ATTR_CODE].coverage_pct == round(2 / 3 * 100, 1)
        assert ATTR_MASS in attrs
        assert attrs[ATTR_MASS].count == 1
        assert attrs[ATTR_MASS].coverage_pct == round(1 / 3 * 100, 1)

        # Relationship: 2 of 3 widgets have synth_stored_in → bin.
        rels = {r.name: r for r in summary.relationships}
        assert REL_STORED_IN in rels
        assert rels[REL_STORED_IN].count == 2
        assert rels[REL_STORED_IN].target_type == TYPE_BIN
        assert rels[REL_STORED_IN].avg_degree == round(2 / 3, 2)
        # A relationship leaf is never also an attribute chip.
        assert REL_STORED_IN not in attrs

    asyncio.run(run())


def test_type_summary_drops_dual_written_literal_of_relationship_leaf(store):
    """Legacy ingest that also set Entity.<rel-leaf> as a string must not
    surface a second attribute chip next to the relationship."""

    async def run():
        uris = await _seed_synth(store)
        w1 = uris["w1"]
        # Dual-write a literal of the SAME leaf the relationship already uses.
        await insert_facts(
            None,
            GRAPH,
            [(w1, f"{IRI_BASE}/types/{TYPE_WIDGET}/attrs/{REL_STORED_IN}", "Bin Alpha")],
            store=store,
        )
        summary = await type_summary(
            store=store, tenant_id=TENANT, kg=KG, type_name=TYPE_WIDGET
        )
        assert summary is not None
        rels = {r.name for r in summary.relationships}
        attrs = {a.name for a in summary.attributes}
        assert REL_STORED_IN in rels
        assert REL_STORED_IN not in attrs

    asyncio.run(run())


def test_type_summary_instance_only_type_without_ontology(store):
    """Instances present even when ontology lookup is empty → 200-shape row."""

    async def run():
        await _seed_synth(store, with_ontology=False)
        summary = await type_summary(
            store=store, tenant_id=TENANT, kg=KG, type_name=TYPE_WIDGET
        )
        assert summary is not None
        assert summary.entity_count == 3
        # No catalog → empty description is fine.
        assert summary.description == ""

    asyncio.run(run())


def test_type_summary_declared_zero_instances(store):
    """Type in ontology, 0 instances in this KG → entity_count 0, not missing."""

    async def run():
        await upsert_type(
            store=store,
            name="SynthEmptyType",
            description="Declared but empty here",
            layer="tenant",
            tenant_id=TENANT,
        )
        summary = await type_summary(
            store=store, tenant_id=TENANT, kg=KG, type_name="SynthEmptyType"
        )
        assert summary is not None
        assert summary.entity_count == 0
        assert summary.attributes == ()
        assert summary.relationships == ()
        assert "empty" in summary.description.lower()

    asyncio.run(run())


def test_type_summary_unknown_type_returns_none(store):
    """Neither declared nor instanced → None (route maps to 404)."""

    async def run():
        await _seed_synth(store)
        missing = await type_summary(
            store=store, tenant_id=TENANT, kg=KG, type_name="SynthNoSuchType"
        )
        assert missing is None

    asyncio.run(run())


def test_type_summary_rejects_unsafe_type_name(store):
    async def run():
        with pytest.raises(InvalidTypeName):
            await type_summary(
                store=store,
                tenant_id=TENANT,
                kg=KG,
                type_name="Bad>Type",
            )

    asyncio.run(run())


def test_type_summary_http_agrees_with_type_counts(
    client, mock_neptune, store, monkeypatch
):
    """End-to-end: type-counts and /summary share the count; SPARQL never touched."""
    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neo4j")
    configure_graph_store(store)
    asyncio.run(_seed_synth(store))

    counts_res = client.get(
        f"/graphs/{TENANT}/kgs/{KG}/type-counts",
        headers=AUTH,
    )
    assert counts_res.status_code == 200, counts_res.text
    by_name = {r["name"]: r["entity_count"] for r in counts_res.json()}
    assert by_name[TYPE_WIDGET] == 3

    summary_res = client.get(
        f"/graphs/{TENANT}/explore/kgs/{KG}/types/{TYPE_WIDGET}/summary",
        headers=AUTH,
    )
    assert summary_res.status_code == 200, summary_res.text
    body = summary_res.json()
    assert body["entity_count"] == by_name[TYPE_WIDGET]
    assert body["name"] == TYPE_WIDGET
    attr_names = {a["name"] for a in body["attributes"]}
    assert ATTR_CODE in attr_names
    rel_names = {r["name"] for r in body["relationships"]}
    assert REL_STORED_IN in rel_names

    # GraphStore path must not hit SPARQL.
    mock_neptune.query.assert_not_called()


def test_type_summary_http_404_for_unknown(client, mock_neptune, store, monkeypatch):
    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neo4j")
    configure_graph_store(store)
    asyncio.run(_seed_synth(store))

    res = client.get(
        f"/graphs/{TENANT}/explore/kgs/{KG}/types/SynthNoSuchType/summary",
        headers=AUTH,
    )
    assert res.status_code == 404
    assert "not found" in res.json()["detail"].lower()
    mock_neptune.query.assert_not_called()


def test_type_summary_http_zero_instances_declared(
    client, mock_neptune, store, monkeypatch
):
    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neo4j")
    configure_graph_store(store)

    async def seed():
        await upsert_type(
            store=store,
            name="SynthEmptyType",
            description="Declared but empty here",
            layer="tenant",
            tenant_id=TENANT,
        )

    asyncio.run(seed())

    res = client.get(
        f"/graphs/{TENANT}/explore/kgs/{KG}/types/SynthEmptyType/summary",
        headers=AUTH,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["entity_count"] == 0
    assert body["attributes"] == []
    mock_neptune.query.assert_not_called()

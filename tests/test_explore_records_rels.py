"""Relationship columns on the paged records endpoint.

Sibling of ``test_explore_records.py`` so that file stays under the 550-line
cap. Object properties must appear once, with the target display name, and
must not consume the observed-extra budget.
"""
import asyncio
import os
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

os.environ["INFONA_API_KEYS"] = '{"test-key": "test-tenant"}'
os.environ["INFONA_NEPTUNE_ENDPOINT"] = "http://fake-neptune:8182"

from infona_client.api.app import create_app
from infona_client.graph.client import NeptuneClient
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.store import configure_graph_store
from tests.test_explore_records import (
    E1,
    LABEL_PRED,
    ONTO,
    RDF_TYPE,
    TENANT,
    TYPE,
    TYPES,
    _get,
    _movie,
    _seed,
)


@pytest.fixture
def store():
    st = MemoryGraphStore()
    configure_graph_store(st)
    return st


@pytest.fixture
def mock_neptune():
    client = AsyncMock(spec=NeptuneClient)
    client.health.return_value = True
    client.update.return_value = None
    return client


@pytest.fixture
def client(store, mock_neptune):
    app = create_app()
    app.state.neptune_client = mock_neptune
    return TestClient(app)


@pytest.fixture
def auth_headers():
    return {"X-API-Key": "test-key"}


def _declare_rel(type_name: str, attr_name: str, target_type: str, literal: str = "year"):
    """Catalog a Movie + target type with one object property and one literal."""
    from infona_client.graph.ontology_catalog import upsert_attribute, upsert_type

    async def declare():
        await upsert_type(name=type_name, tenant_id=TENANT, layer="tenant")
        await upsert_type(name=target_type, tenant_id=TENANT, layer="tenant")
        await upsert_attribute(
            type_name=type_name,
            attr_name=attr_name,
            datatype=target_type,
            tenant_id=TENANT,
            layer="tenant",
        )
        await upsert_attribute(
            type_name=type_name,
            attr_name=literal,
            datatype="string",
            tenant_id=TENANT,
            layer="tenant",
        )

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(declare())


def test_records_relationship_leaf_is_one_column_with_target_name(
    store, client, auth_headers
):
    """A declared object property is a records column with the target's name.

    Explorer Browse pins type-summary relationships as table columns and looks
    up rec[rel.label]. Omitting the leaf made a filled edge render as empty
    dashes. The ``has_*`` strip alias must still not appear as a second column
    (infona-oss #470). Synthetic types only — no production ontology names.
    """
    person_type = "SynthCast"
    person = entity_uri(person_type, "p1")
    rel_pred = f"{ONTO}has_lead"
    _declare_rel(TYPE, "has_lead", person_type)
    _seed(
        store,
        [
            *_movie(E1, label="The Picture", title="The Picture", year="1999"),
            (person, RDF_TYPE, TYPES + person_type),
            (person, LABEL_PRED, "Ada Example"),
            (E1, rel_pred, person),
        ],
    )

    data = _get(client, auth_headers).json()
    assert data["columns"].count("has_lead") == 1, data["columns"]
    assert "lead" not in data["columns"], data["columns"]
    assert "year" in data["columns"]
    assert data["rows"][0]["name"] == "The Picture"
    assert data["rows"][0]["has_lead"] == "Ada Example"


def test_records_relationship_without_has_prefix_is_one_column(
    store, client, auth_headers
):
    """Object properties that are not ``has_*`` still emit exactly one column.

    Mirrors the production enrich path (a type-ranged leaf written as an
    ``onto/<leaf>`` edge + target node). Synthetic names only.
    """
    org_type = "SynthOrg"
    org = entity_uri(org_type, "o1")
    rel_pred = f"{ONTO}synth_sponsor"
    _declare_rel(TYPE, "synth_sponsor", org_type)
    _seed(
        store,
        [
            *_movie(E1, label="The Picture", title="The Picture", year="1999"),
            (org, RDF_TYPE, TYPES + org_type),
            (org, LABEL_PRED, "Ada Labs"),
            (E1, rel_pred, org),
        ],
    )

    data = _get(client, auth_headers).json()
    assert data["columns"].count("synth_sponsor") == 1, data["columns"]
    assert "year" in data["columns"]
    assert data["rows"][0]["synth_sponsor"] == "Ada Labs"


def test_records_declared_relationship_with_no_edge_is_still_a_column(
    store, client, auth_headers
):
    """Declared object properties are columns even with no instance edge.

    Overlay-only minting would hide an unfilled enrich target from the table.
    """
    org_type = "SynthOrg"
    _declare_rel(TYPE, "synth_sponsor", org_type)
    _seed(store, _movie(E1, label="The Picture", title="The Picture", year="1999"))

    data = _get(client, auth_headers).json()
    assert "synth_sponsor" in data["columns"], data["columns"]
    assert data["rows"][0]["synth_sponsor"] == ""
    assert "year" in data["columns"]


def test_records_declared_relationship_survives_extra_column_budget(
    store, client, auth_headers
):
    """Declared rels are exempt from the 24 observed-extra cap."""
    org_type = "SynthOrg"
    org = entity_uri(org_type, "o1")
    _declare_rel(TYPE, "synth_sponsor", org_type)
    triples = [
        *_movie(E1, label="The Picture", title="The Picture", year="1999"),
        (org, RDF_TYPE, TYPES + org_type),
        (org, LABEL_PRED, "Ada Labs"),
        (E1, f"{ONTO}synth_sponsor", org),
    ]
    for i in range(24):
        leaf = f"extra_{i:02d}"
        triples.append((E1, f"{TYPES}{TYPE}/attrs/{leaf}", f"v{i}"))
    _seed(store, triples)

    data = _get(client, auth_headers).json()
    assert "synth_sponsor" in data["columns"], data["columns"]
    assert data["rows"][0]["synth_sponsor"] == "Ada Labs"

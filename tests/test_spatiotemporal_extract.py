"""Datatype-driven extraction + automatic write-path population of the
spatio-temporal index.

Covers :func:`extract_spatiotemporal_facts` (triples → facts) and the auto-index
hook inside :func:`infona_client.graph.kg_writer.insert_facts` (every converged
writer indexes its geometry-bearing entities, scoped per-KG, best-effort).

**Ported by ONTA-527.** The three write-path tests used to drive
``insert_facts`` with a fake Neptune and prove the primary write happened by
asserting the SPARQL it recorded (``assert neptune.updates``). ``insert_facts``
runs the property-graph path now and emits no SPARQL, so that proof was
testing a transport that no longer runs; it is replaced by reading the written
entity back out of the :class:`MemoryGraphStore` the write actually lands in.
The spatio-temporal hook itself is UNCHANGED and still live: unlike the
semantic hook (see ``test_semantic_write_hook.py``) it derives its facts from
the write's own triples and needs no re-read, so it is still called from
``_insert_facts_store``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.queries import kg_graph_uri, parse_kg_graph_uri
from infona_client.graph.scope import GraphScopeError
from infona_client.graph.store import configure_graph_store
from infona_client.spatiotemporal.extract import extract_spatiotemporal_facts
from infona_client.spatiotemporal.registry import (
    get_spatiotemporal_index,
    register_spatiotemporal_index,
    reset_spatiotemporal_index,
)

import pytest

GEO = "http://www.opengis.net/ont/geosparql#wktLiteral"
DT = "http://www.w3.org/2001/XMLSchema#dateTime"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"

TENANT = "demo-tenant"
KG = "EventsSF"


def _dt(y: int, m: int = 1, d: int = 1) -> datetime:
    return datetime(y, m, d, tzinfo=timezone.utc)


def _geom(uri: str, lon: float, lat: float) -> tuple:
    # Canonical literal-attribute predicate shape (`types/<T>/attrs/<leaf>`) so
    # the same triple is BOTH extractable (the extractor is datatype-driven and
    # predicate-blind) and writable as an Entity property on the store path.
    return (uri, "https://graph.infona.ai/types/T/attrs/loc", f"POINT({lon} {lat})^^{GEO}")


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_spatiotemporal_index()
    yield
    reset_spatiotemporal_index()


@pytest.fixture
def store():
    """The GraphStore ``insert_facts`` writes into (conftest installs one too;
    this makes the instance the assertions read back from explicit)."""
    st = MemoryGraphStore()
    configure_graph_store(st)
    return st


# ---------------------------------------------------------------------------
# extract_spatiotemporal_facts
# ---------------------------------------------------------------------------


def test_extracts_point_from_wkt_literal():
    facts = extract_spatiotemporal_facts(
        [_geom("e:1", 2.29, 48.85)], tenant_id=TENANT, kg_name=KG
    )
    assert len(facts) == 1
    f = facts[0]
    assert (f.lon, f.lat) == (2.29, 48.85)
    assert f.tenant_id == TENANT and f.kg_name == KG
    assert f.valid_from is None and f.valid_to is None


def test_entity_without_geometry_is_skipped():
    triples = [
        ("e:person", RDF_TYPE, "https://graph.infona.ai/types/Person"),
        ("e:person", "https://graph.infona.ai/types/Person/birth_date", f"1970-01-01T00:00:00^^{DT}"),
    ]
    assert extract_spatiotemporal_facts(triples, tenant_id=TENANT, kg_name=KG) == []


def test_lone_date_is_not_validity():
    """A single non-validity date must NOT become valid_time (we don't guess)."""
    triples = [
        _geom("e:place", 2.29, 48.85),
        ("e:place", "https://graph.infona.ai/types/Place/founded", f"1889-03-31T00:00:00^^{DT}"),
    ]
    f = extract_spatiotemporal_facts(triples, tenant_id=TENANT, kg_name=KG)[0]
    assert f.valid_from is None and f.valid_to is None


def test_start_end_pair_becomes_validity():
    triples = [
        _geom("e:expo", 2.30, 48.86),
        ("e:expo", "https://graph.infona.ai/types/Event/start_date", f"2024-06-01T00:00:00^^{DT}"),
        ("e:expo", "https://graph.infona.ai/types/Event/end_date", f"2024-06-10T00:00:00^^{DT}"),
    ]
    f = extract_spatiotemporal_facts(triples, tenant_id=TENANT, kg_name=KG)[0]
    assert f.valid_from == _dt(2024, 6, 1) and f.valid_to == _dt(2024, 6, 10)


def test_inverted_start_end_pair_opens_validity():
    """from > to would make PostGIS tstzrange raise and drop the write batch — the
    extractor discards an inverted range to open validity instead (entity still
    indexed by its geometry)."""
    triples = [
        _geom("e:bad", 2.30, 48.86),
        ("e:bad", "https://graph.infona.ai/types/Event/start_date", f"2024-06-10T00:00:00^^{DT}"),
        ("e:bad", "https://graph.infona.ai/types/Event/end_date", f"2024-06-01T00:00:00^^{DT}"),
    ]
    facts = extract_spatiotemporal_facts(triples, tenant_id=TENANT, kg_name=KG)
    assert len(facts) == 1  # still indexed
    assert facts[0].valid_from is None and facts[0].valid_to is None


def test_parse_kg_graph_uri_rejects_companion_graph():
    """A provenance/companion graph (extra path segment) must NOT greedily parse to
    kg_name='<kg>/provenance' — it returns None so only true per-KG graphs route."""
    assert parse_kg_graph_uri(kg_graph_uri(TENANT, KG)) == (TENANT, KG)
    assert parse_kg_graph_uri(kg_graph_uri(TENANT, KG) + "/provenance") is None
    assert parse_kg_graph_uri("https://graph.infona.ai/graphs/demo-tenant") is None


def test_explicit_valid_from_only_open_ended():
    triples = [
        _geom("e:site", 2.29, 48.85),
        ("e:site", "https://graph.infona.ai/types/Site/valid_from", f"2020-01-01T00:00:00^^{DT}"),
    ]
    f = extract_spatiotemporal_facts(triples, tenant_id=TENANT, kg_name=KG)[0]
    assert f.valid_from == _dt(2020) and f.valid_to is None


def test_denormalizes_label_and_type():
    triples = [
        ("e:1", RDF_TYPE, "https://graph.infona.ai/types/Venue"),
        ("e:1", RDFS_LABEL, "Ferry Building"),
        _geom("e:1", -122.39, 37.79),
    ]
    f = extract_spatiotemporal_facts(triples, tenant_id=TENANT, kg_name=KG)[0]
    assert f.attrs == {"label": "Ferry Building", "type": "Venue"}  # PascalCase type kept


def test_out_of_range_point_ignored():
    bad = ("e:bad", "https://graph.infona.ai/types/T/loc", f"POINT(999 999)^^{GEO}")
    assert extract_spatiotemporal_facts([bad], tenant_id=TENANT, kg_name=KG) == []


def test_order_preserved_and_multiple_entities():
    triples = [_geom("e:a", 1.0, 1.0), _geom("e:b", 2.0, 2.0)]
    facts = extract_spatiotemporal_facts(triples, tenant_id=TENANT, kg_name=KG)
    assert [f.entity_uri for f in facts] == ["e:a", "e:b"]


def test_plain_string_with_caret_not_mistyped():
    """A plain string literal containing '^^' (no http tail) is not a typed value."""
    triples = [
        _geom("e:1", 2.29, 48.85),
        ("e:1", "https://graph.infona.ai/types/T/note", "a^^b weird value"),
    ]
    facts = extract_spatiotemporal_facts(triples, tenant_id=TENANT, kg_name=KG)
    assert len(facts) == 1  # the note neither breaks parsing nor adds a fact


# ---------------------------------------------------------------------------
# insert_facts auto-population
# ---------------------------------------------------------------------------


async def test_insert_facts_populates_index_scoped_to_kg(store):
    graph = kg_graph_uri(TENANT, KG)
    triples = [
        ("e:venue", RDF_TYPE, "https://graph.infona.ai/types/Venue"),
        _geom("e:venue", -122.4194, 37.7749),
        # `name` is a RESERVED Entity property key on the store path, so this
        # geometry-less row carries `band_name` instead; its only job is to show
        # that a subject without coordinates adds no index row.
        ("e:noband", "https://graph.infona.ai/types/Band/attrs/band_name", "no geo here"),
    ]
    await insert_facts(None, graph, triples, store=store)
    # The primary write still happened — read it back out of the store the write
    # goes to now (this assertion used to be `assert neptune.updates`).
    assert {e["id"] for e in store.snapshot_entities()} == {"e:venue", "e:noband"}

    idx = get_spatiotemporal_index()
    hit = await idx.query_radius(TENANT, -122.4194, 37.7749, 1_000, kg_name=KG)
    assert {r.entity_uri for r in hit} == {"e:venue"}
    # Nothing leaks into a different KG.
    assert await idx.query_radius(TENANT, -122.4194, 37.7749, 1_000, kg_name="Other") == []


async def test_insert_facts_rejects_non_kg_graph_and_indexes_nothing(store):
    """A write aimed at the tenant ontology graph (no ``/kg/`` segment) indexes
    nothing — and no longer writes anything either.

    ``_index_spatiotemporal`` still carries its own ``parse_kg_graph_uri``
    guard, but on the property-graph path the write never reaches it:
    ``_resolve_graph_session`` cannot derive a (tenant, kg) scope from a tenant
    URI and fails closed first. The property under test is unchanged — a non-KG
    graph produces no index row — the enforcement just moved earlier and got
    louder.
    """
    onto_graph = "https://graph.infona.ai/graphs/demo-tenant"  # no /kg/ segment
    with pytest.raises(GraphScopeError):
        await insert_facts(None, onto_graph, [_geom("e:x", 1.0, 1.0)], store=store)
    idx = get_spatiotemporal_index()
    assert await idx.query_radius(TENANT, 1.0, 1.0, 1_000) == []
    assert store.snapshot_entities() == []


async def test_index_failure_does_not_fail_write(store):
    """A derived-index error must never propagate out of the primary KG write."""

    class _BoomIndex:
        async def upsert_many(self, facts):
            raise RuntimeError("index down")

    register_spatiotemporal_index(_BoomIndex())
    graph = kg_graph_uri(TENANT, KG)
    # Must not raise despite the index blowing up.
    await insert_facts(None, graph, [_geom("e:venue", -122.4, 37.7)], store=store)
    assert [e["id"] for e in store.snapshot_entities()] == ["e:venue"]  # write went through

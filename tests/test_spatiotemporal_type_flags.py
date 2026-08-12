"""Per-type spatio-temporal index markers (COG-103 follow-up).

The Explorer rail marks a type as *spatially indexed* when its instances carry
``geo:wktLiteral`` geometry (the one signal that puts an entity in the
spatio-temporal index) and *temporally indexed* when they carry validity per
the ``spatiotemporal.extract`` recognition rules — an explicit validity bound
(valid_from / valid_to), or a COMPLETE start+end pair. A lone generic date
(e.g. ``release_date``) is NOT temporal: the index never attaches validity for
it, so the marker must not claim otherwise.

Flags are computed inside ``recompute_kg_stats``'s existing whole-KG scan (two
extra datatype aggregates), materialized as boolean triples on the type URI in
the stats graph, and surfaced by both ``/type-counts`` and the per-type
``/summary``.

**Ported by ONTA-527.** The accumulator rules and the ``recompute_kg_stats``
scan below are unchanged and still pass, but note what they now prove: they
exercise a SPARQL PRODUCER that no live consumer reads. ``GET
/kgs/{kg}/type-counts`` — the Explorer rail — takes its GraphStore branch in
production and hardcodes ``spatially_indexed=False, temporally_indexed=False``
for every type, whatever the stats graph says; the per-type ``/summary``
endpoint does still read the markers, but it is SPARQL end to end (no
GraphStore branch at all), so it cannot serve production either. The two
``/type-counts`` tests are ported to the store-backed route; the one that
asserts a type is actually MARKED is a strict xfail pinning that lost
capability (see its reason).
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

os.environ["INFONA_API_KEYS"] = '{"test-key": "test-tenant"}'
os.environ["INFONA_NEPTUNE_ENDPOINT"] = "http://fake-neptune:8182"

from infona_client.api.app import create_app
from infona_client.api.routes.explore import (
    RDF_TYPE,
    _IndexFlagAccumulator,
    _ST_FLAG_AGGREGATES,
    _STAT_SPATIAL,
    _STAT_TEMPORAL,
    recompute_kg_stats,
)
from infona_client.graph.client import NeptuneClient
from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.queries import kg_graph_uri
from infona_client.graph.store import configure_graph_store
from infona_client.spatiotemporal.extract import GEO_WKT

TENANT = "test-tenant"
KG = "web"

TYPES = "https://graph.infona.ai/types/"
ONTO = "https://graph.infona.ai/onto/"
XSD_DATE = "http://www.w3.org/2001/XMLSchema#date"


@pytest.fixture
def mock_neptune():
    client = AsyncMock(spec=NeptuneClient)
    client.health.return_value = True
    client.query.return_value = {"head": {"vars": []}, "results": {"bindings": []}}
    client.update.return_value = None
    return client


@pytest.fixture
def client(mock_neptune):
    app = create_app()
    app.state.neptune_client = mock_neptune
    return TestClient(app)


@pytest.fixture
def auth_headers():
    return {"X-API-Key": "test-key"}


@pytest.fixture
def seeded_store():
    """A KG holding one geometry-bearing Venue and two plain Models.

    ``/type-counts`` reads instance data from the GraphStore now, so the rows
    the route returns have to be written rather than mocked into a SPARQL
    response.
    """
    store = MemoryGraphStore()
    configure_graph_store(store)
    graph = kg_graph_uri(TENANT, KG)
    triples = [
        ("e:venue-1", RDF_TYPE, TYPES + "Venue"),
        ("e:venue-1", TYPES + "Venue/attrs/location", f"POINT(1 2)^^{GEO_WKT}"),
        ("e:model-1", RDF_TYPE, TYPES + "Model"),
        ("e:model-2", RDF_TYPE, TYPES + "Model"),
    ]
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        insert_facts(None, graph, triples, store=store)
    )
    return store


def _rows(*binding_dicts):
    variables: list[str] = []
    for b in binding_dicts:
        for k in b:
            if k not in variables:
                variables.append(k)
    return {
        "head": {"vars": variables},
        "results": {"bindings": [
            {k: {"value": v} for k, v in b.items()} for b in binding_dicts
        ]},
    }


def _empty():
    return {"head": {"vars": []}, "results": {"bindings": []}}


# ---------------------------------------------------------------------------
# 1. Accumulator rules mirror spatiotemporal.extract's validity recognition.
# ---------------------------------------------------------------------------


def test_accumulator_spatial_from_geometry():
    acc = _IndexFlagAccumulator()
    acc.add(ONTO + "location", geo=5, tmp=0)
    assert acc.spatial is True
    assert acc.temporal is False


def test_accumulator_temporal_from_explicit_bound():
    acc = _IndexFlagAccumulator()
    acc.add(ONTO + "valid_from", geo=0, tmp=3)
    assert acc.temporal is True


def test_accumulator_temporal_needs_complete_interval_pair():
    acc = _IndexFlagAccumulator()
    acc.add(ONTO + "start_date", geo=0, tmp=3)
    assert acc.temporal is False, "a lone start does not bound validity"
    acc.add(ONTO + "end_date", geo=0, tmp=3)
    assert acc.temporal is True


def test_accumulator_lone_generic_date_is_not_temporal():
    """release_date is a plain date attribute — the index never attaches
    validity for it, so the type must not be marked temporally indexed."""
    acc = _IndexFlagAccumulator()
    acc.add(ONTO + "release_date", geo=0, tmp=12)
    assert acc.temporal is False


# ---------------------------------------------------------------------------
# 2. The recompute scan carries the datatype aggregates and materializes the
#    per-type flag triples into the stats graph.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recompute_scan_carries_flag_aggregates(mock_neptune):
    captured: list[str] = []

    async def capture_query(sparql, *a, **k):
        captured.append(sparql)
        return _empty()

    mock_neptune.query.side_effect = capture_query

    await recompute_kg_stats(mock_neptune, TENANT, KG)

    scan = next((q for q in captured if "GROUP BY ?type ?p" in q), None)
    assert scan is not None
    assert _ST_FLAG_AGGREGATES in scan
    assert GEO_WKT in scan
    assert XSD_DATE in scan
    # isLiteral guards DATATYPE() so IRI objects don't error the aggregate.
    assert "isLiteral(?o)" in scan


@pytest.mark.asyncio
async def test_recompute_materializes_type_flags(mock_neptune):
    """Venue (geometry) → spatial; Event (geometry + valid_from) → both;
    Session (start+end pair) → temporal; Model (lone release_date) → neither."""

    scan_rows = _rows(
        # rdf:type rows → entity counts
        {"type": TYPES + "Venue", "p": RDF_TYPE, "cnt": "3", "rel": "0"},
        {"type": TYPES + "Event", "p": RDF_TYPE, "cnt": "4", "rel": "0"},
        {"type": TYPES + "Session", "p": RDF_TYPE, "cnt": "2", "rel": "0"},
        {"type": TYPES + "Model", "p": RDF_TYPE, "cnt": "5", "rel": "0"},
        # Venue: WKT geometry attribute
        {"type": TYPES + "Venue", "p": ONTO + "location", "cnt": "3", "rel": "0",
         "geo": "3", "tmp": "0", "sample": "POINT(1 2)"},
        # Event: geometry + explicit validity bound
        {"type": TYPES + "Event", "p": ONTO + "location", "cnt": "4", "rel": "0",
         "geo": "4", "tmp": "0", "sample": "POINT(3 4)"},
        {"type": TYPES + "Event", "p": ONTO + "valid_from", "cnt": "4", "rel": "0",
         "geo": "0", "tmp": "4", "sample": "2026-01-01"},
        # Session: complete start+end pair, no geometry
        {"type": TYPES + "Session", "p": ONTO + "start_date", "cnt": "2", "rel": "0",
         "geo": "0", "tmp": "2", "sample": "2026-01-01"},
        {"type": TYPES + "Session", "p": ONTO + "end_date", "cnt": "2", "rel": "0",
         "geo": "0", "tmp": "2", "sample": "2026-01-02"},
        # Model: lone generic date → neither flag
        {"type": TYPES + "Model", "p": ONTO + "release_date", "cnt": "5", "rel": "0",
         "geo": "0", "tmp": "5", "sample": "2025-11-01"},
    )

    async def route(sparql, *a, **k):
        if "GROUP BY ?type ?p" in sparql:
            return scan_rows
        return _empty()

    mock_neptune.query.side_effect = route
    updates: list[str] = []

    async def capture_update(sparql, *a, **k):
        updates.append(sparql)

    mock_neptune.update.side_effect = capture_update

    await recompute_kg_stats(mock_neptune, TENANT, KG)

    insert = next((u for u in updates if "INSERT DATA" in u), "")
    assert f"<{TYPES}Venue> <{_STAT_SPATIAL}> true ." in insert
    assert f"<{TYPES}Venue> <{_STAT_TEMPORAL}> true ." not in insert
    assert f"<{TYPES}Event> <{_STAT_SPATIAL}> true ." in insert
    assert f"<{TYPES}Event> <{_STAT_TEMPORAL}> true ." in insert
    assert f"<{TYPES}Session> <{_STAT_TEMPORAL}> true ." in insert
    assert f"<{TYPES}Session> <{_STAT_SPATIAL}> true ." not in insert
    assert f"<{TYPES}Model> <{_STAT_SPATIAL}> true ." not in insert
    assert f"<{TYPES}Model> <{_STAT_TEMPORAL}> true ." not in insert


# ---------------------------------------------------------------------------
# 3. /type-counts merges the stats-graph markers onto the count rows.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason=(
        "LOST CAPABILITY (pre-dates ONTA-527, surfaced by it): the per-type "
        "spatio-temporal markers never reach the Explorer rail on Neo4j. "
        "api/routes/knowledge_graphs.py::list_type_counts takes its GraphStore "
        "branch first (graph.explore_store.type_counts returns rows, so the "
        "SPARQL tail is unreachable in production) and constructs every "
        "TypeCount with spatially_indexed=False, temporally_indexed=False "
        "hardcoded — _read_type_index_flags is never called. The producer side "
        "still works (the _IndexFlagAccumulator + recompute_kg_stats tests "
        "above pass), but the flags are materialized into a stats GRAPH that "
        "only a SPARQL read can see, and there is no property-graph store for "
        "them. So a KG full of geometry renders as 'not spatially indexed' in "
        "the Explorer. Not fixed here: it needs a stats/flags port to the "
        "GraphStore, not a test change."
    ),
    strict=True,
)
def test_type_counts_include_index_flags(seeded_store, client, auth_headers):
    """A geometry-bearing type is reported as spatially indexed.

    The Venue seeded here carries a ``geo:wktLiteral`` — the one signal that
    puts an entity in the spatio-temporal index — and the Models carry none.
    """
    resp = client.get(f"/graphs/{TENANT}/kgs/{KG}/type-counts", headers=auth_headers)
    assert resp.status_code == 200
    by_name = {t["name"]: t for t in resp.json()}
    assert by_name["Venue"]["spatially_indexed"] is True
    assert by_name["Venue"]["temporally_indexed"] is False
    assert by_name["Model"]["spatially_indexed"] is False
    assert by_name["Model"]["temporally_indexed"] is False


def test_type_counts_returns_flagged_rows_without_touching_neptune(
    seeded_store, client, mock_neptune, auth_headers
):
    """The endpoint that powers the Explorer rail answers from the store alone.

    This is the port of ``test_type_counts_survive_flag_read_failure``, which
    made the stats-graph flag read raise and asserted the route still answered
    200 with the flags defaulted off. That read no longer happens at all on the
    GraphStore branch, so the failure mode it guarded is unreachable; what
    survives — and is still worth pinning — is that the route returns
    well-formed, correctly-counted rows with the flags defaulted off and
    without issuing a single SPARQL query (``mock_neptune`` is left unstubbed
    on purpose: any call would return an AsyncMock and blow up the parse).
    """
    resp = client.get(f"/graphs/{TENANT}/kgs/{KG}/type-counts", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == [
        {"name": "Model", "entity_count": 2,
         "spatially_indexed": False, "temporally_indexed": False},
        {"name": "Venue", "entity_count": 1,
         "spatially_indexed": False, "temporally_indexed": False},
    ]
    mock_neptune.query.assert_not_called()


# ---------------------------------------------------------------------------
# 4. /summary surfaces the flags — from materialized stats AND the live scan.
# ---------------------------------------------------------------------------


def test_summary_reads_flags_from_stats(client, mock_neptune, auth_headers):
    from infona_client.api.routes import explore as _explore_mod
    _explore_mod._summary_cache.clear()

    def route(sparql, *a, **k):
        if "?label" in sparql and "subClassOf" in sparql:
            return _rows({"label": "Venue"})
        if "attrLabel" in sparql:
            return _empty()
        if "entityCount" in sparql:
            return _rows({"ec": "3", "sp": "true"})
        if "forType" in sparql:
            return _rows({"pred": ONTO + "location", "cnt": "3", "rel": "0"})
        return _empty()

    mock_neptune.query.side_effect = route

    resp = client.get(
        f"/graphs/{TENANT}/explore/kgs/{KG}/types/Venue/summary", headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["spatially_indexed"] is True
    assert data["temporally_indexed"] is False


def test_summary_accepts_numeric_boolean_lexical_form(client, mock_neptune, auth_headers):
    """"1"^^xsd:boolean is an equally valid true — a backfill writer using the
    numeric lexical form must not silently read as False."""
    from infona_client.api.routes import explore as _explore_mod
    _explore_mod._summary_cache.clear()

    def route(sparql, *a, **k):
        if "?label" in sparql and "subClassOf" in sparql:
            return _rows({"label": "Store"})
        if "attrLabel" in sparql:
            return _empty()
        if "entityCount" in sparql:
            return _rows({"ec": "7", "sp": "1", "tp": "1"})
        if "forType" in sparql:
            return _rows({"pred": ONTO + "location", "cnt": "7", "rel": "0"})
        return _empty()

    mock_neptune.query.side_effect = route

    resp = client.get(
        f"/graphs/{TENANT}/explore/kgs/{KG}/types/Store/summary", headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["spatially_indexed"] is True
    assert resp.json()["temporally_indexed"] is True


def test_summary_computes_flags_on_live_scan_fallback(client, mock_neptune, auth_headers):
    from infona_client.api.routes import explore as _explore_mod
    _explore_mod._summary_cache.clear()

    def route(sparql, *a, **k):
        if "?label" in sparql and "subClassOf" in sparql:
            return _rows({"label": "Session"})
        if "attrLabel" in sparql:
            return _empty()
        if "entityCount" in sparql or "forType" in sparql:
            return _empty()  # stats not materialized → live scan
        if "GROUP BY ?p" in sparql:
            return _rows(
                {"p": RDF_TYPE, "cnt": "2", "rel": "0"},
                {"p": ONTO + "start_date", "cnt": "2", "rel": "0",
                 "geo": "0", "tmp": "2", "sample": "2026-01-01"},
                {"p": ONTO + "end_date", "cnt": "2", "rel": "0",
                 "geo": "0", "tmp": "2", "sample": "2026-01-02"},
            )
        return _empty()

    mock_neptune.query.side_effect = route

    resp = client.get(
        f"/graphs/{TENANT}/explore/kgs/{KG}/types/Session/summary", headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["spatially_indexed"] is False
    assert data["temporally_indexed"] is True

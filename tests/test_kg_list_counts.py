"""`list_kgs` must serve KG rows from stored metadata, not a live scan.

Counting every triple in a KG is a full scan (seconds for a large KG). The
Explorer page calls `/graphs/{tenant}/kgs` on every load, so the count is stored
with the registration and served from it.

**Ported by ONTA-527, and it found a gap.** The registration + count used to
live in the tenant metadata graph, read by one SPARQL query with a
``_live_triple_count`` fallback that persisted what it computed. Registration is
a ``:KnowledgeGraph`` node now (``graph/kg_registry.py``) and
``list_kgs``'s ``neo4j_kg_registry_active()`` branch serves
``coalesce(k.triple_count, 0)`` directly. That kept the "stored count is served
without a scan" half — and dropped the other half entirely: **nothing computes a
triple count on the property-graph path**. ``create_kg`` writes 0 and
``ensure_kg_registered_store`` leaves it alone, so a KG full of data reports
``triple_count: 0`` forever. That is pinned as a strict xfail below rather than
quietly dropped.

The two invalid-name cases are ported to their new mechanism. The old hazard was
IRI interpolation — ``kg_graph_uri`` raising inside ``asyncio.gather`` took down
the whole listing, and ``kg_meta_uri`` would have let a ``>`` close an IRI early
inside an UPDATE. Neither exists on the registry path (names are Cypher
parameters, and the listing mints no IRI), so what survives is the property
those tests protected: one corrupt row must not sink the listing, and must stay
visible rather than being silently swallowed.
"""

import asyncio

import pytest

from infona_client.api.routes.knowledge_graphs import KG_TRIPLE_COUNT
from infona_client.graph.kg_registry import upsert_registered_kg
from infona_client.graph.kg_stats_store import reset_kg_stats_store
from infona_client.graph.queries import InvalidKGName, kg_graph_uri
from infona_client.graph.store import get_graph_store

TENANT = "test-tenant"


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _fresh_kg_stats_store():
    """Isolate the process-wide dashboard-summary store across these tests."""
    reset_kg_stats_store()
    yield
    reset_kg_stats_store()


def _assert_no_scan(mock_neptune):
    mock_neptune.query.assert_not_called()
    mock_neptune.update.assert_not_called()


def test_stored_count_served_without_live_scan(client, mock_neptune, auth_headers):
    _run(upsert_registered_kg(TENANT, "kg-a", description="A", triple_count=218261))

    resp = client.get(f"/graphs/{TENANT}/kgs", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == [
        {
            "name": "kg-a",
            "description": "A",
            "triple_count": 218261,
            "entity_count": 0,
            "edge_count": 0,
            "status": "active",
            "stats_updated_at": None,
            "ai_description": "",
        }
    ]

    # The hot path must not scan anything — this is one registry read.
    _assert_no_scan(mock_neptune)


def test_stored_zero_is_served_without_live_scan(client, mock_neptune, auth_headers):
    """A stored ``0`` is a real value, not "missing" — list_kgs must not rescan.

    This is the sticky-zero trap: create KG → list (stores 0) → ingest → list
    still shows 0. On the SPARQL path the write path's ``invalidate_triple_count``
    was the fix; see the xfail below for what replaced it (nothing).
    """
    _run(upsert_registered_kg(TENANT, "kg-a", description="A", triple_count=0))

    resp = client.get(f"/graphs/{TENANT}/kgs", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()[0]["triple_count"] == 0
    _assert_no_scan(mock_neptune)


@pytest.mark.xfail(
    reason=(
        "BUG (introduced by the Neo4j cutover, surfaced by ONTA-527): no code "
        "path computes a KG's triple count any more, so GET /kgs reports 0 for "
        "a KG full of data. api/routes/knowledge_graphs.py::list_kgs returns "
        "coalesce(k.triple_count, 0) from the :KnowledgeGraph registry row on "
        "its neo4j branch; the only writers of that field are create_kg (always "
        "0) and ensure_kg_registered_store (leaves it NULL → 0). The SPARQL "
        "branch's _live_triple_count fallback + _store_triple_count write-back "
        "is unreachable, and refresh_after_write's invalidate_triple_count now "
        "clears a value nothing will ever recompute. Fix belongs in the "
        "registry: count via the store (entity/assertion counts) on a stats "
        "miss and persist it, the same lazy materialization the SPARQL path had."
    ),
    strict=True,
)
def test_ingested_kg_reports_a_non_zero_triple_count(client, auth_headers):
    from infona_client.graph.iri import IRI_BASE
    from infona_client.graph.kg_writer import insert_facts, refresh_after_write
    from infona_client.graph.ontology_queries import entity_uri

    graph = f"{IRI_BASE}/graphs/{TENANT}/kg/kg-a"
    rdf_type = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"

    async def ingest():
        triples = []
        for i in range(5):
            uri = entity_uri("Widget", f"w{i}")
            triples.append((uri, rdf_type, f"{IRI_BASE}/types/Widget"))
            triples.append((uri, f"{IRI_BASE}/types/Widget/attrs/price", str(i)))
        await insert_facts(None, graph, triples)
        await refresh_after_write(None, tenant_id=TENANT, kg_name="kg-a")

    _run(ingest())

    row = client.get(f"/graphs/{TENANT}/kgs", headers=auth_headers).json()[0]
    assert row["name"] == "kg-a"
    assert row["triple_count"] > 0


def test_invalidate_triple_count_drops_stored_value():
    """Unit-level: the DELETE ``invalidate_triple_count`` emits targets the
    kg_triple_count predicate for that KG URI.

    Kept as-is: ``refresh_after_write`` still calls this helper whenever a
    caller passes a legacy client. It is a no-op in practice now (nothing on the
    registry path reads or writes that predicate) — the xfail above is where
    that dead end is recorded.
    """
    from unittest.mock import AsyncMock

    from infona_client.api.routes.knowledge_graphs import invalidate_triple_count
    from infona_client.graph.queries import kg_meta_uri, tenant_graph_uri

    async def run():
        neptune = AsyncMock()
        await invalidate_triple_count(neptune, TENANT, "kg-a")
        assert neptune.update.await_count == 1
        sparql = neptune.update.await_args.args[0]
        assert KG_TRIPLE_COUNT in sparql
        assert "DELETE" in sparql
        assert kg_meta_uri(TENANT, "kg-a") in sparql
        assert tenant_graph_uri(TENANT) in sparql

    asyncio.run(run())


# A registered name that cannot legally be interpolated into a graph IRI. The
# registry refuses to mint one (``upsert_registered_kg`` raises ValueError), so
# the tests below plant it directly — the same premise the SPARQL version had: a
# corrupt row can already exist, from a pre-validation registration or a writer
# that bypassed the guard.
BAD_NAME = "bad>name"


def _plant_corrupt_registration(name: str = BAD_NAME) -> None:
    # Straight at the store's registry writer, which does not validate — the
    # module-level ``upsert_registered_kg`` is the guard being bypassed, exactly
    # as a pre-validation registration or a rogue writer would.
    _run(
        get_graph_store().kg_registry_upsert(
            TENANT, name, description="broken", triple_count=0
        )
    )


def test_bad_named_kg_is_precondition_for_this_regression():
    """Guard the premise: this name really does make ``kg_graph_uri`` raise.

    If ONTA-414's validation were ever relaxed, the regression test below would
    pass vacuously (no exception to swallow). Assert the hazard exists.
    """
    with pytest.raises(InvalidKGName):
        kg_graph_uri(TENANT, BAD_NAME)


def test_invalid_kg_name_degrades_one_row_not_the_whole_listing(
    client, mock_neptune, auth_headers
):
    """One un-IRI-able registered name must not 422 the entire KG list.

    The mechanism changed (no per-KG IRI is minted while listing, so nothing
    raises inside a ``gather``), but the guarantee is the same one an operator
    depends on: a single corrupt registration must degrade to its own row while
    every other KG lists normally.
    """
    _run(upsert_registered_kg(TENANT, "kg-a", description="A", triple_count=11))
    _run(upsert_registered_kg(TENANT, "kg-c", description="C", triple_count=7))
    _plant_corrupt_registration()

    resp = client.get(f"/graphs/{TENANT}/kgs", headers=auth_headers)
    assert resp.status_code == 200, (
        f"one invalid KG name must not fail the listing; got {resp.status_code} "
        f"{resp.text}"
    )

    by_name = {kg["name"]: kg for kg in resp.json()}
    # Every registration is still listed — including the broken one.
    assert set(by_name) == {"kg-a", BAD_NAME, "kg-c"}
    # The healthy rows keep their real counts...
    assert by_name["kg-a"]["triple_count"] == 11
    assert by_name["kg-c"]["triple_count"] == 7
    # ...and only the un-countable row degrades.
    assert by_name[BAD_NAME]["triple_count"] == 0

    # The bad name never reaches a query builder: the listing is one scoped
    # registry read and no SPARQL at all.
    _assert_no_scan(mock_neptune)


def test_invalid_kg_name_stays_visible_rather_than_being_swallowed(
    client, auth_headers
):
    """A corrupt registration must stay findable.

    The SPARQL version asserted a ``kg_name_invalid_skipped`` warning, because
    degrading to ``triple_count: 0`` was otherwise indistinguishable from an
    empty KG and the log was the only remaining signal. Those two log sites live
    in ``_live_triple_count`` / ``_store_triple_count``, which the registry
    branch never calls, so the warning is gone — what replaces it is that the
    row itself is now surfaced verbatim, under its corrupt name, instead of
    being filtered out of the listing. Pinned here so a future "clean up the
    listing" change cannot make a corrupt row invisible instead of visible.
    """
    _plant_corrupt_registration()

    resp = client.get(f"/graphs/{TENANT}/kgs", headers=auth_headers)
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["name"] for r in rows] == [BAD_NAME]
    assert rows[0]["description"] == "broken"
    assert rows[0]["triple_count"] == 0

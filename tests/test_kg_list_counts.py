"""`list_kgs` must serve triple counts from stored metadata, not a live scan.

Counting every triple in a KG graph is a full scan (seconds for a large KG).
The Explorer page calls `/graphs/{tenant}/kgs` on every load, so the count is
stored alongside the KG registration and read in the same metadata query. KGs
with no stored count yet fall back to a live COUNT(*) — which is then persisted
so the next read is again a single tiny lookup.
"""

import pytest

from infona_client.api.routes.knowledge_graphs import KG_TRIPLE_COUNT
from infona_client.graph.kg_stats_store import reset_kg_stats_store
from infona_client.graph.queries import InvalidKGName, kg_graph_uri

TENANT = "test-tenant"


@pytest.fixture(autouse=True)
def _fresh_kg_stats_store():
    """Isolate the process-wide dashboard-summary store across these tests."""
    reset_kg_stats_store()
    yield
    reset_kg_stats_store()


def _binding(**vals):
    return {k: {"value": v} for k, v in vals.items()}


def _route(*, stored_count: str | None, live_count: str = "999"):
    """Steer the two query shapes list_kgs issues: the metadata list and the
    fallback COUNT(*). `stored_count=None` omits ?count so the fallback fires."""

    def route(sparql, *args, **kwargs):
        if "COUNT(*)" in sparql:
            return {
                "head": {"vars": ["c"]},
                "results": {"bindings": [_binding(c=live_count)]},
            }
        # Dashboard-summary backfill reads (per-KG stats graph): no stats
        # materialized in this test → empty, so the store row stays unset.
        if "entityCount" in sparql or "SUM(?rel)" in sparql or "forType" in sparql:
            return {"head": {"vars": []}, "results": {"bindings": []}}
        # The metadata list query.
        row = {"name": "kg-a", "desc": "A"}
        if stored_count is not None:
            row["count"] = stored_count
        return {
            "head": {"vars": ["name", "desc", "count"]},
            "results": {"bindings": [_binding(**row)]},
        }

    return route


def test_stored_count_served_without_live_scan(client, mock_neptune, auth_headers):
    mock_neptune.query.side_effect = _route(stored_count="218261")

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

    # The hot path must NOT issue a full-graph COUNT(*) when the count is stored.
    queries = [c.args[0] for c in mock_neptune.query.call_args_list if c.args]
    assert not any("COUNT(*)" in q for q in queries), (
        f"stored count should avoid a live scan; queries={queries}"
    )


def test_missing_count_falls_back_to_live_scan_and_persists(
    client, mock_neptune, auth_headers
):
    mock_neptune.query.side_effect = _route(stored_count=None, live_count="42")

    resp = client.get(f"/graphs/{TENANT}/kgs", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()[0]["triple_count"] == 42

    # Fallback path: a live COUNT(*) was issued...
    queries = [c.args[0] for c in mock_neptune.query.call_args_list if c.args]
    assert any("COUNT(*)" in q for q in queries)

    # ...and the freshly computed count was written back for next time.
    updates = [c.args[0] for c in mock_neptune.update.call_args_list if c.args]
    assert any(
        KG_TRIPLE_COUNT in u and "42" in u for u in updates
    ), f"computed count should be persisted; updates={updates}"


def test_stored_zero_is_served_without_live_scan(client, mock_neptune, auth_headers):
    """A stored ``0`` is a real value, not "missing" — list_kgs must not live-scan.

    This is the sticky-zero trap: create KG → list (stores 0) → ingest without
    invalidating → list still shows 0. The write-path fix is that
    ``refresh_after_write`` drops the stored count; this test pins that a
    *present* zero is still served as zero (no accidental fallback that would
    mask the bug by always recounting).
    """
    mock_neptune.query.side_effect = _route(stored_count="0", live_count="999")

    resp = client.get(f"/graphs/{TENANT}/kgs", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()[0]["triple_count"] == 0

    queries = [c.args[0] for c in mock_neptune.query.call_args_list if c.args]
    assert not any("COUNT(*)" in q for q in queries), (
        f"stored zero must not trigger a live scan; queries={queries}"
    )


def test_invalidate_triple_count_drops_stored_value():
    """After invalidation the next list_kgs path sees no stored count.

    Unit-level: the DELETE emitted by ``invalidate_triple_count`` targets the
    kg_triple_count predicate for that KG URI. Integration of
    refresh_after_write → invalidate is covered in test_kg_writer.
    """
    import asyncio
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


# A registered name that cannot legally be interpolated into a graph IRI. Both KG
# registration paths validate (``KGCreate.name``'s pattern and
# ``ensure_kg_registered``'s ``is_valid_kg_name`` branch) — but that does NOT make
# this unreachable: ``POST /graphs/{tenant}/triples`` writes arbitrary triples via
# ``insert_triples`` into ``tenant_graph_uri``, the same base graph ``list_kgs``
# reads registrations from, and SPARQL literal escaping leaves ``>`` intact. A
# pre-ONTA-414 registration is the other arrival vector.
BAD_NAME = "bad>name"


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

    ``list_kgs`` fans ``_live_triple_count`` out over every KG missing a stored
    count under ``asyncio.gather``, and ONTA-414 made ``kg_graph_uri`` raise
    ``InvalidKGName`` (mapped to 422 app-wide). With the URI minted outside the
    per-KG guard, a single bad registration took down the whole workspace's
    Explorer KG list. It must degrade to ``triple_count: 0`` for that row while
    every other KG still lists normally.
    """
    rows = [
        {"name": "kg-a", "desc": "A"},  # no stored count → live fallback
        {"name": BAD_NAME, "desc": "broken"},  # no stored count → would raise
        {"name": "kg-c", "desc": "C", "count": "7"},  # stored count
    ]

    def route(sparql, *args, **kwargs):
        if "COUNT(*)" in sparql:
            return {
                "head": {"vars": ["c"]},
                "results": {"bindings": [_binding(c="11")]},
            }
        if "entityCount" in sparql or "SUM(?rel)" in sparql or "forType" in sparql:
            return {"head": {"vars": []}, "results": {"bindings": []}}
        return {
            "head": {"vars": ["name", "desc", "count"]},
            "results": {"bindings": [_binding(**r) for r in rows]},
        }

    mock_neptune.query.side_effect = route

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

    # The bad name must never reach Neptune inside an IRI: it fails closed
    # before any SPARQL is built, so no query carries it.
    queries = [c.args[0] for c in mock_neptune.query.call_args_list if c.args]
    assert not any(BAD_NAME in q for q in queries), (
        f"invalid name must not be interpolated into SPARQL; queries={queries}"
    )

    # Same for the count store-back. Degrading the row above makes
    # `_store_triple_count` REACHABLE with the bad name (before the fix the
    # request died in `gather` first), and `kg_meta_uri` — unlike
    # `kg_graph_uri` — does NOT validate. Its `is_valid_kg_name` guard must
    # keep a `>`-bearing name out of the metadata graph's UPDATE, where it
    # would close the IRI early and inject SPARQL.
    updates = [c.args[0] for c in mock_neptune.update.call_args_list if c.args]
    assert not any(BAD_NAME in u for u in updates), (
        f"invalid name must not be interpolated into an UPDATE; updates={updates}"
    )
    # The healthy live-fallback row still gets persisted — the guard is
    # name-scoped, it does not suppress the whole store-back pass.
    assert any(KG_TRIPLE_COUNT in u and "11" in u for u in updates), (
        f"healthy computed count should still persist; updates={updates}"
    )


def test_invalid_kg_name_is_logged_not_silently_zeroed(
    client, mock_neptune, auth_headers
):
    """A corrupt registration must stay findable after we stop 422-ing on it.

    Degrading to ``triple_count: 0`` is indistinguishable from a legitimately
    empty KG, so the only remaining signal that a row is corrupt is the log line.
    Before the listing fix the corruption was loud (a 422 on every Explorer
    load); without this assertion the fix would trade a broken page for silence.
    """
    rows = [{"name": BAD_NAME, "desc": "broken"}]

    def route(sparql, *args, **kwargs):
        if "entityCount" in sparql or "SUM(?rel)" in sparql or "forType" in sparql:
            return {"head": {"vars": []}, "results": {"bindings": []}}
        return {
            "head": {"vars": ["name", "desc"]},
            "results": {"bindings": [_binding(**r) for r in rows]},
        }

    mock_neptune.query.side_effect = route

    # structlog writes to stdout rather than through stdlib logging, so `caplog`
    # sees nothing here — capture via structlog's own testing hook.
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        resp = client.get(f"/graphs/{TENANT}/kgs", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()[0]["triple_count"] == 0

    skipped = [e for e in logs if e.get("event") == "kg_name_invalid_skipped"]
    assert skipped, (
        f"corrupt registration must be logged, not silently zeroed; logs={logs}"
    )
    # The line must name the offending KG so an operator can locate the row.
    assert all(e.get("kg_name") == BAD_NAME for e in skipped), skipped
    assert {e.get("log_level") for e in skipped} == {"warning"}, skipped
    # Assert on `op` so this can't pass with only ONE of the two guards intact:
    # the count fallback and the store-back are separate skips and both must
    # report. Without this, reverting just the `_live_triple_count` pre-check
    # would still leave `_store_triple_count`'s log and look green.
    assert {e.get("op") for e in skipped} == {
        "live_triple_count",
        "store_triple_count",
    }, f"both skip sites must log; got {[e.get('op') for e in skipped]}"

"""Deleting a KG must invalidate its precomputed type-stats.

The stats row and the in-memory summary cache are both keyed by KG name, so a
KG recreated under the same name would serve the deleted graph's stale counts
unless delete busts them. Regression test for that bug (seen while recording the
live ER demo: a re-ingested `demo-live` showed the prior run's post-resolution
count instead of the fresh fragmented count).

**Ported by ONTA-527, and it found a regression.** ``delete_kg`` used to run a
long best-effort cleanup: ``DROP SILENT GRAPH`` for the stats + drift graphs,
``drop_kg_stats`` (which evicts ``explore._summary_cache`` and the durable stats
row), the example-bank purge, the spatio-temporal / semantic index clears, the
NL-planning cache eviction, ``invalidate_kg_status`` and the reconcile-schedule
removal. The property-graph branch
(``api/routes/knowledge_graphs.py::delete_kg``, the ``neo4j_kg_registry_active()``
early-return) does THREE things — drop the registry row, DETACH DELETE the
instance nodes, delete the durable stats row — and then ``return``s before all of
the above. Only the durable-stats half survives, so the tests below split into
what still holds (row deleted) and what silently does not (every cache).

The cache evictions are pinned as strict xfails rather than deleted: each names
a stale-read that is now reachable in production.

One thing this file cannot check hermetically: the instance-node purge runs via
``store._run(...)``, which ``MemoryGraphStore`` does not implement, so the branch
is a no-op here and exercised only against a real Neo4j.
"""
import asyncio
import time

import pytest

from infona_client.api.routes.explore import _summary_cache
from infona_client.graph.kg_registry import list_registered_kgs, upsert_registered_kg
from infona_client.graph.kg_stats_store import (
    KgStats,
    get_kg_stats_store,
    reset_kg_stats_store,
)

TENANT = "test-tenant"
KG = "demo-live"


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _fresh_stats_store():
    reset_kg_stats_store()
    _summary_cache.clear()
    yield
    reset_kg_stats_store()
    _summary_cache.clear()


def _register_kg_with_stats():
    _run(upsert_registered_kg(TENANT, KG, description="live demo"))
    _run(
        get_kg_stats_store().upsert(
            KgStats(tenant_id=TENANT, kg_name=KG, entity_count=43, edge_count=7)
        )
    )


def test_delete_kg_drops_the_registration_and_the_durable_stats_row(
    client, mock_neptune, auth_headers
):
    """The half of the old cleanup that survived the cutover.

    ``DROP SILENT GRAPH <…/stats>`` had no property-graph equivalent — the
    materialized dashboard-summary ROW is what carries those counts now, and
    deleting the KG must take it with the registration.
    """
    _register_kg_with_stats()
    assert [e["name"] for e in _run(list_registered_kgs(TENANT))] == [KG]

    resp = client.delete(f"/graphs/{TENANT}/kgs/{KG}", headers=auth_headers)
    assert resp.status_code == 200

    assert _run(list_registered_kgs(TENANT)) == []
    assert _run(get_kg_stats_store().get(TENANT, KG)) is None
    # No SPARQL was emitted for any of it.
    mock_neptune.update.assert_not_called()


@pytest.mark.xfail(
    reason=(
        "BUG (introduced by the Neo4j cutover, surfaced by ONTA-527): "
        "api/routes/knowledge_graphs.py::delete_kg returns from its "
        "neo4j_kg_registry_active() branch without calling "
        "explore.drop_kg_stats, so the in-process _summary_cache entries for "
        "the deleted KG survive their full 30-minute TTL. A KG recreated under "
        "the same name (the live-demo flow this test was written for) serves "
        "the DELETED graph's per-type counts from that cache. The durable "
        "stats row is deleted directly on the same branch, which is why only "
        "the cache half fails."
    ),
    strict=True,
)
def test_delete_kg_busts_the_summary_cache(client, auth_headers):
    _register_kg_with_stats()
    cache_key = (TENANT, KG, "Person")
    _summary_cache[cache_key] = (time.monotonic(), {"entity_count": 43})

    assert (
        client.delete(f"/graphs/{TENANT}/kgs/{KG}", headers=auth_headers).status_code
        == 200
    )

    assert cache_key not in _summary_cache


@pytest.mark.xfail(
    reason=(
        "BUG (ONTA-453 regression, introduced by the Neo4j cutover): "
        "api/routes/knowledge_graphs.py::delete_kg's neo4j branch never calls "
        "graph/kg_status.py::invalidate_kg_status, so the POSITIVE "
        "'this KG holds data' verdict stays cached for KG_STATUS_CACHE_TTL "
        "(60s) after the KG is deleted. kg_data_status short-circuits on that "
        "verdict, so a question asked in the minute after a delete sails past "
        "the missing-KG guard and is answered out of the tenant base graph plus "
        "the global layers — the confidently-wrong answer that guard exists to "
        "stop. Every other eviction on the old delete path went the same way "
        "(example bank, spatio-temporal + semantic index clears, NL ontology "
        "cache, reconcile schedule); this one is pinned because it changes an "
        "ANSWER, not just a number."
    ),
    strict=True,
)
def test_delete_kg_busts_the_kg_status_verdict_cache(client, auth_headers):
    from infona_client.graph import kg_status

    _register_kg_with_stats()
    kg_status.invalidate_kg_status(TENANT)
    kg_status._kg_ok_cache[(TENANT, KG)] = time.time()

    assert (
        client.delete(f"/graphs/{TENANT}/kgs/{KG}", headers=auth_headers).status_code
        == 200
    )

    assert (TENANT, KG) not in kg_status._kg_ok_cache


@pytest.mark.xfail(
    reason=(
        "Same root cause as test_delete_kg_busts_the_summary_cache (delete_kg's "
        "neo4j branch skips explore.drop_kg_stats), pinned end-to-end because "
        "this is the shape the bug was originally reported in: delete + "
        "re-create under the same name, then read the type summary. The read "
        "half — explore.get_type_summary — is still un-ported SPARQL, so the "
        "recomputed number comes from the mocked client here; the cache bust in "
        "front of it is real code that runs before any query and is what fails."
    ),
    strict=True,
)
def test_recreated_kg_reports_fresh_count_not_stale_cache(
    client, mock_neptune, auth_headers
):
    """End-to-end: delete + recreate under the same name → endpoint shows the
    NEW contents, never the deleted KG's cached count.

    This is the exact live-demo scenario: a prior run left Person cached at the
    post-ER count (43); deleting and re-ingesting the same 3 CSVs must surface
    the fresh, pre-ER count (162) through the real summary endpoint.
    """
    from infona_client.api.routes.explore import RDF_TYPE

    def _summary_query_router(person_count: int):
        def route(sparql, *args, **kwargs):
            if "entityCount" in sparql or "forType" in sparql:
                return {"head": {"vars": []}, "results": {"bindings": []}}
            if "?e ?p ?o" in sparql:
                return {
                    "head": {"vars": ["p", "cnt", "sample", "rel"]},
                    "results": {
                        "bindings": [
                            {
                                "p": {"value": RDF_TYPE},
                                "cnt": {"value": str(person_count)},
                                "rel": {"value": "0"},
                            },
                        ]
                    },
                }
            return {"head": {"vars": []}, "results": {"bindings": []}}

        return route

    _register_kg_with_stats()
    summary_url = f"/graphs/{TENANT}/explore/kgs/{KG}/types/Person/summary"
    cache_key = (TENANT, KG, "Person")

    # A prior read warmed the hot cache with the stale post-ER count.
    _summary_cache[cache_key] = (time.monotonic(), {"entity_count": 43})

    # Drop the KG (delete must bust the stale entry)...
    assert (
        client.delete(f"/graphs/{TENANT}/kgs/{KG}", headers=auth_headers).status_code
        == 200
    )
    assert cache_key not in _summary_cache

    # ...then re-ingest the same name: the new KG has 162 Person rows (no ER
    # yet) and no materialized stats, so the endpoint live-scans to 162.
    mock_neptune.query.side_effect = _summary_query_router(162)
    resp = client.get(summary_url, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["entity_count"] == 162  # fresh, not the stale 43

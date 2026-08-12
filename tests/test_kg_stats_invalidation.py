"""Deleting a KG must invalidate its precomputed type-stats.

The stats row and the in-memory summary cache are both keyed by KG name, so a
KG recreated under the same name would serve the deleted graph's stale counts
unless delete busts them. Regression test for that bug (seen while recording the
live ER demo: a re-ingested `demo-live` showed the prior run's post-resolution
count instead of the fresh fragmented count).

**Fixed by ONTA-532.** ONTA-527 ported these tests and found that the Neo4j
branch of ``delete_kg`` early-returned after registry / DETACH / durable-stats
and skipped every derived-state eviction (``drop_kg_stats`` / summary cache,
example bank, spatiotemporal + semantic clears, NL cache, ``invalidate_kg_status``,
reconcile schedule). ONTA-532 hoists that shared cleanup so both backends run it;
the strict xfails below are now live assertions.

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


def test_delete_kg_busts_the_summary_cache(client, auth_headers):
    _register_kg_with_stats()
    cache_key = (TENANT, KG, "Person")
    _summary_cache[cache_key] = (time.monotonic(), {"entity_count": 43})

    assert (
        client.delete(f"/graphs/{TENANT}/kgs/{KG}", headers=auth_headers).status_code
        == 200
    )

    assert cache_key not in _summary_cache


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


def test_recreated_kg_reports_fresh_count_not_stale_cache(
    client, mock_neptune, auth_headers
):
    """End-to-end: delete + recreate under the same name → endpoint shows the
    NEW contents, never the deleted KG's cached count.

    Live-demo shape (ONTA-532): a prior run left Person cached at a post-ER
    count (43); deleting and re-ingesting under the same name must surface the
    fresh count through the real summary endpoint, not the deleted graph's
    cached 43. Neo4j path uses GraphStore (P-A1a), so we re-seed instances via
    insert_facts rather than a SPARQL mock.
    """
    from infona_client.graph.iri import IRI_BASE
    from infona_client.graph.kg_writer import insert_facts
    from infona_client.graph.ontology_queries import entity_uri
    from infona_client.graph.queries import kg_graph_uri
    from infona_client.graph.store import get_graph_store

    RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
    PERSON_TYPE = f"{IRI_BASE}/types/Person"
    FRESH_COUNT = 5  # post-recreate; deliberately != stale 43

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

    # ...then re-register under the same name and seed FRESH_COUNT Person rows.
    _run(upsert_registered_kg(TENANT, KG, description="live demo recreated"))
    graph = kg_graph_uri(TENANT, KG)
    triples = [
        (entity_uri("Person", f"p{i}"), RDF_TYPE, PERSON_TYPE)
        for i in range(FRESH_COUNT)
    ]
    _run(insert_facts(None, graph, triples, store=get_graph_store()))

    resp = client.get(summary_url, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["entity_count"] == FRESH_COUNT  # fresh, not the stale 43

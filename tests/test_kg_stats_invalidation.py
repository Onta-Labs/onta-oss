"""Deleting a KG must invalidate its precomputed type-stats.

The stats graph URI and the in-memory summary cache are both keyed by KG name,
so a KG recreated under the same name would serve the deleted graph's stale
counts unless delete busts them. Regression test for that bug (seen while
recording the live ER demo: a re-ingested `demo-live` showed the prior run's
post-resolution count instead of the fresh fragmented count).
"""
import time

from cograph_client.api.routes.explore import (
    RDF_TYPE,
    _stats_graph_uri,
    _summary_cache,
)

TENANT = "test-tenant"
KG = "demo-live"


def test_delete_kg_drops_stats_graph_and_busts_cache(client, mock_neptune, auth_headers):
    stats_uri = _stats_graph_uri(TENANT, KG)

    # Seed an in-memory summary as if a prior read had warmed the cache.
    cache_key = (TENANT, KG, "Person")
    _summary_cache[cache_key] = (0.0, {"entity_count": 43})
    assert cache_key in _summary_cache

    resp = client.delete(f"/graphs/{TENANT}/kgs/{KG}", headers=auth_headers)
    assert resp.status_code == 200

    # The stats graph must have been dropped (not just the data graph).
    updates = [c.args[0] for c in mock_neptune.update.call_args_list if c.args]
    assert any(
        "DROP SILENT GRAPH" in u and stats_uri in u for u in updates
    ), f"stats graph {stats_uri} was never dropped; updates={updates}"

    # And the hot cache entry must be gone.
    assert cache_key not in _summary_cache


def _summary_query_router(person_count: int):
    """Route the summary endpoint's reads so a fresh KG live-scans to N Persons.

    The endpoint issues several queries; we only need to steer two: the
    precomputed-stats lookups return empty (no stats materialized yet, as on a
    fresh ingest) so the endpoint falls back to a live instance scan, whose
    ``rdf:type`` row carries the entity count.
    """
    def route(sparql, *args, **kwargs):
        if "entityCount" in sparql or "forType" in sparql:
            # No precomputed stats → force the live-scan fallback.
            return {"head": {"vars": []}, "results": {"bindings": []}}
        if "?e ?p ?o" in sparql:
            # Live instance scan: the rdf:type row's count is the entity count.
            return {
                "head": {"vars": ["p", "cnt", "sample", "rel"]},
                "results": {"bindings": [
                    {"p": {"value": RDF_TYPE},
                     "cnt": {"value": str(person_count)},
                     "rel": {"value": "0"}},
                ]},
            }
        # Ontology + attribute-definition lookups: irrelevant to the count.
        return {"head": {"vars": []}, "results": {"bindings": []}}

    return route


def test_recreated_kg_reports_fresh_count_not_stale_cache(client, mock_neptune, auth_headers):
    """End-to-end: delete + recreate under the same name → endpoint shows the
    NEW contents, never the deleted KG's cached count.

    This is the exact live-demo scenario: a prior run left Person cached at the
    post-ER count (43); deleting and re-ingesting the same 3 CSVs must surface
    the fresh, pre-ER count (162) through the real summary endpoint.
    """
    summary_url = f"/graphs/{TENANT}/explore/kgs/{KG}/types/Person/summary"
    cache_key = (TENANT, KG, "Person")
    try:
        # A prior read warmed the hot cache with the stale post-ER count.
        _summary_cache[cache_key] = (time.monotonic(), {"entity_count": 43})

        # Drop the KG (delete must bust the stale entry)...
        assert client.delete(f"/graphs/{TENANT}/kgs/{KG}", headers=auth_headers).status_code == 200
        assert cache_key not in _summary_cache

        # ...then re-ingest the same name: the new KG has 162 Person rows (no ER
        # yet) and no materialized stats, so the endpoint live-scans to 162.
        mock_neptune.query.side_effect = _summary_query_router(162)
        resp = client.get(summary_url, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["entity_count"] == 162  # fresh, not the stale 43
    finally:
        _summary_cache.pop(cache_key, None)

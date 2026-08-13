"""Explorer type-summary cache must die even when SPARQL recompute is retired.

Regression for job c7c2c7d2: ClinicalTrial enrich wrote 25 fills, Explorer
refresh still showed the old table because ``recompute_kg_stats`` SPARQL-died
on ``SparqlClientRetired`` *before* evicting ``_summary_cache`` (30 min TTL),
and ``_safe_recompute`` swallowed the error.
"""

from __future__ import annotations

import asyncio
import time

from infona_client.api.routes import explore
from infona_client.graph.client import NeptuneClient


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_invalidate_summary_cache_drops_only_that_kg():
    explore._summary_cache.clear()
    explore._summary_cache[("gt-demo", "label-compliance", "ClinicalTrial")] = (
        time.monotonic(),
        {"attributes": [{"name": "nct_id"}]},
    )
    explore._summary_cache[("gt-demo", "other-kg", "ClinicalTrial")] = (
        time.monotonic(),
        {"attributes": [{"name": "keep"}]},
    )
    n = explore.invalidate_summary_cache("gt-demo", "label-compliance")
    assert n == 1
    assert ("gt-demo", "label-compliance", "ClinicalTrial") not in explore._summary_cache
    assert ("gt-demo", "other-kg", "ClinicalTrial") in explore._summary_cache
    explore._summary_cache.clear()


def test_schedule_recompute_evicts_cache_before_sparql_scan():
    """The sync schedule path must bust the cache even if no event loop task runs."""
    explore._summary_cache.clear()
    key = ("gt-demo", "label-compliance", "ClinicalTrial")
    explore._summary_cache[key] = (
        time.monotonic(),
        {"attributes": [{"name": "nct_id"}]},
    )
    client = NeptuneClient(endpoint="http://127.0.0.1:9")
    try:
        explore.schedule_recompute(client, "gt-demo", "label-compliance")
    except RuntimeError:
        # No running loop — eviction must still have happened.
        pass
    assert key not in explore._summary_cache
    explore._summary_cache.clear()


def test_recompute_kg_stats_on_retired_client_evicts_and_skips_sparql():
    explore._summary_cache.clear()
    key = ("gt-demo", "label-compliance", "ClinicalTrial")
    explore._summary_cache[key] = (
        time.monotonic(),
        {"attributes": [{"name": "nct_id"}]},
    )
    client = NeptuneClient(endpoint="http://127.0.0.1:9")

    async def _go():
        return await explore.recompute_kg_stats(client, "gt-demo", "label-compliance")

    out = _run(_go())
    assert out["backend"] == "graphstore"
    assert out["cache_evicted"] is True
    assert key not in explore._summary_cache
    explore._summary_cache.clear()

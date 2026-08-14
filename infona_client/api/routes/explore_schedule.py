"""Drop / schedule / backfill for Explorer type-stats.

``schedule_recompute`` evicts ``_summary_cache`` first, then coalesces
the background scan per (tenant, kg). Uses the shared objects in
:mod:`explore_state` — do not rebind those names here.

``_safe_recompute`` looks up ``recompute_kg_stats`` on the public
``explore`` module so tests that patch it keep working.
"""

from __future__ import annotations

import asyncio
from typing import Any

from infona_client.api.routes.explore_common import (
    _SUMMARY_BACKFILL_CONCURRENCY,
    _drift_history_graph_uri,
    _host,
    _stats_graph_uri,
    logger,
)
from infona_client.api.routes.explore_resolve import invalidate_summary_cache
from infona_client.api.routes.explore_state import (
    _bg_tasks,
    _recompute_inflight,
    _recompute_pending,
)


async def drop_kg_stats(client: Any, tenant_id: str, kg_name: str) -> None:
    """Drop a KG's precomputed stats and evict its in-memory summaries.

    Called when a KG is deleted. The stats graph URI is derived from the KG
    name, so without this a KG later recreated under the same name would serve
    the deleted graph's stale counts until the next recompute lands.

    Backend-aware (ONTA-532): the SPARQL named-graph DROP only runs when the
    Neo4j registry is inactive. Neo4j has no per-KG stats/drift named graphs —
    the durable dashboard-summary row + the in-process ``_summary_cache`` are
    what carry those counts, and those are always cleared below. Calling this
    from the Neo4j delete path must not emit SPARQL.
    """
    from infona_client.graph.kg_registry import neo4j_kg_registry_active

    if not neo4j_kg_registry_active():
        stats = _stats_graph_uri(tenant_id, kg_name)
        hist = _drift_history_graph_uri(tenant_id, kg_name)
        # Drop the drift-history graph too (COG-57): its URI is derived from the KG
        # name, so a KG recreated under the same name would otherwise inherit the
        # deleted KG's distribution. Matches the stats-graph cleanup rationale above.
        await client.update(f"DROP SILENT GRAPH <{stats}> ; DROP SILENT GRAPH <{hist}>")
    invalidate_summary_cache(tenant_id, kg_name)
    # Drop the materialized dashboard-summary row too — its key is derived from
    # the KG name, so a KG recreated under the same name would otherwise inherit
    # the deleted KG's counts. Best-effort, matching the cache eviction above.
    try:
        from infona_client.graph.kg_stats_store import get_kg_stats_store

        await get_kg_stats_store().delete(tenant_id, kg_name)
    except Exception:  # noqa: BLE001
        logger.warning("kg_stats_store_delete_failed", kg=kg_name, exc_info=True)


async def _safe_recompute(client: Any, tenant_id: str, kg_name: str) -> None:
    try:
        await _host().recompute_kg_stats(client, tenant_id, kg_name)
    except Exception:
        # Cache was already evicted at the top of recompute_kg_stats (and
        # again in schedule_recompute). Log the scan failure — do not hide
        # another SparqlClientRetired the way job c7c2c7d2 did.
        logger.warning(
            "recompute_kg_stats_failed",
            tenant_id=tenant_id,
            kg_name=kg_name,
            exc_info=True,
        )


def schedule_recompute(client: Any, tenant_id: str, kg_name: str) -> None:
    """Fire-and-forget a stats recompute (used by the endpoint + ingest hook).

    Coalesced per (tenant, KG): while a scan is in flight, further requests do
    not stack up N whole-KG scans, but they are not dropped either. They mark
    the KG pending, and exactly one follow-up scan runs when the current one
    finishes. Dropping them would be a correctness bug, not just a lost
    refresh: the concurrent-batch ingest path (``refresh_after_write`` per
    ``POST /ingest/csv/rows``) and the ``POST /recompute-stats`` that the CLI
    fires right after the last batch both land inside the ~15s scan, so the
    last writer's numbers would never be persisted.

    Evicts ``_summary_cache`` *synchronously* before the background scan so a
    refresh that lands between write and scan completion cannot serve the
    pre-write Explorer table. On Neo4j the scan itself is a no-op; eviction
    is the product-visible recompute.
    """
    invalidate_summary_cache(tenant_id, kg_name)
    key = (tenant_id, kg_name)
    if key in _recompute_inflight:
        # Defer, don't discard: the running scan may predate this caller's write.
        _recompute_pending.add(key)
        return
    _recompute_inflight.add(key)
    # Look up on the public explore module so tests that patch
    # ``explore._safe_recompute`` (the coalescing suite) still bind.
    coro = _host()._safe_recompute(client, tenant_id, kg_name)
    try:
        task = asyncio.create_task(coro)
    except Exception:
        # No running loop (a sync caller): never leave the key stuck marked
        # in-flight, or this KG could never be recomputed again.
        coro.close()
        _recompute_inflight.discard(key)
        raise
    _bg_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _bg_tasks.discard(t)
        # Order matters: clear the in-flight marker BEFORE re-scheduling, or the
        # re-schedule below would see itself as in-flight and fall into the
        # pending branch, losing the deferred request for good. Done callbacks
        # run on the event loop with no await points between these statements,
        # so no request can slip in mid-sequence.
        _recompute_inflight.discard(key)
        if key in _recompute_pending:
            _recompute_pending.discard(key)
            try:
                schedule_recompute(client, tenant_id, kg_name)
            except Exception:  # noqa: BLE001, best-effort; never raise into the loop
                logger.warning(
                    "recompute_rerun_schedule_failed", kg=kg_name, exc_info=True
                )

    task.add_done_callback(_done)


def schedule_summary_backfill(rows: list) -> None:
    """Fire-and-forget generation of any missing one-line KG summaries.

    Kept OFF the ``list_kgs`` hot path (the file's stated rule: never compute the
    summary live on a request) — the descriptions are persisted by the background
    task and appear on the NEXT list. Recompute fills them at write time going
    forward, so this only covers rows that predate the feature. No-op when
    nothing is pending.
    """
    pending = [r for r in rows if not r.ai_description and r.type_breakdown]
    if not pending:
        return
    task = asyncio.create_task(_host()._run_summary_backfill(pending))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _run_summary_backfill(pending: list) -> None:
    """Generate + persist summaries for the given stats rows (bounded fan-out).

    Best-effort throughout: a generation miss or store hiccup just leaves the
    line blank for the next sweep. Concurrency is capped so a big tenant can't
    fire one LLM call per KG simultaneously."""
    from infona_client.graph.kg_stats_store import get_kg_stats_store
    from infona_client.graph.kg_summary import generate_kg_summary

    sem = asyncio.Semaphore(_SUMMARY_BACKFILL_CONCURRENCY)

    async def _one(row):
        async with sem:
            return await generate_kg_summary(row.kg_name, row.type_breakdown)

    summaries = await asyncio.gather(
        *(_one(r) for r in pending), return_exceptions=True
    )
    store = get_kg_stats_store()
    for row, desc in zip(pending, summaries):
        if isinstance(desc, str) and desc:
            row.ai_description = desc
            row.ai_description_types = sorted(row.type_breakdown)
            try:
                await store.upsert(row)
            except Exception:  # noqa: BLE001 — persistence is a cache warm, not required
                logger.warning("kg_summary_backfill_upsert_failed", kg=row.kg_name)


"""Schedule-row management and runner dispatch for the semantic reconciler.

Mutable process state (``_hook_store``, ``_ensured_reconcile``, ``_bg_tasks``)
lives on the public facade so ``reset_for_tests`` and hook memos stay
patchable. ``reconcile_kg`` / ``run_embed_fill_sweep`` are looked up via
``_host()`` so tests that replace them on the facade still fire.
"""

from __future__ import annotations

import asyncio
from typing import Any

from infona_client.semantic.reconciler_common import _host
from infona_client.semantic.reconciler_const import (
    EMBED_FILL_SCHEDULE_ID,
    SEMANTIC_EMBED_FILL_ACTION,
    SEMANTIC_RECONCILE_ACTION,
    _GLOBAL_KG,
    _SYSTEM_TENANT,
)
from infona_client.semantic.reconciler_env import (
    _ensure_memo_ttl_s,
    _now,
    embed_fill_interval_s,
    reconcile_interval_s,
    semantic_index_enabled,
)


def reconcile_schedule_id(tenant_id: str, kg_name: str) -> str:
    """Deterministic per-KG reconcile schedule id (idempotent ensure/remove)."""
    return f"semantic-reconcile:{tenant_id}:{kg_name}"


async def ensure_embed_fill_schedule(store: Any):
    """Idempotently ensure the single global embed-fill schedule row.

    Called from app startup when the feature is enabled. ``get``-then-``create``
    (not blind create) so a restart never resets a live row's ``next_run``;
    a changed cadence knob is applied in place. Races between replicas converge
    because the id is deterministic and the durable store upserts by id.
    """
    from infona_client.enrichment.models import JobCategory
    from infona_client.scheduling.models import Schedule

    h = _host()
    interval = embed_fill_interval_s()
    existing = await store.get(EMBED_FILL_SCHEDULE_ID)
    if existing is not None:
        if existing.interval_seconds != interval:
            existing.interval_seconds = interval
            existing.cron = None
            await store.update(existing)
            h.logger.info(
                "semantic_embed_fill_schedule_retuned", interval_seconds=interval
            )
        return existing
    now = _now()
    schedule = Schedule(
        id=EMBED_FILL_SCHEDULE_ID,
        tenant_id=_SYSTEM_TENANT,
        kg_name=_GLOBAL_KG,
        category=JobCategory.reconciliation,
        action=SEMANTIC_EMBED_FILL_ACTION,
        interval_seconds=interval,
        enabled=True,
        next_run=now,  # first sweep fires on the next runner poll
        created_at=now,
    )
    await store.create(schedule)
    h.logger.info("semantic_embed_fill_schedule_created", interval_seconds=interval)
    return schedule


async def ensure_reconcile_schedule(
    store: Any, tenant_id: str, kg_name: str, *, due_now: bool = False
):
    """Idempotently ensure the per-KG reconcile schedule row.

    A fresh row is seeded ``next_run=now`` — the FIRST reconcile of a KG is the
    backfill, so it should fire on the next runner poll, then settle onto the
    hourly cadence. ``due_now=True`` (the on-demand reindex route) pulls an
    EXISTING row's ``next_run`` forward to now instead of waiting out the hour.
    """
    from infona_client.enrichment.models import JobCategory
    from infona_client.scheduling.models import Schedule

    h = _host()
    sid = reconcile_schedule_id(tenant_id, kg_name)
    now = _now()
    existing = await store.get(sid)
    if existing is not None:
        if due_now and (existing.next_run is None or existing.next_run > now):
            existing.next_run = now
            existing.enabled = True
            await store.update(existing)
            h.logger.info(
                "semantic_reconcile_pulled_forward",
                tenant_id=tenant_id,
                kg_name=kg_name,
            )
        return existing
    schedule = Schedule(
        id=sid,
        tenant_id=tenant_id,
        kg_name=kg_name,
        category=JobCategory.reconciliation,
        action=SEMANTIC_RECONCILE_ACTION,
        interval_seconds=reconcile_interval_s(),
        enabled=True,
        next_run=now,  # first run = the backfill
        created_at=now,
    )
    await store.create(schedule)
    h.logger.info(
        "semantic_reconcile_schedule_created", tenant_id=tenant_id, kg_name=kg_name
    )
    return schedule


async def remove_reconcile_schedule(store: Any, tenant_id: str, kg_name: str) -> None:
    """Drop the per-KG reconcile row (the KG-delete path) + the hook's memo, so
    a same-named KG recreated later in this process re-ensures a fresh row."""
    await store.delete(reconcile_schedule_id(tenant_id, kg_name))
    _host()._ensured_reconcile.pop((tenant_id, kg_name), None)


async def ensure_reconcile_schedule_from_hook(tenant_id: str, kg_name: str) -> None:
    """Best-effort, TTL-memoized ensure used by ``kg_writer._index_semantic``.

    This is how a KG that receives writes while the feature is enabled gets its
    recurring reconcile row without any operator action (already-ingested,
    write-quiet KGs use the reindex route instead). Exceptions propagate to the
    hook's catch-all (the memo is only set on success, so the next write
    retries).

    **Deleting the auto-created schedule row is NOT a durable opt-out.** As
    long as the feature gate (``INFONA_SEMANTIC_INDEX_ENABLED``) is on and the
    KG keeps receiving writes, this hook resurrects a deleted
    ``semantic-reconcile:{tenant}:{kg}`` row within one memo TTL
    (``INFONA_SEMANTIC_ENSURE_MEMO_TTL_S``, default 600s) — by design, so a
    stray CRUD delete can't silently disable correctness maintenance for a
    live KG. The durable off-switch is the env gate itself (flip it off and
    stale rows become logged no-ops — see :func:`dispatch_semantic_schedule`).
    """
    h = _host()
    key = (tenant_id, kg_name)
    deadline = h._ensured_reconcile.get(key)
    if deadline is not None and h._now_monotonic() < deadline:
        return
    if h._hook_store is None:
        from infona_client.scheduling.store import make_schedule_store

        h._hook_store = make_schedule_store()
    await ensure_reconcile_schedule(h._hook_store, tenant_id, kg_name)
    h._ensured_reconcile[key] = h._now_monotonic() + _ensure_memo_ttl_s()


def reset_for_tests() -> None:
    """Test hook: restore pristine module state between tests."""
    h = _host()
    h._hook_store = None
    h._ensured_reconcile.clear()
    h._bg_tasks.clear()


async def dispatch_semantic_schedule(schedule: Any, *, client: Any) -> None:
    """Route a claimed semantic Schedule row to its duty.

    Called from ``api.routes.actions.dispatch_scheduled_action`` (the same
    dispatch seam every other scheduled action uses, so claim exclusivity is
    inherited from the runner). Deliberately creates NO job rows — a 5-minute
    sweep would flood the unified Jobs feed; the structlog counters emitted by
    the duties are the observability surface. Gated on the master env knob so
    stale rows left over from a disable are cheap no-ops, not surprise spend.
    """
    h = _host()
    if not semantic_index_enabled():
        h.logger.info(
            "semantic_schedule_skipped_disabled",
            schedule_id=schedule.id,
            action=schedule.action,
        )
        return
    if schedule.action == SEMANTIC_EMBED_FILL_ACTION:
        await h.run_embed_fill_sweep()
    elif schedule.action == SEMANTIC_RECONCILE_ACTION:
        await h.reconcile_kg(client, schedule.tenant_id, schedule.kg_name)
    else:  # defensive: only semantic actions are routed here
        raise ValueError(f"not a semantic schedule action: {schedule.action!r}")


def schedule_reconcile_task(neptune: Any, tenant_id: str, kg_name: str) -> None:
    """Fire-and-forget one reconcile (the reindex route's no-runner fallback)."""
    h = _host()

    async def _safe() -> None:
        try:
            await h.reconcile_kg(neptune, tenant_id, kg_name)
        except Exception:  # noqa: BLE001 — background task must not crash the loop
            h.logger.warning(
                "semantic_reconcile_task_failed",
                tenant_id=tenant_id,
                kg_name=kg_name,
                exc_info=True,
            )

    task = asyncio.create_task(_safe())
    h._bg_tasks.add(task)
    task.add_done_callback(h._bg_tasks.discard)

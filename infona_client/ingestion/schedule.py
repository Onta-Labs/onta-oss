"""Recurring reads for a saved extract source (ONTA-555).

"Schedule regular reads" is NOT a second scheduler. An extract schedule is an
ordinary :class:`~infona_client.scheduling.models.Schedule` row with
``action="extract"`` and ``params={"extract_slug": …}``, stored in the same
schedule store and fired by the same runner (``FOR UPDATE SKIP LOCKED`` claim,
missed-tick collapsing, poll loop) as enrich / dedupe / notify. This module is
just the mapping between one extract source and its row.

The extract-source routes expose it as ``PUT``/``DELETE
/extract-sources/{slug}/schedule`` so a client sets a cadence in one call
instead of hand-joining schedule rows to sources; both paths write through the
same store and the same ``compute_next_run``.

Boundary: OSS, gated exactly like the rest of the persist family (hosted
extract entitlement on the route). Self-hosted deployments with no entitlement
checker registered schedule freely — see docs/oss_proprietary_boundary.md §33.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from infona_client.enrichment.models import JobCategory
from infona_client.ingestion.models import (
    MIN_SCHEDULE_INTERVAL_SECONDS,
    SCHEDULE_DAILY,
    SCHEDULE_HOURLY,
    SCHEDULE_WEEKLY,
    ExtractScheduleInfo,
    ExtractScheduleRequest,
)
from infona_client.scheduling.models import Schedule
from infona_client.scheduling.next_run import compute_next_run

#: The schedule action this module owns. Also in ``ScheduleAction`` /
#: ``USER_SCHEDULABLE_ACTIONS`` (scheduling/models.py).
EXTRACT_ACTION = "extract"

#: Re-exported so callers reach the cadence vocabulary through this module
#: rather than importing the contract file directly.
HOURLY = SCHEDULE_HOURLY
DAILY = SCHEDULE_DAILY
WEEKLY = SCHEDULE_WEEKLY
MIN_INTERVAL_SECONDS = MIN_SCHEDULE_INTERVAL_SECONDS


def schedule_params(slug: str) -> dict:
    """Params payload identifying which saved source a row re-reads."""
    return {"extract_slug": slug}


def schedule_info(schedule: Schedule) -> ExtractScheduleInfo:
    return ExtractScheduleInfo(
        id=schedule.id,
        interval_seconds=schedule.interval_seconds,
        cron=schedule.cron,
        enabled=schedule.enabled,
        next_run=schedule.next_run,
        last_run=schedule.last_run,
    )


def _matches(schedule: Schedule, slug: str) -> bool:
    return schedule.action == EXTRACT_ACTION and str(
        (schedule.params or {}).get("extract_slug") or ""
    ) == slug


async def find_extract_schedule(
    store, tenant_id: str, slug: str
) -> Optional[Schedule]:
    """The row for one source, or None. One schedule per source by construction."""
    for schedule in await store.list_for_tenant(tenant_id):
        if _matches(schedule, slug):
            return schedule
    return None


async def schedules_by_slug(store, tenant_id: str) -> dict[str, Schedule]:
    """All of a tenant's extract schedules, keyed by source slug (one list call)."""
    out: dict[str, Schedule] = {}
    for schedule in await store.list_for_tenant(tenant_id):
        if schedule.action != EXTRACT_ACTION:
            continue
        slug = str((schedule.params or {}).get("extract_slug") or "")
        if slug and slug not in out:
            out[slug] = schedule
    return out


async def upsert_extract_schedule(
    store,
    *,
    tenant_id: str,
    slug: str,
    kg_name: str,
    body: ExtractScheduleRequest,
) -> Schedule:
    """Create or replace the cadence for one source. Recomputes ``next_run``.

    ``compute_next_run`` raises ``NotImplementedError`` for a cron expression
    without the optional ``croniter`` package; the route maps that to 501, the
    same contract ``/schedules`` already has.
    """
    now = datetime.now(timezone.utc)
    existing = await find_extract_schedule(store, tenant_id, slug)
    schedule = Schedule(
        id=existing.id if existing else str(uuid.uuid4()),
        tenant_id=tenant_id,
        kg_name=kg_name,
        category=JobCategory.ingest,
        action=EXTRACT_ACTION,
        params=schedule_params(slug),
        cron=(body.cron or None) if body.cron and body.cron.strip() else None,
        interval_seconds=body.interval_seconds,
        enabled=body.enabled,
        last_run=existing.last_run if existing else None,
        created_at=existing.created_at if existing else now,
    )
    schedule.next_run = compute_next_run(schedule, now)
    if existing:
        await store.update(schedule)
    else:
        await store.create(schedule)
    return schedule


async def delete_extract_schedule(store, tenant_id: str, slug: str) -> bool:
    """Remove a source's cadence. Idempotent — False when there was none."""
    existing = await find_extract_schedule(store, tenant_id, slug)
    if existing is None:
        return False
    await store.delete(existing.id)
    return True

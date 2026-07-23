"""Ask-AI ACTION endpoints (COG-99).

The Ask-AI panel's action cards ("Find & merge duplicates", "Enrich with new
attributes", "Suggest new relationships") each kick off a tracked job and
return a ``job_id`` the UI can poll. Every action creates an ``EnrichJob`` in
the configured job store and returns ``{job_id, status, poll_url}``; the UI
polls the unified ``GET /graphs/{tenant}/jobs/{id}`` (served by the enrich
get-job route) for progress.

Boundary note: relationship *suggestion* (the recommender) is PREMIUM and not
shipped in OSS. The "suggest-relationships" action therefore creates a tracked
job and degrades gracefully — if no premium recommender hook is registered it
records a clear terminal state and still returns a job id so the UI flow works.
A registration seam (mirroring the ``register_*`` plugin pattern) is provided so
the premium side can later fill it in, without OSS ever importing premium code.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator

from cograph_client.api.deps import (
    get_enrichment_job_store,
    get_executor,
    get_neptune_client,
)
from cograph_client.auth.api_keys import TenantContext, get_tenant
from cograph_client.enrichment.executor import EnrichmentExecutor
from cograph_client.enrichment.models import (
    ConflictPolicy,
    EnrichJob,
    EnrichmentTier,
    EnrichScope,
    JobCategory,
    JobStatus,
    JobTrigger,
    _validate_entity_uris_field,
)
from cograph_client.graph.client import NeptuneClient
from cograph_client.graph.queries import kg_graph_uri
from cograph_client.pipeline.stage_trace import (
    finalize_job_stage_trace,
    open_job_stage_trace,
)

logger = structlog.stdlib.get_logger("cograph.actions")

router = APIRouter(prefix="/graphs/{tenant}/actions")


# Background fire-and-forget tasks: CPython only holds a *weak* reference to a
# bare ``asyncio.create_task(...)`` result, so it can be garbage-collected
# mid-flight and silently strand a job. Keep a strong reference in a module-level
# set and drop it on completion (mirrors explore.py's schedule_recompute).
_bg_tasks: set = set()


def _spawn(coro) -> None:
    """Schedule a background coroutine, keeping a strong ref until it finishes."""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# --- Premium recommender seam (COG-99) ----------------------------------------
# Relationship suggestion is premium. OSS exposes a registration hook (mirroring
# register_external_verifier / register_adapter) so a downstream deployment can
# wire a recommender WITHOUT OSS importing any cograph.* module. The hook, when
# present, is awaited with (client, tenant_id, kg_name) and returns a result
# dict recorded on the job. When absent, the action degrades to a no-op job.
RelationshipRecommender = Callable[
    [NeptuneClient, str, str], Awaitable[dict]
]
_recommender: Optional[RelationshipRecommender] = None


def register_relationship_recommender(
    recommender: Optional[RelationshipRecommender],
) -> None:
    """Register (or clear) the premium relationship recommender.

    Pass ``None`` to clear. Without a registered recommender the
    suggest-relationships action records a terminal no-op job.
    """
    global _recommender
    _recommender = recommender


# --- Request bodies -----------------------------------------------------------


class KGActionRequest(BaseModel):
    kg_name: str


class EnrichActionRequest(BaseModel):
    """Mirrors the enrich create-job body."""

    type_name: str
    attributes: list[str]
    kg_name: str
    tier: EnrichmentTier = EnrichmentTier.lite
    conflict_policy: ConflictPolicy = ConflictPolicy.stage
    confidence_min: float = 0.85
    limit: Optional[int] = None
    # COG-112 scoped enrichment (mirrors EnrichRequest). entity_uris wins.
    scope: Optional[EnrichScope] = None
    entity_uris: Optional[list[str]] = None
    # Optional enrichment knobs (mirror EnrichRequest). Both default None → same
    # behavior as today when omitted. instructions → adapter lookup context;
    # sources → adapter-chain override (unknown names fall back gracefully).
    instructions: Optional[str] = None
    sources: Optional[list[str]] = None
    # Optional HARD per-run spend ceiling (USD) for this job (ONTA-282/ONTA-378;
    # mirrors EnrichRequest). Default None → deployment default. An explicit value
    # bounds THIS job via the executor's ``resolve_spend_ceiling(...)`` override.
    spend_ceiling_usd: Optional[float] = None

    # Reject malformed IRIs at the API boundary with 422 (COG-112 review fix #1).
    _check_entity_uris = field_validator("entity_uris")(_validate_entity_uris_field)


# --- Helpers ------------------------------------------------------------------


def _poll_url(tenant_id: str, job_id: str) -> str:
    """Where the UI polls a job. Points at the enrich get-job route, which is
    the canonical full-job view (the unified /jobs list is a summary view)."""
    return f"/graphs/{tenant_id}/enrich/jobs/{job_id}"


def _new_job(
    *,
    tenant_id: str,
    kg_name: str,
    category: JobCategory,
    type_name: str = "",
    attributes: Optional[list[str]] = None,
    tier: EnrichmentTier = EnrichmentTier.lite,
    conflict_policy: ConflictPolicy = ConflictPolicy.stage,
    confidence_min: float = 0.85,
    limit: Optional[int] = None,
    cost_note: Optional[str] = None,
    scope: Optional[EnrichScope] = None,
    entity_uris: Optional[list[str]] = None,
    instructions: Optional[str] = None,
    sources: Optional[list[str]] = None,
    spend_ceiling_usd: Optional[float] = None,
) -> EnrichJob:
    job = EnrichJob(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        kg_name=kg_name,
        type_name=type_name,
        attributes=attributes or [],
        tier=tier,
        status=JobStatus.queued,
        created_at=datetime.now(timezone.utc),
        conflict_policy=conflict_policy,
        confidence_min=confidence_min,
        limit=limit,
        category=category,
        trigger=JobTrigger.manual,
        cost_note=cost_note,
        scope=scope,
        entity_uris=entity_uris,
        instructions=instructions,
        sources=sources,
        # Per-run HARD spend ceiling (ONTA-378): None → deployment default.
        spend_ceiling_usd=spend_ceiling_usd,
    )
    # P0/A9 live open (ONTA-388): every action-created job opens P0 on create.
    open_job_stage_trace(job)
    return job


# --- scheduled dispatch (COG-136) ---------------------------------------------
# The schedule firing loop and the /actions/* routes must create+run jobs the
# SAME way, so the worker logic isn't duplicated and the two paths can't drift.
# The routes keep their existing _new_job + _spawn(worker) flow byte-for-byte;
# this helper is the ONE place that maps a Schedule (action + category + params)
# onto the same job + same worker, tagged trigger=scheduled. The runner imports
# and awaits it; the routes are unchanged.


async def _resolve_scheduled_auto_tier(job: EnrichJob) -> None:
    """Resolve the ``auto`` meta-tier for a SCHEDULED enrich job, in place.

    The manual route resolves ``auto`` before creating a job (COG-124) and can
    bounce an ambiguous request back to the user; a scheduled firing has no
    user to ask, so dispatch previously ran the literal ``auto`` chain — the
    defensive wikidata fallback — silently different from the same request
    made interactively. Mirror the route: resolve via the shared router; on
    the (rare) ambiguous outcome re-resolve with no LLM key, which forces the
    deterministic heuristic that always lands on a concrete tier. Also mirror
    the route's confidence handling (COG-121 web floor) for the resolved tier
    when the schedule didn't pin an explicit confidence.
    """
    if job.tier is not EnrichmentTier.auto:
        return
    # Lazy import: enrich.py imports the executor machinery this module also
    # feeds; importing at call time keeps module import order irrelevant.
    from cograph_client.api.routes.enrich import (
        _effective_confidence_min,
        _openrouter_key,
    )
    from cograph_client.enrichment.tier_router import resolve_auto_tier

    decision = await resolve_auto_tier(job.attributes, job.type_name, _openrouter_key())
    if decision.needs_clarification or not decision.resolved_tier:
        decision = await resolve_auto_tier(job.attributes, job.type_name, None)
    job.tier = EnrichmentTier(decision.resolved_tier)
    job.confidence_min = _effective_confidence_min(job.tier, job.confidence_min)
    logger.info(
        "scheduled_auto_tier_resolved",
        job_id=job.id,
        resolved_tier=job.tier.value,
        routing_note=decision.routing_note,
    )


def _job_from_schedule(schedule) -> EnrichJob:
    """Build an ``EnrichJob`` for a scheduled firing of ``schedule``.

    Mirrors ``_new_job`` (manual path) but tags ``trigger=scheduled`` and pulls
    the action payload from ``schedule.params`` (the enrich body fields when the
    action is ``enrich``; dedupe/suggest carry no extra payload). Unknown/extra
    params are ignored so adding a field to an action body never breaks dispatch.
    """
    params = schedule.params or {}
    tier = params.get("tier", EnrichmentTier.lite)
    conflict_policy = params.get("conflict_policy", ConflictPolicy.stage)
    scope = params.get("scope")
    if scope is not None and not isinstance(scope, EnrichScope):
        scope = EnrichScope.model_validate(scope)
    job = _new_job(
        tenant_id=schedule.tenant_id,
        kg_name=schedule.kg_name,
        category=schedule.category,
        type_name=params.get("type_name", ""),
        attributes=params.get("attributes") or [],
        tier=EnrichmentTier(tier) if not isinstance(tier, EnrichmentTier) else tier,
        conflict_policy=(
            ConflictPolicy(conflict_policy)
            if not isinstance(conflict_policy, ConflictPolicy)
            else conflict_policy
        ),
        confidence_min=params.get("confidence_min", 0.85),
        limit=params.get("limit"),
        scope=scope,
        entity_uris=params.get("entity_uris"),
        instructions=params.get("instructions"),
        sources=params.get("sources"),
        spend_ceiling_usd=params.get("spend_ceiling_usd"),
    )
    job.trigger = JobTrigger.scheduled
    return job


async def dispatch_scheduled_action(
    schedule,
    *,
    client: NeptuneClient,
    job_store,
    executor: EnrichmentExecutor,
    schedule_store=None,
) -> Optional[EnrichJob]:
    """Create + run the job for a due ``schedule``, reusing the route workers.

    Returns the created job (``None`` for the semantic maintenance + ``notify``
    actions, which create no job rows). The worker runs to completion when awaited
    (the runner awaits it); callers that want fire-and-forget can wrap the returned
    coroutine themselves. Action → worker mapping is identical to the routes:

    - ``find-merge-duplicates`` → :func:`_run_dedupe`
    - ``enrich``                → :meth:`EnrichmentExecutor.run`
    - ``suggest-relationships`` → :func:`_run_suggest` (premium recommender), or
      a terminal no-op job when no recommender is wired (mirrors the route's
      graceful degrade).
    - ``notify`` (ONTA-235) → :func:`_run_notify`. Snapshot the watched value(s),
      diff against the previous fire's snapshot on the row, and DELIVER a change
      payload through the registered ``DeliverySink`` ONLY when something changed;
      persist the fresh snapshot back (needs ``schedule_store``). Creates no job
      row.
    - ``semantic-embed-fill`` / ``semantic-reconcile`` (ONTA-181) →
      ``semantic.reconciler.dispatch_semantic_schedule``. Routed through THIS
      seam (not a private loop) so semantic maintenance inherits the runner's
      SKIP LOCKED claim exclusivity. Deliberately job-row-free — a 5-minute
      sweep would flood the unified Jobs feed; observability is the
      reconciler's structlog counters.
    """
    action = schedule.action

    if action in ("semantic-embed-fill", "semantic-reconcile"):
        # Lazy import: keeps the semantic subsystem out of this module's import
        # graph (mirrors the runner's lazy import of this function).
        from cograph_client.semantic.reconciler import dispatch_semantic_schedule

        await dispatch_semantic_schedule(schedule, client=client)
        return None

    if action == "notify":
        # Standing-alert / weekly-refresh: watch → diff → deliver-on-change.
        # Job-row-free (like the semantic actions); observability is the
        # structlog counters + the DeliveryResult in _run_notify.
        await _run_notify(schedule, client=client, schedule_store=schedule_store)
        return None

    job = _job_from_schedule(schedule)

    if action == "enrich":
        await _resolve_scheduled_auto_tier(job)
        await job_store.create(job)
        await executor.run(job, schedule.tenant_id)
        return job

    if action == "find-merge-duplicates":
        await job_store.create(job)
        await _run_dedupe(
            client, job_store, job.id, schedule.tenant_id, schedule.kg_name
        )
        return job

    if action == "suggest-relationships":
        if not job.cost_note:
            job.cost_note = (
                "Relationship suggestion requires the premium recommender."
            )
        if _recommender is None:
            # Degrade gracefully, exactly like the route: terminal failed job.
            now = datetime.now(timezone.utc)
            job.status = JobStatus.failed
            job.error = (
                job.cost_note + " No recommender is wired in this deployment."
            )
            job.completed_at = now
            job.last_run = now
            await job_store.create(job)
            return job
        await job_store.create(job)
        await _run_suggest(
            client, job_store, job.id, schedule.tenant_id, schedule.kg_name
        )
        return job

    # Defensive: ScheduleAction is a closed Literal, so this is unreachable.
    raise ValueError(f"unknown schedule action: {action!r}")


# --- notify (standing alert / weekly refresh; ONTA-235) -----------------------


async def _run_notify(
    schedule,
    *,
    client: NeptuneClient,
    schedule_store=None,
) -> "DeliveryResult":
    """Watch → diff → deliver-on-change for a due ``notify`` schedule.

    1. Snapshot the watched value(s) from the KG (``params['watch']``).
    2. Diff against the snapshot the previous fire persisted on the row
       (``params['last_snapshot']``). The FIRST fire only establishes the
       baseline — it delivers nothing (see ``diff_snapshots``).
    3. When (and only when) something changed, deliver an ``old → new`` change
       payload through the registered ``DeliverySink`` (best-effort HTTP POST in
       OSS; a premium reliable sink supersedes it via ``register_delivery_sink``).
       Delivery is SSRF-guarded inside the sink.
    4. Persist the fresh snapshot back onto the row (via ``schedule_store``) so the
       next fire diffs against it.

    Never raises: a read/deliver hiccup is logged and returned as a structured
    ``DeliveryResult`` so one bad notify can't sink a runner sweep. Returns the
    delivery outcome (``ok`` also True for the no-change / baseline case, where
    there was nothing to deliver).
    """
    from cograph_client.graph.queries import kg_graph_uri
    from cograph_client.scheduling.delivery import (
        DeliveryResult,
        DeliveryTarget,
        get_delivery_sink,
    )
    from cograph_client.scheduling.watch import (
        SNAPSHOT_KEY,
        diff_snapshots,
        snapshot_watch,
    )

    params = dict(schedule.params or {})
    watch = params.get("watch") or {}
    instance_graph = (
        kg_graph_uri(schedule.tenant_id, schedule.kg_name)
        if schedule.kg_name
        else None
    )

    current = await snapshot_watch(client, watch, instance_graph)
    previous = params.get(SNAPSHOT_KEY)
    changes = diff_snapshots(previous, current)

    result = DeliveryResult(ok=True)
    if changes:
        target = DeliveryTarget.from_params(params.get("sink"))
        if target is None:
            logger.warning(
                "notify_no_sink",
                schedule_id=schedule.id,
                tenant=schedule.tenant_id,
            )
        else:
            payload = {
                "schedule_id": schedule.id,
                "tenant_id": schedule.tenant_id,
                "kg_name": schedule.kg_name,
                "changes": changes,
                "fired_at": datetime.now(timezone.utc).isoformat(),
            }
            sink = get_delivery_sink()
            result = await sink.deliver(target, payload)
            logger.info(
                "notify_delivered",
                schedule_id=schedule.id,
                tenant=schedule.tenant_id,
                changes=len(changes),
                ok=result.ok,
                blocked=result.blocked,
                status=result.status_code,
            )

    # Persist the fresh snapshot back so the next fire diffs against it. Only when
    # we could read a snapshot (a read failure yields {} → keep the old baseline
    # so a transient outage doesn't reset the watch). Best-effort: a store hiccup
    # must not fail the tick.
    if schedule_store is not None and current:
        try:
            latest = await schedule_store.get(schedule.id)
            if latest is not None:
                new_params = dict(latest.params or {})
                new_params[SNAPSHOT_KEY] = current
                latest.params = new_params
                await schedule_store.update(latest)
        except Exception:  # noqa: BLE001 — snapshot persistence is best-effort
            logger.warning(
                "notify_snapshot_persist_failed",
                schedule_id=schedule.id,
                exc_info=True,
            )
    return result


# --- find & merge duplicates (dedupe) -----------------------------------------


async def _run_dedupe(
    client: NeptuneClient,
    job_store,
    job_id: str,
    tenant_id: str,
    kg_name: str,
) -> None:
    """Background worker: run second-pass ER over a KG and record the report.

    Reuses resolver.er.rebuild.rebuild_kg directly (the same primitive the
    explore.py ``er-rebuild`` route uses). Records the rebuild report into the
    job's progress/error and flips status to applied (or failed) + last_run.
    On success it also schedules a type-stats recompute, mirroring the
    ``er_rebuild`` route — a dedupe collapses fragments and changes per-type
    counts, so the Explorer's precomputed stats are stale until recomputed.
    """
    from cograph_client.graph.kg_writer import refresh_after_write
    from cograph_client.resolver.er.rebuild import rebuild_kg

    from cograph_client.pipeline.stage_trace import ensure_job_stage_trace_open

    job = await job_store.get(job_id)
    if job is None:
        return
    job.status = JobStatus.running
    job.started_at = datetime.now(timezone.utc)
    ensure_job_stage_trace_open(job)
    await job_store.update(job)

    try:
        report = await rebuild_kg(client, kg_graph_uri(tenant_id, kg_name))
        job = await job_store.get(job_id) or job
        absorbed = int(report.get("fragments_absorbed_total", 0))
        # Record before/after merge volume into the job's progress counters so
        # the UI can show "N duplicates merged" without a bespoke field.
        job.progress.total = absorbed
        job.progress.processed = absorbed
        job.error = (
            f"merged {absorbed} duplicate fragment(s) across "
            f"{len(report.get('types', []))} type(s)"
        )
        job.status = JobStatus.applied
        # Shared post-write housekeeping path (kg_writer.refresh_after_write):
        # merge changed counts, not the type schema → affected_types=() (no
        # re-embed; still cache-invalidates + recomputes Explorer type-stats).
        await refresh_after_write(
            client, tenant_id=tenant_id, kg_name=kg_name, affected_types=()
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "dedupe_action_failed", tenant=tenant_id, kg=kg_name, error=str(exc)
        )
        job = await job_store.get(job_id) or job
        job.status = JobStatus.failed
        job.error = f"dedupe failed: {exc}"
    finally:
        now = datetime.now(timezone.utc)
        job.completed_at = now
        job.last_run = now
        # P0/A9 finalize (ONTA-388): never leave stage projects running.
        st = getattr(getattr(job, "status", None), "value", None) or str(
            getattr(job, "status", "") or ""
        )
        finalize_job_stage_trace(
            job,
            terminal_status=st,
            error=job.error if st == "failed" else None,
            summary={
                "category": "dedupe",
                "processed": getattr(job.progress, "processed", None),
                "total": getattr(job.progress, "total", None),
            },
            p0_output={"status": st, "error": job.error},
        )
        await job_store.update(job)


@router.post("/find-merge-duplicates", status_code=202)
async def find_merge_duplicates(
    body: KGActionRequest,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
    job_store=Depends(get_enrichment_job_store),
):
    """Kick off a dedupe job (second-pass entity resolution) over a KG."""
    job = _new_job(
        tenant_id=tenant.tenant_id,
        kg_name=body.kg_name,
        category=JobCategory.dedupe,
    )
    await job_store.create(job)
    _spawn(
        _run_dedupe(client, job_store, job.id, tenant.tenant_id, body.kg_name)
    )
    return {
        "job_id": job.id,
        "status": job.status.value,
        "poll_url": _poll_url(tenant.tenant_id, job.id),
    }


# --- enrich (enrichment) ------------------------------------------------------


@router.post("/enrich", status_code=202)
async def enrich_action(
    body: EnrichActionRequest,
    tenant: TenantContext = Depends(get_tenant),
    executor: EnrichmentExecutor = Depends(get_executor),
    job_store=Depends(get_enrichment_job_store),
):
    """Kick off an enrichment job, reusing the existing EnrichmentExecutor.

    Same job-creation + executor wiring as POST /enrich/jobs, but tagged with
    ``category=enrichment`` and returning the action-shaped response.
    """
    job = _new_job(
        tenant_id=tenant.tenant_id,
        kg_name=body.kg_name,
        category=JobCategory.enrichment,
        type_name=body.type_name,
        attributes=body.attributes,
        tier=body.tier,
        conflict_policy=body.conflict_policy,
        confidence_min=body.confidence_min,
        limit=body.limit,
        scope=body.scope,
        entity_uris=body.entity_uris,
        instructions=body.instructions,
        sources=body.sources,
        spend_ceiling_usd=body.spend_ceiling_usd,
    )
    await job_store.create(job)
    _spawn(executor.run(job, tenant.tenant_id))
    return {
        "job_id": job.id,
        "status": job.status.value,
        "poll_url": _poll_url(tenant.tenant_id, job.id),
    }


# --- suggest relationships (reconciliation; premium) --------------------------


async def _run_suggest(
    client: NeptuneClient,
    job_store,
    job_id: str,
    tenant_id: str,
    kg_name: str,
) -> None:
    """Background worker for suggest-relationships when a recommender IS wired."""
    from cograph_client.pipeline.stage_trace import ensure_job_stage_trace_open

    job = await job_store.get(job_id)
    if job is None:
        return
    job.status = JobStatus.running
    job.started_at = datetime.now(timezone.utc)
    ensure_job_stage_trace_open(job)
    await job_store.update(job)
    try:
        result = await _recommender(client, tenant_id, kg_name)  # type: ignore[misc]
        job = await job_store.get(job_id) or job
        job.status = JobStatus.review
        job.error = f"recommender produced {len(result.get('suggestions', []))} suggestion(s)"
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "suggest_action_failed", tenant=tenant_id, kg=kg_name, error=str(exc)
        )
        job = await job_store.get(job_id) or job
        job.status = JobStatus.failed
        job.error = f"recommender failed: {exc}"
    finally:
        now = datetime.now(timezone.utc)
        job.completed_at = now
        job.last_run = now
        st = getattr(getattr(job, "status", None), "value", None) or str(
            getattr(job, "status", "") or ""
        )
        finalize_job_stage_trace(
            job,
            terminal_status=st,
            error=job.error if st == "failed" else None,
            summary={"category": "reconciliation"},
            p0_output={"status": st, "error": job.error},
        )
        await job_store.update(job)


@router.post("/suggest-relationships", status_code=202)
async def suggest_relationships(
    body: KGActionRequest,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
    job_store=Depends(get_enrichment_job_store),
):
    """Kick off a relationship-suggestion (reconciliation) job.

    Relationship suggestion is a PREMIUM capability. If no recommender hook is
    registered, the job is created and immediately resolved to a terminal
    ``failed`` state with a clear message — but a ``job_id`` is still returned
    so the UI's create-then-poll flow works unchanged. When a premium
    recommender is registered (via ``register_relationship_recommender``), the
    job runs it in the background and lands in ``review``.
    """
    premium_note = "Relationship suggestion requires the premium recommender."
    job = _new_job(
        tenant_id=tenant.tenant_id,
        kg_name=body.kg_name,
        category=JobCategory.reconciliation,
        cost_note=premium_note,
    )

    if _recommender is None:
        # Degrade gracefully: terminal job, no background work, clear message.
        now = datetime.now(timezone.utc)
        job.status = JobStatus.failed
        job.error = (
            premium_note + " No recommender is wired in this deployment."
        )
        job.completed_at = now
        job.last_run = now
        # P0 was opened in _new_job; finalize so no project stays running.
        finalize_job_stage_trace(
            job,
            terminal_status="failed",
            error=job.error,
            summary={"category": "reconciliation", "premium_missing": True},
        )
        await job_store.create(job)
        return {
            "job_id": job.id,
            "status": job.status.value,
            "poll_url": _poll_url(tenant.tenant_id, job.id),
        }

    await job_store.create(job)
    _spawn(
        _run_suggest(client, job_store, job.id, tenant.tenant_id, body.kg_name)
    )
    return {
        "job_id": job.id,
        "status": job.status.value,
        "poll_url": _poll_url(tenant.tenant_id, job.id),
    }

"""Per-tenant API-usage report — the dashboard "API usage" panel's one route.

Composes two already-durable sources at read time:

- request metrics (count / errors / latency) from the usage store, recorded
  per daily bucket by the request middleware (``usage/recorder.py``);
- spend from the job store — per-run cost already lives on
  ``cograph_jobs.cost``, so cost is aggregated from there rather than being
  double-tracked.

Every series is day-aligned over the requested window, with per-KG and
per-API-key breakdowns and a previous-window totals block for deltas. All
interfaces (webapp / CLI / MCP) read THIS route — interface-convergence rule.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from cograph_client.api.deps import get_enrichment_job_store
from cograph_client.auth.api_keys import TenantContext, get_tenant
from cograph_client.usage.recorder import QUERY_ROUTE_CLASSES, get_usage_recorder
from cograph_client.usage.store import UsageBucket, get_usage_store

router = APIRouter(prefix="/graphs/{tenant}/usage")

# Cap the window: 2× is fetched for the previous-period delta, and the
# breakdown payload grows linearly with days.
MAX_DAYS = 90


class UsageSeries(BaseModel):
    """One day-aligned line: a breakdown member or the total."""

    label: str
    values: list[float]
    total: float


class UsageMetricBlock(BaseModel):
    """A metric's total line plus its per-KG / per-key breakdowns."""

    total: UsageSeries
    by_kg: list[UsageSeries] = Field(default_factory=list)
    by_key: list[UsageSeries] = Field(default_factory=list)


class UsageTotals(BaseModel):
    requests: int = 0
    errors: int = 0
    avg_latency_ms: float = 0.0
    cost_usd: float = 0.0


class UsageReport(BaseModel):
    days: list[str]
    requests: UsageMetricBlock
    latency_ms: UsageMetricBlock
    cost_usd: UsageMetricBlock
    totals: UsageTotals
    prev_totals: UsageTotals
    # Current-window request counts per route class (ask/agent/query/...).
    route_class_requests: dict[str, int] = Field(default_factory=dict)
    # Whether any query-shaped traffic (ask/agent/query/search) was seen in
    # the window — the setup checklist's "queried via API" signal.
    has_queried: bool = False
    # Calendar-month-to-date request count (UTC) — the quota card's "used".
    month_requests: int = 0


# Breakdown lines beyond this many are folded away (top-N by requests).
MAX_BREAKDOWN = 6


def _day_range(start: date, n: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def _series(
    label: str, per_day: dict[date, float], days: list[date], total: float
) -> UsageSeries:
    return UsageSeries(
        label=label,
        values=[round(per_day.get(d, 0.0), 3) for d in days],
        total=round(total, 3),
    )


def _latency_series(
    label: str,
    dur: dict[date, float],
    req: dict[date, float],
    days: list[date],
) -> UsageSeries:
    values = [
        round(dur.get(d, 0.0) / req[d], 1) if req.get(d) else 0.0 for d in days
    ]
    total_req = sum(req.values())
    avg = round(sum(dur.values()) / total_req, 1) if total_req else 0.0
    return UsageSeries(label=label, values=values, total=avg)


def _top_labels(per_label_req: dict[str, float]) -> list[str]:
    ranked = sorted(per_label_req.items(), key=lambda kv: kv[1], reverse=True)
    return [label for label, _ in ranked[:MAX_BREAKDOWN]]


@router.get("", response_model=UsageReport)
async def get_usage(
    days: int = Query(30, ge=1, le=MAX_DAYS, description="Window length in days."),
    tenant: TenantContext = Depends(get_tenant),
    job_store=Depends(get_enrichment_job_store),
) -> UsageReport:
    """Day-aligned usage report for the tenant, newest day last.

    ``days`` sets the current window; the preceding window of equal length is
    aggregated into ``prev_totals`` for period-over-period deltas.
    """
    # Make sure buffered observations (including this morning's traffic) are
    # visible before reading. Cheap no-op when the buffer is empty.
    await get_usage_recorder().flush()

    today = datetime.now(timezone.utc).date()
    window_start = today - timedelta(days=days - 1)
    prev_start = window_start - timedelta(days=days)
    day_list = _day_range(window_start, days)
    month_start = today.replace(day=1)

    buckets = await get_usage_store().query_range(
        tenant.tenant_id, min(prev_start, month_start), today
    )

    # --- request/latency aggregation off the usage buckets ------------------
    req_total: dict[date, float] = {}
    dur_total: dict[date, float] = {}
    req_by_kg: dict[str, dict[date, float]] = {}
    dur_by_kg: dict[str, dict[date, float]] = {}
    req_by_key: dict[str, dict[date, float]] = {}
    dur_by_key: dict[str, dict[date, float]] = {}
    route_class_requests: dict[str, int] = {}
    totals = UsageTotals()
    prev = UsageTotals()
    prev_dur = 0.0
    cur_dur = 0.0
    month_requests = 0

    def _acc(target_req: dict[date, float], target_dur: dict[date, float], b: UsageBucket) -> None:
        target_req[b.day] = target_req.get(b.day, 0.0) + b.requests
        target_dur[b.day] = target_dur.get(b.day, 0.0) + b.duration_ms_sum

    for b in buckets:
        if b.day >= month_start:
            month_requests += b.requests
        if prev_start <= b.day < window_start:
            prev.requests += b.requests
            prev.errors += b.errors
            prev_dur += b.duration_ms_sum
            continue
        if b.day < window_start:
            continue
        totals.requests += b.requests
        totals.errors += b.errors
        cur_dur += b.duration_ms_sum
        route_class_requests[b.route_class] = (
            route_class_requests.get(b.route_class, 0) + b.requests
        )
        _acc(req_total, dur_total, b)
        if b.kg_name:
            _acc(
                req_by_kg.setdefault(b.kg_name, {}),
                dur_by_kg.setdefault(b.kg_name, {}),
                b,
            )
        key_label = b.api_key_hint or "default"
        _acc(
            req_by_key.setdefault(key_label, {}),
            dur_by_key.setdefault(key_label, {}),
            b,
        )

    totals.avg_latency_ms = (
        round(cur_dur / totals.requests, 1) if totals.requests else 0.0
    )
    prev.avg_latency_ms = round(prev_dur / prev.requests, 1) if prev.requests else 0.0

    # --- cost aggregation off the job store ---------------------------------
    cost_total: dict[date, float] = {}
    cost_by_kg: dict[str, dict[date, float]] = {}
    try:
        summaries = await job_store.list_for_tenant(tenant.tenant_id)
    except Exception:  # noqa: BLE001 - cost is additive; report requests regardless
        summaries = []
    for s in summaries:
        if not s.cost:
            continue
        ran_at: Optional[datetime] = s.last_run or s.completed_at or s.created_at
        if ran_at is None:
            continue
        ran_day = ran_at.astimezone(timezone.utc).date() if ran_at.tzinfo else ran_at.date()
        if ran_day > today or ran_day < prev_start:
            continue
        if ran_day < window_start:
            prev.cost_usd += s.cost
            continue
        totals.cost_usd += s.cost
        cost_total[ran_day] = cost_total.get(ran_day, 0.0) + s.cost
        if s.kg_name:
            kg_costs = cost_by_kg.setdefault(s.kg_name, {})
            kg_costs[ran_day] = kg_costs.get(ran_day, 0.0) + s.cost
    totals.cost_usd = round(totals.cost_usd, 2)
    prev.cost_usd = round(prev.cost_usd, 2)

    # --- assemble day-aligned series ----------------------------------------
    kg_labels = _top_labels({k: sum(v.values()) for k, v in req_by_kg.items()})
    key_labels = _top_labels({k: sum(v.values()) for k, v in req_by_key.items()})
    cost_kg_labels = _top_labels({k: sum(v.values()) for k, v in cost_by_kg.items()})

    requests_block = UsageMetricBlock(
        total=_series("Total", req_total, day_list, totals.requests),
        by_kg=[
            _series(k, req_by_kg[k], day_list, sum(req_by_kg[k].values()))
            for k in kg_labels
        ],
        by_key=[
            _series(k, req_by_key[k], day_list, sum(req_by_key[k].values()))
            for k in key_labels
        ],
    )
    latency_block = UsageMetricBlock(
        total=_latency_series("Total", dur_total, req_total, day_list),
        by_kg=[
            _latency_series(k, dur_by_kg[k], req_by_kg[k], day_list)
            for k in kg_labels
        ],
        by_key=[
            _latency_series(k, dur_by_key[k], req_by_key[k], day_list)
            for k in key_labels
        ],
    )
    cost_block = UsageMetricBlock(
        total=_series("Total", cost_total, day_list, totals.cost_usd),
        by_kg=[
            _series(k, cost_by_kg[k], day_list, sum(cost_by_kg[k].values()))
            for k in cost_kg_labels
        ],
        # Job runs aren't attributable to an API key — no per-key cost lines.
        by_key=[],
    )

    return UsageReport(
        days=[d.isoformat() for d in day_list],
        requests=requests_block,
        latency_ms=latency_block,
        cost_usd=cost_block,
        totals=totals,
        prev_totals=prev,
        route_class_requests=route_class_requests,
        has_queried=any(
            route_class_requests.get(c, 0) > 0 for c in QUERY_ROUTE_CLASSES
        ),
        month_requests=month_requests,
    )

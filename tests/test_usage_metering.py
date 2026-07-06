"""Per-tenant API usage metering: store, recorder, middleware, /usage route.

Covers the three layers separately (bucket increments in the store, path
classification + buffering in the recorder, report assembly in the route) plus
the end-to-end middleware path: real requests through the app must show up in
the report.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cograph_client.enrichment.job_store import InMemoryJobStore
from cograph_client.enrichment.models import (
    ConflictPolicy,
    EnrichJob,
    EnrichmentTier,
    JobStatus,
)
from cograph_client.usage.recorder import (
    UsageRecorder,
    classify_request,
    key_hint,
    reset_usage_recorder,
)
from cograph_client.usage.store import (
    InMemoryUsageStore,
    UsageBucket,
    reset_usage_store,
)


@pytest.fixture(autouse=True)
def _reset_usage_singletons():
    reset_usage_store()
    reset_usage_recorder()
    yield
    reset_usage_store()
    reset_usage_recorder()


TODAY = datetime.now(timezone.utc).date()


def bucket(**kw) -> UsageBucket:
    defaults = dict(
        tenant_id="test-tenant",
        day=TODAY,
        kg_name="",
        api_key_hint="",
        route_class="other",
        requests=1,
        errors=0,
        duration_ms_sum=10.0,
    )
    defaults.update(kw)
    return UsageBucket(**defaults)


# --- store ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_add_increments_same_bucket():
    store = InMemoryUsageStore()
    await store.add([bucket(requests=2, duration_ms_sum=20.0)])
    await store.add([bucket(requests=3, errors=1, duration_ms_sum=5.0)])
    rows = await store.query_range("test-tenant", TODAY, TODAY)
    assert len(rows) == 1
    assert rows[0].requests == 5
    assert rows[0].errors == 1
    assert rows[0].duration_ms_sum == 25.0


@pytest.mark.asyncio
async def test_store_query_range_filters_tenant_and_days():
    store = InMemoryUsageStore()
    await store.add(
        [
            bucket(),
            bucket(tenant_id="other-tenant"),
            bucket(day=TODAY - timedelta(days=40)),
        ]
    )
    rows = await store.query_range(
        "test-tenant", TODAY - timedelta(days=30), TODAY
    )
    assert len(rows) == 1
    assert rows[0].tenant_id == "test-tenant"


# --- recorder: classification ------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/health", None),
        ("/v1/me/tenants", None),
        ("/graphs/t1/ask", ("t1", "", "ask")),
        ("/graphs/t1/agent", ("t1", "", "agent")),
        ("/graphs/t1/query", ("t1", "", "query")),
        ("/graphs/t1/search", ("t1", "", "search")),
        ("/graphs/t1/jobs", ("t1", "", "jobs")),
        ("/graphs/t1/usage", ("t1", "", "usage")),
        ("/graphs/t1/kgs", ("t1", "", "kgs")),
        ("/graphs/t1/kgs/imdb/type-counts", ("t1", "imdb", "kgs")),
        (
            "/graphs/t1/explore/kgs/imdb/types/Movie/summary",
            ("t1", "imdb", "explore"),
        ),
        ("/graphs/t1/normalize/rules", ("t1", "", "normalize")),
        ("/graphs/t1/unknown-thing", ("t1", "", "other")),
    ],
)
def test_classify_request(path, expected):
    assert classify_request(path) == expected


def test_key_hint():
    assert key_hint(None) == ""
    assert key_hint("") == ""
    assert key_hint("sk_live_abcd1234") == "1234"


# --- recorder: buffering + flush ---------------------------------------------


@pytest.mark.asyncio
async def test_recorder_buffers_and_flushes():
    store = InMemoryUsageStore()
    rec = UsageRecorder(store=store)
    rec.observe("/graphs/t1/ask", "POST", 200, 120.0, "key-abcd")
    rec.observe("/graphs/t1/ask", "POST", 500, 80.0, "key-abcd")
    rec.observe("/graphs/t1/kgs", "GET", 200, 10.0, "key-abcd")
    # Skipped: unauthenticated + non-tenant + preflight.
    rec.observe("/graphs/t1/ask", "POST", 401, 5.0, "bad-key")
    rec.observe("/graphs/t1/ask", "POST", 403, 5.0, "bad-key")
    rec.observe("/health", "GET", 200, 1.0, None)
    rec.observe("/graphs/t1/ask", "OPTIONS", 200, 1.0, None)

    await rec.flush()
    rows = await store.query_range("t1", TODAY, TODAY)
    by_class = {r.route_class: r for r in rows}
    assert set(by_class) == {"ask", "kgs"}
    ask = by_class["ask"]
    assert ask.requests == 2
    assert ask.errors == 1
    assert ask.duration_ms_sum == 200.0
    assert ask.api_key_hint == "abcd"

    # Flushing again with an empty buffer is a no-op, not a duplicate write.
    await rec.flush()
    rows = await store.query_range("t1", TODAY, TODAY)
    assert sum(r.requests for r in rows) == 3


@pytest.mark.asyncio
async def test_recorder_rebuffers_on_store_failure():
    class FailingStore(InMemoryUsageStore):
        def __init__(self) -> None:
            super().__init__()
            self.fail = True

        async def add(self, buckets):
            if self.fail:
                raise RuntimeError("db down")
            await super().add(buckets)

    store = FailingStore()
    rec = UsageRecorder(store=store)
    rec.observe("/graphs/t1/ask", "POST", 200, 50.0, None)
    await rec.flush()  # swallowed; increments re-buffered
    assert await store.query_range("t1", TODAY, TODAY) == []
    store.fail = False
    await rec.flush()
    rows = await store.query_range("t1", TODAY, TODAY)
    assert len(rows) == 1 and rows[0].requests == 1


# --- /usage route -------------------------------------------------------------


def _seed_job(job_store: InMemoryJobStore, *, cost: float, when: datetime, kg: str):
    import asyncio
    import uuid

    job = EnrichJob(
        id=str(uuid.uuid4()),
        tenant_id="test-tenant",
        kg_name=kg,
        type_name="Thing",
        attributes=["name"],
        tier=EnrichmentTier.lite,
        status=JobStatus.applied,
        created_at=when,
        last_run=when,
        conflict_policy=ConflictPolicy.skip,
        cost=cost,
    )
    asyncio.run(job_store.create(job))


def test_usage_route_end_to_end(client, auth_headers, app):
    """Real requests through the middleware surface in the /usage report."""
    job_store = InMemoryJobStore()
    app.state.enrichment_job_store = job_store
    now = datetime.now(timezone.utc)
    _seed_job(job_store, cost=1.25, when=now, kg="imdb")
    _seed_job(job_store, cost=0.75, when=now - timedelta(days=45), kg="imdb")

    # Generate real traffic (each of these is itself metered by middleware).
    client.get("/graphs/test-tenant/jobs", headers=auth_headers)
    client.get("/graphs/test-tenant/jobs", headers=auth_headers)

    res = client.get("/graphs/test-tenant/usage?days=30", headers=auth_headers)
    assert res.status_code == 200
    report = res.json()

    assert len(report["days"]) == 30
    # ≥2 jobs requests + possibly this usage call ordering; totals count the
    # two /jobs calls at minimum.
    assert report["totals"]["requests"] >= 2
    assert report["totals"]["avg_latency_ms"] > 0
    assert report["route_class_requests"].get("jobs", 0) >= 2
    assert report["has_queried"] is False
    assert report["month_requests"] >= 2

    # Cost: only the in-window job counts toward totals; the 45-day-old one
    # falls in the previous window.
    assert report["totals"]["cost_usd"] == 1.25
    assert report["prev_totals"]["cost_usd"] == 0.75
    assert report["cost_usd"]["by_kg"][0]["label"] == "imdb"
    assert report["cost_usd"]["by_kg"][0]["total"] == 1.25

    # Day-aligned series: last day carries today's traffic.
    assert report["requests"]["total"]["values"][-1] >= 2
    assert len(report["requests"]["total"]["values"]) == 30

    # A query-shaped request flips has_queried.
    client.post(
        "/graphs/test-tenant/query",
        headers=auth_headers,
        json={"sparql": "SELECT * WHERE { ?s ?p ?o } LIMIT 1"},
    )
    res = client.get("/graphs/test-tenant/usage?days=30", headers=auth_headers)
    assert res.status_code == 200
    assert res.json()["has_queried"] is True


def test_usage_route_requires_auth(client):
    res = client.get("/graphs/test-tenant/usage")
    assert res.status_code == 401


def test_usage_route_tenant_isolation(client, auth_headers):
    """The report is always scoped to the KEY's tenant, never the path's.

    Legacy single-tenant static keys route to their own tenant regardless of
    the path (documented `get_tenant` semantics — multi-tenant keys get a 403
    from `_resolve_allowed`, covered in test_auth_multi_tenant.py). So a
    static key asking for another tenant's usage must see its OWN numbers,
    not the other tenant's.
    """
    # Traffic attributed to test-tenant (the key's own tenant)...
    client.get("/graphs/test-tenant/jobs", headers=auth_headers)
    # ...and pre-seeded usage for the other tenant that must NOT be readable.
    import asyncio

    from cograph_client.usage.store import get_usage_store

    asyncio.run(
        get_usage_store().add(
            [bucket(tenant_id="other-tenant", requests=999, route_class="ask")]
        )
    )

    res = client.get("/graphs/other-tenant/usage?days=7", headers=auth_headers)
    assert res.status_code == 200
    report = res.json()
    assert report["totals"]["requests"] < 999
    assert report["route_class_requests"].get("ask", 0) == 0


def test_usage_route_days_validation(client, auth_headers):
    assert (
        client.get("/graphs/test-tenant/usage?days=0", headers=auth_headers).status_code
        == 422
    )
    assert (
        client.get(
            "/graphs/test-tenant/usage?days=91", headers=auth_headers
        ).status_code
        == 422
    )

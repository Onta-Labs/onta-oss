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
    FLUSH_INTERVAL_S,
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
        ("/graphs/t1/ask", ("", "ask")),
        ("/graphs/t1/agent", ("", "agent")),
        ("/graphs/t1/query", ("", "query")),
        ("/graphs/t1/search", ("", "search")),
        ("/graphs/t1/jobs", ("", "jobs")),
        ("/graphs/t1/usage", ("", "usage")),
        ("/graphs/t1/kgs", ("", "kgs")),
        ("/graphs/t1/kgs/imdb/type-counts", ("imdb", "kgs")),
        (
            "/graphs/t1/explore/kgs/imdb/types/Movie/summary",
            ("imdb", "explore"),
        ),
        ("/graphs/t1/normalize/rules", ("", "normalize")),
        ("/graphs/t1/unknown-thing", ("", "other")),
    ],
)
def test_classify_request(path, expected):
    assert classify_request(path) == expected


def test_classify_request_caps_kg_name_length():
    kg, _ = classify_request(f"/graphs/t1/kgs/{'x' * 500}/type-counts")
    assert len(kg) == 128


def test_key_hint():
    assert key_hint(None) == ""
    assert key_hint("") == ""
    assert key_hint("sk_live_abcd1234") == "1234"


# --- recorder: buffering + flush ---------------------------------------------


@pytest.mark.asyncio
async def test_recorder_buffers_and_flushes():
    store = InMemoryUsageStore()
    rec = UsageRecorder(store=store)
    rec.observe("/graphs/t1/ask", "POST", 200, 120.0, "key-abcd", tenant="t1")
    rec.observe("/graphs/t1/ask", "POST", 500, 80.0, "key-abcd", tenant="t1")
    rec.observe("/graphs/t1/kgs", "GET", 200, 10.0, "key-abcd", tenant="t1")
    # Skipped: unauthenticated (no resolved tenant) + non-tenant + preflight.
    rec.observe("/graphs/t1/ask", "POST", 401, 5.0, "bad-key", tenant=None)
    rec.observe("/graphs/t1/ask", "POST", 404, 5.0, None, tenant=None)
    rec.observe("/health", "GET", 200, 1.0, None, tenant=None)
    rec.observe("/graphs/t1/ask", "OPTIONS", 200, 1.0, None, tenant="t1")

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
async def test_recorder_attributes_to_authenticated_tenant_not_path():
    """The path's {tenant} segment must never drive attribution."""
    store = InMemoryUsageStore()
    rec = UsageRecorder(store=store)
    # Legacy single-tenant key: served as its OWN tenant even on another
    # tenant's path — the record must follow the authenticated identity.
    rec.observe("/graphs/victim-tenant/jobs", "GET", 200, 10.0, "k", tenant="t1")
    await rec.flush()
    assert await store.query_range("victim-tenant", TODAY, TODAY) == []
    rows = await store.query_range("t1", TODAY, TODAY)
    assert len(rows) == 1 and rows[0].requests == 1


@pytest.mark.asyncio
async def test_recorder_schedules_flush_when_due():
    """The opportunistic in-loop flush path actually writes to the store."""
    import asyncio

    store = InMemoryUsageStore()
    rec = UsageRecorder(store=store)
    rec._last_flush -= FLUSH_INTERVAL_S + 1  # force "due"
    rec.observe("/graphs/t1/ask", "POST", 200, 5.0, None, tenant="t1")
    for _ in range(5):  # let the created task run
        await asyncio.sleep(0)
        if await store.query_range("t1", TODAY, TODAY):
            break
    rows = await store.query_range("t1", TODAY, TODAY)
    assert len(rows) == 1 and rows[0].requests == 1


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
    rec.observe("/graphs/t1/ask", "POST", 200, 50.0, None, tenant="t1")
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


def test_unauthenticated_pre_auth_responses_record_nothing(client):
    """404/405 responses never reach the auth dependency — they must not be
    attributed to the path-named tenant (unauthenticated data corruption /
    storage-amplification vector if they were)."""
    import asyncio

    from cograph_client.usage.recorder import get_usage_recorder
    from cograph_client.usage.store import get_usage_store

    # No key: 404 (no such route), 405 (wrong method), 401 (real route).
    assert client.get("/graphs/victim-tenant/zzz-not-a-route").status_code == 404
    assert client.get("/graphs/victim-tenant/ask").status_code == 405
    assert client.post("/graphs/victim-tenant/query", json={}).status_code == 401
    # Wrong key on a real route: 401.
    assert (
        client.get(
            "/graphs/victim-tenant/jobs", headers={"X-API-Key": "wrong-key"}
        ).status_code
        == 401
    )

    asyncio.run(get_usage_recorder().flush())
    rows = asyncio.run(
        get_usage_store().query_range(
            "victim-tenant", TODAY - timedelta(days=1), TODAY
        )
    )
    assert rows == []


def test_cross_tenant_path_records_under_key_tenant(client, auth_headers):
    """A legacy single-tenant key on another tenant's path is SERVED as its
    own tenant (documented get_tenant semantics) — the usage row must land
    under that same authenticated tenant, never the path tenant."""
    import asyncio

    from cograph_client.usage.recorder import get_usage_recorder
    from cograph_client.usage.store import get_usage_store

    assert (
        client.get("/graphs/other-tenant/jobs", headers=auth_headers).status_code
        == 200
    )
    asyncio.run(get_usage_recorder().flush())
    store = get_usage_store()
    assert (
        asyncio.run(
            store.query_range("other-tenant", TODAY - timedelta(days=1), TODAY)
        )
        == []
    )
    own = asyncio.run(
        store.query_range("test-tenant", TODAY - timedelta(days=1), TODAY)
    )
    assert sum(r.requests for r in own) >= 1


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

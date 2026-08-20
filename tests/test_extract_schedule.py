"""Recurring reads for a saved extract source (ONTA-555).

The point of these tests is that scheduling did NOT grow a second scheduler:
a cadence is an ordinary row in the shared schedule store, it fires through the
same ``dispatch_scheduled_action`` seam every other action uses, and the work it
does is the same shared saved-run path the manual Run button calls.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from infona_client.api_registry.secret_store import reset_tenant_secret_store
from infona_client.enrichment.models import JobCategory
from infona_client.ingestion.extract_source_store import (
    StoredExtractSource,
    make_extract_source_store,
    reset_extract_source_store,
)
from infona_client.ingestion.models import (
    DltExtractSource,
    DltResourceMap,
    DltSourceSpec,
    ExtractScheduleRequest,
    MIN_SCHEDULE_INTERVAL_SECONDS,
)
from infona_client.ingestion.schedule import (
    EXTRACT_ACTION,
    DAILY,
    delete_extract_schedule,
    find_extract_schedule,
    upsert_extract_schedule,
)
from infona_client.resolver.models import IngestResult
from infona_client.scheduling.models import USER_SCHEDULABLE_ACTIONS
from infona_client.scheduling.store import InMemoryScheduleStore, reset_schedule_store

TENANT = "test-tenant"

CREATE = {
    "slug": "crm",
    "title": "Example CRM",
    "source": {
        "kind": "rest_api",
        "base_url": "https://api.example.com",
        "auth": {"type": "bearer", "secret_ref": "token"},
        "resources": ["v1/contacts"],
    },
    "map": {"v1/contacts": {"type": "Contact", "id_field": "id"}},
    "kg": "people",
}


def setup_function():
    reset_extract_source_store()
    reset_tenant_secret_store()
    # The app's schedule store is a process singleton — a cadence left behind by
    # one test would show up in the next one's /schedules assertion.
    reset_schedule_store()


def _source(slug: str = "crm", *, mapped: bool = True, enabled: bool = True):
    return DltExtractSource(
        slug=slug,
        title="CRM",
        kind="rest_api",
        source=DltSourceSpec(
            kind="rest_api",
            base_url="https://api.example.com",
            auth={"type": "bearer", "secret_ref": "token"},
            resources=["v1/contacts"],
        ),
        map={"v1/contacts": DltResourceMap(type="Contact", id_field="id")} if mapped else {},
        kg="people",
        enabled=enabled,
    )


# --- the vocabulary -----------------------------------------------------------


def test_extract_is_user_schedulable():
    """A workspace sets its own read cadence — this is not a platform-managed row."""
    assert EXTRACT_ACTION in USER_SCHEDULABLE_ACTIONS


# --- the mapping layer --------------------------------------------------------


async def test_upsert_creates_then_replaces_one_row():
    store = InMemoryScheduleStore()
    first = await upsert_extract_schedule(
        store,
        tenant_id=TENANT,
        slug="crm",
        kg_name="people",
        body=ExtractScheduleRequest(interval_seconds=DAILY),
    )
    assert first.action == EXTRACT_ACTION
    assert first.category == JobCategory.ingest
    assert first.params == {"extract_slug": "crm"}
    assert first.next_run is not None and first.next_run > datetime.now(timezone.utc)

    second = await upsert_extract_schedule(
        store,
        tenant_id=TENANT,
        slug="crm",
        kg_name="people",
        body=ExtractScheduleRequest(interval_seconds=3600),
    )
    assert second.id == first.id, "changing the cadence must not mint a second row"
    assert len(await store.list_for_tenant(TENANT)) == 1
    assert second.interval_seconds == 3600


async def test_delete_is_idempotent():
    store = InMemoryScheduleStore()
    assert await delete_extract_schedule(store, TENANT, "crm") is False
    await upsert_extract_schedule(
        store,
        tenant_id=TENANT,
        slug="crm",
        kg_name="people",
        body=ExtractScheduleRequest(interval_seconds=DAILY),
    )
    assert await delete_extract_schedule(store, TENANT, "crm") is True
    assert await find_extract_schedule(store, TENANT, "crm") is None


def test_interval_floor_is_enforced():
    with pytest.raises(ValueError):
        ExtractScheduleRequest(interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS - 1)
    with pytest.raises(ValueError):
        ExtractScheduleRequest(interval_seconds=DAILY, cron="0 * * * *")
    with pytest.raises(ValueError):
        ExtractScheduleRequest()
    assert ExtractScheduleRequest(cron="0 6 * * *").interval_seconds is None


# --- firing -------------------------------------------------------------------


async def test_dispatch_runs_the_shared_saved_run_path():
    """A fired schedule does the SAME work as pressing Run — same handoff."""
    from infona_client.api.routes.actions import dispatch_scheduled_action

    store = make_extract_source_store()
    await store.upsert(StoredExtractSource(tenant_id=TENANT, source=_source()))
    schedules = InMemoryScheduleStore()
    schedule = await upsert_extract_schedule(
        schedules,
        tenant_id=TENANT,
        slug="crm",
        kg_name="people",
        body=ExtractScheduleRequest(interval_seconds=DAILY),
    )

    with patch(
        "infona_client.ingestion.run.run_dlt_ingest",
        new=AsyncMock(return_value=IngestResult(rows_in=3, triples_inserted=9)),
    ) as run:
        job = await dispatch_scheduled_action(
            schedule, client=object(), job_store=None, executor=None
        )

    assert job is None, "the run path opens its own file-ingest job row"
    assert run.await_count == 1
    body = run.await_args.kwargs["body"]
    assert body.kg == "people"
    assert body.source.auth.secret_ref == "store:dlt:crm/token"


async def test_fire_follows_the_source_graph_not_the_stale_row():
    """Re-point a source at another KG and the cadence follows it.

    The schedule row keeps a ``kg_name`` for labelling, but the fire reads the
    SAVED SOURCE — otherwise a scheduled read would keep writing to the graph
    the source was pointed at when the cadence was created, while Read now wrote
    to the new one.
    """
    from infona_client.ingestion.saved_run import run_scheduled_extract

    store = make_extract_source_store()
    await store.upsert(StoredExtractSource(tenant_id=TENANT, source=_source()))
    schedules = InMemoryScheduleStore()
    schedule = await upsert_extract_schedule(
        schedules,
        tenant_id=TENANT,
        slug="crm",
        kg_name="people",
        body=ExtractScheduleRequest(interval_seconds=DAILY),
    )
    moved = _source().model_copy(update={"kg": "customers"})
    await store.upsert(StoredExtractSource(tenant_id=TENANT, source=moved))

    with patch(
        "infona_client.ingestion.run.run_dlt_ingest",
        new=AsyncMock(return_value=IngestResult(rows_in=1)),
    ) as run:
        await run_scheduled_extract(schedule, neptune=object())
    assert run.await_args.kwargs["body"].kg == "customers"


def test_patch_repoints_the_cadence_label(client, auth_headers):
    client.post("/graphs/test-tenant/extract-sources", json=CREATE, headers=auth_headers)
    client.put(
        "/graphs/test-tenant/extract-sources/crm/schedule",
        json={"interval_seconds": DAILY},
        headers=auth_headers,
    )
    client.patch(
        "/graphs/test-tenant/extract-sources/crm",
        json={"kg": "customers"},
        headers=auth_headers,
    )
    rows = client.get("/graphs/test-tenant/schedules", headers=auth_headers).json()
    assert [r["kg_name"] for r in rows] == ["customers"]


@pytest.mark.parametrize("reason", ["missing", "disabled"])
async def test_dispatch_skips_a_stale_schedule(reason):
    """A row pointing at a deleted / paused source must not raise mid-sweep."""
    from infona_client.ingestion.saved_run import run_scheduled_extract

    store = make_extract_source_store()
    if reason == "disabled":
        await store.upsert(
            StoredExtractSource(tenant_id=TENANT, source=_source(enabled=False))
        )
    schedules = InMemoryScheduleStore()
    schedule = await upsert_extract_schedule(
        schedules,
        tenant_id=TENANT,
        slug="crm",
        kg_name="people",
        body=ExtractScheduleRequest(interval_seconds=DAILY),
    )
    with patch("infona_client.ingestion.run.run_dlt_ingest", new=AsyncMock()) as run:
        assert await run_scheduled_extract(schedule, neptune=object()) is None
    assert run.await_count == 0


# --- routes -------------------------------------------------------------------


def test_schedule_routes_round_trip(client, auth_headers):
    created = client.post(
        "/graphs/test-tenant/extract-sources", json=CREATE, headers=auth_headers
    )
    assert created.status_code == 200, created.text
    assert created.json()["schedule"] is None

    put = client.put(
        "/graphs/test-tenant/extract-sources/crm/schedule",
        json={"interval_seconds": DAILY},
        headers=auth_headers,
    )
    assert put.status_code == 200, put.text
    schedule = put.json()["schedule"]
    assert schedule["interval_seconds"] == DAILY
    assert schedule["enabled"] is True
    assert schedule["next_run"]

    listed = client.get("/graphs/test-tenant/extract-sources", headers=auth_headers)
    assert listed.json()[0]["schedule"]["id"] == schedule["id"]

    got = client.get("/graphs/test-tenant/extract-sources/crm", headers=auth_headers)
    assert got.json()["schedule"]["interval_seconds"] == DAILY

    cleared = client.delete(
        "/graphs/test-tenant/extract-sources/crm/schedule", headers=auth_headers
    )
    assert cleared.status_code == 200
    assert cleared.json()["schedule"] is None


def test_schedule_rejects_a_source_that_cannot_run_yet(client, auth_headers):
    unmapped = {**CREATE, "slug": "unmapped", "map": {}}
    assert (
        client.post(
            "/graphs/test-tenant/extract-sources", json=unmapped, headers=auth_headers
        ).status_code
        == 200
    )
    resp = client.put(
        "/graphs/test-tenant/extract-sources/unmapped/schedule",
        json={"interval_seconds": DAILY},
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert "map" in resp.text


def test_schedule_404s_for_an_unknown_source(client, auth_headers):
    resp = client.put(
        "/graphs/test-tenant/extract-sources/ghost/schedule",
        json={"interval_seconds": DAILY},
        headers=auth_headers,
    )
    assert resp.status_code == 404


def test_deleting_a_source_removes_its_cadence(client, auth_headers):
    client.post("/graphs/test-tenant/extract-sources", json=CREATE, headers=auth_headers)
    client.put(
        "/graphs/test-tenant/extract-sources/crm/schedule",
        json={"interval_seconds": DAILY},
        headers=auth_headers,
    )
    assert (
        client.delete(
            "/graphs/test-tenant/extract-sources/crm", headers=auth_headers
        ).status_code
        == 200
    )
    remaining = client.get("/graphs/test-tenant/schedules", headers=auth_headers)
    assert remaining.status_code == 200
    assert remaining.json() == [], "a cadence must not outlive its source"

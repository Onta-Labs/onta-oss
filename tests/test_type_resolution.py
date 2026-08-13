"""Type-name resolution guard for enrichment.

Root-cause fix for the silent no-op: entity select keys on the declared type
name case-sensitively, so a miscased/unknown type (e.g. a lowercase
``organization`` vs the declared ``Organization``) matched zero entities and
the job finished "Completed" having enriched nothing.

Covers the shared resolver, the executor safety net (which guards EVERY caller
of ``run()`` — direct enrich, schedules, actions), and the enrich route's
up-front 422 / auto-correction.

ONTA-527: types come from the ontology catalog (GraphStore), not SPARQL rows.
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from infona_client.enrichment.cache import EnrichmentCache
from infona_client.enrichment.executor import EnrichmentExecutor
from infona_client.enrichment.job_store import InMemoryJobStore
from infona_client.enrichment.models import (
    ConflictPolicy,
    EnrichJob,
    EnrichmentTier,
    JobStatus,
)
from infona_client.enrichment.strategy import (
    list_declared_types,
    resolve_type_name,
    unknown_type_message,
)
from infona_client.graph.client import NeptuneClient
from infona_client.graph.store import reset_graph_store_for_tests
from tests._enrichment_prov_helpers import seed_declared_types


def _fake_neptune():
    n = AsyncMock(spec=NeptuneClient)
    n.query.side_effect = AssertionError("enrich type resolution must not SPARQL")
    n.update.return_value = None
    return n


# ── resolver unit tests ──────────────────────────────────────────────────────


def test_resolve_exact_match():
    async def run():
        await seed_declared_types(["Organization", "Person"], tenant_id="t")
        canonical, known = await resolve_type_name(_fake_neptune(), "t", "Organization")
        assert canonical == "Organization"
        assert set(known) == {"Organization", "Person"}

    asyncio.run(run())


def test_resolve_case_insensitive_autocorrect():
    async def run():
        await seed_declared_types(["Organization", "Person"], tenant_id="t")
        canonical, _ = await resolve_type_name(_fake_neptune(), "t", "organization")
        assert canonical == "Organization"

    asyncio.run(run())


def test_resolve_unknown_type_returns_none_with_known():
    async def run():
        await seed_declared_types(["Organization", "Person"], tenant_id="t")
        canonical, known = await resolve_type_name(_fake_neptune(), "t", "Widget")
        assert canonical is None
        assert known  # non-empty → the caller rejects rather than proceeds

    asyncio.run(run())


def test_resolve_fails_open_on_read_error():
    async def run():
        reset_graph_store_for_tests()
        canonical, known = await resolve_type_name(_fake_neptune(), "t", "organization")
        assert canonical is None
        assert known == []  # empty → the caller proceeds unchanged

    asyncio.run(run())


def test_resolve_fails_open_on_empty_ontology():
    async def run():
        assert await resolve_type_name(_fake_neptune(), "t", "organization") == (None, [])

    asyncio.run(run())


def test_list_declared_types_empty_catalog_is_empty():
    async def run():
        assert await list_declared_types(_fake_neptune(), "t") == []

    asyncio.run(run())


def test_resolve_trims_whitespace_and_stores_canonical():
    async def run():
        await seed_declared_types(["Organization"], tenant_id="t")
        canonical, _ = await resolve_type_name(_fake_neptune(), "t", "  organization  ")
        assert canonical == "Organization"

    asyncio.run(run())


def test_resolve_exact_match_wins_over_case_variant():
    async def run():
        await seed_declared_types(["Organization", "organization"], tenant_id="t")
        assert (await resolve_type_name(_fake_neptune(), "t", "organization"))[0] == "organization"
        assert (await resolve_type_name(_fake_neptune(), "t", "Organization"))[0] == "Organization"

    asyncio.run(run())


def test_unknown_type_message_lists_available_types():
    msg = unknown_type_message("organisation", ["Organization", "Person"])
    assert "organisation" in msg
    assert "Organization" in msg and "Person" in msg


# ── executor safety net (covers schedules/actions, not only the route) ────────


def _make_job(type_name):
    return EnrichJob(
        id="job-x",
        tenant_id="test-tenant",
        kg_name="kg",
        type_name=type_name,
        attributes=["url"],
        tier=EnrichmentTier.lite,
        status=JobStatus.queued,
        created_at=datetime.now(timezone.utc),
        conflict_policy=ConflictPolicy.skip,
    )


def test_executor_fails_unknown_type_with_clear_error():
    async def run():
        await seed_declared_types(["Organization", "Person"])
        store = InMemoryJobStore()
        job = _make_job("Widget")
        await store.create(job)
        executor = EnrichmentExecutor(_fake_neptune(), store, EnrichmentCache(), AsyncMock())
        await executor.run(job, "test-tenant")
        final = await store.get(job.id)
        assert final.status == JobStatus.failed
        assert "doesn't exist" in (final.error or "")
        assert final.error_summary and final.error_summary[0].kind == "job"

    asyncio.run(run())


def test_executor_autocorrects_miscased_type():
    async def run():
        await seed_declared_types(["Organization"])
        store = InMemoryJobStore()
        job = _make_job("organization")
        await store.create(job)
        executor = EnrichmentExecutor(_fake_neptune(), store, EnrichmentCache(), AsyncMock())
        await executor.run(job, "test-tenant")
        final = await store.get(job.id)
        assert final.type_name == "Organization"
        assert final.status != JobStatus.failed

    asyncio.run(run())


def test_executor_fails_open_when_no_types_declared():
    async def run():
        store = InMemoryJobStore()
        job = _make_job("whatever")
        await store.create(job)
        executor = EnrichmentExecutor(_fake_neptune(), store, EnrichmentCache(), AsyncMock())
        await executor.run(job, "test-tenant")
        final = await store.get(job.id)
        assert final.status != JobStatus.failed
        assert final.type_name == "whatever"

    asyncio.run(run())


# ── enrich route (immediate feedback on the direct path) ──────────────────────


def test_route_rejects_unknown_type_422(client, auth_headers):
    asyncio.run(seed_declared_types(["Organization", "Person"]))
    resp = client.post(
        "/graphs/test-tenant/enrich/jobs",
        headers=auth_headers,
        json={
            "type_name": "Widget",
            "attributes": ["url"],
            "kg_name": "kg",
            "tier": "lite",
        },
    )
    assert resp.status_code == 422
    assert "doesn't exist" in resp.json()["detail"]


def test_route_autocorrects_miscased_type(client, auth_headers):
    asyncio.run(seed_declared_types(["Organization"]))
    resp = client.post(
        "/graphs/test-tenant/enrich/jobs",
        headers=auth_headers,
        json={
            "type_name": "organization",
            "attributes": ["url"],
            "kg_name": "kg",
            "tier": "lite",
        },
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["routing_note"] and "Organization" in data["routing_note"]
    got = client.get(
        f"/graphs/test-tenant/enrich/jobs/{data['job_id']}", headers=auth_headers
    )
    assert got.json()["type_name"] == "Organization"


def test_route_proceeds_when_ontology_unavailable(client, auth_headers):
    # Empty catalog → fail-open, so a normal create still succeeds.
    resp = client.post(
        "/graphs/test-tenant/enrich/jobs",
        headers=auth_headers,
        json={
            "type_name": "Product",
            "attributes": ["url"],
            "kg_name": "kg",
            "tier": "lite",
        },
    )
    assert resp.status_code == 202

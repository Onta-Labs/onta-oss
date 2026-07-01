"""Type-name resolution guard for enrichment.

Root-cause fix for the silent no-op: the entity SELECT keys on
``?e a <types/Name>`` case-sensitively, so a miscased/unknown type (e.g. a
lowercase ``organization`` vs the declared ``Organization``) matched zero
entities and the job finished "Completed" having enriched nothing.

Covers the shared resolver, the executor safety net (which guards EVERY caller
of ``run()`` — direct enrich, schedules, actions), and the enrich route's
up-front 422 / auto-correction.
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from cograph_client.enrichment.cache import EnrichmentCache
from cograph_client.enrichment.executor import EnrichmentExecutor
from cograph_client.enrichment.job_store import InMemoryJobStore
from cograph_client.enrichment.models import (
    ConflictPolicy,
    EnrichJob,
    EnrichmentTier,
    JobStatus,
)
from cograph_client.enrichment.strategy import (
    list_declared_types,
    resolve_type_name,
    unknown_type_message,
)
from cograph_client.graph.client import NeptuneClient


def _types_response(names):
    """SPARQL result mirroring list_types_query: a ?type class URI + ?label."""
    bindings = [
        {
            "type": {"type": "uri", "value": f"https://cograph.tech/types/{n}"},
            "label": {"type": "literal", "value": n},
        }
        for n in names
    ]
    return {
        "head": {"vars": ["type", "label", "comment", "parent"]},
        "results": {"bindings": bindings},
    }


def _fake_neptune(query_return=None, query_side_effect=None):
    n = AsyncMock(spec=NeptuneClient)
    if query_side_effect is not None:
        n.query.side_effect = query_side_effect
    else:
        n.query.return_value = query_return
    n.update.return_value = None
    return n


# ── resolver unit tests ──────────────────────────────────────────────────────


def test_resolve_exact_match():
    async def run():
        n = _fake_neptune(_types_response(["Organization", "Person"]))
        canonical, known = await resolve_type_name(n, "t", "Organization")
        assert canonical == "Organization"
        assert set(known) == {"Organization", "Person"}

    asyncio.run(run())


def test_resolve_case_insensitive_autocorrect():
    async def run():
        n = _fake_neptune(_types_response(["Organization", "Person"]))
        canonical, _ = await resolve_type_name(n, "t", "organization")
        assert canonical == "Organization"

    asyncio.run(run())


def test_resolve_unknown_type_returns_none_with_known():
    async def run():
        n = _fake_neptune(_types_response(["Organization", "Person"]))
        canonical, known = await resolve_type_name(n, "t", "Widget")
        assert canonical is None
        assert known  # non-empty → the caller rejects rather than proceeds

    asyncio.run(run())


def test_resolve_fails_open_on_read_error():
    async def run():
        n = _fake_neptune(query_side_effect=RuntimeError("neptune down"))
        canonical, known = await resolve_type_name(n, "t", "organization")
        assert canonical is None
        assert known == []  # empty → the caller proceeds unchanged

    asyncio.run(run())


def test_resolve_fails_open_on_empty_ontology():
    async def run():
        n = _fake_neptune(_types_response([]))
        assert await resolve_type_name(n, "t", "organization") == (None, [])

    asyncio.run(run())


def test_list_declared_types_ignores_non_type_rows():
    # An entities-style response (no ?type binding) yields no declared types →
    # fail-open. This is exactly what keeps the many blanket-AsyncMock executor
    # tests green now that run() resolves the type first.
    async def run():
        entities_resp = {
            "head": {"vars": ["e", "label", "vals"]},
            "results": {
                "bindings": [
                    {
                        "e": {
                            "type": "uri",
                            "value": "https://cograph.tech/entities/Product/p1",
                        },
                        "label": {"type": "literal", "value": "Bosch"},
                    }
                ]
            },
        }
        n = _fake_neptune(entities_resp)
        assert await list_declared_types(n, "t") == []

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
        neptune = _fake_neptune(_types_response(["Organization", "Person"]))
        store = InMemoryJobStore()
        job = _make_job("Widget")
        await store.create(job)
        executor = EnrichmentExecutor(neptune, store, EnrichmentCache(), AsyncMock())
        await executor.run(job, "test-tenant")
        final = await store.get(job.id)
        assert final.status == JobStatus.failed
        assert "doesn't exist" in (final.error or "")
        assert final.error_summary and final.error_summary[0].kind == "job"

    asyncio.run(run())


def test_executor_autocorrects_miscased_type():
    async def run():
        # Blanket types response: list_types returns it (→ correct), then the
        # later strategy/SELECT reads parse to nothing → an empty but NOT failed
        # run. The point under test is that type_name was corrected in place.
        neptune = _fake_neptune(_types_response(["Organization"]))
        store = InMemoryJobStore()
        job = _make_job("organization")
        await store.create(job)
        executor = EnrichmentExecutor(neptune, store, EnrichmentCache(), AsyncMock())
        await executor.run(job, "test-tenant")
        final = await store.get(job.id)
        assert final.type_name == "Organization"
        assert final.status != JobStatus.failed

    asyncio.run(run())


def test_executor_fails_open_when_no_types_declared():
    async def run():
        # Empty ontology read → known == [] → proceed unchanged (never a false
        # fail when the type list genuinely can't be loaded).
        neptune = _fake_neptune(_types_response([]))
        store = InMemoryJobStore()
        job = _make_job("whatever")
        await store.create(job)
        executor = EnrichmentExecutor(neptune, store, EnrichmentCache(), AsyncMock())
        await executor.run(job, "test-tenant")
        final = await store.get(job.id)
        assert final.status != JobStatus.failed
        assert final.type_name == "whatever"

    asyncio.run(run())


# ── enrich route (immediate feedback on the direct path) ──────────────────────


def test_route_rejects_unknown_type_422(client, auth_headers, mock_neptune):
    mock_neptune.query.return_value = _types_response(["Organization", "Person"])
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


def test_route_autocorrects_miscased_type(client, auth_headers, mock_neptune):
    mock_neptune.query.return_value = _types_response(["Organization"])
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
    # The created job carries the canonical type, not the miscased input.
    got = client.get(
        f"/graphs/test-tenant/enrich/jobs/{data['job_id']}", headers=auth_headers
    )
    assert got.json()["type_name"] == "Organization"


def test_route_proceeds_when_ontology_unavailable(client, auth_headers, mock_neptune):
    # Empty type list (read failed / none declared) → fail-open, so a normal
    # create still succeeds — no regression when the type list can't load.
    mock_neptune.query.return_value = {"head": {"vars": []}, "results": {"bindings": []}}
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

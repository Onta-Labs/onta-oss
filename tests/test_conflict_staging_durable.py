"""ONTA-246 — durable conflict staging.

MECHANISM tests on INVENTED Widget schema. Proves:
  * the default job store is durable (Postgres) whenever a DSN is configured;
  * a staged conflict carries BOTH sources' provenance (incumbent + proposed);
  * the staged conflict survives a store round-trip (serialize → validate, the
    durability boundary) and `apply_decisions` still resolves it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from cograph_client.enrichment.cache import EnrichmentCache
from cograph_client.enrichment.executor import EnrichmentExecutor, _attr_uri
from cograph_client.enrichment.job_store import (
    InMemoryJobStore,
    PostgresJobStore,
    make_job_store,
)
from cograph_client.enrichment.models import (
    ConflictPolicy,
    ConflictReview,
    EnrichJob,
    JobStatus,
    RowResult,
    Verdict,
)

from tests._enrichment_prov_helpers import (
    FakeWikidata,
    all_updates,
    entities_query_response,
    make_job,
    query_router,
)


def test_make_job_store_defaults_to_postgres_when_dsn_present(monkeypatch):
    """The default store is durable (Postgres) whenever a DSN is configured, so the
    review queue survives a restart; in-memory only when no DSN."""
    from cograph_client.enrichment import job_store as js

    monkeypatch.setattr(js.settings, "database_url", "postgresql://x/y", raising=False)
    assert isinstance(make_job_store(), PostgresJobStore)

    monkeypatch.setattr(js.settings, "database_url", None, raising=False)
    js.reset_job_store()
    assert isinstance(make_job_store(), InMemoryJobStore)


def test_staged_conflict_carries_both_sources(monkeypatch):
    """A staged conflict row carries BOTH sources' provenance: the incumbent value's
    source (read from its companions at selection time) AND the proposed verdict's
    source. Invented Widget schema."""
    import cograph_client.api.routes.explore as explore_mod

    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)

    async def run():
        sku_pred = _attr_uri("Widget", "sku")
        src_pred = _attr_uri("Widget", "sku_source_url")
        vat_pred = _attr_uri("Widget", "sku_verified_at")
        existing_stamp = "2020-01-01T00:00:00+00:00"
        rows = [{
            "uri": "https://cograph.tech/entities/Widget/e1",
            "label": "Alpha Widget",
            "vals": (
                f"{sku_pred}::WX-OLD||"
                f"{src_pred}::https://old.example/sku||"
                f"{vat_pred}::{existing_stamp}"
            ),
        }]
        neptune = AsyncMock()
        neptune.query.side_effect = query_router(entities_query_response(rows))
        neptune.update.return_value = None
        executor = EnrichmentExecutor(
            neptune, InMemoryJobStore(), EnrichmentCache(),
            FakeWikidata({("Alpha Widget", "sku"): [
                Verdict(value="WX-NEW", confidence=0.9, source="web",
                        source_url="https://new.example/sku")
            ]}),
        )
        job = make_job(type_name="Widget", attributes=["sku"], policy=ConflictPolicy.stage)
        await executor._jobs.create(job)
        await executor.run(job, "test-tenant")

        final = await executor._jobs.get(job.id)
        assert final.status == JobStatus.review
        conflict = next(r for r in final.results if r.action == "conflict")
        assert conflict.existing_value == "WX-OLD"
        assert conflict.existing_source_url == "https://old.example/sku"
        assert conflict.existing_verified_at == existing_stamp
        assert conflict.verdict.source_url == "https://new.example/sku"

    asyncio.run(run())


def test_conflict_survives_store_roundtrip_and_applies():
    """The staged conflict (with BOTH sources) survives a job-store round-trip
    (serialize → validate, the durability boundary) and `apply_decisions` still
    resolves it — proving the review queue is durable, not lost on restart."""

    async def run():
        row = RowResult(
            entity_uri="https://cograph.tech/entities/Widget/e1",
            attribute="sku",
            existing_value="WX-OLD",
            verdict=Verdict(value="WX-NEW", confidence=0.9, source="web",
                            source_url="https://new.example/sku"),
            action="conflict",
            existing_source_url="https://old.example/sku",
            existing_verified_at="2020-01-01T00:00:00+00:00",
        )
        job = make_job(type_name="Widget", attributes=["sku"], policy=ConflictPolicy.stage)
        job.status = JobStatus.review
        job.results = [row]

        # The PostgresJobStore durability boundary: full JSON serialize → re-validate
        # (exactly what create()/get() do across a restart).
        restored = EnrichJob.model_validate_json(job.model_dump_json())
        assert restored.status == JobStatus.review
        r = restored.results[0]
        assert r.existing_source_url == "https://old.example/sku"
        assert r.existing_verified_at == "2020-01-01T00:00:00+00:00"
        assert r.verdict.source_url == "https://new.example/sku"

        # apply_decisions still resolves it: accept the proposal → value written.
        neptune = AsyncMock()
        neptune.query.side_effect = query_router(entities_query_response([]))
        neptune.update.return_value = None
        store = InMemoryJobStore()
        await store.create(restored)
        executor = EnrichmentExecutor(neptune, store, EnrichmentCache(), FakeWikidata({}))
        n = await executor.apply_decisions(
            restored.id,
            [ConflictReview(
                entity_uri=r.entity_uri, attribute=r.attribute,
                existing_value=r.existing_value, proposed=r.verdict, decision="accept",
            )],
        )
        assert n == 1
        writes = all_updates(neptune)
        assert "WX-NEW" in writes  # accepted value written via the shared path
        assert _attr_uri("Widget", "sku_source_url") in writes  # its provenance stamped

    asyncio.run(run())

"""Tests for the bounded server-side ``wait_for_job`` long-poll route and the
``JobStatus.is_terminal()`` helper it defers to (persona-eval async-settling
blocker).

Covers, with invented data (no persona/domain special-casing):

- ``is_terminal()`` classifies queued/running as in-flight and everything else
  as terminal.
- ``wait_for_job`` returns PROMPTLY when the job is ALREADY terminal.
- ``wait_for_job`` BLOCKS then returns the terminal job when it completes
  mid-wait (a gated fake store flips status after N reads).
- ``wait_for_job`` returns the job with its ``running`` status (HTTP 200, NOT an
  error) at the timeout for a still-running job.
- The wait is BOUNDED — it honors the cap, never busy-waits (it async-sleeps
  between reads, and a job store that never settles still returns by the cap).
- A partially-populated graph (a job still ``running`` with ``filled>0``) is
  queryable: the ``/query`` (SPARQL) path returns the entities landed so far,
  with no job-status gate.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

import cograph_client.api.routes.enrich as enrich_mod
from cograph_client.api.deps import get_enrichment_job_store
from cograph_client.enrichment.job_store import InMemoryJobStore
from cograph_client.enrichment.models import (
    ConflictPolicy,
    EnrichJob,
    EnrichmentTier,
    JobCategory,
    JobProgress,
    JobStatus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_job(
    *,
    job_id: str = "job-1",
    tenant_id: str = "test-tenant",
    status: JobStatus = JobStatus.running,
    progress: JobProgress | None = None,
) -> EnrichJob:
    return EnrichJob(
        id=job_id,
        tenant_id=tenant_id,
        kg_name="kg",
        type_name="Widget",
        attributes=["price"],
        tier=EnrichmentTier.lite,
        status=status,
        progress=progress or JobProgress(),
        created_at=datetime.now(timezone.utc),
        conflict_policy=ConflictPolicy.stage,
        category=JobCategory.discovery,
    )


class _GatedStore:
    """An in-memory job store whose ``get`` flips the job to a terminal status
    after a set number of reads — a deterministic stand-in for a job that
    completes mid-wait. Records how many times ``get`` was called so a test can
    assert the wait actually blocked (looped) rather than returning on read #1.
    """

    def __init__(self, job: EnrichJob, *, settle_after: int, final: JobStatus):
        self._job = job
        self._settle_after = settle_after
        self._final = final
        self.get_calls = 0

    async def get(self, job_id: str):
        self.get_calls += 1
        if job_id != self._job.id:
            return None
        if self.get_calls >= self._settle_after:
            self._job = self._job.model_copy(update={"status": self._final})
        return self._job.model_copy(deep=True)


def _override_store(app, store) -> None:
    """Point the wait route's job-store dependency at ``store``."""
    app.dependency_overrides[get_enrichment_job_store] = lambda: store


# ---------------------------------------------------------------------------
# JobStatus.is_terminal()
# ---------------------------------------------------------------------------


def test_is_terminal_classification():
    # In-flight: the job is still doing work and may still advance.
    assert JobStatus.queued.is_terminal() is False
    assert JobStatus.running.is_terminal() is False
    # Terminal: settled / errored / stopped / parked-for-review.
    assert JobStatus.applied.is_terminal() is True
    assert JobStatus.failed.is_terminal() is True
    assert JobStatus.cancelled.is_terminal() is True
    assert JobStatus.review.is_terminal() is True


# ---------------------------------------------------------------------------
# wait_for_job — already terminal returns promptly
# ---------------------------------------------------------------------------


def test_wait_returns_promptly_when_already_terminal(client, auth_headers, app):
    store = InMemoryJobStore()
    asyncio.run(
        store.create(_make_job(job_id="done", status=JobStatus.applied))
    )
    _override_store(app, store)

    loop = asyncio.new_event_loop()
    start = loop.time()
    r = client.get(
        "/graphs/test-tenant/enrich/jobs/done/wait?timeout_s=30",
        headers=auth_headers,
    )
    elapsed = loop.time() - start
    loop.close()

    assert r.status_code == 200
    assert r.json()["status"] == "applied"
    # An already-terminal job must not block for anything close to the timeout.
    assert elapsed < 2.0


def test_wait_returns_promptly_for_review_status(client, auth_headers, app):
    """`review` is terminal (run finished, parked for conflict decisions) — the
    waiter must return, not keep blocking as if the job were still working."""
    store = InMemoryJobStore()
    asyncio.run(
        store.create(_make_job(job_id="rev", status=JobStatus.review))
    )
    _override_store(app, store)

    r = client.get(
        "/graphs/test-tenant/enrich/jobs/rev/wait?timeout_s=30",
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert r.json()["status"] == "review"


# ---------------------------------------------------------------------------
# wait_for_job — blocks then returns terminal when the job completes mid-wait
# ---------------------------------------------------------------------------


def test_wait_blocks_then_returns_terminal_on_completion(
    client, auth_headers, app, monkeypatch
):
    # Shrink the poll interval so the test is fast but STILL exercises the
    # async-sleep loop (multiple reads before settling).
    monkeypatch.setattr(enrich_mod, "WAIT_POLL_INTERVAL_S", 0.01)

    job = _make_job(job_id="settling", status=JobStatus.running)
    store = _GatedStore(job, settle_after=3, final=JobStatus.applied)
    _override_store(app, store)

    r = client.get(
        "/graphs/test-tenant/enrich/jobs/settling/wait?timeout_s=30",
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert r.json()["status"] == "applied"
    # It must have LOOPED (blocked) — not returned on the first read — before the
    # job settled on the 3rd read.
    assert store.get_calls >= 3


# ---------------------------------------------------------------------------
# wait_for_job — timeout returns running (not an error)
# ---------------------------------------------------------------------------


def test_wait_returns_running_at_timeout_not_error(
    client, auth_headers, app, monkeypatch
):
    monkeypatch.setattr(enrich_mod, "WAIT_POLL_INTERVAL_S", 0.01)

    store = InMemoryJobStore()
    asyncio.run(
        store.create(_make_job(job_id="slow", status=JobStatus.running))
    )
    _override_store(app, store)

    # A never-settling job with a tiny timeout must return 200 + running, NOT
    # raise / 4xx / 5xx.
    r = client.get(
        "/graphs/test-tenant/enrich/jobs/slow/wait?timeout_s=0.05",
        headers=auth_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "running"
    assert body["id"] == "slow"


# ---------------------------------------------------------------------------
# wait_for_job — bounded: honors the cap, never busy-waits
# ---------------------------------------------------------------------------


def test_wait_is_bounded_by_the_cap(client, auth_headers, app, monkeypatch):
    """A requested timeout ABOVE the hard cap is clamped: the wait can never
    block longer than WAIT_MAX_TIMEOUT_S. We prove boundedness by clamping the
    cap itself down to a tiny value and asserting the call returns quickly with
    the still-running job."""
    monkeypatch.setattr(enrich_mod, "WAIT_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(enrich_mod, "WAIT_MAX_TIMEOUT_S", 0.1)

    store = InMemoryJobStore()
    asyncio.run(
        store.create(_make_job(job_id="cap", status=JobStatus.running))
    )
    _override_store(app, store)

    loop = asyncio.new_event_loop()
    start = loop.time()
    # Ask for far longer than the (patched) cap — it must clamp to the cap.
    r = client.get(
        "/graphs/test-tenant/enrich/jobs/cap/wait?timeout_s=9999",
        headers=auth_headers,
    )
    elapsed = loop.time() - start
    loop.close()

    assert r.status_code == 200
    assert r.json()["status"] == "running"
    # Bounded by the clamped cap (0.1s) — a generous ceiling proves it did not
    # honor the 9999s request and did not busy-spin indefinitely.
    assert elapsed < 3.0


def test_wait_does_not_busy_wait(app, monkeypatch):
    """The wait must async-SLEEP between reads, not busy-spin: over a fixed
    budget the number of job-store reads is bounded by budget / poll_interval,
    not thousands of tight-loop iterations."""
    monkeypatch.setattr(enrich_mod, "WAIT_POLL_INTERVAL_S", 0.02)
    monkeypatch.setattr(enrich_mod, "WAIT_MAX_TIMEOUT_S", 0.2)

    job = _make_job(job_id="spin", status=JobStatus.running)
    # Never settles.
    store = _GatedStore(job, settle_after=10**9, final=JobStatus.applied)

    class _Tenant:
        tenant_id = "test-tenant"

    asyncio.run(
        enrich_mod.wait_for_job(
            job_id="spin",
            timeout_s=9999.0,  # clamped to the 0.2s cap
            tenant=_Tenant(),
            job_store=store,
        )
    )
    # budget 0.2s / interval 0.02s ≈ 10 reads. A busy-wait would be thousands.
    # Give generous slack for scheduler jitter but stay far below a spin count.
    assert store.get_calls <= 40


# ---------------------------------------------------------------------------
# wait_for_job — auth / scoping parity with get_job
# ---------------------------------------------------------------------------


def test_wait_404_for_unknown_job(client, auth_headers, app):
    _override_store(app, InMemoryJobStore())
    r = client.get(
        "/graphs/test-tenant/enrich/jobs/nope/wait", headers=auth_headers
    )
    assert r.status_code == 404


def test_wait_404_for_other_tenants_job(client, auth_headers, app):
    store = InMemoryJobStore()
    asyncio.run(
        store.create(
            _make_job(
                job_id="theirs",
                tenant_id="other-tenant",
                status=JobStatus.applied,
            )
        )
    )
    _override_store(app, store)
    r = client.get(
        "/graphs/test-tenant/enrich/jobs/theirs/wait", headers=auth_headers
    )
    # Tenant-scoped: another tenant's job is not visible → 404, never leaked.
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Partial-graph queryability: a mid-build graph returns the entities so far
# ---------------------------------------------------------------------------


def test_partial_graph_is_queryable_mid_job(
    client, auth_headers, app, mock_neptune
):
    """A graph that is only PARTIALLY populated by an in-flight discovery job
    (job status still `running`, filled>0) is queryable: the read path hits the
    live graph store with NO job-status gate, so `/query` returns the entities
    landed so far. This is what lets 'graph populated with sourced entities' be
    confirmed before the job fully completes."""
    # A running discovery job that has already written some entities.
    store = InMemoryJobStore()
    asyncio.run(
        store.create(
            _make_job(
                job_id="mid",
                status=JobStatus.running,
                progress=JobProgress(total=200, processed=12, filled=12),
            )
        )
    )
    _override_store(app, store)

    # The live graph already has 12 Widget entities written by the in-flight job.
    mock_neptune.query.return_value = {
        "head": {"vars": ["cnt"]},
        "results": {
            "bindings": [{"cnt": {"type": "literal", "value": "12"}}]
        },
    }

    # Reading the graph mid-job returns the landed entities — the running job
    # never blocks or hides them.
    r = client.post(
        "/graphs/test-tenant/query",
        headers=auth_headers,
        json={
            "query": (
                "SELECT (COUNT(?e) AS ?cnt) WHERE "
                "{ ?e a <https://cograph.tech/types/Widget> }"
            )
        },
    )
    assert r.status_code == 200
    bindings = r.json()["bindings"]
    assert bindings[0]["cnt"] == "12"

    # And the job it came from is still running — we confirmed a partial graph.
    job = client.get(
        "/graphs/test-tenant/enrich/jobs/mid/wait?timeout_s=0.01",
        headers=auth_headers,
    ).json()
    assert job["status"] == "running"
    assert job["progress"]["filled"] == 12


@pytest.fixture(autouse=True)
def _clear_overrides(app):
    yield
    app.dependency_overrides.clear()

"""OSS-side unit tests for the ONTA-391 locate→scrape wiring in the discovery
capability.

These cover the OSS half of ONTA-391 — the seams the premium ``locate_scrape``
provider plugs into — without any external API key:

* **Provider-ladder policy (registry short-circuits ahead of the general default).**
  ``_merge_registry_ensemble`` keeps a registered API source (registry Tier -1)
  AHEAD of the general web provider, and ``api_only`` drops web spend entirely — so
  a registered API covering the ask short-circuits BEFORE the (Parallel-backed)
  general default ever runs. This is the OSS-observable form of the ticket's "Order
  0: registry" gate.
* **P1 stage-trace surfacing.** ``_record_locate_trace`` projects a provider's
  ``locate → select_urls → fetch`` step counts into the operator Job Trace, so P1
  shows the page-minimisation work (search calls, candidate URLs, URLs selected,
  pages fetched) and an honest miss reason — the ticket's "A1 stage-trace shows
  locate → select → fetch page counts".
* **Back-compat.** ``DiscoverResult.locate_trace`` defaults to ``None`` so an
  enumeration provider that never locates+scrapes is unaffected and records no
  locate/select/fetch actions.

No network, no LLM — pure offline unit tests (CI-safe).
"""

from __future__ import annotations

from datetime import datetime, timezone

from cograph_client.agent.capabilities.web_ingest_cap import (
    _merge_registry_ensemble,
    _record_locate_trace,
)
from cograph_client.api_registry.router import MODE_API_ONLY, MODE_API_PLUS_WEB
from cograph_client.enrichment.models import (
    ConflictPolicy,
    EnrichJob,
    EnrichmentTier,
    JobCategory,
    JobProgress,
    JobStatus,
    JobTrigger,
)
from cograph_client.pipeline.stage_trace import StageProjectId, attach_recorder
from cograph_client.web_sources.base import DiscoverResult


# --------------------------------------------------------------------------- #
# helpers / fakes
# --------------------------------------------------------------------------- #

class _Prov:
    """Minimal provider stand-in (identity is all the ensemble merge cares about)."""

    def __init__(self, name: str) -> None:
        self.name = name


def _job(**kw) -> EnrichJob:
    base = dict(
        id="job-ls-1",
        tenant_id="demo-tenant",
        kg_name="colleges",
        type_name="College",
        attributes=["name", "city"],
        tier=EnrichmentTier.lite,
        status=JobStatus.running,
        created_at=datetime.now(timezone.utc),
        conflict_policy=ConflictPolicy.stage,
        category=JobCategory.discovery,
        progress=JobProgress(total=50, processed=0, filled=0),
        trigger=JobTrigger.manual,
    )
    base.update(kw)
    return EnrichJob(**base)


def _p1_actions(job) -> list:
    # A true no-op leaves stage_trace None (the recorder was never attached).
    if job is None or getattr(job, "stage_trace", None) is None:
        return []
    p1 = next(
        p for p in job.stage_trace.projects if p.project_id == StageProjectId.p1
    )
    return list(p1.actions)


# --------------------------------------------------------------------------- #
# Policy: a registered API short-circuits AHEAD of the general (Parallel) default
# --------------------------------------------------------------------------- #

def test_registry_api_only_short_circuits_before_general_default():
    """``api_only`` → the registry source alone; the general web provider (the
    Parallel-backed ``locate_scrape`` default in prod) is dropped, no web spend."""
    general = _Prov("locate_scrape")
    registry = _Prov("api:collegescorecard")
    merged = _merge_registry_ensemble([general], [registry], MODE_API_ONLY)
    assert merged == [registry]
    assert general not in merged


def test_registry_api_plus_web_puts_registry_ahead_of_general():
    """``api_plus_web`` → registry FIRST, then the general default (dedupe-free
    overlap). The registered API is still consulted BEFORE Parallel."""
    general = _Prov("locate_scrape")
    registry = _Prov("api:collegescorecard")
    merged = _merge_registry_ensemble([general], [registry], MODE_API_PLUS_WEB)
    assert merged[0] is registry
    assert general in merged
    assert merged.index(registry) < merged.index(general)


def test_no_registry_source_leaves_general_default_untouched():
    general = _Prov("locate_scrape")
    assert _merge_registry_ensemble([general], [], MODE_API_ONLY) == [general]


# --------------------------------------------------------------------------- #
# P1 stage-trace surfacing (locate → select_urls → fetch [→ locate_miss])
# --------------------------------------------------------------------------- #

def test_record_locate_trace_emits_locate_select_fetch_actions():
    job = _job()
    trace = {
        "locate_calls": 1,
        "urls_located": 6,
        "urls_selected": 3,
        "pages_fetched": 2,
        "escalated": False,
        "skip_reason": None,
    }
    _record_locate_trace(job, trace, "locate_scrape", "colleges in BC")
    names = [a.name for a in _p1_actions(job)]
    assert names == ["locate", "select_urls", "fetch"]
    by = {a.name: a for a in _p1_actions(job)}
    assert by["locate"].meta["urls_located"] == 6
    assert by["locate"].meta["locate_calls"] == 1
    assert by["select_urls"].meta["urls_selected"] == 3
    assert by["fetch"].meta["pages_fetched"] == 2
    # page-minimisation is visible in the human-readable detail too
    assert "pages_fetched=2" in by["fetch"].detail


def test_record_locate_trace_escalation_and_miss_are_surfaced():
    job = _job()
    trace = {
        "locate_calls": 2,
        "urls_located": 4,
        "urls_selected": 1,
        "pages_fetched": 0,
        "escalated": True,
        "skip_reason": "located pages yielded no structured rows",
    }
    _record_locate_trace(job, trace, "locate_scrape", "obscure roster")
    by = {a.name: a for a in _p1_actions(job)}
    assert set(by) == {"locate", "select_urls", "fetch", "locate_miss"}
    assert by["locate"].meta["escalated"] is True
    assert "(escalated)" in by["fetch"].detail
    assert by["locate_miss"].meta["skip_reason"].startswith("located pages yielded")


def test_record_locate_trace_noop_without_trace_or_job():
    # None trace (an enumeration provider) → no P1 actions recorded.
    job = _job()
    _record_locate_trace(job, None, "openrouter_discovery", "anything")
    assert _p1_actions(job) == []
    # None job → no crash, no-op.
    _record_locate_trace(None, {"locate_calls": 1}, "x", "y")  # must not raise


def test_record_locate_trace_never_raises_on_bad_trace():
    job = _job()
    # A malformed trace (non-dict) is ignored, never sinks discovery.
    _record_locate_trace(job, ["not", "a", "dict"], "locate_scrape", "q")
    assert _p1_actions(job) == []


# --------------------------------------------------------------------------- #
# Back-compat: DiscoverResult.locate_trace default
# --------------------------------------------------------------------------- #

def test_discover_result_locate_trace_defaults_none():
    """An existing provider that constructs DiscoverResult with no locate_trace is
    unaffected (field defaults to None → capability records no locate actions)."""
    assert DiscoverResult().locate_trace is None
    assert DiscoverResult(rows=[{"name": "x"}]).locate_trace is None

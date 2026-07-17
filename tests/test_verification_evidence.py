"""Tests for the independent-evidence gathering verifier (ONTA-364).

All OFFLINE + DETERMINISTIC: a FAKE ``PageFetcher`` registered through the shared
substrate registry (``register_page_fetcher`` / reset in teardown) returns canned
``FetchedPage``s — NO real network. Each acceptance test carries a load-bearing
control (esp. the host-independence differential, which would fail if the
same-host rejection were a no-op).
"""

from __future__ import annotations

import asyncio

import pytest

import cograph_client.verification.evidence as ev
from cograph_client.resolver.models import CleanFact, CleanOutcome
from cograph_client.retrieval.errors import CostCeilingExceeded
from cograph_client.retrieval.fetch import (
    get_page_fetchers,
    register_page_fetcher,
    reset_page_fetchers,
)
from cograph_client.retrieval.types import FetchedPage
from cograph_client.verification.evidence import EvidenceVerifier
from cograph_client.verification.types import TruthVerdict
from cograph_client.verification.verifier import FactVerifier

SOURCE_HOST = "own-source.com"
DISTINCT_URL = "https://corroborator.org/report"
SAME_HOST_URL = "https://own-source.com/report"


# --- fakes ------------------------------------------------------------------- #


class FakePageFetcher:
    """A canned fetch ladder rung: returns pre-baked pages per URL, no network."""

    def __init__(
        self,
        pages: dict[str, FetchedPage],
        *,
        name: str = "fake",
        tier: int = 0,
        cost_per_call: float = 0.0,
        is_paid: bool = False,
        raises: Exception | None = None,
    ) -> None:
        self._pages = pages
        self.name = name
        self.tier = tier
        self.cost_per_call = cost_per_call
        self.is_paid = is_paid
        self._raises = raises
        self.calls: list[str] = []

    async def fetch(self, url: str, *, want: str = "") -> FetchedPage:
        self.calls.append(url)
        if self._raises is not None:
            raise self._raises
        page = self._pages.get(url)
        if page is None:
            return FetchedPage(url=url, ok=False, error="not found", tier=self.name)
        return page


def _page(url: str, text: str) -> FetchedPage:
    return FetchedPage(url=url, text=text, ok=True, tier="fake")


def _fact(raw: str = "42%", clean: str | None = "0.42") -> CleanFact:
    return CleanFact(
        datatype="string",
        raw_value=raw,
        clean_value=clean,
        outcome=CleanOutcome.TRANSFORMED,
        entity_id="entity/1",
        attribute="conversion_rate",
    )


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_page_fetchers()
    yield
    reset_page_fetchers()


# --- protocol conformance ---------------------------------------------------- #


def test_evidence_verifier_satisfies_factverifier_protocol():
    assert isinstance(EvidenceVerifier(source_host=SOURCE_HOST), FactVerifier)


# --- distinct-host corroboration ⇒ SUPPORTED --------------------------------- #


def test_distinct_host_corroboration_supported():
    fetcher = FakePageFetcher(
        {DISTINCT_URL: _page(DISTINCT_URL, "Independent audit found a 42% conversion rate.")}
    )
    register_page_fetcher(fetcher)  # reached via default_ladder() → get_page_fetchers()

    verifier = EvidenceVerifier(source_host=SOURCE_HOST, candidate_urls=[DISTINCT_URL])
    result = verifier.verify(_fact())

    assert result.verdict is TruthVerdict.SUPPORTED
    assert result.confidence > 0.0
    assert result.evidence, "expected at least one EvidenceRef"
    ref = result.evidence[0]
    # The evidence host is DISTINCT from the fact's own source host.
    assert ref.host.casefold() != SOURCE_HOST
    assert ref.host.casefold() == "corroborator.org"
    # The fetch went through the SHARED registry ladder (no private fetcher).
    assert fetcher.calls == [DISTINCT_URL]
    assert [f.name for f in get_page_fetchers()] == ["fake"]


# --- host-independence differential (LOAD-BEARING) --------------------------- #


def test_host_independence_differential():
    """SAME fact + SAME corroborating text: a DISTINCT host ⇒ SUPPORTED, but the
    fact's OWN source host as the only corroboration ⇒ UNVERIFIABLE. This proves
    the same-host rejection actually gates — it would fail if the rule were a no-op
    (the same-host page contains the value and would otherwise SUPPORT)."""
    corroborating = "Independent audit found a 42% conversion rate."
    fetcher = FakePageFetcher(
        {
            DISTINCT_URL: _page(DISTINCT_URL, corroborating),
            SAME_HOST_URL: _page(SAME_HOST_URL, corroborating),
        }
    )
    register_page_fetcher(fetcher)

    distinct = EvidenceVerifier(source_host=SOURCE_HOST, candidate_urls=[DISTINCT_URL])
    same = EvidenceVerifier(source_host=SOURCE_HOST, candidate_urls=[SAME_HOST_URL])

    distinct_result = distinct.verify(_fact())
    same_result = same.verify(_fact())

    assert distinct_result.verdict is TruthVerdict.SUPPORTED
    assert same_result.verdict is TruthVerdict.UNVERIFIABLE
    assert not same_result.evidence
    # The same-host candidate was rejected BEFORE any fetch — no cost, no page read.
    assert SAME_HOST_URL not in fetcher.calls
    assert "same-host" in same_result.reason


# --- surface-form (pre-coercion) comparison ---------------------------------- #


def test_matches_surface_form_not_only_coerced_value():
    """Evidence carries the surface form "42%" while A3 coerced it to "0.42"; the
    coerced value is ABSENT from the page. Matching the surface form still SUPPORTS
    — verification compares against the persisted surface form, not only the coerced
    value (ONTA-347)."""
    text = "Independent report: the rate is 42% (measured over Q3)."
    assert "0.42" not in text  # control: coerced value is NOT present
    fetcher = FakePageFetcher({DISTINCT_URL: _page(DISTINCT_URL, text)})
    register_page_fetcher(fetcher)

    verifier = EvidenceVerifier(source_host=SOURCE_HOST, candidate_urls=[DISTINCT_URL])
    result = verifier.verify(_fact(raw="42%", clean="0.42"))

    assert result.verdict is TruthVerdict.SUPPORTED
    assert "42%" in result.evidence[0].snippet


# --- distinct-host contradiction ⇒ REFUTED ----------------------------------- #


def test_distinct_host_contradiction_refuted():
    text = "Independent audit reports a 90% conversion rate, not the claimed figure."
    assert "42%" not in text  # control: the fact's value is absent
    fetcher = FakePageFetcher({DISTINCT_URL: _page(DISTINCT_URL, text)})
    register_page_fetcher(fetcher)

    verifier = EvidenceVerifier(
        source_host=SOURCE_HOST,
        candidate_urls=[DISTINCT_URL],
        contradicting_values=["90%"],
    )
    result = verifier.verify(_fact())

    assert result.verdict is TruthVerdict.REFUTED
    assert result.evidence
    assert result.evidence[0].host.casefold() == "corroborator.org"


# --- budget / cost-ceiling degrade ------------------------------------------- #


def test_tiny_budget_degrades_to_unverifiable_without_fetching():
    """A paid rung the budget can't afford is never called → clean UNVERIFIABLE."""
    fetcher = FakePageFetcher(
        {DISTINCT_URL: _page(DISTINCT_URL, "42% here")},
        cost_per_call=0.01,
        is_paid=True,
    )
    register_page_fetcher(fetcher)

    verifier = EvidenceVerifier(
        source_host=SOURCE_HOST, candidate_urls=[DISTINCT_URL], budget_usd=0.0
    )
    result = verifier.verify(_fact())

    assert result.verdict is TruthVerdict.UNVERIFIABLE
    assert fetcher.calls == []  # never spent — the paid rung was skipped
    assert "budget" in result.reason or "ceiling" in result.reason


def test_injected_cost_ceiling_exceeded_degrades_cleanly():
    """A rung raising CostCeilingExceeded degrades to UNVERIFIABLE — never raises."""
    fetcher = FakePageFetcher({}, raises=CostCeilingExceeded("run budget spent"))
    register_page_fetcher(fetcher)

    verifier = EvidenceVerifier(source_host=SOURCE_HOST, candidate_urls=[DISTINCT_URL])
    result = verifier.verify(_fact())  # must not raise

    assert result.verdict is TruthVerdict.UNVERIFIABLE


def test_generic_fetch_error_fails_closed():
    fetcher = FakePageFetcher({}, raises=RuntimeError("network down"))
    register_page_fetcher(fetcher)

    verifier = EvidenceVerifier(source_host=SOURCE_HOST, candidate_urls=[DISTINCT_URL])
    result = verifier.verify(_fact())  # must not raise

    assert result.verdict is TruthVerdict.UNVERIFIABLE


# --- shared cost seam -------------------------------------------------------- #


def test_cost_metered_through_shared_source_cost_seam(monkeypatch):
    """Fetch cost is read through the ONE shared retrieval.cost.source_cost reducer,
    not a private copy."""
    calls = {"n": 0}
    real = ev.source_cost

    def spy(source):
        calls["n"] += 1
        return real(source)

    monkeypatch.setattr(ev, "source_cost", spy)

    fetcher = FakePageFetcher({DISTINCT_URL: _page(DISTINCT_URL, "42% observed")})
    register_page_fetcher(fetcher)

    verifier = EvidenceVerifier(source_host=SOURCE_HOST, candidate_urls=[DISTINCT_URL])
    result = verifier.verify(_fact())

    assert result.verdict is TruthVerdict.SUPPORTED
    assert calls["n"] > 0, "source_cost (the shared cost seam) was never consulted"


# --- fail-closed when source host is unknown --------------------------------- #


def test_missing_source_host_withholds_verdict():
    fetcher = FakePageFetcher({DISTINCT_URL: _page(DISTINCT_URL, "42% observed")})
    register_page_fetcher(fetcher)

    verifier = EvidenceVerifier(candidate_urls=[DISTINCT_URL])  # no source host
    result = verifier.verify(_fact())

    assert result.verdict is TruthVerdict.UNVERIFIABLE
    assert "source host" in result.reason
    assert fetcher.calls == []  # never fetched — independence unprovable


def test_no_candidates_is_unverifiable():
    verifier = EvidenceVerifier(source_host=SOURCE_HOST)
    result = verifier.verify(_fact())
    assert result.verdict is TruthVerdict.UNVERIFIABLE


# --- source_url derivation + per-call overrides ------------------------------ #


def test_source_url_derives_host_and_gates_same_host():
    """A full source_url (not just a host) is accepted and its host gates."""
    fetcher = FakePageFetcher({SAME_HOST_URL: _page(SAME_HOST_URL, "42% here")})
    register_page_fetcher(fetcher)

    verifier = EvidenceVerifier(
        source_url="https://own-source.com/data/page?token=x",
        candidate_urls=[SAME_HOST_URL],
    )
    result = verifier.verify(_fact())

    assert result.verdict is TruthVerdict.UNVERIFIABLE
    assert SAME_HOST_URL not in fetcher.calls


# --- sync/async bridge ------------------------------------------------------- #


def test_averify_async_path():
    fetcher = FakePageFetcher({DISTINCT_URL: _page(DISTINCT_URL, "42% corroborated")})
    register_page_fetcher(fetcher)

    verifier = EvidenceVerifier(source_host=SOURCE_HOST, candidate_urls=[DISTINCT_URL])
    result = asyncio.run(verifier.averify(_fact()))

    assert result.verdict is TruthVerdict.SUPPORTED


def test_sync_verify_inside_running_loop_uses_thread_bridge():
    """verify() is sync but the substrate fetch is async; called from WITHIN a
    running loop it must still complete (worker-thread bridge), not deadlock or
    fail-close falsely."""
    fetcher = FakePageFetcher({DISTINCT_URL: _page(DISTINCT_URL, "42% corroborated")})
    register_page_fetcher(fetcher)

    async def driver():
        verifier = EvidenceVerifier(source_host=SOURCE_HOST, candidate_urls=[DISTINCT_URL])
        # A synchronous verify() call from inside a running event loop.
        return verifier.verify(_fact())

    result = asyncio.run(driver())
    assert result.verdict is TruthVerdict.SUPPORTED


def test_per_call_overrides_on_averify():
    """averify accepts source_host / candidate_urls / contradicting_values / budget
    as explicit per-call overrides (direct programmatic use without a ctor)."""
    fetcher = FakePageFetcher({DISTINCT_URL: _page(DISTINCT_URL, "rate is 90%")})
    register_page_fetcher(fetcher)

    verifier = EvidenceVerifier()  # nothing configured
    result = asyncio.run(
        verifier.averify(
            _fact(),
            source_host=SOURCE_HOST,
            candidate_urls=[DISTINCT_URL],
            contradicting_values=["90%"],
        )
    )
    assert result.verdict is TruthVerdict.REFUTED

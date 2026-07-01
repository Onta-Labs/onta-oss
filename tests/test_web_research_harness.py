"""Unit tests for the web-research harness (ADR 0006 / ONTA-166).

All offline — every stage is either injected as a fake or exercised with the
deterministic OSS default (the cite-or-abstain verifier). No network, no LLM.
"""

from __future__ import annotations

import dataclasses

import pytest

from cograph_client.research.fetch import (
    FetchedPage,
    StaticHttpFetcher,
    fetcher_cost,
    html_to_text,
    is_fetchable_url,
    register_page_fetcher,
    reset_page_fetchers,
)
from cograph_client.research.harness import WebResearchHarness
from cograph_client.research.types import (
    Budget,
    ResearchResult,
    ResearchRow,
    SchemaField,
    TargetSchema,
)
from cograph_client.research.verify import (
    CiteOrAbstainVerifier,
    VerifyOutcome,
    register_research_verifier,
    reset_research_verifier,
)
from cograph_client.web_sources.base import (
    DiscoverResult,
    register_web_source,
    reset_web_sources,
)


@pytest.fixture(autouse=True)
def _clean_registries():
    reset_page_fetchers()
    reset_research_verifier()
    reset_web_sources()
    yield
    reset_page_fetchers()
    reset_research_verifier()
    reset_web_sources()


class FakeFetcher:
    """A PageFetcher that returns canned pages and counts its calls."""

    def __init__(
        self,
        pages: dict | None = None,
        default: FetchedPage | None = None,
        *,
        name: str = "static",
        tier: int = 0,
        is_paid: bool = False,
        cost_per_call: float = 0.0,
    ) -> None:
        self.pages = pages or {}
        self.default = default
        self.name = name
        self.tier = tier
        self.is_paid = is_paid
        self.cost_per_call = cost_per_call
        self.calls = 0
        self.seen: list[str] = []

    async def fetch(self, url: str, *, want: str = "") -> FetchedPage:
        self.calls += 1
        self.seen.append(url)
        if url in self.pages:
            return self.pages[url]
        if self.default is not None:
            return dataclasses.replace(self.default, url=url)
        return FetchedPage(url=url, ok=False, error="not found")


def _schema() -> TargetSchema:
    return TargetSchema(
        entity="model",
        fields=[SchemaField(name="name"), SchemaField(name="score")],
    )


def _rich_page(url: str) -> FetchedPage:
    return FetchedPage(url=url, text="Model data. " * 50, title="Leaderboard", ok=True)


async def _rows_extractor(pages, schema, **kw):
    return [
        ResearchRow(
            values={"name": "Alpha", "score": "94"},
            citations=[pages[0].url],
            confidence=0.9,
        )
    ]


# --- happy path -------------------------------------------------------------- #
async def test_url_mode_fetches_extracts_verifies_and_synthesizes():
    url = "https://example.com/leaderboard"
    fetcher = FakeFetcher({url: _rich_page(url)})
    harness = WebResearchHarness(fetchers=[fetcher], extractor=_rows_extractor)

    res = await harness.run("list the models with scores", schema=_schema(), urls=[url])

    assert not res.abstained
    assert len(res.rows) == 1
    assert res.rows[0].values["name"] == "Alpha"
    assert [c.url for c in res.citations] == [url]
    assert res.confidence > 0.5
    csv = res.to_csv()
    assert "name,score,sources" in csv
    assert "Alpha,94," in csv
    assert fetcher.calls == 1


# --- cite-or-abstain --------------------------------------------------------- #
async def test_uncited_rows_are_dropped_and_run_abstains():
    url = "https://example.com/page"
    fetcher = FakeFetcher({url: _rich_page(url)})

    async def _uncited(pages, schema, **kw):
        return [ResearchRow(values={"name": "Ghost"}, citations=[], confidence=0.1)]

    harness = WebResearchHarness(fetchers=[fetcher], extractor=_uncited)
    res = await harness.run("q", schema=_schema(), urls=[url])

    assert res.abstained
    assert res.rows == []
    assert "couldn't verify" in res.answer.lower()


# --- budget: fetch cap ------------------------------------------------------- #
async def test_budget_caps_number_of_fetches():
    urls = [f"https://example.com/{i}" for i in range(4)]
    fetcher = FakeFetcher(default=_rich_page("x"))
    harness = WebResearchHarness(fetchers=[fetcher], extractor=_rows_extractor)

    budget = Budget(max_fetches=1, max_iterations=1)
    res = await harness.run("q", schema=_schema(), urls=urls, budget=budget)

    assert fetcher.calls == 1
    assert budget.fetches_used == 1
    assert res.iterations == 1


# --- fetch ladder escalation ------------------------------------------------- #
async def test_ladder_escalates_when_cheap_rung_is_incomplete():
    url = "https://example.com/js"
    cheap = FakeFetcher(default=FetchedPage(url=url, text="short", ok=True), name="static", tier=0)
    pricey = FakeFetcher(
        default=FetchedPage(url=url, text="rendered content " * 40, tier="render", ok=True),
        name="render",
        tier=2,
        is_paid=True,
        cost_per_call=0.02,
    )
    seen_tiers: list[str] = []

    async def _record(pages, schema, **kw):
        seen_tiers.extend(p.tier for p in pages)
        return [ResearchRow(values={"name": "R"}, citations=[pages[0].url], confidence=0.8)]

    harness = WebResearchHarness(fetchers=[cheap, pricey], extractor=_record)
    res = await harness.run("q", schema=_schema(), urls=[url], budget=Budget(max_fetches=4))

    assert cheap.calls == 1
    assert pricey.calls == 1  # escalated because the cheap rung looked incomplete
    assert "render" in seen_tiers  # extraction ran against the rendered page
    assert not res.abstained


async def test_ladder_stops_at_cheap_rung_when_complete():
    url = "https://example.com/ok"
    cheap = FakeFetcher(default=_rich_page(url), name="static", tier=0)
    pricey = FakeFetcher(default=_rich_page(url), name="render", tier=2, is_paid=True, cost_per_call=0.02)
    harness = WebResearchHarness(fetchers=[cheap, pricey], extractor=_rows_extractor)

    await harness.run("q", schema=_schema(), urls=[url], budget=Budget(max_fetches=4))

    assert cheap.calls == 1
    assert pricey.calls == 0  # cheap rung was good enough — don't pay for render


# --- discovery provider path ------------------------------------------------- #
async def test_discovery_rows_flow_through_with_provenance_citations():
    async def _discover(query, **kw):
        return DiscoverResult(
            rows=[{"name": "A", "score": "1"}],
            provenance={"A": "https://src.example/a"},
            sources=["https://src.example/a"],
        )

    # A fetcher that can't read the source, so the only rows come from discovery.
    fetcher = FakeFetcher(default=FetchedPage(url="x", ok=False, error="blocked"))

    async def _empty(pages, schema, **kw):
        return []

    harness = WebResearchHarness(discover=_discover, fetchers=[fetcher], extractor=_empty)
    res = await harness.run("find models", schema=_schema())

    assert not res.abstained
    assert any(r.values.get("name") == "A" for r in res.rows)
    assert res.rows[0].citations == ["https://src.example/a"]


# --- graceful degradation ---------------------------------------------------- #
async def test_no_sources_and_no_urls_abstains_cleanly():
    async def _empty(pages, schema, **kw):
        return []

    harness = WebResearchHarness(extractor=_empty)  # default static fetcher, no urls
    res = await harness.run("q", schema=_schema())

    assert res.abstained
    assert res.rows == []


# --- custom verifier override ------------------------------------------------ #
async def test_registered_verifier_overrides_default():
    url = "https://example.com/p"
    fetcher = FakeFetcher({url: _rich_page(url)})

    class KeepEverything:
        name = "keep_all"

        async def verify(self, question, rows, pages, *, schema):
            return VerifyOutcome(rows=list(rows), confidence=0.99, abstained=False)

    register_research_verifier(KeepEverything())

    async def _uncited(pages, schema, **kw):
        return [ResearchRow(values={"name": "X"}, citations=[], confidence=0.0)]

    harness = WebResearchHarness(fetchers=[fetcher], extractor=_uncited)
    res = await harness.run("q", schema=_schema(), urls=[url])

    assert not res.abstained  # the override kept the otherwise-uncited row
    assert res.confidence == pytest.approx(0.99)


# --- static fetcher unit: SSRF guard + html reduction ------------------------ #
def test_is_fetchable_url_blocks_internal_and_non_http():
    assert is_fetchable_url("https://example.com/x")
    assert is_fetchable_url("http://public.example.org")
    assert not is_fetchable_url("http://localhost/x")
    assert not is_fetchable_url("http://127.0.0.1/")
    assert not is_fetchable_url("http://10.0.0.1/")
    assert not is_fetchable_url("http://169.254.169.254/latest/meta-data")
    assert not is_fetchable_url("ftp://example.com/x")
    assert not is_fetchable_url("not a url")


def test_html_to_text_strips_chrome_and_reads_title():
    title, text = html_to_text(
        "<html><head><title>Board</title><style>.x{}</style></head>"
        "<body><script>var junk=1;</script><h1>Models</h1><p>Alpha 94</p></body></html>"
    )
    assert title == "Board"
    assert "Models" in text and "Alpha 94" in text
    assert "junk" not in text  # script content dropped


def test_static_fetcher_declares_free_cost_and_tier_zero():
    f = StaticHttpFetcher()
    assert f.tier == 0
    assert fetcher_cost(f) == (False, 0.0)


# --- budget accounting ------------------------------------------------------- #
def test_budget_counters_and_limits():
    b = Budget(max_fetches=2, max_llm_calls=1).start()
    assert b.can_fetch() and b.can_call_llm()
    b.note_fetch()
    assert b.can_fetch()  # 1 of 2 used
    b.note_fetch()
    assert not b.can_fetch()  # exhausted
    b.note_llm()
    assert not b.can_call_llm()
    assert b.remaining_fetches() == 0


# --- artifact ---------------------------------------------------------------- #
def test_result_to_csv_uses_schema_columns():
    res = ResearchResult(
        question="q",
        schema=_schema(),
        rows=[
            ResearchRow(values={"name": "A", "score": "1"}, citations=["u1"]),
            ResearchRow(values={"name": "B", "score": "2"}, citations=["u2", "u3"]),
        ],
    )
    csv = res.to_csv()
    lines = csv.strip().splitlines()
    assert lines[0] == "name,score,sources"
    assert lines[1] == "A,1,u1"
    assert lines[2] == "B,2,u2; u3"


async def test_cite_or_abstain_keeps_cited_rows():
    v = CiteOrAbstainVerifier()
    rows = [
        ResearchRow(values={"name": "A"}, citations=["u"], confidence=0.5),
        ResearchRow(values={"name": "B"}, citations=[], confidence=0.0),
    ]

    outcome = await v.verify(
        "q", rows, [], schema=TargetSchema(entity="m", fields=[SchemaField(name="name")])
    )
    assert len(outcome.rows) == 1
    assert outcome.rows[0].values["name"] == "A"
    assert not outcome.abstained
    assert outcome.dropped == 1

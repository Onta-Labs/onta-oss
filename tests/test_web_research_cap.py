"""Tests for the web-research agent capability (ONTA-166).

Covers the plan/execute contract offline: graceful degradation with nothing to
read, a research plan when the user supplies URLs, and an end-to-end execute
against a fake fetcher + fake extractor (no network, no LLM).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from infona_client.agent.capabilities.web_research_cap import WebResearchCapability
from infona_client.agent.registry import AgentContext, PlanStep
from infona_client.research import extract as research_extract
from infona_client.research.fetch import (
    FetchedPage,
    register_page_fetcher,
    reset_page_fetchers,
)
from infona_client.research.types import ResearchRow
from infona_client.research.verify import reset_research_verifier
from infona_client.web_sources.base import register_web_source, reset_web_sources


@pytest.fixture(autouse=True)
def _clean_registries():
    reset_page_fetchers()
    reset_research_verifier()
    reset_web_sources()
    yield
    reset_page_fetchers()
    reset_research_verifier()
    reset_web_sources()


def _ctx(urls=None, medium="") -> AgentContext:
    return AgentContext(
        tenant_id="t1",
        kg_name="kg",
        neptune=MagicMock(),
        openrouter_key="",  # keyless → planner uses its deterministic fallback
        urls=list(urls or []),
        medium=medium,
    )


class _FakeFetcher:
    name = "static"
    tier = 0
    is_paid = False
    cost_per_call = 0.0

    async def fetch(self, url: str, *, want: str = "") -> FetchedPage:
        return FetchedPage(url=url, text="Alpha score 94. " * 30, ok=True)


# --- OSS scope guard ----------------------------------------------------------- #
def test_oss_boot_registers_no_page_fetcher():
    """ONTA-293: a default OSS boot must leave the fetch ladder EMPTY.

    Open-web retrieval is out of OSS scope: the user's own agent does the finding
    and hands the content over. `register_default_capabilities()` used to call
    `register_default_fetchers()`, which wired the static fetcher at boot and made
    a bare OSS deployment browse the web on the user's behalf. The substrate
    (guards, ladder, cost seam) deliberately STAYS importable — this asserts only
    that nothing is wired by default. Premium registers into it; an OSS deployment
    can opt back in by calling `register_default_fetchers()` itself.
    """
    from infona_client.agent.planner import register_default_capabilities
    from infona_client.retrieval import get_page_fetchers, register_default_fetchers

    register_default_capabilities()
    assert get_page_fetchers() == []

    # ...and the opt-in still works, so nothing was removed, only unwired.
    register_default_fetchers()
    assert [f.name for f in get_page_fetchers()] == ["static"]


# --- plan -------------------------------------------------------------------- #
async def test_plan_degrades_when_nothing_to_read():
    cap = WebResearchCapability()
    steps = await cap.plan(_ctx(), "what are the top TTS models by score")
    assert len(steps) == 1
    assert steps[0].action == "answer"
    payload = steps[0].params["answer_payload"]
    assert "don't retrieve pages from the web" in payload["answer"]


async def test_fetcher_without_provider_offers_urls_rather_than_claiming_no_web():
    """The two unavailable causes must not share a message.

    A deployment WITH a fetcher but no query provider (e.g. FIRECRAWL_API_KEY set
    but no OpenRouter/Parallel key) can still read a URL you hand it. Telling that
    user "I don't retrieve pages from the web" is false and sends them away
    instead of telling them the one thing that would work.
    """
    register_page_fetcher(_FakeFetcher())  # fetcher present, no web source
    cap = WebResearchCapability()
    steps = await cap.plan(_ctx(), "research the S&P 500 and give me a CSV")
    assert steps[0].action == "answer"
    answer = steps[0].params["answer_payload"]["answer"]
    assert "Share one or more URLs" in answer
    assert "don't retrieve pages from the web" not in answer


async def test_plan_degrades_with_urls_when_no_fetcher_registered():
    """OSS scope guard (ONTA-293): supplying URLs is NOT enough on its own.

    Open-web retrieval is out of OSS scope, so OSS registers no page fetcher and
    URL mode stays dormant. Before this decision a bare deployment fell through to
    an implicit StaticHttpFetcher and silently fetched the page. The degraded
    answer must point the caller at their OWN agent, which is the sanctioned way
    to get web content into a graph in OSS.
    """
    cap = WebResearchCapability()
    steps = await cap.plan(
        _ctx(urls=["https://example.com/board"]),
        "pull the scores from this page",
    )
    assert len(steps) == 1
    assert steps[0].action == "answer"
    answer = steps[0].params["answer_payload"]["answer"]
    assert "don't retrieve pages from the web" in answer
    assert "hand me the content" in answer


async def test_plan_with_urls_returns_research_step():
    # A registered fetcher is what makes URL mode available (premium, or an OSS
    # deployment that opted in via register_default_fetchers()).
    register_page_fetcher(_FakeFetcher())
    cap = WebResearchCapability()
    steps = await cap.plan(
        _ctx(urls=["https://example.com/board"]),
        "pull the scores from this page",
    )
    assert len(steps) == 1
    step = steps[0]
    assert step.action == "research"
    assert step.params["urls"] == ["https://example.com/board"]
    assert step.preview["writes_to_graph"] is False
    assert "estimated_usd" in step.cost


class _Provider:
    name = "fake"

    async def discover(self, query, **kw):  # pragma: no cover - not called in plan
        from infona_client.web_sources.base import DiscoverResult

        return DiscoverResult()


async def test_plan_available_via_registered_provider_without_urls():
    # A registered discovery provider makes open-web research available with no
    # URLs supplied — but ONLY alongside a registered fetcher, since the harness
    # reads every page the provider returns through the ladder.
    register_web_source(_Provider())
    register_page_fetcher(_FakeFetcher())
    cap = WebResearchCapability()
    steps = await cap.plan(_ctx(), "research the S&P 500 and give me a CSV")
    assert steps[0].action == "research"


async def test_provider_without_fetcher_does_not_resurrect_an_implicit_fetcher():
    """ONTA-293 regression guard, caught in review of infona-oss#287.

    The first cut gated only on `provider is None and not can_read_urls`, so a
    registered web source satisfied the gate on its own. `execute` then built the
    harness, whose `default_ladder()` fell back to an unregistered
    StaticHttpFetcher and fetched live pages — exactly the implicit behaviour this
    work removes, just one branch over. A provider with NO fetcher must degrade.
    """
    register_web_source(_Provider())  # deliberately no fetcher
    cap = WebResearchCapability()
    steps = await cap.plan(_ctx(), "research the S&P 500 and give me a CSV")
    assert steps[0].action == "answer"
    assert "don't retrieve pages from the web" in steps[0].params["answer_payload"]["answer"]
    # ...and the substrate agrees: nothing registered means nothing to fetch with.
    from infona_client.retrieval import default_ladder

    assert default_ladder() == []


async def test_plan_asks_for_clarification_when_ambiguous(monkeypatch):
    # A genuinely ambiguous question surfaces a no-write 'answer' step carrying the
    # clarifying questions — the confirm-before-spend research step is never made.
    # Questions ride the payload STRUCTURED (question + options) for reply chips,
    # with the options also inlined in the plain-text answer.
    from infona_client.research.types import (
        ClarifyingQuestion,
        ResearchPlan,
        SchemaField,
        TargetSchema,
    )

    async def _ambiguous_plan(instruction, **kw):
        return ResearchPlan(
            question=instruction,
            needs_clarification=True,
            clarifying_questions=[
                ClarifyingQuestion(
                    question="Best by what metric?",
                    options=["benchmark score", "price"],
                ),
                ClarifyingQuestion(question="Which modality?"),
            ],
            schema=TargetSchema(entity="model", fields=[SchemaField(name="name")]),
        )

    monkeypatch.setattr(
        "infona_client.research.plan.plan_research", _ambiguous_plan
    )
    register_page_fetcher(_FakeFetcher())
    cap = WebResearchCapability()
    steps = await cap.plan(
        _ctx(urls=["https://example.com/board"]), "list the best models"
    )
    assert len(steps) == 1
    # Canonical clarify shape (planner short-circuits action="clarify" →
    # kind:"clarify"), not a research step and not a bespoke answer payload.
    assert steps[0].action == "clarify"
    p = steps[0].params
    assert p["questions"] == [
        {"question": "Best by what metric?", "options": ["benchmark score", "price"]},
        {"question": "Which modality?", "options": []},
    ]
    # Two questions → options inlined in the primary question text for old clients.
    assert "(benchmark score / price)" in p["question"]


async def test_plan_single_clarifying_question_is_canonical_chip(monkeypatch):
    # One question is a perfect fit for the canonical {question, options} shape.
    from infona_client.research.types import ClarifyingQuestion, ResearchPlan

    async def _one_q(instruction, **kw):
        return ResearchPlan(
            question=instruction,
            needs_clarification=True,
            clarifying_questions=[
                ClarifyingQuestion(question="Which modality?", options=["LLMs", "image"])
            ],
        )

    monkeypatch.setattr("infona_client.research.plan.plan_research", _one_q)
    register_page_fetcher(_FakeFetcher())
    cap = WebResearchCapability()
    steps = await cap.plan(_ctx(urls=["https://example.com/x"]), "best models?")
    assert steps[0].action == "clarify"
    assert steps[0].params["question"] == "Which modality?"
    assert steps[0].params["options"] == ["LLMs", "image"]


async def test_paid_fetcher_shows_up_in_cost_estimate():
    class _PaidFetcher(_FakeFetcher):
        name = "render"
        tier = 2
        is_paid = True
        cost_per_call = 0.02

    register_page_fetcher(_PaidFetcher())
    cap = WebResearchCapability()
    steps = await cap.plan(_ctx(urls=["https://example.com/x"]), "read this")
    assert steps[0].cost["estimated_usd"] > 0
    assert steps[0].cost["paid_calls"] > 0


# --- execute ----------------------------------------------------------------- #
async def test_execute_runs_harness_and_returns_cited_artifact(monkeypatch):
    register_page_fetcher(_FakeFetcher())

    async def _fake_extract(pages, schema, **kw):
        return [
            ResearchRow(
                values={"name": "Alpha", "score": "94"},
                citations=[pages[0].url],
                confidence=0.9,
            )
        ]

    # The harness imports extract_rows lazily from this module — patch it there.
    monkeypatch.setattr(research_extract, "extract_rows", _fake_extract)

    cap = WebResearchCapability()
    step = PlanStep(
        capability="web_research",
        action="research",
        params={
            "question": "list the models with scores",
            "schema": {
                "entity": "model",
                "fields": [{"name": "name"}, {"name": "score"}],
            },
            "urls": ["https://example.com/board"],
            "max_rows": 50,
            "budget": {"max_iterations": 1, "max_fetches": 2, "max_llm_calls": 4},
        },
    )
    out = await cap.execute(
        _ctx(urls=["https://example.com/board"], medium="mcp"), step
    )

    assert out["kind"] == "research_result"
    assert out["abstained"] is False
    assert len(out["rows"]) == 1
    assert out["rows"][0]["values"]["name"] == "Alpha"
    assert out["sources"] == ["https://example.com/board"]
    assert "Alpha,94," in out["artifact_csv"]
    assert out["confidence"] > 0.5
    # Per-stage cost/latency trace rides the response, tagged with the medium
    # the canonical /agent request declared.
    assert out["trace"]["medium"] == "mcp"
    stages = {e["stage"] for e in out["trace"]["events"]}
    assert {"fetch", "extract", "verify"}.issubset(stages)
    assert out["trace"]["totals"]["elapsed_ms"] >= 0


async def test_execute_abstains_without_readable_sources(monkeypatch):
    async def _empty(pages, schema, **kw):
        return []

    monkeypatch.setattr(research_extract, "extract_rows", _empty)
    register_page_fetcher(_FakeFetcher())

    cap = WebResearchCapability()
    step = PlanStep(
        capability="web_research",
        action="research",
        params={
            "question": "q",
            "schema": {"entity": "item", "fields": [{"name": "answer"}]},
            "urls": [],
            "budget": {"max_iterations": 1, "max_fetches": 2},
        },
    )
    out = await cap.execute(_ctx(), step)
    assert out["kind"] == "research_result"
    assert out["abstained"] is True
    assert out["rows"] == []

"""Unit tests for the web-discovery capability (web_ingest).

No network and no LLM: the web-source provider is a fake returning canned rows,
and the entity/attribute spec is injected via plan()'s ``parsed`` hook (so the
LLM resolver never runs). These exercise the full rail — graceful degradation,
the attribute-confirmation clarify, the confirmed-attributes plan (which now
previews the DISCOVERED multi-type ontology shape from the sample), and
execute → SchemaResolver.ingest (the same multi-type extract→resolve→insert path
document ingest commits through).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from unittest.mock import MagicMock

from cograph_client.agent.capabilities import web_ingest_cap
from cograph_client.agent.capabilities.web_ingest_cap import WebIngestCapability
from cograph_client.agent.registry import AgentContext
from cograph_client.resolver.models import (
    ExtractedAttribute,
    ExtractedEntity,
    ExtractedRelationship,
    ExtractionResult,
    IngestResult,
)
from cograph_client.resolver.schema_resolver import SchemaResolver
from cograph_client.web_sources import (
    DiscoverResult,
    register_web_source,
    reset_web_sources,
)

FULL_ROWS = [
    {"name": "anthropic/claude-opus-4-8", "context_length": "200000"},
    {"name": "openai/gpt-5", "context_length": "400000"},
    {"name": "google/gemini-2.5-flash", "context_length": "1000000"},
    {"name": "meta/llama-4", "context_length": "128000"},
]

# Spec as the LLM resolver would return it (already normalized). ``query`` is the
# CLEAN search subject the resolver distills from the raw message.
CONFIRMED_SPEC = {
    "entity_type": "OpenRouterModel",
    "key_attribute": "name",
    "query": "OpenRouter models",
    "confirmed_attributes": ["context_length"],
    "suggested_attributes": ["provider", "context_length"],
}
ENTITY_ONLY_SPEC = {
    "entity_type": "OpenRouterModel",
    "key_attribute": "name",
    "query": "OpenRouter models",
    "confirmed_attributes": [],
    "suggested_attributes": ["provider", "context_length", "pricing"],
}
# A place-shaped query the spec classified into the generic "place" kind (ONTA-190).
PLACE_SPEC = {
    "entity_type": "CoffeeShop",
    "key_attribute": "name",
    "query": "coffee shops in the Mission",
    "query_kind": "place",
    "confirmed_attributes": ["address"],
    "suggested_attributes": ["address", "phone", "rating"],
}
# Entity-only, but the resolver picked an explicit SHORT core set out of a broader
# comprehensive suggested list — exercises "pre-select the few most-important, not
# every column".
CORE_SPEC = {
    "entity_type": "Physician",
    "key_attribute": "name",
    "query": "primary care physicians in Tustin",
    "confirmed_attributes": [],
    "core_attributes": ["specialty", "city", "phone"],
    "suggested_attributes": [
        "specialty",
        "practice_name",
        "address",
        "city",
        "phone",
        "accepted_insurance",
        "board_certification",
        "npi_number",
    ],
}


# Above the auto-confirm gate (_PREVIEW_GATE_USD, default $0.50) → plan() runs
# the FULL sample+shape preview. Tests that exercise the preview machinery
# register their provider with this; cheap/free providers take the lean fast
# path (no plan-time provider call) covered by the fast-path tests below.
RICH = {"is_paid": True, "cost_per_call": 0.75}


class FakeProvider:
    """Canned provider that honors hint_columns (projects rows to them).

    When ``provenance=True`` it also returns a per-record provenance map (keyed by
    the projected row's name → a distinct per-row page URL), mirroring how the real
    adapters populate ``DiscoverResult.provenance``, so the per-record source-URL
    threading can be exercised end-to-end."""

    def __init__(
        self, *, is_paid: bool = False, cost_per_call: float = 0.0,
        rows=None, provenance: bool = False,
    ) -> None:
        self.name = "fake"
        self.is_paid = is_paid
        self.cost_per_call = cost_per_call
        self._rows = FULL_ROWS if rows is None else rows
        self._provenance = provenance
        self.calls: list[tuple] = []

    async def discover(self, query, *, sample, max_rows, hint_columns, context, urls=None):
        self.calls.append((query, sample, max_rows, tuple(hint_columns or ())))
        rows = self._rows[: (5 if sample else max_rows)]
        if hint_columns:
            rows = [{c: r.get(c, "unknown") for c in hint_columns} for r in rows]
        prov: dict[str, str] = {}
        if self._provenance:
            # Distinct page per row, keyed the way the real adapters key it
            # (r.get("name", str(i))), so each record traces to its own URL.
            prov = {
                r.get("name", str(i)): f"https://src.example/page-{i}"
                for i, r in enumerate(rows)
            }
        return DiscoverResult(
            rows=rows,
            provenance=prov,
            sources=["https://openrouter.ai/models"],
            estimated_total=len(self._rows),
            is_partial=sample,
        )


class KindFakeProvider(FakeProvider):
    """A FakeProvider that specializes in a generic query kind (ONTA-190). Named
    distinctly so a test can assert which provider plan() selected. A non-empty
    query_kinds also keeps it out of the general no-name query default."""

    def __init__(self, *, name: str = "kind_fake", kinds=frozenset({"place"}), **kw):
        super().__init__(**kw)
        self.name = name
        self.query_kinds = kinds


def _ctx(prior_clarify: int = 0) -> AgentContext:
    return AgentContext(
        tenant_id="demo-tenant",
        kg_name="models",
        neptune=MagicMock(),
        anthropic_key="sk-ant-test",
        openrouter_key="",
        extras={"prior_clarify_count": prior_clarify},
    )


def _patch_preview(monkeypatch, *, entities, relationships=(), existing=None):
    """Make plan-time previewing deterministic: stub _fetch_ontology (existing
    types) and _extract (the multi-type extraction the preview reads)."""
    existing_types = {name: "" for name in (existing or [])}

    async def fake_fetch_ontology(self, graph_uri):
        return existing_types, {}

    async def fake_extract(self, content, content_type, existing=None):
        return ExtractionResult(
            entities=list(entities), relationships=list(relationships)
        )

    monkeypatch.setattr(SchemaResolver, "_fetch_ontology", fake_fetch_ontology)
    monkeypatch.setattr(SchemaResolver, "_extract", fake_extract)


# A simple single-type extraction the FakeProvider rows would yield.
def _single_type_entities():
    return [
        ExtractedEntity(
            type_name="OpenRouterModel",
            id=r["name"],
            attributes=[
                ExtractedAttribute(name="context_length", value=r["context_length"])
            ],
        )
        for r in FULL_ROWS[:5]
    ]


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_web_sources()
    yield
    reset_web_sources()


async def test_no_provider_degrades_to_not_enabled_answer():
    steps = await WebIngestCapability().plan(_ctx(), "find a list of OpenRouter models")
    assert len(steps) == 1 and steps[0].action == "answer"
    assert "isn't enabled" in steps[0].params["answer_payload"]["answer"]


async def test_entity_only_asks_to_confirm_attributes():
    register_web_source(FakeProvider())
    steps = await WebIngestCapability().plan(
        _ctx(), "a list of OpenRouter models", parsed=ENTITY_ONLY_SPEC
    )
    assert len(steps) == 1
    step = steps[0]
    assert step.action == "clarify"
    # Both clickable options carry the concrete attribute set so the next turn
    # converges without new UI.
    opts = step.params["options"]
    assert opts[0].startswith("Use these: name")
    assert "provider" in opts[0] and "context_length" in opts[0]
    assert opts[1] == "Just the name"


async def test_clarify_pre_selects_only_core_attributes():
    """The clarify recommends a SHORT set (core_attributes), not the whole
    comprehensive suggested list, and its question stays terse — it does NOT
    re-list the attributes (they're already the chips the client renders)."""
    register_web_source(FakeProvider())
    steps = await WebIngestCapability().plan(
        _ctx(), "primary care physicians in Tustin", parsed=CORE_SPEC
    )
    assert len(steps) == 1
    step = steps[0]
    assert step.action == "clarify"

    opts = step.params["options"]
    # Pre-selected option carries the key + ONLY the core few, in order.
    assert opts[0] == "Use these: name, specialty, city, phone"
    assert opts[1] == "Just the name"
    # Comprehensive-but-not-core columns are NOT pushed as pre-selected chips.
    assert "npi_number" not in opts[0]
    assert "board_certification" not in opts[0]

    # Terse question: entity + key are bolded, but the attribute list is NOT
    # repeated in prose (that was the "question repeats the options" bug).
    q = step.params["question"]
    assert "**Physician**" in q and "**name**" in q
    assert "specialty" not in q and "practice_name" not in q


async def test_confirmed_attributes_builds_discovery_plan(monkeypatch):
    provider = FakeProvider(**RICH)
    register_web_source(provider)
    _patch_preview(monkeypatch, entities=_single_type_entities())

    steps = await WebIngestCapability().plan(
        _ctx(), "can we ingest the models OpenRouter currently offers?",
        parsed=CONFIRMED_SPEC,
    )
    assert len(steps) == 1
    step = steps[0]
    assert step.action == "discover_ingest"

    # Sample fetched with the CLEAN search subject (from spec.query) + the
    # COMPREHENSIVE hint (key ∪ confirmed ∪ suggested) as hint_columns — NOT the
    # raw conversational sentence, and NOT the confirmed minimal list (Cause 1:
    # the provider projects to hint_columns, so a thin hint starves the fetch).
    q, sample, _max, cols = provider.calls[0]
    assert sample is True
    assert q == "OpenRouter models"
    # key=name, confirmed=[context_length], suggested=[provider, context_length].
    assert set(cols) == {"name", "context_length", "provider"}
    # The card text uses the clean subject, never echoes the raw question.
    assert "OpenRouter models" in step.rationale
    assert "can we ingest" not in step.rationale

    # Preview surfaces the DISCOVERED ontology shape (multi-type engine output).
    names = {t["name"] for t in step.preview["discovered_types"]}
    assert "OpenRouterModel" in names
    assert step.preview["relationships"] == []
    # No more flat mapping persisted; proposed_type stays as a useful label.
    assert "mapping" not in step.params
    assert step.params["proposed_type"] == "OpenRouterModel"
    # attributes = the confirmed naming set; hint_columns = the comprehensive
    # fetch union, persisted so execute() fetches the same rich projection.
    assert step.params["attributes"] == ["name", "context_length"]
    assert set(step.params["hint_columns"]) == {"name", "context_length", "provider"}


# --- query-kind routing (ONTA-190) ------------------------------------------


async def test_place_kind_routes_to_specialized_provider(monkeypatch):
    """When the spec classifies the query as kind="place" AND a provider
    specializing in that kind is registered, plan() PREFERS the specialized
    provider over the general default — persisting its name so execute() re-selects
    the same one. Generic: the capability routes by the kind, never by a provider
    name."""
    general = FakeProvider()  # the general default (no query_kinds)
    place = KindFakeProvider(name="place_src", kinds=frozenset({"place"}))
    register_web_source(general)
    register_web_source(place)

    steps = await WebIngestCapability().plan(
        _ctx(), "coffee shops in the Mission", parsed=PLACE_SPEC
    )
    step = steps[0]
    assert step.action == "discover_ingest"
    # The specialized provider is persisted as the PRIMARY, and the ensemble
    # carries BOTH (specialized first, general breadth second — neither source is
    # complete alone). Cheap providers take the lean fast path — NO plan-time
    # sample call on either provider.
    assert step.params["provider"] == "place_src"
    assert step.params["providers"] == ["place_src", "fake"]
    assert not place.calls and not general.calls


async def test_place_kind_falls_back_to_default_when_unregistered(monkeypatch):
    """DORMANT without the specialized provider: the SAME place-kind spec, but no
    kind provider registered, uses the general default — kind routing is a pure
    no-op, so the general path still handles everything."""
    general = FakeProvider()  # only the general default is registered
    register_web_source(general)

    steps = await WebIngestCapability().plan(
        _ctx(), "coffee shops in the Mission", parsed=PLACE_SPEC
    )
    step = steps[0]
    assert step.action == "discover_ingest"
    # No specialized provider → the general default is persisted (fast path:
    # selection only, no plan-time sample).
    assert step.params["provider"] == general.name


async def test_non_place_kind_ignores_specialized_provider(monkeypatch):
    """A general (non-place) query never routes to the place provider even when one
    is registered: query_kind is None on CONFIRMED_SPEC, so the general default is
    used and the place source is left untouched."""
    general = FakeProvider()
    place = KindFakeProvider(name="place_src", kinds=frozenset({"place"}))
    register_web_source(general)
    register_web_source(place)

    steps = await WebIngestCapability().plan(
        _ctx(), "models OpenRouter offers", parsed=CONFIRMED_SPEC
    )
    step = steps[0]
    assert step.params["provider"] == general.name
    # No kind match → the ensemble is the general provider ALONE (the place
    # source must not join queries outside its kind).
    assert step.params["providers"] == [general.name]
    # Fast path: neither provider is touched at plan time; the place source in
    # particular is never invoked for a query outside its kind.
    assert not place.calls


async def test_place_only_deployment_serves_place_query(monkeypatch):
    """PLACE-ONLY DEPLOYMENT (the blocker): ONLY a kind-specialized provider is
    registered (no general default — e.g. GOOGLE_PLACES_API_KEY set but no
    OpenRouter/Gemini/Perplexity key). The availability gate must NOT refuse a
    place query — it routes it to the specialized provider (a plan, not the
    "not enabled" dead end)."""
    place = KindFakeProvider(name="place_src", kinds=frozenset({"place"}))
    register_web_source(place)  # NO general provider registered

    steps = await WebIngestCapability().plan(
        _ctx(), "coffee shops in the Mission", parsed=PLACE_SPEC
    )
    step = steps[0]
    assert step.action == "discover_ingest"
    assert step.params["provider"] == "place_src"
    # Place-only deployment → the ensemble is just the specialized provider.
    assert step.params["providers"] == ["place_src"]


async def test_place_only_deployment_gracefully_refuses_general_query(monkeypatch):
    """PLACE-ONLY DEPLOYMENT, GENERAL query: the only provider can't serve this
    query's kind (query_kind is None on CONFIRMED_SPEC). Gracefully refuse with an
    answer step — NOT a crash, and NOT proceeding with no provider."""
    place = KindFakeProvider(name="place_src", kinds=frozenset({"place"}))
    register_web_source(place)  # NO general provider registered
    _patch_preview(monkeypatch, entities=_single_type_entities())

    steps = await WebIngestCapability().plan(
        _ctx(), "models OpenRouter offers", parsed=CONFIRMED_SPEC
    )
    assert len(steps) == 1 and steps[0].action == "answer"
    assert "isn't enabled" in steps[0].params["answer_payload"]["answer"]
    # The place provider was never invoked for a query outside its kind.
    assert not place.calls


async def test_no_provider_at_all_still_refuses(monkeypatch):
    """Empty registry (no general AND no kind provider) → the gate still refuses
    every query, unchanged from before."""
    steps = await WebIngestCapability().plan(
        _ctx(), "coffee shops in the Mission", parsed=PLACE_SPEC
    )
    assert len(steps) == 1 and steps[0].action == "answer"
    assert "isn't enabled" in steps[0].params["answer_payload"]["answer"]


async def test_preview_surfaces_multiple_types_and_relationships(monkeypatch):
    """The plan card previews the multi-type ontology + the relationship the
    extractor inferred between two distinct entity types."""
    provider = FakeProvider(**RICH)
    register_web_source(provider)
    entities = [
        ExtractedEntity(
            type_name="Model", id="claude-opus",
            attributes=[ExtractedAttribute(name="context_length", value="200000")],
        ),
        ExtractedEntity(
            type_name="Provider", id="anthropic",
            attributes=[ExtractedAttribute(name="homepage", value="anthropic.com")],
        ),
    ]
    rels = [ExtractedRelationship(
        source_id="claude-opus", predicate="provided_by", target_id="anthropic",
    )]
    _patch_preview(monkeypatch, entities=entities, relationships=rels, existing=["Provider"])

    steps = await WebIngestCapability().plan(
        _ctx(), "models OpenRouter offers", parsed=CONFIRMED_SPEC
    )
    step = steps[0]
    types = {t["name"]: t for t in step.preview["discovered_types"]}
    assert set(types) == {"Model", "Provider"}
    # is_new reflects the existing ontology (Provider exists, Model is new).
    assert types["Model"]["is_new"] is True
    assert types["Provider"]["is_new"] is False
    assert "context_length" in types["Model"]["attributes"]

    rels_out = step.preview["relationships"]
    assert len(rels_out) == 1
    assert rels_out[0] == {
        "source": "Model", "predicate": "provided_by", "target": "Provider",
    }


async def test_preview_summary_frames_shape_as_estimate(monkeypatch):
    """FIX 5: the discovered TYPES/relationships are an ESTIMATE from the small
    sample, not a guarantee — the user-facing summary must say so (only the
    column projection is stable preview→commit). Wording-only assertion."""
    provider = FakeProvider(**RICH)
    register_web_source(provider)
    _patch_preview(monkeypatch, entities=_single_type_entities())

    steps = await WebIngestCapability().plan(
        _ctx(), "models OpenRouter offers", parsed=CONFIRMED_SPEC
    )
    summary = steps[0].preview["summary"].lower()
    # Must NOT over-claim certainty ("Discovered N types") and must signal the
    # commit may differ.
    assert "estimated" in summary
    assert "may differ" in summary
    assert "discovered " not in summary


async def test_preview_degrades_to_flat_when_extract_fails(monkeypatch):
    """If the plan-time extractor raises, plan() still returns a confirmable plan
    card (degraded flat single-type preview) — no exception propagates."""
    provider = FakeProvider(**RICH)
    register_web_source(provider)

    async def fake_fetch_ontology(self, graph_uri):
        return {}, {}

    async def boom_extract(self, content, content_type, existing=None):
        raise RuntimeError("extractor unavailable")

    monkeypatch.setattr(SchemaResolver, "_fetch_ontology", fake_fetch_ontology)
    monkeypatch.setattr(SchemaResolver, "_extract", boom_extract)

    steps = await WebIngestCapability().plan(
        _ctx(), "models OpenRouter offers", parsed=CONFIRMED_SPEC
    )
    assert len(steps) == 1
    step = steps[0]
    assert step.action == "discover_ingest"
    # Degraded: one flat discovered type = the proposed type with the attributes.
    dts = step.preview["discovered_types"]
    assert len(dts) == 1
    assert dts[0]["name"] == "OpenRouterModel"
    assert dts[0]["attributes"] == ["name", "context_length"]
    assert step.preview["relationships"] == []


async def test_slow_sample_degrades_to_confirmable_plan(monkeypatch):
    """ROOT-CAUSE (Add-data-from-the-web timeout): a web source that can't return a
    sample within the preview budget must NOT strand the user on the proxy's 504
    'took too long'. plan() bounds the sample fetch (_SAMPLE_BUDGET_S) and, on a
    timeout, degrades to a CONFIRMABLE flat-preview discovery plan (the full pull
    still runs on confirm as a background job) — and returns promptly, not after
    the provider's own 60s timeout."""
    monkeypatch.setattr(web_ingest_cap, "_SAMPLE_BUDGET_S", 0.05)

    class SlowSampleProvider(FakeProvider):
        async def discover(self, query, *, sample, max_rows, hint_columns, context, urls=None):
            if sample:
                await asyncio.sleep(5)  # far exceeds the (patched) sample budget
            return await super().discover(
                query, sample=sample, max_rows=max_rows,
                hint_columns=hint_columns, context=context, urls=urls,
            )

    register_web_source(SlowSampleProvider(**RICH))

    steps = await asyncio.wait_for(
        WebIngestCapability().plan(
            _ctx(), "models OpenRouter offers", parsed=CONFIRMED_SPEC
        ),
        timeout=2,  # the fix bounds it to ~0.05s; a hang here IS the regression
    )
    assert len(steps) == 1
    step = steps[0]
    # A confirmable plan — NOT the answer/clarify dead end that would strand the user.
    assert step.action == "discover_ingest"
    # Degraded flat preview: the proposed type + its attributes, no sample rows.
    assert step.preview["sample_rows"] == []
    assert step.preview["sources"] == []
    dts = step.preview["discovered_types"]
    assert len(dts) == 1 and dts[0]["name"] == "OpenRouterModel"
    assert dts[0]["attributes"] == ["name", "context_length"]
    assert step.preview["relationships"] == []
    # Summary signals the degradation rather than over-claiming a discovered shape.
    assert "couldn't fully preview" in step.preview["summary"].lower()
    # The comprehensive fetch hint is still persisted so execute() runs the REAL,
    # full discovery on confirm — nothing is lost, only the rich preview is skipped.
    assert set(step.params["hint_columns"]) == {"name", "context_length", "provider"}


async def test_slow_shape_estimate_degrades_to_flat_preview(monkeypatch):
    """The sample renders fine but the ontology-shape extractor (whose own LLM
    timeout is 60s, longer than the whole request budget) can't finish within the
    shape budget → plan() bounds it (_SHAPE_BUDGET_S) and degrades to a flat
    preview, still a confirmable plan, no timeout propagated to the request."""
    monkeypatch.setattr(web_ingest_cap, "_SHAPE_BUDGET_S", 0.05)
    register_web_source(FakeProvider(**RICH))

    async def fast_fetch_ontology(self, graph_uri):
        return {}, {}

    async def slow_extract(self, content, content_type, existing=None):
        await asyncio.sleep(5)  # far exceeds the (patched) shape budget
        return ExtractionResult(entities=[], relationships=[])

    monkeypatch.setattr(SchemaResolver, "_fetch_ontology", fast_fetch_ontology)
    monkeypatch.setattr(SchemaResolver, "_extract", slow_extract)

    steps = await asyncio.wait_for(
        WebIngestCapability().plan(
            _ctx(), "models OpenRouter offers", parsed=CONFIRMED_SPEC
        ),
        timeout=2,
    )
    step = steps[0]
    assert step.action == "discover_ingest"
    # The SAMPLE rendered (rows present); only the shape estimate degraded to flat.
    assert step.preview["sample_rows"]
    dts = step.preview["discovered_types"]
    assert len(dts) == 1 and dts[0]["name"] == "OpenRouterModel"
    assert "couldn't fully preview" in step.preview["summary"].lower()


async def test_commit_to_suggested_after_prior_clarify(monkeypatch):
    register_web_source(FakeProvider())
    _patch_preview(monkeypatch, entities=_single_type_entities())
    # Entity-only spec, but we've already asked once → commit to suggested,
    # don't clarify again.
    steps = await WebIngestCapability().plan(
        _ctx(prior_clarify=1), "a list of OpenRouter models", parsed=ENTITY_ONLY_SPEC
    )
    assert len(steps) == 1
    assert steps[0].action == "discover_ingest"
    assert steps[0].params["attributes"] == ["name", "provider", "context_length", "pricing"]


async def test_paid_provider_quotes_cost(monkeypatch):
    register_web_source(FakeProvider(is_paid=True, cost_per_call=0.01))
    _patch_preview(monkeypatch, entities=_single_type_entities())
    steps = await WebIngestCapability().plan(
        _ctx(), "list of OpenRouter models", parsed=CONFIRMED_SPEC
    )
    cost = steps[0].cost
    assert cost["paid_calls"] == 1
    assert cost["estimated_usd"] == pytest.approx(0.01)
    assert "Paid web discovery" in cost["note"]


def test_estimate_cost_prices_pagination_fanout():
    """A paginating paid provider (rows_per_call set) is priced as cost_per_call ×
    ceil(rows / rows_per_call), NOT a single call — so a multi-page pull isn't
    under-quoted (the nit-2 fix)."""

    class _Paginating:
        name = "paginating"
        is_paid = True
        cost_per_call = 0.017
        rows_per_call = 20

    # 100 rows / 20 per request = 5 paid requests.
    cost = web_ingest_cap._estimate_cost(_Paginating(), estimated_total=100, cap=200)
    assert cost["paid_calls"] == 5
    assert cost["estimated_usd"] == pytest.approx(0.085)  # 0.017 × 5
    assert cost["per_call_cost_usd"] == pytest.approx(0.017)
    assert "paginated request" in cost["note"]

    # A partial final page still counts (21 rows → 2 requests).
    cost2 = web_ingest_cap._estimate_cost(_Paginating(), estimated_total=21, cap=200)
    assert cost2["paid_calls"] == 2
    assert cost2["estimated_usd"] == pytest.approx(0.034)


def test_estimate_cost_single_call_when_no_rows_per_call():
    """A paid provider WITHOUT rows_per_call bills one call for the whole run (the
    backward-compatible default) — unchanged from before the fanout fix."""

    class _WholeRun:
        name = "wholerun"
        is_paid = True
        cost_per_call = 0.05
        # no rows_per_call

    cost = web_ingest_cap._estimate_cost(_WholeRun(), estimated_total=500, cap=1000)
    assert cost["paid_calls"] == 1
    assert cost["estimated_usd"] == pytest.approx(0.05)
    assert "paginated request" not in cost["note"]


def test_paid_call_count_helper():
    """_paid_call_count: ceil(rows / rows_per_call), min 1; default 1 when
    rows_per_call is unset/0 or rows is 0; robust to a malformed value."""

    class _P:
        rows_per_call = 20

    class _NoPer:
        pass

    class _Bad:
        rows_per_call = "oops"

    assert web_ingest_cap._paid_call_count(_P(), 100) == 5
    assert web_ingest_cap._paid_call_count(_P(), 1) == 1
    assert web_ingest_cap._paid_call_count(_P(), 0) == 1  # no rows → 1
    assert web_ingest_cap._paid_call_count(_NoPer(), 100) == 1  # unset → whole run
    assert web_ingest_cap._paid_call_count(_Bad(), 100) == 1  # malformed → 1


async def test_cheap_provider_skips_plan_time_preview():
    """At/under the auto-confirm gate the client starts the job straight from the
    attribute confirm, so plan() skips the expensive sample+shape preview: NO
    plan-time provider call, no extraction LLM — a lean, immediately-confirmable
    step carrying everything execute() needs (same params contract as the rich
    path), with the cost still quoted so clients can gate on it."""
    provider = FakeProvider(is_paid=True, cost_per_call=0.03)
    register_web_source(provider)
    steps = await WebIngestCapability().plan(
        _ctx(), "can we ingest the models OpenRouter currently offers?",
        parsed=CONFIRMED_SPEC,
    )
    assert len(steps) == 1
    step = steps[0]
    assert step.action == "discover_ingest"
    assert provider.calls == []  # the fast path never touches the provider
    # Same persisted contract as the rich path — execute() runs unchanged.
    assert step.params["query"] == "OpenRouter models"
    assert step.params["attributes"] == ["name", "context_length"]
    assert set(step.params["hint_columns"]) == {"name", "context_length", "provider"}
    assert step.params["max_rows"] == web_ingest_cap._DEFAULT_PLAN_CAP
    assert step.params["provider"] == "fake"
    # Cost quoted for the client-side auto-confirm gate.
    assert step.cost["paid_calls"] == 1
    assert step.cost["estimated_usd"] == pytest.approx(0.03)
    # Lean preview: a summary line only — no sampled rows / discovered shape.
    assert step.preview["summary"]
    assert "sample_rows" not in step.preview
    assert "discovered_types" not in step.preview


async def test_free_provider_also_skips_plan_time_preview():
    """Free providers ride the same fast path (they were always auto-confirmed;
    the plan-time sample was pure latency for them too)."""
    provider = FakeProvider()
    register_web_source(provider)
    steps = await WebIngestCapability().plan(
        _ctx(), "models OpenRouter offers", parsed=CONFIRMED_SPEC
    )
    assert steps[0].action == "discover_ingest"
    assert provider.calls == []
    assert steps[0].cost["paid_calls"] == 0


def test_default_cap_settles_within_run_timeout():
    """The default discovery cap MUST be small enough that a first interactive
    build SETTLES to a terminal state inside the run's own wall-clock budget.

    persona-eval m3 RCA: with cap=200 and the measured ~9.5s/record sequential
    extraction, a physician build needs ~32 min to fill but _RUN_TIMEOUT_S is 600s,
    so the job ALWAYS hit the wall at ~60 records and flipped to ``failed`` — the
    graph never settled, and different follow-up tasks saw contradictory partial
    snapshots. The default must fit: cap × per-record-seconds < _RUN_TIMEOUT_S, with
    margin. This test pins the invariant so a future bump to the default (or a drop
    in the run timeout) can't silently reintroduce the never-settling build.
    """
    # Conservative worst-case per-record extraction time measured on the deployed
    # backend (b66e2ef2): 600s wall / 63 records landed ≈ 9.5s/record.
    per_record_s = 9.5
    fill_estimate_s = web_ingest_cap._DEFAULT_PLAN_CAP * per_record_s
    # Require the whole fill to complete with ≥15% headroom under the wall, so
    # search-phase latency + variance don't push a realistic run over.
    assert fill_estimate_s <= 0.85 * web_ingest_cap._RUN_TIMEOUT_S, (
        f"default cap {web_ingest_cap._DEFAULT_PLAN_CAP} × {per_record_s}s/record "
        f"= {fill_estimate_s:.0f}s does not settle under the "
        f"{web_ingest_cap._RUN_TIMEOUT_S:.0f}s run timeout — an interactive build "
        "would never finish in-session"
    )


def test_default_cap_is_env_overridable(monkeypatch):
    """Ops can retune the interactive default without a deploy (e.g. a batch
    deployment that also raises _RUN_TIMEOUT_S and wants the old 200)."""
    import importlib

    monkeypatch.setenv("COGRAPH_DISCOVERY_DEFAULT_CAP", "37")
    try:
        importlib.reload(web_ingest_cap)
        assert web_ingest_cap._DEFAULT_PLAN_CAP == 37
    finally:
        # Restore the module to its env-free default so later tests see the real
        # constant (reload rebinds the module-level symbol).
        monkeypatch.delenv("COGRAPH_DISCOVERY_DEFAULT_CAP", raising=False)
        importlib.reload(web_ingest_cap)


async def test_plan_emits_registry_route_stage_timing(monkeypatch):
    """Observability (ONTA-198 follow-up): the plan path times its LLM stages.
    Every query-mode plan consults the registry, so a `stage_timing` span for
    `registry_route` is emitted (it fires even when the router self-degrades).

    Records against a mock module logger (not capture_logs): under the full suite
    the module logger is cached by earlier tests, so capture_logs intercepts
    nothing — a swapped-in mock is order-independent."""
    from unittest.mock import MagicMock

    rec = MagicMock()
    monkeypatch.setattr(web_ingest_cap, "logger", rec)
    register_web_source(FakeProvider())
    await WebIngestCapability().plan(
        _ctx(), "models OpenRouter offers", parsed=CONFIRMED_SPEC
    )
    routes = [
        c for c in rec.info.call_args_list
        if c.args and c.args[0] == "stage_timing"
        and c.kwargs.get("stage") == "registry_route"
    ]
    assert routes, "plan must time the registry-route stage"
    assert isinstance(routes[0].kwargs["duration_ms"], (int, float))


async def test_empty_sample_returns_message():
    register_web_source(FakeProvider(rows=[], **RICH))
    steps = await WebIngestCapability().plan(
        _ctx(), "find a list of nonsense xyzzy", parsed=CONFIRMED_SPEC
    )
    assert len(steps) == 1 and steps[0].action == "answer"
    assert "couldn't find anything" in steps[0].params["answer_payload"]["answer"]


def test_empty_message_query_mode_says_rephrase():
    """No URL → open-web search genuinely found nothing → rephrase advice."""
    msg = web_ingest_cap._empty_sample_message(
        "obscure query", [], DiscoverResult(rows=[])
    )
    assert "couldn't find anything on the web" in msg
    assert "rephrasing" in msg


def test_empty_message_url_mode_no_error_does_not_say_rephrase():
    """URL mode, page read but no records → explain the page, NEVER tell a user
    who pasted a specific link to 'rephrase their search' (the original bug)."""
    url = "https://humannessindex.vapi.ai/"
    msg = web_ingest_cap._empty_sample_message(
        "models and their scores", [url], DiscoverResult(rows=[], sources=[url])
    )
    assert url in msg
    assert "couldn't find a list or table" in msg
    assert "rephras" not in msg.lower()
    assert "narrow" not in msg.lower()


def test_empty_message_url_mode_error_surfaces_reason():
    """URL mode + provider error → surface the reason + retry, not rephrase."""
    url = "https://humannessindex.vapi.ai/"
    msg = web_ingest_cap._empty_sample_message(
        "models",
        [url],
        DiscoverResult(rows=[], sources=[url], error="HTTP 502 upstream"),
    )
    assert url in msg
    assert "couldn't read" in msg
    assert "502" in msg
    assert "rephras" not in msg.lower()


async def test_execute_runs_full_discover_and_ingests(monkeypatch):
    provider = FakeProvider()
    register_web_source(provider)
    _patch_preview(monkeypatch, entities=_single_type_entities())

    captured: dict = {}

    async def fake_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None, **_kw):
        captured.update(
            content=content, tenant_id=tenant_id,
            content_type=content_type, source=source,
        )
        rows = json.loads(content)
        return IngestResult(entities_extracted=len(rows), entities_resolved=len(rows))

    monkeypatch.setattr(SchemaResolver, "ingest", fake_ingest)

    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    cap = WebIngestCapability()
    step = (await cap.plan(_ctx(), "find a list of OpenRouter models", parsed=CONFIRMED_SPEC))[0]
    ack = await cap.execute(_ctx(), step)
    assert ack["kind"] == "ack" and "background" in ack["message"]
    # Distilled job title for client job cards — the clean search subject, not
    # the user's raw sentence.
    assert ack["title"] == "OpenRouter models"

    await spawned["task"]

    # Full pull (sample=False) with the COMPREHENSIVE hint (key ∪ confirmed ∪
    # suggested) — the SAME rich projection the sample used (the FETCH is the
    # stable part preview→commit; the discovered shape is only an estimate),
    # NOT the confirmed minimal list. Committed through the multi-type ingest
    # path (content_type="json").
    assert provider.calls[-1][1] is False
    assert set(provider.calls[-1][3]) == {"name", "context_length", "provider"}
    assert captured["content_type"] == "json"
    # The JSON round-trips back to the rows the provider returned (projected to
    # the comprehensive hint).
    rows_back = json.loads(captured["content"])
    assert len(rows_back) == len(FULL_ROWS)
    assert set(rows_back[0].keys()) == {"name", "context_length", "provider"}
    # The clean search subject (spec.query) is what the provider + source use.
    assert captured["source"] == "web:fake:OpenRouter models"


async def test_run_routes_multi_type_and_refreshes(monkeypatch):
    """The commit routes through ingest (content_type="json") and the post-write
    refresh is driven by the multi-type result: every type the ingest created or
    extended is in the affected_types passed to refresh_after_write."""
    provider = FakeProvider()
    register_web_source(provider)
    _patch_preview(monkeypatch, entities=_single_type_entities())

    ingest_calls: dict = {}

    async def spy_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None, **_kw):
        ingest_calls.update(content=content, content_type=content_type)
        return IngestResult(
            types_created=["Model", "Provider"],
            attributes_added=["Model.provided_by"],
            entities_resolved=4,
        )

    monkeypatch.setattr(SchemaResolver, "ingest", spy_ingest)

    refreshed: dict = {}

    async def fake_refresh(neptune, *, tenant_id, kg_name, affected_types):
        refreshed.update(affected_types=affected_types, kg_name=kg_name)

    monkeypatch.setattr(web_ingest_cap, "refresh_after_write", fake_refresh)

    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    cap = WebIngestCapability()
    step = (await cap.plan(_ctx(), "list of OpenRouter models", parsed=CONFIRMED_SPEC))[0]
    await cap.execute(_ctx(), step)
    await spawned["task"]

    assert ingest_calls["content_type"] == "json"
    # affected_types = types_created ∪ owning-types of attributes_added.
    assert refreshed["affected_types"] == {"Model", "Provider"}


def _ctx_with_store(store) -> AgentContext:
    """Agent context carrying a job store, as the agent route injects it."""
    return AgentContext(
        tenant_id="demo-tenant",
        kg_name="models",
        neptune=MagicMock(),
        anthropic_key="sk-ant-test",
        openrouter_key="",
        extras={"prior_clarify_count": 0, "enrichment_job_store": store},
    )


async def test_execute_tracks_job_with_results_and_platforms(monkeypatch):
    """With a job store present, execute creates a tracked discovery job, returns
    its id + initial status, and drives it to applied with a result count, the
    platforms consulted, and the run cost — so the client can poll a live status."""
    from cograph_client.enrichment.job_store import InMemoryJobStore
    from cograph_client.enrichment.models import JobCategory, JobStatus

    provider = FakeProvider(is_paid=True, cost_per_call=0.09)
    register_web_source(provider)
    _patch_preview(monkeypatch, entities=_single_type_entities())

    async def fake_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None, **_kw):
        rows = json.loads(content)
        return IngestResult(entities_extracted=len(rows), entities_resolved=len(rows))

    monkeypatch.setattr(SchemaResolver, "ingest", fake_ingest)

    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    store = InMemoryJobStore()
    cap = WebIngestCapability()
    step = (
        await cap.plan(_ctx_with_store(store), "list of OpenRouter models", parsed=CONFIRMED_SPEC)
    )[0]
    ack = await cap.execute(_ctx_with_store(store), step)

    # The ack hands back a job id + initial status to poll on.
    assert ack["kind"] == "ack"
    job_id = ack["job_id"]
    assert ack["job_status"] == "queued"

    # The job is in the store immediately (queued), then completes after the run.
    queued = await store.get(job_id)
    assert queued is not None
    assert queued.category == JobCategory.discovery
    assert queued.cost == pytest.approx(0.09)

    await spawned["task"]

    done = await store.get(job_id)
    assert done.status == JobStatus.applied
    assert done.result_count == len(FULL_ROWS)
    assert done.progress.total == len(FULL_ROWS)
    assert done.progress.processed == len(FULL_ROWS)
    assert "openrouter.ai" in (done.platforms or [])
    assert done.type_name == "OpenRouterModel"
    assert done.completed_at is not None


async def test_run_logs_resolved_write_target(monkeypatch):
    """Observability (ONTA-198): a discovery run records the EXACT graph it writes
    into — ``web_ingest_run_start`` up front and ``web_ingest_complete`` at the end
    both carry ``kg_name`` + ``instance_graph``. Without this a run that reports
    "N filled" but whose rows never appear in the Explorer is undiagnosable: you
    cannot tell which graph the resolver actually wrote to."""
    import structlog
    from cograph_client.enrichment.job_store import InMemoryJobStore

    provider = FakeProvider()
    register_web_source(provider)
    _patch_preview(monkeypatch, entities=_single_type_entities())

    async def fake_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None, **_kw):
        rows = json.loads(content)
        return IngestResult(entities_extracted=len(rows), entities_resolved=len(rows))

    monkeypatch.setattr(SchemaResolver, "ingest", fake_ingest)

    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    store = InMemoryJobStore()
    cap = WebIngestCapability()
    step = (
        await cap.plan(_ctx_with_store(store), "list of OpenRouter models", parsed=CONFIRMED_SPEC)
    )[0]
    with structlog.testing.capture_logs() as logs:
        await cap.execute(_ctx_with_store(store), step)
        await spawned["task"]

    want_graph = "https://cograph.tech/graphs/demo-tenant/kg/models"
    start = [e for e in logs if e.get("event") == "web_ingest_run_start"]
    assert start, "run must log its write target up front"
    assert start[0]["kg_name"] == "models"
    assert start[0]["instance_graph"] == want_graph

    complete = [e for e in logs if e.get("event") == "web_ingest_complete"]
    assert complete, "a successful run must log completion"
    assert complete[0]["kg_name"] == "models"
    assert complete[0]["instance_graph"] == want_graph
    # A resolvable KG never trips the base-graph misroute warning.
    assert not [e for e in logs if e.get("event") == "web_ingest_no_target_kg"]


async def test_run_warns_when_write_target_missing(monkeypatch):
    """When kg_name is empty the run has no per-KG target, so instance data would
    land in the tenant BASE graph (invisible to the Explorer). The run still
    proceeds (behavior unchanged) but logs ``web_ingest_no_target_kg`` loudly and
    records ``instance_graph=None`` in ``web_ingest_run_start`` so the misroute is
    obvious instead of a silent black hole."""
    import structlog
    from cograph_client.enrichment.job_store import InMemoryJobStore

    provider = FakeProvider()
    register_web_source(provider)
    _patch_preview(monkeypatch, entities=_single_type_entities())

    async def fake_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None, **_kw):
        rows = json.loads(content)
        return IngestResult(entities_extracted=len(rows), entities_resolved=len(rows))

    monkeypatch.setattr(SchemaResolver, "ingest", fake_ingest)

    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    store = InMemoryJobStore()

    def _ctx_no_kg() -> AgentContext:
        return AgentContext(
            tenant_id="demo-tenant",
            kg_name="",  # no KG context resolved upstream
            neptune=MagicMock(),
            anthropic_key="sk-ant-test",
            openrouter_key="",
            extras={"prior_clarify_count": 0, "enrichment_job_store": store},
        )

    cap = WebIngestCapability()
    step = (await cap.plan(_ctx_no_kg(), "list of OpenRouter models", parsed=CONFIRMED_SPEC))[0]
    # Ensure the persisted step carries no target either, so kg_name truly resolves empty.
    step.params.pop("kg_name", None)
    with structlog.testing.capture_logs() as logs:
        await cap.execute(_ctx_no_kg(), step)
        await spawned["task"]

    start = [e for e in logs if e.get("event") == "web_ingest_run_start"]
    assert start and start[0]["instance_graph"] is None
    assert start[0]["kg_name"] is None
    assert [e for e in logs if e.get("event") == "web_ingest_no_target_kg"], (
        "an empty kg_name must raise the base-graph misroute warning"
    )


async def test_execute_marks_job_failed_on_error(monkeypatch):
    """A discovery that raises mid-ingest leaves the job failed with an error, not
    silently dropped — so the live status can show the failure."""
    from cograph_client.enrichment.job_store import InMemoryJobStore
    from cograph_client.enrichment.models import JobStatus

    register_web_source(FakeProvider())
    _patch_preview(monkeypatch, entities=_single_type_entities())

    async def boom(self, *a, **k):
        raise RuntimeError("ingest exploded")

    monkeypatch.setattr(SchemaResolver, "ingest", boom)
    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    store = InMemoryJobStore()
    cap = WebIngestCapability()
    step = (
        await cap.plan(_ctx_with_store(store), "list of OpenRouter models", parsed=CONFIRMED_SPEC)
    )[0]
    ack = await cap.execute(_ctx_with_store(store), step)
    await spawned["task"]

    failed = await store.get(ack["job_id"])
    assert failed.status == JobStatus.failed
    assert "ingest exploded" in (failed.error or "")


async def test_run_times_out_and_marks_job_failed(monkeypatch):
    """ONTA-196: a discovery whose ingest STALLS past the per-run wall-clock
    budget must flip the job to ``failed`` (with a timeout message), NEVER leave
    it stuck on ``running``. We patch the budget to a hair, make ingest sleep well
    past it, and assert the terminal state + message — no eternal spinner."""
    from cograph_client.enrichment.job_store import InMemoryJobStore
    from cograph_client.enrichment.models import JobStatus

    # Tiny per-run budget: the ingest below far exceeds it.
    monkeypatch.setattr(web_ingest_cap, "_RUN_TIMEOUT_S", 0.05)

    register_web_source(FakeProvider())
    _patch_preview(monkeypatch, entities=_single_type_entities())

    async def slow_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None, **_kw):
        await asyncio.sleep(5)  # far exceeds the (patched) run budget
        return IngestResult(entities_extracted=0, entities_resolved=0)

    monkeypatch.setattr(SchemaResolver, "ingest", slow_ingest)

    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    store = InMemoryJobStore()
    cap = WebIngestCapability()
    step = (
        await cap.plan(_ctx_with_store(store), "list of OpenRouter models", parsed=CONFIRMED_SPEC)
    )[0]
    ack = await cap.execute(_ctx_with_store(store), step)

    # The wrapper bounds the run to ~0.05s; a hang here IS the regression.
    await asyncio.wait_for(spawned["task"], timeout=3)

    failed = await store.get(ack["job_id"])
    # Terminal FAILED — not stuck on running.
    assert failed.status == JobStatus.failed
    assert failed.status != JobStatus.running
    assert failed.completed_at is not None
    assert "timed out" in (failed.error or "").lower()


def test_capability_registered_by_default():
    from cograph_client.agent.planner import register_default_capabilities
    from cograph_client.agent.registry import get_capability

    register_default_capabilities()
    assert get_capability("web_ingest") is not None


# --- per-record source-URL provenance (ONTA-151) ---------------------------- #


def test_row_source_url_resolves_by_name_then_index():
    """A row's URL resolves by its name first (the adapter keying), then by its
    positional index (so an index-keyed provider also resolves); unknown → None."""
    from cograph_client.agent.capabilities.web_ingest_cap import _row_source_url

    prov = {"anthropic/claude-opus-4-8": "https://a", "1": "https://b"}
    # Name-keyed hit.
    assert _row_source_url({"name": "anthropic/claude-opus-4-8"}, 0, prov) == "https://a"
    # Name miss → positional-index fallback (index-keyed provider).
    assert _row_source_url({"name": "openai/gpt-5"}, 1, prov) == "https://b"
    # Nothing resolvable for this row → None (left un-stamped by the caller).
    assert _row_source_url({"name": "x"}, 9, prov) is None
    # Empty provenance → None.
    assert _row_source_url({"name": "x"}, 0, {}) is None


def test_attach_source_urls_stamps_each_row_and_noop_without_provenance():
    from cograph_client.agent.capabilities.web_ingest_cap import _attach_source_urls

    rows = [{"name": "m0"}, {"name": "m1"}]
    assert _attach_source_urls(rows, {"m0": "https://p0", "m1": "https://p1"}) == 2
    assert rows[0]["source_url"] == "https://p0"
    assert rows[1]["source_url"] == "https://p1"

    # No provenance → rows untouched, returns 0 (free/stub providers).
    rows2 = [{"name": "m0"}]
    assert _attach_source_urls(rows2, {}) == 0
    assert "source_url" not in rows2[0]


def test_attach_source_urls_never_clobbers_and_skips_unknown():
    """A provider-set source_url is preserved; a row with no resolvable URL is
    left un-stamped rather than blanked."""
    from cograph_client.agent.capabilities.web_ingest_cap import _attach_source_urls

    rows = [
        {"name": "m0", "source_url": "https://provider-set"},
        {"name": "m1"},  # has a URL in the map
        {"name": "m2"},  # NO URL in the map → stays un-stamped
    ]
    stamped = _attach_source_urls(rows, {"m1": "https://p1"})
    assert stamped == 1
    assert rows[0]["source_url"] == "https://provider-set"  # not clobbered
    assert rows[1]["source_url"] == "https://p1"
    assert "source_url" not in rows[2]


# --- citation reindex mis-binding fix (ONTA-256) ---------------------------- #


def test_dedupe_rows_with_source_urls_binds_before_reindex():
    """MECHANISM (ONTA-256): a row's source_url must be bound BEFORE dedupe drops
    rows and reindexes the survivors.

    ``_dedupe_rows`` shifts every surviving row's positional index; the provider's
    provenance map is keyed by each row's ORIGINAL position, so deriving the URL by
    position AFTER the drop binds a survivor to a DROPPED neighbour's page. The fix
    stamps first (indices still original) and carries the URL on the row object, so
    the survivor keeps ITS OWN url through the reindex.

    Invented tokens only (Widget-*, page-*): the invariant must hold for ANY
    rows/urls, so nothing here is a domain-specific example.
    """
    from cograph_client.agent.capabilities.web_ingest_cap import (
        _attach_source_urls,
        _dedupe_rows,
        _dedupe_rows_with_source_urls,
    )

    url_a = "https://example.test/page-a"
    url_b = "https://example.test/page-b"
    url_c = "https://example.test/page-c"
    # Provenance keyed by each row's ORIGINAL position — an index-keyed provider,
    # the exact case ``_row_source_url``'s positional fallback serves and the one a
    # reindex corrupts. (A name-keyed map would resolve order-independently and so
    # could never exercise the bug.)
    provenance = {"0": url_a, "1": url_b, "2": url_c}

    def fresh_rows():
        return [
            {"name": "Widget-A"},
            {"name": "Widget-B"},
            {"name": "Widget-C"},
        ]

    # Seed `seen` with the MIDDLE row's dedupe key (computed via _dedupe_rows so we
    # don't hardcode the normalization), so Widget-B is the one dropped — that is
    # what shifts Widget-C from original index 2 down to deduped index 1.
    seed: set[str] = set()
    _dedupe_rows([{"name": "Widget-B"}], "name", seed)

    # --- THE FIX: bind before dedupe -> survivors keep their OWN page. -------- #
    batch = _dedupe_rows_with_source_urls(fresh_rows(), "name", set(seed), provenance)
    survivors = {r["name"]: r.get("source_url") for r in batch}
    assert set(survivors) == {"Widget-A", "Widget-C"}  # Widget-B dropped
    assert survivors["Widget-A"] == url_a
    assert survivors["Widget-C"] == url_c              # its OWN page …
    assert survivors["Widget-C"] != url_b              # … NOT the dropped neighbour's

    # --- REGRESSION GUARD: the OLD order (dedupe THEN attach) mis-binds. ------ #
    # Drop Widget-B first, then attach by the now-shifted index: Widget-C sits at
    # deduped index 1, so the positional lookup hands it Widget-B's page (url_b).
    # This is exactly the bug the fix removes — asserting it proves the ordering is
    # load-bearing, not incidental, and that this test would fail on a revert.
    dropped_first = _dedupe_rows(fresh_rows(), "name", set(seed))
    _attach_source_urls(dropped_first, provenance)
    buggy = {r["name"]: r.get("source_url") for r in dropped_first}
    assert buggy["Widget-C"] == url_b  # the mis-bind the fix prevents


def test_group_rows_by_source_url_partitions_homogeneously():
    """A batch mixing rows from different pages splits into one group per URL, so
    every group is homogeneous in its source_url — the extractor that ingests a
    group can only ever stamp THAT group's page URL. Consecutive-run grouping
    preserves order and record count."""
    from cograph_client.agent.capabilities.web_ingest_cap import (
        _group_rows_by_source_url,
    )

    # Two invented pages, three invented entities (no persona tokens).
    rows = [
        {"name": "Widget A", "source_url": "https://example.test/widgets"},
        {"name": "Widget B", "source_url": "https://example.test/widgets"},
        {"name": "Sprocket X", "source_url": "https://example.test/sprockets"},
    ]
    groups = _group_rows_by_source_url(rows)
    assert len(groups) == 2
    assert [r["name"] for r in groups[0]] == ["Widget A", "Widget B"]
    assert {r["source_url"] for r in groups[0]} == {"https://example.test/widgets"}
    assert [r["name"] for r in groups[1]] == ["Sprocket X"]
    assert {r["source_url"] for r in groups[1]} == {"https://example.test/sprockets"}
    # No record dropped or duplicated by the partition.
    assert sum(len(g) for g in groups) == len(rows)


def test_group_rows_by_source_url_single_and_missing_url():
    """A batch that already shares one URL (or carries none) is a single group —
    identical to the pre-fix single-partition behavior (no needless fan-out)."""
    from cograph_client.agent.capabilities.web_ingest_cap import (
        _group_rows_by_source_url,
    )

    same = [
        {"name": "Gadget 1", "source_url": "https://example.test/gadgets"},
        {"name": "Gadget 2", "source_url": "https://example.test/gadgets"},
    ]
    assert len(_group_rows_by_source_url(same)) == 1

    none = [{"name": "Gadget 1"}, {"name": "Gadget 2"}]
    assert len(_group_rows_by_source_url(none)) == 1

    assert _group_rows_by_source_url([]) == []


async def test_execute_binds_citation_per_source_record(monkeypatch):
    """MECHANISM test for the citation mis-binding fix: when ONE discovery batch
    mixes entities drawn from TWO different pages, each `resolver.ingest` call sees
    rows from exactly ONE page — so an entity's source_url is the URL of the record
    it was extracted from, never the other page's URL broadcast across it.

    Uses invented entities/URLs (Widget/Sprocket) so nothing overfits to personas.
    """
    # A provider whose rows span two pages: two Widgets from page W, one Sprocket
    # from page S. Provenance keys each row to its OWN page (the real adapter key).
    class TwoPageProvider:
        name = "twopage"
        is_paid = True
        cost_per_call = 0.75

        async def discover(self, query, *, sample, max_rows, hint_columns, context, urls=None):
            rows = [
                {"name": "Widget A"},
                {"name": "Widget B"},
                {"name": "Sprocket X"},
            ]
            rows = rows[: (5 if sample else max_rows)]
            prov = {
                "Widget A": "https://example.test/widgets",
                "Widget B": "https://example.test/widgets",
                "Sprocket X": "https://example.test/sprockets",
            }
            return DiscoverResult(
                rows=rows,
                provenance=prov,
                sources=["https://example.test"],
                estimated_total=3,
                is_partial=sample,
            )

    register_web_source(TwoPageProvider())
    _patch_preview(monkeypatch, entities=_single_type_entities())

    # Capture every ingest call's committed rows so we can assert homogeneity.
    ingest_calls: list[list[dict]] = []

    async def fake_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None, **_kw):
        rows = json.loads(content)
        ingest_calls.append(rows)
        return IngestResult(entities_extracted=len(rows), entities_resolved=len(rows))

    monkeypatch.setattr(SchemaResolver, "ingest", fake_ingest)
    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    cap = WebIngestCapability()
    step = (await cap.plan(_ctx(), "find widgets and sprockets", parsed=CONFIRMED_SPEC))[0]
    await cap.execute(_ctx(), step)
    await spawned["task"]

    # Every ingest call is homogeneous in source_url (the citation the extractor
    # can bind is fixed by the partition, not chosen by the LLM per entity).
    assert ingest_calls, "rows were committed"
    for rows in ingest_calls:
        urls = {r.get("source_url") for r in rows}
        assert len(urls) == 1, f"a batch mixed source URLs: {urls}"

    # And each entity ended up with the URL of ITS page — Widgets → widgets page,
    # Sprocket → sprockets page. Never the cross-record broadcast.
    by_name = {r["name"]: r["source_url"] for rows in ingest_calls for r in rows}
    assert by_name["Widget A"] == "https://example.test/widgets"
    assert by_name["Widget B"] == "https://example.test/widgets"
    assert by_name["Sprocket X"] == "https://example.test/sprockets"


async def test_execute_threads_per_record_source_url(monkeypatch):
    """Each discovered entity carries its own source_url drawn from the provider's
    provenance map, committed through the SAME ingest path (content_type="json")
    — and the plan preview already shows the citation column (preview == commit)."""
    provider = FakeProvider(provenance=True, **RICH)
    register_web_source(provider)
    _patch_preview(monkeypatch, entities=_single_type_entities())

    captured: dict = {"content_types": set()}
    committed_rows: list[dict] = []
    # URL set of EACH ingest call, recorded here so the homogeneity check runs in
    # the test body (below) — an assert inside fake_ingest would be swallowed by
    # the run loop's `except Exception: continue` and never fail the test.
    per_call_url_sets: list[set] = []

    async def fake_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None, **_kw):
        captured["content_types"].add(content_type)
        rows = json.loads(content)
        committed_rows.extend(rows)
        per_call_url_sets.append({r.get("source_url") for r in rows})
        return IngestResult(entities_extracted=len(rows), entities_resolved=len(rows))

    monkeypatch.setattr(SchemaResolver, "ingest", fake_ingest)

    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    cap = WebIngestCapability()
    step = (await cap.plan(_ctx(), "find a list of OpenRouter models", parsed=CONFIRMED_SPEC))[0]
    # Preview == commit: the sampled rows already carry source_url.
    assert step.preview["sample_rows"]
    assert all("source_url" in r for r in step.preview["sample_rows"])

    await cap.execute(_ctx(), step)
    await spawned["task"]

    assert committed_rows, "rows were committed"
    assert captured["content_types"] == {"json"}  # same ingest path
    # Each ingest call saw rows from ONE page only (citation-binding fix) — asserted
    # here in the test body so a mixed-URL batch actually fails.
    assert per_call_url_sets, "at least one ingest call ran"
    for urls in per_call_url_sets:
        assert len(urls) == 1, f"an ingest batch mixed source URLs: {urls}"
    # Every committed record traces to its OWN page; source_url rides alongside
    # the confirmed attributes (an extra provenance column, not a replacement).
    # The provider gives each row a distinct page, so the citation-binding fix
    # commits them in per-page batches — the source_url still equals THIS record's
    # own page (keyed by name), independent of batch ordering.
    by_name = {r["name"]: r for r in committed_rows}
    for r in FULL_ROWS:
        got = by_name[r["name"]]
        idx = FULL_ROWS.index(r)
        assert got["source_url"] == f"https://src.example/page-{idx}"
        assert "name" in got and "context_length" in got


async def test_execute_no_provenance_adds_no_source_url(monkeypatch):
    """A provider that supplies no provenance (e.g. a free source) yields entities
    with NO source_url column — the threading degrades silently, never stamping a
    blank citation."""
    provider = FakeProvider()  # provenance=False
    register_web_source(provider)
    _patch_preview(monkeypatch, entities=_single_type_entities())

    captured: dict = {}

    async def fake_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None, **_kw):
        captured.update(content=content)
        rows = json.loads(content)
        return IngestResult(entities_extracted=len(rows), entities_resolved=len(rows))

    monkeypatch.setattr(SchemaResolver, "ingest", fake_ingest)
    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    cap = WebIngestCapability()
    step = (await cap.plan(_ctx(), "list of OpenRouter models", parsed=CONFIRMED_SPEC))[0]
    await cap.execute(_ctx(), step)
    await spawned["task"]

    rows_back = json.loads(captured["content"])
    assert rows_back
    assert all("source_url" not in r for r in rows_back)


async def test_planner_short_circuits_capability_clarify(monkeypatch):
    """End-to-end: a discover turn whose capability needs attribute confirmation
    returns {kind:"clarify"} via the planner's clarify short-circuit."""
    import json as _json

    from cograph_client.agent import planner as planner_mod
    from cograph_client.agent.registry import register_capability, reset_capabilities

    async def fake_classify_chat(*_a, **_k):
        return _json.dumps({"intents": ["discover"]})

    async def fake_spec_chat(*_a, **_k):
        return _json.dumps(ENTITY_ONLY_SPEC)

    monkeypatch.setattr(planner_mod, "openrouter_chat", fake_classify_chat)
    monkeypatch.setattr(web_ingest_cap, "openrouter_chat", fake_spec_chat)

    reset_capabilities()
    register_capability(WebIngestCapability())
    register_web_source(FakeProvider())

    ctx = AgentContext(
        tenant_id="demo-tenant", kg_name="models", neptune=MagicMock(),
        anthropic_key="sk-ant-test", openrouter_key="k",
    )
    result = await planner_mod.handle(ctx, "a list of OpenRouter models")
    assert result["kind"] == "clarify"
    assert any(o.startswith("Use these:") for o in result["options"])
    reset_capabilities()


async def test_execute_records_provider_log_on_success(monkeypatch):
    """A completed discovery run records which web provider was used and how many
    records it returned — surfaced in the run-detail view alongside platforms."""
    from cograph_client.enrichment.job_store import InMemoryJobStore

    register_web_source(FakeProvider())
    _patch_preview(monkeypatch, entities=_single_type_entities())

    async def fake_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None, **_kw):
        rows = json.loads(content)
        return IngestResult(entities_extracted=len(rows), entities_resolved=len(rows))

    monkeypatch.setattr(SchemaResolver, "ingest", fake_ingest)
    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    store = InMemoryJobStore()
    cap = WebIngestCapability()
    step = (
        await cap.plan(_ctx_with_store(store), "list of OpenRouter models", parsed=CONFIRMED_SPEC)
    )[0]
    ack = await cap.execute(_ctx_with_store(store), step)
    await spawned["task"]

    done = await store.get(ack["job_id"])
    assert len(done.provider_logs) == 1
    plog = done.provider_logs[0]
    assert plog.provider == "fake"
    assert plog.status == "ok"
    assert plog.matches == len(FULL_ROWS)
    assert done.error_summary == []


async def test_execute_records_provider_error_when_discover_fails(monkeypatch):
    """When the web provider itself fails during the full pull, the job is failed
    AND the provider log + error summary attribute the failure to that provider."""
    from cograph_client.enrichment.job_store import InMemoryJobStore
    from cograph_client.enrichment.models import JobStatus

    class FullFailProvider(FakeProvider):
        async def discover(self, query, *, sample, max_rows, hint_columns, context, urls=None):
            if not sample:  # plan-time sample works; the full pull explodes
                raise RuntimeError("provider 503 unavailable")
            return await super().discover(
                query, sample=sample, max_rows=max_rows,
                hint_columns=hint_columns, context=context, urls=urls,
            )

    register_web_source(FullFailProvider())
    _patch_preview(monkeypatch, entities=_single_type_entities())
    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    store = InMemoryJobStore()
    cap = WebIngestCapability()
    step = (
        await cap.plan(_ctx_with_store(store), "list of OpenRouter models", parsed=CONFIRMED_SPEC)
    )[0]
    ack = await cap.execute(_ctx_with_store(store), step)
    await spawned["task"]

    failed = await store.get(ack["job_id"])
    assert failed.status == JobStatus.failed
    assert len(failed.provider_logs) == 1
    assert failed.provider_logs[0].provider == "fake"
    assert failed.provider_logs[0].status == "error"
    assert failed.error_summary and failed.error_summary[0].provider == "fake"
    assert "503" in (failed.provider_logs[0].last_error or "")


async def test_execute_does_not_blame_provider_when_ingest_fails(monkeypatch):
    """A failure AFTER the provider returned (ingest/refresh) is a job-level
    error: the provider log stays 'ok' (not mis-blamed) and the error-summary
    entry is kind='job' with no provider attribution."""
    from cograph_client.enrichment.job_store import InMemoryJobStore
    from cograph_client.enrichment.models import JobStatus

    register_web_source(FakeProvider())  # discover() succeeds
    _patch_preview(monkeypatch, entities=_single_type_entities())

    async def boom(self, *a, **k):  # ingest explodes after a clean discover
        raise RuntimeError("ingest exploded")

    monkeypatch.setattr(SchemaResolver, "ingest", boom)
    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    store = InMemoryJobStore()
    cap = WebIngestCapability()
    step = (
        await cap.plan(_ctx_with_store(store), "list of OpenRouter models", parsed=CONFIRMED_SPEC)
    )[0]
    ack = await cap.execute(_ctx_with_store(store), step)
    await spawned["task"]

    failed = await store.get(ack["job_id"])
    assert failed.status == JobStatus.failed
    # Provider ran fine — its log is NOT flipped to error.
    assert failed.provider_logs[0].provider == "fake"
    assert failed.provider_logs[0].status == "ok"
    # The failure is attributed at job level, not to the provider.
    assert failed.error_summary and failed.error_summary[0].kind == "job"
    assert failed.error_summary[0].provider is None
    assert "ingest exploded" in (failed.error or "")


# --- _resolve_spec query_kind classification (ONTA-190) ---------------------


async def _run_resolve_spec(monkeypatch, spec_json: str, instruction: str) -> dict:
    """Drive _resolve_spec with a mocked LLM returning ``spec_json``."""
    async def fake_chat(*_a, **_k):
        return spec_json

    monkeypatch.setattr(web_ingest_cap, "openrouter_chat", fake_chat)
    ctx = AgentContext(
        tenant_id="demo-tenant", kg_name="places", neptune=MagicMock(),
        anthropic_key="sk-ant-test", openrouter_key="k",
    )
    return await web_ingest_cap._resolve_spec(ctx, instruction)


async def test_resolve_spec_classifies_place_query(monkeypatch):
    """A place-shaped query → the LLM emits query_kind="place", carried through
    _normalize_spec so plan() can route it to a place-specialized provider."""
    spec = json.dumps({
        "entity_type": "CoffeeShop",
        "key_attribute": "name",
        "query": "coffee shops in the Mission",
        "query_kind": "place",
        "confirmed_attributes": ["address"],
        "suggested_attributes": ["address", "phone", "rating"],
    })
    out = await _run_resolve_spec(monkeypatch, spec, "coffee shops in the Mission SF")
    assert out["query_kind"] == "place"
    assert out["entity_type"] == "CoffeeShop"


async def test_resolve_spec_general_query_has_no_kind(monkeypatch):
    """A non-place query → query_kind is None (the LLM returned null), so plan()
    keeps the general default provider — kind routing stays dormant."""
    spec = json.dumps({
        "entity_type": "Model",
        "key_attribute": "name",
        "query": "OpenRouter models",
        "query_kind": None,
        "confirmed_attributes": [],
        "suggested_attributes": ["provider", "context_length"],
    })
    out = await _run_resolve_spec(monkeypatch, spec, "list of OpenRouter models")
    assert out["query_kind"] is None


async def test_resolve_spec_normalizes_literal_null_kind(monkeypatch):
    """LLMs sometimes emit the STRING "null" (or omit the key) for a non-specialized
    query — both collapse to None so no spurious routing happens."""
    spec = json.dumps({
        "entity_type": "Model",
        "key_attribute": "name",
        "query": "OpenRouter models",
        "query_kind": "null",  # literal string, not JSON null
        "confirmed_attributes": [],
        "suggested_attributes": ["provider"],
    })
    out = await _run_resolve_spec(monkeypatch, spec, "list of OpenRouter models")
    assert out["query_kind"] is None

    # Key entirely absent → also None (defensive .get on the normalized spec).
    spec2 = json.dumps({
        "entity_type": "Model",
        "key_attribute": "name",
        "query": "OpenRouter models",
        "confirmed_attributes": [],
        "suggested_attributes": ["provider"],
    })
    out2 = await _run_resolve_spec(monkeypatch, spec2, "list of OpenRouter models")
    assert out2["query_kind"] is None


async def test_resolve_spec_warns_when_llm_text_unparsed(monkeypatch):
    """When the LLM returns text that isn't a JSON object, _resolve_spec must SURFACE
    the degrade (a `web_ingest_spec_unparsed` warning) rather than silently falling
    through to the fallback spec — so a future non-JSON degrade is never invisible.
    The exception path already logs `web_ingest_spec_failed`; this is the parse miss.

    Records against a mock module logger (order-independent under the full suite —
    the module logger is cached by earlier tests so capture_logs sees nothing)."""
    from unittest.mock import MagicMock as _MagicMock

    async def fake_chat(*_a, **_k):
        return "here is your spec but it is prose, not json at all"

    rec = _MagicMock()
    monkeypatch.setattr(web_ingest_cap, "logger", rec)
    monkeypatch.setattr(web_ingest_cap, "openrouter_chat", fake_chat)
    ctx = AgentContext(
        tenant_id="demo-tenant", kg_name="places", neptune=MagicMock(),
        anthropic_key="sk-ant-test", openrouter_key="k",
    )
    out = await web_ingest_cap._resolve_spec(ctx, "list of OpenRouter models")
    # Degraded to the deterministic fallback spec (never 500s / never empty).
    assert out["entity_type"]
    warned = [c for c in rec.warning.call_args_list
              if c.args and c.args[0] == "web_ingest_spec_unparsed"]
    assert warned, "unparsed LLM text must emit web_ingest_spec_unparsed"


def test_norm_query_kind_lowercases_and_slugs():
    """A real kind is lowercased + slugged so casing/punctuation from the LLM still
    matches a provider's query_kinds; empty/null-ish collapse to None."""
    assert web_ingest_cap._norm_query_kind("Place") == "place"
    assert web_ingest_cap._norm_query_kind("PLACE") == "place"
    assert web_ingest_cap._norm_query_kind("  place  ") == "place"
    assert web_ingest_cap._norm_query_kind(None) is None
    assert web_ingest_cap._norm_query_kind("null") is None
    assert web_ingest_cap._norm_query_kind("none") is None
    assert web_ingest_cap._norm_query_kind("") is None


# --- enumeration fan-out (ONTA-192) ------------------------------------------ #

# An "all X in Y and Z" ask the spec PARTITIONED into self-contained sub-queries.
FAN_SPEC = {
    "entity_type": "Physician",
    "key_attribute": "name",
    "query": "primary care physicians in Tustin and Santa Ana, California",
    "query_kind": "place",
    "subqueries": [
        "primary care physicians in Tustin, CA",
        "primary care physicians in Santa Ana, CA",
    ],
    "confirmed_attributes": ["specialty", "city"],
    "suggested_attributes": ["specialty", "city", "phone"],
}

TUSTIN_ROWS = [
    {"name": "Alina Reyes, MD", "specialty": "Family Medicine", "city": "Tustin", "phone": "1"},
    {"name": "Priya Nair, DO", "specialty": "Family Medicine", "city": "Tustin", "phone": "2"},
    {"name": "Dr. Overlap", "specialty": "Internal Medicine", "city": "Tustin", "phone": "3"},
]
SANTA_ANA_ROWS = [
    {"name": "Marcus Chen, MD", "specialty": "Internal Medicine", "city": "Santa Ana", "phone": "4"},
    # The SAME Tustin record (same identity signals, shouty casing) listed under
    # both city searches — the true duplicate the cross-batch merge must drop.
    # (Same NAME with different city/phone would be a DISTINCT record and kept.)
    {"name": "DR OVERLAP", "specialty": "Internal Medicine", "city": "Tustin", "phone": "3"},
    {"name": "Samuel Ortiz, MD", "specialty": "Geriatrics", "city": "Santa Ana", "phone": "6"},
]


class PerQueryProvider(FakeProvider):
    """Rows keyed BY QUERY — each sub-query yields its own batch. "Dr. Overlap"
    appears under both cities (with different casing/punctuation) so the
    cross-batch key dedupe is observable."""

    def __init__(self, rows_by_query: dict, *, fail_queries=frozenset(), **kw):
        super().__init__(**kw)
        self._by_query = rows_by_query
        self._fail = set(fail_queries)

    async def discover(self, query, *, sample, max_rows, hint_columns, context, urls=None):
        self.calls.append((query, sample, max_rows, tuple(hint_columns or ())))
        if query in self._fail:
            raise RuntimeError(f"provider down for {query!r}")
        rows = list(self._by_query.get(query, []))[:max_rows]
        if hint_columns:
            rows = [{c: r.get(c, "unknown") for c in hint_columns} for r in rows]
        return DiscoverResult(
            rows=rows,
            sources=[f"https://directory.example/{query.rsplit(' ', 1)[-1]}"],
            estimated_total=len(rows),
            is_partial=False,
        )


def test_norm_subqueries_sanitizes():
    """Strings only, stripped, case-insensitively deduped, capped at the fan-out
    ceiling; malformed input degrades to [] (single-query behavior)."""
    f = web_ingest_cap._norm_subqueries
    assert f(["a", "  b ", "A", "", None, 3, "c"]) == ["a", "b", "c"]
    assert f([f"q{i}" for i in range(10)]) == [f"q{i}" for i in range(6)]
    assert f("not a list") == []
    assert f(None) == []


def test_estimate_cost_prices_subquery_fanout():
    """A single-call-per-run provider bills one call PER SUB-QUERY; a paginating
    provider splits the row cap across sub-queries so the total page count (and
    price) stays ≈ the single-run figure."""

    class _WholeRun:
        name = "wholerun"
        is_paid = True
        cost_per_call = 0.03

    cost = web_ingest_cap._estimate_cost(_WholeRun(), 200, 200, subqueries=3)
    assert cost["paid_calls"] == 3
    assert cost["estimated_usd"] == pytest.approx(0.09)
    assert "across 3 sub-queries" in cost["note"]

    class _Paginating:
        name = "paginating"
        is_paid = True
        cost_per_call = 0.017
        rows_per_call = 20

    # 200 rows / 2 sub-queries = 100 each = 5 pages each = 10 total — the same
    # 10 pages a single 200-row run would bill.
    cost2 = web_ingest_cap._estimate_cost(_Paginating(), 200, 200, subqueries=2)
    assert cost2["paid_calls"] == 10
    assert cost2["estimated_usd"] == pytest.approx(0.17)


async def test_plan_persists_subqueries_on_fast_path():
    """The lean fast path persists the enumeration partition for execute() and
    prices the plan as one run per sub-query."""
    provider = FakeProvider(is_paid=True, cost_per_call=0.03)
    register_web_source(provider)
    steps = await WebIngestCapability().plan(
        _ctx(), "all primary care physicians in Tustin and Santa Ana", parsed=FAN_SPEC
    )
    step = steps[0]
    assert step.action == "discover_ingest"
    assert step.params["subqueries"] == FAN_SPEC["subqueries"]
    assert step.cost["paid_calls"] == 2
    assert step.cost["estimated_usd"] == pytest.approx(0.06)


async def test_execute_fans_out_dedupes_and_streams(monkeypatch):
    """The fan-out run: one discovery per sub-query (sample=False), batches
    deduped on the normalized key attribute across sub-queries, each batch
    ingested as it lands (streaming progress), one merged job driven to applied
    with the exact unique count."""
    from cograph_client.enrichment.job_store import InMemoryJobStore
    from cograph_client.enrichment.models import JobStatus

    provider = PerQueryProvider(
        {
            FAN_SPEC["subqueries"][0]: TUSTIN_ROWS,
            FAN_SPEC["subqueries"][1]: SANTA_ANA_ROWS,
        },
        is_paid=True,
        cost_per_call=0.03,
    )
    register_web_source(provider)

    batches: list[int] = []

    async def fake_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None, **_kw):
        rows = json.loads(content)
        batches.append(len(rows))
        return IngestResult(
            entities_extracted=len(rows), entities_resolved=len(rows),
            types_created=["Physician"],
        )

    monkeypatch.setattr(SchemaResolver, "ingest", fake_ingest)

    refreshes: list = []

    async def fake_refresh(neptune, *, tenant_id, kg_name, affected_types):
        refreshes.append(set(affected_types))

    monkeypatch.setattr(web_ingest_cap, "refresh_after_write", fake_refresh)

    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    store = InMemoryJobStore()
    cap = WebIngestCapability()
    step = (
        await cap.plan(
            _ctx_with_store(store),
            "all primary care physicians in Tustin and Santa Ana",
            parsed=FAN_SPEC,
        )
    )[0]
    ack = await cap.execute(_ctx_with_store(store), step)
    await spawned["task"]

    # One full (sample=False) discovery per sub-query, in order — each bounded
    # to the per-sub-query row share the plan priced (cap / n_sub), so actual
    # paid pagination can never exceed the quoted estimate.
    full_calls = [c for c in provider.calls if c[1] is False]
    assert [c[0] for c in full_calls] == FAN_SPEC["subqueries"]
    import math as _math
    per_sub = _math.ceil(200 / len(FAN_SPEC["subqueries"]))
    assert all(c[2] <= per_sub for c in full_calls)

    # Two batches ingested as they landed: 3 from Tustin, then 2 from Santa Ana
    # ("DR OVERLAP" deduped against "Dr. Overlap" across batches).
    assert batches == [3, 2]
    # ONE refresh for the whole fan-out, with the union of affected types.
    assert refreshes == [{"Physician"}]

    done = await store.get(ack["job_id"])
    assert done.status == JobStatus.applied
    assert done.result_count == 5
    assert done.progress.processed == 5
    assert done.progress.total == 5
    # The provider log accumulated the whole fan-out: 2 attempts; matches counts
    # what the provider FOUND (pre-dedupe: 3+3), not the post-merge uniques.
    (plog,) = done.provider_logs
    assert plog.attempts == 2
    assert plog.matches == 6
    assert plog.no_match == 0
    assert plog.status == "ok"
    # Platforms = distinct HOSTS consulted — both sub-query pages live on the
    # same directory host, so the cross-batch merge dedupes them to one entry.
    assert done.platforms == ["directory.example"]


async def test_execute_subquery_failure_is_partial(monkeypatch):
    """One sub-query dying at the provider must not sink the run: the others still
    land, the job completes with what was found, and the provider log records the
    error alongside the successes."""
    from cograph_client.enrichment.job_store import InMemoryJobStore
    from cograph_client.enrichment.models import JobStatus

    provider = PerQueryProvider(
        {FAN_SPEC["subqueries"][1]: SANTA_ANA_ROWS},
        fail_queries={FAN_SPEC["subqueries"][0]},
        is_paid=True,
        cost_per_call=0.03,
    )
    register_web_source(provider)

    async def fake_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None, **_kw):
        rows = json.loads(content)
        return IngestResult(entities_extracted=len(rows), entities_resolved=len(rows))

    monkeypatch.setattr(SchemaResolver, "ingest", fake_ingest)
    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    store = InMemoryJobStore()
    cap = WebIngestCapability()
    step = (
        await cap.plan(_ctx_with_store(store), "physicians in two cities", parsed=FAN_SPEC)
    )[0]
    ack = await cap.execute(_ctx_with_store(store), step)
    await spawned["task"]

    done = await store.get(ack["job_id"])
    assert done.status == JobStatus.applied  # partial coverage, not a failure
    assert done.result_count == len(SANTA_ANA_ROWS)
    (plog,) = done.provider_logs
    assert plog.errors == 1 and "provider down" in (plog.last_error or "")
    assert plog.matches == len(SANTA_ANA_ROWS)


async def test_execute_all_subqueries_failing_fails_job(monkeypatch):
    """EVERY sub-query dying at the provider → a failed job with the provider-
    attributed error (not a silent empty success)."""
    from cograph_client.enrichment.job_store import InMemoryJobStore
    from cograph_client.enrichment.models import JobStatus

    provider = PerQueryProvider(
        {}, fail_queries=set(FAN_SPEC["subqueries"]),
        is_paid=True, cost_per_call=0.03,
    )
    register_web_source(provider)
    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    store = InMemoryJobStore()
    cap = WebIngestCapability()
    step = (
        await cap.plan(_ctx_with_store(store), "physicians in two cities", parsed=FAN_SPEC)
    )[0]
    ack = await cap.execute(_ctx_with_store(store), step)
    await spawned["task"]

    done = await store.get(ack["job_id"])
    assert done.status == JobStatus.failed
    assert "provider down" in (done.error or "")
    assert done.error_summary and done.error_summary[0].provider == provider.name


# --- provider ensemble (kind-specialized + general together) ------------------ #


async def test_plan_prices_ensemble_sum():
    """A kind-matched query with BOTH providers registered prices the plan as the
    SUM of each provider's run — the ensemble consults both at execute time."""
    general = FakeProvider(is_paid=True, cost_per_call=0.03)
    place = KindFakeProvider(
        name="place_src", is_paid=True, cost_per_call=0.05,
    )
    register_web_source(general)
    register_web_source(place)

    steps = await WebIngestCapability().plan(
        _ctx(), "coffee shops in the Mission", parsed=PLACE_SPEC
    )
    step = steps[0]
    assert step.params["providers"] == ["place_src", "fake"]
    # One call each (no rows_per_call, no subqueries): 0.05 + 0.03.
    assert step.cost["paid_calls"] == 2
    assert step.cost["estimated_usd"] == pytest.approx(0.08)
    assert "'place_src' + 'fake'" in step.cost["note"]


async def test_execute_ensemble_merges_and_dedupes_across_providers(monkeypatch):
    """The ensemble run consults the specialized provider FIRST, then the general
    one, per sub-query — merged through the same key dedupe (a record found by
    both sources lands once, the specialized row winning) — with one provider
    log per ensemble member."""
    from cograph_client.enrichment.job_store import InMemoryJobStore
    from cograph_client.enrichment.models import JobStatus

    sq1, sq2 = FAN_SPEC["subqueries"]
    place = KindFakeProvider(
        name="place_src",
        is_paid=True,
        cost_per_call=0.05,
    )
    # The place source only knows Tustin rows (keyed per query).
    place_rows = {sq1: TUSTIN_ROWS, sq2: []}
    general_rows = {
        # General web re-finds one Tustin doc (dupe) + finds Santa Ana docs.
        sq1: [TUSTIN_ROWS[0]],
        sq2: SANTA_ANA_ROWS,
    }

    class KindPerQuery(KindFakeProvider):
        def __init__(self, table, **kw):
            super().__init__(**kw)
            self._table = table

        async def discover(self, query, *, sample, max_rows, hint_columns, context, urls=None):
            self.calls.append((query, sample, max_rows))
            rows = list(self._table.get(query, []))[:max_rows]
            if hint_columns:
                rows = [{c: r.get(c, "unknown") for c in hint_columns} for r in rows]
            return DiscoverResult(
                rows=rows, sources=["https://places.example/x"],
                estimated_total=len(rows), is_partial=False,
            )

    place = KindPerQuery(place_rows, name="place_src", is_paid=True, cost_per_call=0.05)
    general = PerQueryProvider(general_rows, is_paid=True, cost_per_call=0.03)
    register_web_source(general)
    register_web_source(place)

    batches: list[int] = []

    async def fake_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None, **_kw):
        rows = json.loads(content)
        batches.append(len(rows))
        return IngestResult(entities_extracted=len(rows), entities_resolved=len(rows))

    monkeypatch.setattr(SchemaResolver, "ingest", fake_ingest)
    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    store = InMemoryJobStore()
    cap = WebIngestCapability()
    step = (
        await cap.plan(
            _ctx_with_store(store), "all physicians in two cities", parsed=FAN_SPEC
        )
    )[0]
    assert step.params["providers"] == ["place_src", "fake"]
    ack = await cap.execute(_ctx_with_store(store), step)
    await spawned["task"]

    # Per sub-query: specialized first, then general — for both sub-queries.
    assert [c[0] for c in place.calls] == [sq1, sq2]
    assert [c[0] for c in general.calls if c[1] is False] == [sq1, sq2]

    # Batches: place/Tustin=3, general/Tustin=0 (its 1 row deduped — no batch),
    # place/SantaAna=0 (no batch), general/SantaAna=2 ("DR OVERLAP" deduped
    # against place's "Dr. Overlap" ACROSS providers AND cities). Unique = 5.
    assert batches == [3, 2]

    done = await store.get(ack["job_id"])
    assert done.status == JobStatus.applied
    assert done.result_count == 5
    assert done.progress.processed == 5

    # One provider log PER ensemble member, each with its own tally. matches
    # counts what each provider FOUND (pre-dedupe); a batch that dedupes to
    # nothing is NOT "no_match" (the provider did find rows) — only a genuinely
    # EMPTY result is (place_src's Santa Ana call returned zero rows).
    logs = {pl.provider: pl for pl in done.provider_logs}
    assert set(logs) == {"place_src", "fake"}
    assert logs["place_src"].matches == 3 and logs["place_src"].status == "ok"
    assert logs["place_src"].no_match == 1  # its Santa Ana search found nothing
    assert logs["fake"].matches == 4 and logs["fake"].status == "ok"
    assert logs["fake"].no_match == 0
    # Both hosts consulted.
    assert set(done.platforms or []) == {"places.example", "directory.example"}


async def test_execute_ensemble_survives_specialized_provider_outage(monkeypatch):
    """The specialized provider erroring on EVERY sub-query must not sink the run —
    the general provider still lands its rows and the job completes (partial
    coverage), with the outage recorded on the specialized provider's log."""
    from cograph_client.enrichment.job_store import InMemoryJobStore
    from cograph_client.enrichment.models import JobStatus

    sq1, sq2 = FAN_SPEC["subqueries"]

    class DownKindProvider(KindFakeProvider):
        async def discover(self, query, **kw):
            raise RuntimeError("places quota exhausted")

    place = DownKindProvider(name="place_src", is_paid=True, cost_per_call=0.05)
    general = PerQueryProvider(
        {sq1: TUSTIN_ROWS, sq2: SANTA_ANA_ROWS}, is_paid=True, cost_per_call=0.03
    )
    register_web_source(general)
    register_web_source(place)

    async def fake_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None, **_kw):
        rows = json.loads(content)
        return IngestResult(entities_extracted=len(rows), entities_resolved=len(rows))

    monkeypatch.setattr(SchemaResolver, "ingest", fake_ingest)
    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    store = InMemoryJobStore()
    cap = WebIngestCapability()
    step = (
        await cap.plan(
            _ctx_with_store(store), "all physicians in two cities", parsed=FAN_SPEC
        )
    )[0]
    ack = await cap.execute(_ctx_with_store(store), step)
    await spawned["task"]

    done = await store.get(ack["job_id"])
    assert done.status == JobStatus.applied  # general coverage still landed
    assert done.result_count == 5  # TUSTIN(3) + SANTA_ANA(3) − 1 cross-city dupe
    logs = {pl.provider: pl for pl in done.provider_logs}
    assert logs["place_src"].errors == 2
    assert logs["place_src"].status == "error"
    assert "quota exhausted" in (logs["place_src"].last_error or "")
    assert logs["fake"].status == "ok"


# --- adversarial-review hardening (F1/F2/F4/F6/F7) ---------------------------- #


def test_row_key_same_name_different_signals_are_distinct():
    """F1: a bare-name key collapsed every 'Starbucks' branch to one record.
    The composite key keeps same-NAME rows apart when an identity signal
    (address/city/phone) differs, and dedupes only true duplicates."""
    seen: set[str] = set()
    rows = [
        {"name": "Starbucks", "address": "14642 Newport Ave", "city": "Tustin"},
        {"name": "Starbucks", "address": "1125 E 17th St", "city": "Santa Ana"},
        {"name": "STARBUCKS", "address": "14642 Newport Ave.", "city": "Tustin"},
    ]
    kept = web_ingest_cap._dedupe_rows(rows, "name", seen)
    # Two branches kept; the shouty re-listing of the first branch deduped.
    assert len(kept) == 2
    assert {r["city"] for r in kept} == {"Tustin", "Santa Ana"}

    # No identity signal at all → name-only key still dedupes exact re-finds…
    seen2: set[str] = set()
    bare = [{"name": "Dr. Alina Reyes"}, {"name": "dr alina reyes"}]
    assert len(web_ingest_cap._dedupe_rows(bare, "name", seen2)) == 1
    # …and a row with no key value is always kept (nothing to match on).
    assert web_ingest_cap._dedupe_rows([{"x": 1}], "name", set()) == [{"x": 1}]


async def test_url_mode_never_fans_out():
    """F6: URL-targeted extraction reads FIXED pages — partitioned sub-queries
    would just re-scrape (and re-bill) the same URLs. plan() drops the spec's
    partition in URL mode."""
    from tests.test_web_ingest_urls import UrlProvider

    register_web_source(UrlProvider())
    spec = dict(FAN_SPEC)  # spec LLM proposed a partition anyway
    ctx = _ctx()
    ctx.urls = ["https://directory.example/all"]
    steps = await WebIngestCapability().plan(
        ctx, "pull all physicians from this page", parsed=spec
    )
    step = steps[0]
    assert step.action == "discover_ingest"
    assert step.params["subqueries"] == []
    assert step.params["urls"] == ["https://directory.example/all"]


async def test_lean_plan_carries_server_owned_auto_confirm_flag():
    """F7: the ≤gate decision is SERVER-owned — the lean plan says
    auto_confirm=true so clients obey it instead of re-deriving the gate from a
    twin constant that can drift. Above-gate (rich) plans carry no flag."""
    register_web_source(FakeProvider(is_paid=True, cost_per_call=0.03))
    steps = await WebIngestCapability().plan(
        _ctx(), "models OpenRouter offers", parsed=CONFIRMED_SPEC
    )
    assert steps[0].cost.get("auto_confirm") is True


async def test_rich_plan_has_no_auto_confirm_flag(monkeypatch):
    register_web_source(FakeProvider(**RICH))
    _patch_preview(monkeypatch, entities=_single_type_entities())
    steps = await WebIngestCapability().plan(
        _ctx(), "models OpenRouter offers", parsed=CONFIRMED_SPEC
    )
    assert steps[0].action == "discover_ingest"
    assert steps[0].cost.get("auto_confirm") is None


async def test_never_consulted_ensemble_member_is_skipped(monkeypatch):
    """F4: an ensemble member never reached (cap filled before its turn) rolls
    up as status='skipped' — the ProviderLog contract's 'named but never
    consulted' — not 'no_match' (which claims it ran and found nothing)."""
    from cograph_client.enrichment.job_store import InMemoryJobStore
    from cograph_client.enrichment.models import JobStatus
    from cograph_client.agent.registry import PlanStep

    place = KindFakeProvider(name="place_src", rows=TUSTIN_ROWS)
    general = FakeProvider(rows=SANTA_ANA_ROWS)
    register_web_source(general)
    register_web_source(place)

    async def fake_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None, **_kw):
        rows = json.loads(content)
        return IngestResult(entities_extracted=len(rows), entities_resolved=len(rows))

    monkeypatch.setattr(SchemaResolver, "ingest", fake_ingest)
    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    # Hand-built step: cap=3 → the specialized provider alone fills it and the
    # general member is never consulted.
    step = PlanStep(
        capability="web_ingest",
        action="discover_ingest",
        params={
            "query": "physicians in Tustin",
            "subqueries": [],
            "proposed_type": "Physician",
            "attributes": ["name", "specialty", "city"],
            "hint_columns": ["name", "specialty", "city", "phone"],
            "max_rows": 3,
            "kg_name": "models",
            "provider": "place_src",
            "providers": ["place_src", "fake"],
            "urls": [],
        },
        rationale="test",
        confidence=1.0,
    )
    store = InMemoryJobStore()
    cap_obj = WebIngestCapability()
    ack = await cap_obj.execute(_ctx_with_store(store), step)
    await spawned["task"]

    done = await store.get(ack["job_id"])
    assert done.status == JobStatus.applied
    assert done.result_count == 3
    logs = {pl.provider: pl for pl in done.provider_logs}
    assert logs["place_src"].status == "ok"
    assert logs["fake"].status == "skipped"
    assert logs["fake"].attempts == 0


# ---------------------------------------------------------------------------
# ONTA-239 — honor user-specified fields + converge attribute names
# ---------------------------------------------------------------------------


def test_explicit_user_fields_parses_a_use_these_chip():
    """The server-generated ``Use these: …`` confirmation chip is harvested verbatim
    (snake_cased), so a confirmed field list is an authoritative floor even if the
    LLM spec resolver later drops or renames some of them."""
    got = web_ingest_cap._explicit_user_fields(
        "Use these: name, latency_ttfb_ms, word_error_rate, mos_score"
    )
    assert got == ["name", "latency_ttfb_ms", "word_error_rate", "mos_score"]


def test_explicit_user_fields_parses_natural_list_and_stops_at_prose():
    """A natural 'with fields a, b, c and d' list is harvested; a trailing prose
    clause (a long >4-word run) is NOT swept in as a giant fake field."""
    got = web_ingest_cap._explicit_user_fields(
        "find TTS providers with fields latency, word error rate, "
        "supports spanish, streaming and this is a long trailing note we ignore"
    )
    # 'and' → comma; multi-word tokens snake_cased; the trailing >4-word prose run
    # is rejected (not a field-shaped token) and ends the harvest.
    assert got == [
        "latency",
        "word_error_rate",
        "supports_spanish",
        "streaming",
    ]


def test_explicit_user_fields_empty_for_entity_only_ask():
    """No explicit list marker → no floor (entity-only ask keeps today's behavior:
    the clarify path proposes attributes)."""
    assert web_ingest_cap._explicit_user_fields(
        "I'm looking for a list of OpenRouter models"
    ) == []


def test_explicit_user_fields_does_not_false_positive_on_entity_phrases():
    """Conservative-by-design: bare verbs ("collect"/"include") are NOT list
    markers, so an entity phrase is never mistaken for a field list and cannot
    pollute the attribute floor."""
    assert web_ingest_cap._explicit_user_fields(
        "collect the primary care physicians in Tustin"
    ) == []
    assert web_ingest_cap._explicit_user_fields(
        "ingest models and include their pricing"
    ) == []
    assert web_ingest_cap._explicit_user_fields(
        "add all the coffee shops in San Francisco"
    ) == []


def test_snap_to_declared_converges_on_existing_names():
    """A confirmed name that matches an existing DECLARED attribute (case-insensitive)
    snaps to the declared spelling; a genuinely new one passes through unchanged —
    mirroring enrichment's _validate_enrich_request so the two rails converge."""
    out = web_ingest_cap._snap_to_declared(
        ["Realtime_Audio_Duration_Per_Minute", "brand_new_field"],
        ["realtime_audio_duration_per_minute", "name"],
    )
    assert out == ["realtime_audio_duration_per_minute", "brand_new_field"]


async def test_user_field_floor_survives_llm_dropping_fields(monkeypatch):
    """RCA core (ONTA-239 Cluster 2a): even when the LLM spec resolver returns a
    SHRUNKEN confirmed set, every field the user explicitly listed survives into the
    plan's ``attributes``. The floor is parsed deterministically from the
    instruction, independent of the (non-deterministic) resolver."""
    provider = FakeProvider(**RICH)
    register_web_source(provider)
    _patch_preview(monkeypatch, entities=_single_type_entities())

    # The LLM DROPPED most of the user's fields (the observed failure), keeping a
    # generic subset — but the user's instruction enumerated all of them.
    lossy_spec = {
        "entity_type": "VoiceProvider",
        "key_attribute": "name",
        "query": "voice AI providers",
        "confirmed_attributes": ["pricing", "streaming"],  # LLM shrank the list
        "suggested_attributes": ["pricing", "streaming", "provider"],
    }
    instruction = (
        "Add voice AI providers. Use these: name, latency_ttfb_ms, "
        "word_error_rate, mos_score, barge_in_support, supports_spanish, "
        "hipaa_eligible, streaming"
    )
    steps = await WebIngestCapability().plan(
        _ctx(prior_clarify=1), instruction, parsed=lossy_spec
    )
    assert len(steps) == 1 and steps[0].action == "discover_ingest"
    attrs = steps[0].params["attributes"]
    # Every user-named field is present, none dropped.
    for f in [
        "name",
        "latency_ttfb_ms",
        "word_error_rate",
        "mos_score",
        "barge_in_support",
        "supports_spanish",
        "hipaa_eligible",
        "streaming",
    ]:
        assert f in attrs, f"user field {f!r} was dropped from the plan attributes"


async def test_discovery_snaps_confirmed_to_declared_type_attrs(monkeypatch):
    """RCA core (ONTA-239 Cluster 2b): web-ingest grounds the confirmed attribute
    names against the target type's ALREADY-DECLARED attributes, so it converges on
    the enrich rail's spelling instead of minting a divergent synonym."""
    provider = FakeProvider(**RICH)
    register_web_source(provider)
    _patch_preview(monkeypatch, entities=_single_type_entities())

    # The type already declares this attribute (as the enrich rail would have named
    # it). The user/LLM refer to the SAME concept with a different casing/spelling.
    async def fake_schema(neptune, tenant_id, type_name):
        return {
            "attributes": ["realtime_audio_duration_per_minute", "name"],
            "relationships": [],
        }

    monkeypatch.setattr(web_ingest_cap, "list_type_schema", fake_schema)

    spec = {
        "entity_type": "VoiceProvider",
        "key_attribute": "name",
        "query": "voice AI providers",
        "confirmed_attributes": ["Realtime_Audio_Duration_Per_Minute"],
        "suggested_attributes": ["Realtime_Audio_Duration_Per_Minute"],
    }
    steps = await WebIngestCapability().plan(
        _ctx(prior_clarify=1), "voice AI providers", parsed=spec
    )
    assert steps[0].action == "discover_ingest"
    # Snapped to the declared spelling, not the user's divergent casing.
    assert "realtime_audio_duration_per_minute" in steps[0].params["attributes"]
    assert "Realtime_Audio_Duration_Per_Minute" not in steps[0].params["attributes"]


async def test_type_schema_read_failure_degrades_gracefully(monkeypatch):
    """A failing ontology read must NOT 500 the plan — grounding is best-effort, so
    the plan still builds with the confirmed names verbatim."""
    provider = FakeProvider(**RICH)
    register_web_source(provider)
    _patch_preview(monkeypatch, entities=_single_type_entities())

    async def boom(neptune, tenant_id, type_name):
        raise RuntimeError("neptune down")

    monkeypatch.setattr(web_ingest_cap, "list_type_schema", boom)

    steps = await WebIngestCapability().plan(
        _ctx(prior_clarify=1), "OpenRouter models", parsed=CONFIRMED_SPEC
    )
    assert steps[0].action == "discover_ingest"
    assert steps[0].params["attributes"] == ["name", "context_length"]


# ---------------------------------------------------------------------------
# ONTA-382 — exhaustive attribute set is a CEILING (allowlist extraction)
# ---------------------------------------------------------------------------


async def test_explicit_user_fields_plan_is_exhaustive_and_closed(monkeypatch):
    """User enumerated a closed field list → plan marks attributes_exhaustive and
    the committed attributes equal the user set (+ key) — LLM may not extend."""
    provider = FakeProvider(**RICH)
    register_web_source(provider)
    _patch_preview(monkeypatch, entities=_single_type_entities())

    # LLM tries to pad with many extras (the ~49-attr symptom).
    padded_spec = {
        "entity_type": "CoffeeShop",
        "key_attribute": "name",
        "query": "coffee shops",
        "confirmed_attributes": [
            "name",
            "website",
            "type",
            "phone",
            "rating",
            "price_level",
            "hours",
            "latitude",
            "longitude",
            "review_count",
        ],
        "suggested_attributes": [
            "phone",
            "rating",
            "price_level",
            "hours",
            "amenities",
        ],
    }
    instruction = (
        "Add coffee shops. Use these: name, website, type"
    )
    steps = await WebIngestCapability().plan(
        _ctx(prior_clarify=1), instruction, parsed=padded_spec
    )
    assert steps[0].action == "discover_ingest"
    params = steps[0].params
    assert params["attributes_exhaustive"] is True
    # Ceiling: only user-named fields (+ key already in the list).
    assert set(params["attributes"]) == {"name", "website", "type"}
    # Fetch hint may still be comprehensive (ceiling is at extraction, not fetch).
    assert "rating" in params["hint_columns"] or "phone" in params["hint_columns"]


async def test_entity_only_plan_is_illustrative_open_mode(monkeypatch):
    """No explicit field list → attributes_exhaustive=False (open/illustrative);
    LLM confirmed/suggested may extend. Open-mode regression for ONTA-382."""
    provider = FakeProvider(**RICH)
    register_web_source(provider)
    _patch_preview(monkeypatch, entities=_single_type_entities())

    steps = await WebIngestCapability().plan(
        _ctx(prior_clarify=1),
        "I'm looking for a list of OpenRouter models",
        parsed=CONFIRMED_SPEC,
    )
    assert steps[0].action == "discover_ingest"
    params = steps[0].params
    assert params.get("attributes_exhaustive") is False
    # Illustrative: LLM confirmed fields land on the plan.
    assert "context_length" in params["attributes"]
    assert "name" in params["attributes"]


async def test_exhaustive_ceiling_survives_llm_extension_attempt(monkeypatch):
    """Acceptance: exhaustive {name, website, type} → plan attrs ⊆ requested
    (± key). LLM-only extras never enter the committed set."""
    provider = FakeProvider(**RICH)
    register_web_source(provider)
    _patch_preview(monkeypatch, entities=_single_type_entities())

    lossy_but_padded = {
        "entity_type": "Biz",
        "key_attribute": "name",
        "query": "local businesses",
        "confirmed_attributes": ["name", "website", "type", "fax", "twitter"],
        "suggested_attributes": ["fax", "twitter", "employees"],
    }
    steps = await WebIngestCapability().plan(
        _ctx(prior_clarify=1),
        # Comma-only list (no trailing "and X") so the field parser harvests every
        # named field; the exhaustive ceiling then closes the set to exactly these.
        "find businesses with fields name, website, type",
        parsed=lossy_but_padded,
    )
    attrs = set(steps[0].params["attributes"])
    requested = {"name", "website", "type"}
    assert steps[0].params["attributes_exhaustive"] is True
    assert attrs <= requested | {"name"}  # key always allowed
    assert attrs == requested
    assert "fax" not in attrs and "twitter" not in attrs


# ---------------------------------------------------------------------------
# ONTA-238 — discovery job progress observability + verifiable completion
# ---------------------------------------------------------------------------


async def test_running_job_shows_early_total_and_phase(monkeypatch):
    """A discovery job seeds a non-zero ``total`` and a ``phase`` the moment it goes
    running — BEFORE the first batch completes — so an early poll reads ~N/0 with a
    'searching'/'ingesting' phase instead of a flat, stalled-looking 0/0. The
    completion path settles the total to the exact count and phase 'done'."""
    from cograph_client.enrichment.job_store import InMemoryJobStore
    from cograph_client.enrichment.models import JobStatus

    provider = FakeProvider(is_paid=True, cost_per_call=0.09)
    register_web_source(provider)
    _patch_preview(monkeypatch, entities=_single_type_entities())

    gate = asyncio.Event()
    seen: dict = {}

    async def gated_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None, **_kw):
        # Block the FIRST ingest so we can observe the mid-run job state: total is
        # already seeded and the phase has advanced to 'ingesting'.
        await gate.wait()
        rows = json.loads(content)
        return IngestResult(entities_extracted=len(rows), entities_resolved=len(rows))

    monkeypatch.setattr(SchemaResolver, "ingest", gated_ingest)

    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    store = InMemoryJobStore()
    cap = WebIngestCapability()
    step = (
        await cap.plan(_ctx_with_store(store), "list of OpenRouter models", parsed=CONFIRMED_SPEC)
    )[0]
    ack = await cap.execute(_ctx_with_store(store), step)
    job_id = ack["job_id"]

    # Let the run start and reach the gated ingest.
    for _ in range(50):
        await asyncio.sleep(0)
        mid = await store.get(job_id)
        if mid.status == JobStatus.running and mid.progress.total > 0:
            break
    seen["mid"] = await store.get(job_id)
    # Early total is seeded (the plan cap) — NOT 0 — while still running.
    assert seen["mid"].status == JobStatus.running
    assert seen["mid"].progress.total > 0
    # The phase reports what is happening (searching or ingesting), never blank.
    assert seen["mid"].progress.phase in {"searching", "ingesting"}

    # Release the ingest and let the run finish.
    gate.set()
    await spawned["task"]

    done = await store.get(job_id)
    assert done.status == JobStatus.applied
    # Terminal settle: exact count + phase 'done'.
    assert done.progress.total == len(FULL_ROWS)
    assert done.progress.processed == len(FULL_ROWS)
    assert done.progress.phase == "done"


async def test_empty_completed_job_is_distinguishable_from_running(monkeypatch):
    """A discovery run that found NOTHING lands terminal (applied) with phase 'done'
    and result_count 0 — distinguishable from a still-running job, closing the
    'completed-empty looks like running' gap in the RCA."""
    from cograph_client.enrichment.job_store import InMemoryJobStore
    from cograph_client.enrichment.models import JobStatus

    provider = FakeProvider(is_paid=True, cost_per_call=0.09, rows=[])  # nothing found
    register_web_source(provider)
    _patch_preview(monkeypatch, entities=_single_type_entities())

    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    store = InMemoryJobStore()
    cap = WebIngestCapability()
    step = (
        await cap.plan(_ctx_with_store(store), "list of OpenRouter models", parsed=CONFIRMED_SPEC)
    )[0]
    ack = await cap.execute(_ctx_with_store(store), step)
    await spawned["task"]

    done = await store.get(ack["job_id"])
    assert done.status == JobStatus.applied  # terminal, not running
    assert done.result_count == 0
    assert done.progress.phase == "done"
    # The rolling total is settled to the exact (zero) count — NOT left at the
    # early ``cap`` seed — so a complete-and-empty job reads 0/0, not a misleading
    # 0/200 that looks unfinished to a progress-ratio consumer.
    assert done.progress.total == 0
    assert done.progress.processed == 0


async def test_failed_job_marks_phase_failed(monkeypatch):
    """A failed discovery run stamps phase 'failed' so a phase-keyed client can
    retire its spinner on the terminal signal."""
    from cograph_client.enrichment.job_store import InMemoryJobStore
    from cograph_client.enrichment.models import JobStatus

    provider = FakeProvider(is_paid=True, cost_per_call=0.09)
    register_web_source(provider)
    _patch_preview(monkeypatch, entities=_single_type_entities())

    async def boom_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None, **_kw):
        raise RuntimeError("ingest exploded")

    monkeypatch.setattr(SchemaResolver, "ingest", boom_ingest)

    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    store = InMemoryJobStore()
    cap = WebIngestCapability()
    step = (
        await cap.plan(_ctx_with_store(store), "list of OpenRouter models", parsed=CONFIRMED_SPEC)
    )[0]
    ack = await cap.execute(_ctx_with_store(store), step)
    await spawned["task"]

    done = await store.get(ack["job_id"])
    assert done.status == JobStatus.failed
    assert done.progress.phase == "failed"


# --- in-session progress observability (ONTA-243) ---------------------------- #
#
# These assert on the MECHANISM — incremental per-record ``processed``/``filled``
# flushing while a discovery job is still ``running``, and a distinct terminal
# state — using an INVENTED domain (``Widget`` with a ``color`` attribute) so
# nothing overfits to any persona's fields. The stub provider returns 6 synthetic
# rows and the stub ingest is gated on an ``asyncio.Event`` the test controls, so
# the run can be paused mid-flight and the live job state inspected.


# Six synthetic Widget rows — a made-up domain, deliberately NOT any persona's.
WIDGET_ROWS = [
    {"name": f"widget-{i}", "color": c}
    for i, c in enumerate(["red", "green", "blue", "cyan", "magenta", "yellow"])
]
WIDGET_SPEC = {
    "entity_type": "Widget",
    "key_attribute": "name",
    "query": "Widgets",
    "confirmed_attributes": ["color"],
    "suggested_attributes": ["color"],
}


def _widget_entities(rows):
    return [
        ExtractedEntity(
            type_name="Widget",
            id=r["name"],
            attributes=[ExtractedAttribute(name="color", value=r["color"])],
        )
        for r in rows
    ]


async def test_progress_moves_while_running(monkeypatch):
    """RC1 — ``processed`` AND ``filled`` both move mid-run, WHILE the job is still
    ``running``. The bug: ``filled`` was written ONLY at _finish_job, so a poller
    saw running/processed:0/filled:0 the entire session even as the job worked.
    We force a fine sub-batch, gate the stub ingest so the run pauses after the
    first sub-batch has landed, and assert the LIVE job already shows non-zero
    ``processed`` and ``filled`` before any terminal state."""
    from cograph_client.enrichment.job_store import InMemoryJobStore
    from cograph_client.enrichment.models import JobStatus

    # One record per sub-batch → maximal streaming granularity, domain-agnostic.
    monkeypatch.setattr(web_ingest_cap, "_DISCOVERY_INGEST_SUBBATCH", 1)

    register_web_source(FakeProvider(rows=WIDGET_ROWS))
    _patch_preview(monkeypatch, entities=_widget_entities(WIDGET_ROWS))

    # Coordination: the FIRST sub-batch's ingest runs to completion (so the run
    # flushes non-zero progress), then the SECOND blocks on ``release`` — at which
    # point the first has already committed but the run is still going, giving a
    # deterministic mid-run snapshot. ``second_reached`` tells the test we're there.
    calls = {"n": 0}
    release = asyncio.Event()
    second_reached = asyncio.Event()

    async def gated_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None, **_kw):
        rows = json.loads(content)
        calls["n"] += 1
        if calls["n"] == 2:
            # First sub-batch has committed + flushed; pause here mid-run.
            second_reached.set()
            await release.wait()
        return IngestResult(entities_extracted=len(rows), entities_resolved=len(rows))

    monkeypatch.setattr(SchemaResolver, "ingest", gated_ingest)

    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    store = InMemoryJobStore()
    cap = WebIngestCapability()
    step = (
        await cap.plan(_ctx_with_store(store), "a list of Widgets", parsed=WIDGET_SPEC)
    )[0]
    ack = await cap.execute(_ctx_with_store(store), step)
    job_id = ack["job_id"]

    # Run is now paused entering the SECOND sub-batch — the first has landed.
    await asyncio.wait_for(second_reached.wait(), timeout=3)

    mid = await store.get(job_id)
    # The job is STILL running — not yet terminal.
    assert mid.status == JobStatus.running
    # …yet BOTH headline counters have already moved off zero (the regression:
    # ``filled`` used to be written only at _finish_job, so it read 0 mid-run).
    assert mid.progress.processed > 0
    assert mid.progress.filled > 0

    # Release the run and let it finish cleanly.
    release.set()
    await asyncio.wait_for(spawned["task"], timeout=3)


async def test_terminal_state_has_final_counts(monkeypatch):
    """RC1 — at completion the job is a DISTINCT terminal state with honest final
    counts: applied, ``filled`` == every row, ``processed`` == ``total``,
    ``result_count`` == the rows, ``completed_at`` set, phase 'done'."""
    from cograph_client.enrichment.job_store import InMemoryJobStore
    from cograph_client.enrichment.models import JobStatus

    monkeypatch.setattr(web_ingest_cap, "_DISCOVERY_INGEST_SUBBATCH", 2)

    register_web_source(FakeProvider(rows=WIDGET_ROWS))
    _patch_preview(monkeypatch, entities=_widget_entities(WIDGET_ROWS))

    async def fake_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None, **_kw):
        rows = json.loads(content)
        return IngestResult(entities_extracted=len(rows), entities_resolved=len(rows))

    monkeypatch.setattr(SchemaResolver, "ingest", fake_ingest)
    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    store = InMemoryJobStore()
    cap = WebIngestCapability()
    step = (
        await cap.plan(_ctx_with_store(store), "a list of Widgets", parsed=WIDGET_SPEC)
    )[0]
    ack = await cap.execute(_ctx_with_store(store), step)
    await asyncio.wait_for(spawned["task"], timeout=3)

    done = await store.get(ack["job_id"])
    assert done.status == JobStatus.applied
    assert done.progress.filled == len(WIDGET_ROWS)
    assert done.progress.processed == done.progress.total == len(WIDGET_ROWS)
    assert done.result_count == len(WIDGET_ROWS)
    assert done.completed_at is not None
    assert done.progress.phase == "done"


async def test_terminal_state_failed_on_provider_error(monkeypatch):
    """RC1 negative — a discovery whose PROVIDER raises reaches a distinct FAILED
    terminal state (never a silent success, never stuck running), with an error
    and completed_at set."""
    from cograph_client.enrichment.job_store import InMemoryJobStore
    from cograph_client.enrichment.models import JobStatus

    class BoomProvider(FakeProvider):
        async def discover(self, *a, **k):
            raise RuntimeError("provider unreachable")

    register_web_source(BoomProvider(rows=WIDGET_ROWS))
    _patch_preview(monkeypatch, entities=_widget_entities(WIDGET_ROWS))

    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    store = InMemoryJobStore()
    cap = WebIngestCapability()
    step = (
        await cap.plan(_ctx_with_store(store), "a list of Widgets", parsed=WIDGET_SPEC)
    )[0]
    ack = await cap.execute(_ctx_with_store(store), step)
    await asyncio.wait_for(spawned["task"], timeout=3)

    failed = await store.get(ack["job_id"])
    assert failed.status == JobStatus.failed
    assert failed.completed_at is not None
    assert "provider unreachable" in (failed.error or "")


async def test_category_filter_finds_discovery_not_enrichment(monkeypatch):
    """RC2 (backend) — a discovery job is filed under category=discovery, so a
    ``discovery`` filter finds it and an ``enrichment`` filter does NOT. This is
    the honest scoping the persona's ``category:'enrichment'`` guess broke on."""
    from cograph_client.enrichment.job_store import InMemoryJobStore
    from cograph_client.enrichment.models import JobCategory

    register_web_source(FakeProvider(rows=WIDGET_ROWS))
    _patch_preview(monkeypatch, entities=_widget_entities(WIDGET_ROWS))

    async def fake_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None, **_kw):
        rows = json.loads(content)
        return IngestResult(entities_extracted=len(rows), entities_resolved=len(rows))

    monkeypatch.setattr(SchemaResolver, "ingest", fake_ingest)
    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    store = InMemoryJobStore()
    cap = WebIngestCapability()
    step = (
        await cap.plan(_ctx_with_store(store), "a list of Widgets", parsed=WIDGET_SPEC)
    )[0]
    ack = await cap.execute(_ctx_with_store(store), step)
    await asyncio.wait_for(spawned["task"], timeout=3)

    # The unified jobs listing (what GET /jobs reads) filters by category the same
    # way the route does (summaries filtered on ``category``).
    summaries = await store.list_for_tenant("demo-tenant")
    discovery = [s for s in summaries if s.category == JobCategory.discovery]
    enrichment = [s for s in summaries if s.category == JobCategory.enrichment]
    assert any(s.id == ack["job_id"] for s in discovery)
    assert not any(s.id == ack["job_id"] for s in enrichment)


# --- structured-intent fidelity (ONTA-244) ---------------------------------- #
#
# All on INVENTED types/fields (Widget/Sprocket/Gadget, sku/color/weight_kg/…) so
# nothing overfits to a persona's real schema. They assert on MECHANISM: an
# explicitly-listed schema survives to the plan with no clarify, a named type is
# never downgraded to WebRecord, and the meta-framing never leaks into the query.


# A spec as a DEGRADED spec LLM would return it — the entity_type collapsed to the
# generic WebRecord placeholder and confirmed_attributes empty — even though the
# user's message clearly named a Widget type + four fields. This is the exact
# failure mode ONTA-244 fixes: the deterministic floors must recover both.
DEGRADED_SPEC = {
    "entity_type": "WebRecord",
    "key_attribute": "name",
    "query": "Widgets",
    "confirmed_attributes": [],
    "suggested_attributes": ["description"],
}


async def test_explicit_attributes_survive_with_no_clarify(monkeypatch):
    """Fidelity — when the message enumerates fields, they survive to the plan's
    ``attributes`` and the turn commits to a ``discover_ingest`` plan with NO
    clarify, even on the FIRST turn (prior_clarify_count == 0)."""
    register_web_source(FakeProvider(rows=WIDGET_ROWS))
    _patch_preview(monkeypatch, entities=_widget_entities(WIDGET_ROWS))

    steps = await WebIngestCapability().plan(
        _ctx(),
        "Add Widget records with sku, color, weight_kg, warranty_months",
        parsed=DEGRADED_SPEC,
    )
    assert len(steps) == 1
    step = steps[0]
    assert step.action == "discover_ingest"  # committed, not a clarify
    assert step.action != "clarify"
    attrs = set(step.params["attributes"])
    assert {"sku", "color", "weight_kg", "warranty_months"} <= attrs


async def test_named_type_not_downgraded_to_webrecord(monkeypatch):
    """No downgrade — the plan commits to the user's named type (Widget), NOT the
    WebRecord placeholder the degraded spec returned."""
    register_web_source(FakeProvider(rows=WIDGET_ROWS))
    _patch_preview(monkeypatch, entities=_widget_entities(WIDGET_ROWS))

    steps = await WebIngestCapability().plan(
        _ctx(),
        "Add Widget records with sku, color, weight_kg, warranty_months",
        parsed=DEGRADED_SPEC,
    )
    step = steps[0]
    assert step.params["proposed_type"] == "Widget"
    assert step.params["proposed_type"] != "WebRecord"


async def test_entity_only_still_clarifies(monkeypatch):
    """The picker still fires for a genuinely under-specified ask — a bare entity
    with NO fields and a brand-new type. This is the case the clarify EXISTS for;
    ONTA-244 narrows it, it does not remove it."""
    register_web_source(FakeProvider(rows=WIDGET_ROWS))

    entity_only = {
        "entity_type": "Sprocket",
        "key_attribute": "name",
        "query": "Sprockets",
        "confirmed_attributes": [],
        "core_attributes": ["size"],
        "suggested_attributes": ["size", "material"],
    }
    steps = await WebIngestCapability().plan(
        _ctx(), "a list of Sprockets", parsed=entity_only
    )
    assert len(steps) == 1
    assert steps[0].action == "clarify"


async def test_query_excludes_meta_framing_sentence(monkeypatch):
    """The executed query is the clean SUBJECT, never the user's routing
    meta-correction — even when the spec LLM returns no clean query and the
    capability falls back to cleaning the raw instruction."""
    register_web_source(FakeProvider(rows=WIDGET_ROWS))
    _patch_preview(monkeypatch, entities=_widget_entities(WIDGET_ROWS))

    # Spec with an EMPTY query forces the _clean_query fallback on the raw sentence.
    spec = {
        "entity_type": "Gadget",
        "key_attribute": "name",
        "query": "",
        "confirmed_attributes": ["voltage"],
        "suggested_attributes": ["voltage"],
    }
    steps = await WebIngestCapability().plan(
        _ctx(),
        "This is a new discovery task, not enrichment - find Gadgets in Zone 3",
        parsed=spec,
    )
    step = steps[0]
    assert step.action == "discover_ingest"
    q = step.params["query"].lower()
    assert "not enrichment" not in q
    assert "discovery task" not in q
    # The real subject survived the strip.
    assert "gadget" in q or "zone 3" in q


def test_explicit_user_type_recovers_named_type():
    """Unit — the deterministic type parser recovers a Capitalized named type from
    an unambiguous frame, and returns '' for a lowercased entity phrase (so a real
    LLM-resolved type is never overridden by a false positive)."""
    from cograph_client.agent.capabilities.web_ingest_cap import _explicit_user_type

    assert _explicit_user_type("Add Widget records with sku, color") == "Widget"
    assert _explicit_user_type("discover Sprocket entities in Region 7") == "Sprocket"
    assert _explicit_user_type("Gadget records with voltage") == "Gadget"
    # A two-word capitalized type.
    assert _explicit_user_type("pull SolarPanel rows") == "SolarPanel"
    # Lowercased entity phrase → no false positive (stays '' → keeps WebRecord).
    assert _explicit_user_type("collect the physicians in Tustin") == ""
    assert _explicit_user_type("a list of models") == ""


def test_clean_query_strips_meta_framing():
    """Unit — _clean_query drops a leading discover-vs-enrich self-label so the
    subject after it survives; a normal query is untouched."""
    from cograph_client.agent.capabilities.web_ingest_cap import _clean_query

    out = _clean_query(
        "This is a new discovery task, not enrichment - find Gadgets in Zone 3"
    )
    low = out.lower()
    assert "not enrichment" not in low and "discovery task" not in low
    assert "gadget" in low
    # A plain subject is returned essentially unchanged (filler aside).
    assert "widgets in region 7" in _clean_query("Widgets in Region 7").lower()


def test_explicit_user_fields_records_with_requires_enumeration():
    """Unit — the "<Type> records with …" frame harvests a field list ONLY when the
    tail is a real ENUMERATION (2+ comma/"and"-joined items). A lone trailing phrase
    after "records with" is a FILTER, not a field list, and must NOT be harvested —
    otherwise "records with high error rates" would mint a junk `high_error_rates`
    attribute (the precision guard for the widened marker)."""
    from cograph_client.agent.capabilities.web_ingest_cap import _explicit_user_fields

    # Real enumerations → harvested.
    assert _explicit_user_fields(
        "Add Widget records with sku, color, weight_kg, warranty_months"
    ) == ["sku", "color", "weight_kg", "warranty_months"]
    assert _explicit_user_fields("pull Gadget records with voltage and amperage") == [
        "voltage",
        "amperage",
    ]
    # Lone trailing phrases / filters → NOT harvested (no junk attribute).
    assert _explicit_user_fields("find records with high error rates") == []
    assert _explicit_user_fields("get entities with a rating above 4") == []
    assert _explicit_user_fields("pull records with names") == []
    # The strict "with fields …" marker still harvests even a single field.
    assert _explicit_user_fields("discover Widgets with fields sku") == ["sku"]


# ---------------------------------------------------------------------------
# ONTA-244 (part 2) — schema intake robust to real LLM output. The deployed
# failure (persona-eval priya-nayar 20260708T122741Z): a persona enumerated ~20
# per-record fields, several with inline "(…)" annotations, and the plan STILL
# came back type=WebRecord + attributes=[name, provider] — the deterministic
# floor parser truncated the list at the FIRST annotated field because the
# parenthetical made the token fail _FIELD_TOKEN and the harvest ``break``s on
# the first non-field token. These tests reproduce that class with INVENTED
# fields (no persona/domain tokens) and prove the deterministic parse recovers.
# ---------------------------------------------------------------------------


def test_explicit_user_fields_survives_inline_annotations():
    """RCA (the real deployed gap): an inline "(…)"/"[…]" annotation on a field —
    "kind (a/b/c)", "weight [kg]" — must NOT truncate the list. The deployed parser
    stopped at the first annotated field; here the whole enumerated list survives,
    annotations stripped, order + names preserved. Invented fields, no domain
    tokens."""
    from cograph_client.agent.capabilities.web_ingest_cap import _explicit_user_fields

    # A comma list where the 3rd field carries a slash-bearing parenthetical — the
    # exact shape that collapsed the deployed list to its two leading fields.
    got = _explicit_user_fields(
        "each record needs these attributes: sku, weight_kg, "
        "kind (physical/digital/bundle), is_active, region, price_cents (USD)"
    )
    assert got == [
        "sku",
        "weight_kg",
        "kind",
        "is_active",
        "region",
        "price_cents",
    ], "an inline annotation truncated the field list (the deployed bug)"

    # Bracketed unit annotations are stripped too.
    assert _explicit_user_fields(
        "fields: latency [ms], throughput [tokens/s], name"
    ) == ["latency", "throughput", "name"]

    # A genuine trailing PROSE clause (not a bracketed annotation) still ends the
    # run — the annotation stripper does not turn prose into a harvestable field.
    assert _explicit_user_fields(
        "fields: sku, region and grab the rest if you happen to find them"
    ) == ["sku", "region"]


def test_explicit_user_type_recovers_lowercase_each_record_noun():
    """RCA (type half): an "each <noun> record …" shape description names the
    per-record type even when the noun is lowercase — the capitalized-only frames
    missed it, so the deployed plan kept WebRecord. Recover + singularize +
    PascalCase, while rejecting the generic record nouns so no junk type is
    minted."""
    from cograph_client.agent.capabilities.web_ingest_cap import _explicit_user_type

    assert _explicit_user_type("each product record needs sku, price") == "Product"
    assert _explicit_user_type("every company entity should have name") == "Company"
    # Plural noun is singularized.
    assert _explicit_user_type("each companies record has revenue") == "Company"
    # Generic record nouns / fillers → no type (stays WebRecord + clarifies).
    assert _explicit_user_type("each record should have a name") == ""
    assert _explicit_user_type("each data row needs a value") == ""
    # An ordinary entity phrase is still not mistaken for a type.
    assert _explicit_user_type("collect the coffee shops in San Francisco") == ""


async def test_annotated_multifield_request_yields_full_typed_plan(monkeypatch):
    """END-TO-END reproduction of the DEPLOYED failure class (persona-eval RCA),
    with INVENTED fields and a WRONG spec-LLM stub.

    The scenario the deployed backend fails: the caller enumerates a rich,
    inline-ANNOTATED field list AND names the record type via an "each <noun>
    record" shape sentence. The (real) spec LLM under-extracts — it returns the
    generic WebRecord placeholder and a SHRUNKEN [name, provider] attribute set
    (exactly the transcript). We inject that WRONG spec via ``parsed`` so the test
    drives the DETERMINISTIC recovery path, not a lucky LLM.

    Asserts the plan the deployed backend could NOT produce:
      * commits to ``discover_ingest`` with NO clarify,
      * ``proposed_type`` is the caller's named type (recovered from "each gadget
        record"), NOT the WebRecord placeholder,
      * ``attributes`` contains EVERY enumerated field — including the ones behind
        an inline "(…)"/"[…]" annotation that truncated the deployed list.

    On the deployed code this fails twice over: proposed_type == 'WebRecord' and
    attributes == ['name', 'provider'] (the LLM's shrunken set)."""
    register_web_source(FakeProvider(rows=WIDGET_ROWS))
    _patch_preview(monkeypatch, entities=_widget_entities(WIDGET_ROWS))

    # New type → the ontology read returns no declared attributes, so the ONLY
    # thing that can save the schema is the deterministic instruction parse.
    async def empty_schema(neptune, tenant_id, type_name):
        return {"attributes": [], "relationships": []}

    monkeypatch.setattr(web_ingest_cap, "list_type_schema", empty_schema)

    # The WRONG spec the real LLM returned in the transcript: placeholder type +
    # a collapsed 2-field set. Invented, domain-neutral fields throughout.
    wrong_spec = {
        "entity_type": "WebRecord",
        "key_attribute": "name",
        "query": "gadgets",
        "confirmed_attributes": ["name", "provider"],
        "suggested_attributes": ["name", "provider"],
    }
    instruction = (
        "Discover gadgets from the web. I need each gadget record to have these "
        "attributes: sku, weight_kg (float), is_active (true/false), region, "
        "price_cents (USD), warranty_months, color (hex), rating [0-5]"
    )

    steps = await WebIngestCapability().plan(
        _ctx(prior_clarify=1), instruction, parsed=wrong_spec
    )
    assert len(steps) == 1
    step = steps[0]
    # Committed — not stranded on the attribute-picker clarify.
    assert step.action == "discover_ingest", step.action
    # Type preserved, NOT downgraded to the WebRecord placeholder the spec returned.
    assert step.params["proposed_type"] == "Gadget"
    assert step.params["proposed_type"] != "WebRecord"
    # Every enumerated field survives — including those the deployed parser dropped
    # at the first inline annotation (weight_kg, is_active, price_cents, color,
    # rating were all behind a "(…)"/"[…]" annotation).
    attrs = set(step.params["attributes"])
    for f in [
        "sku",
        "weight_kg",
        "is_active",
        "region",
        "price_cents",
        "warranty_months",
        "color",
        "rating",
    ]:
        assert f in attrs, f"user field {f!r} dropped from plan attributes"
    # The LLM's shrunken set is NOT what the plan settled on.
    assert attrs != {"name", "provider"}


# ---------------------------------------------------------------------------
# _resolve_spec FALLBACK on a resolver-LLM FAILURE — the fix here. The
# persona-eval RCA: the spec LLM timed out (~15s), _resolve_spec fell to the bare
# [name, description, url] default, and a fully-specified ask (an enumerated field
# list + a named record type) silently collapsed to a name/description capture —
# the listed fields never landed. These stub the spec LLM to FAIL and assert the
# DETERMINISTIC fallback recovers the user's fields + type from the CURRENT
# request, surfaces genuine degradation, and leaves the happy path unchanged.
# INVENTED, domain-neutral tokens throughout: they assert the MECHANISM (any
# enumerated list, any named type), never a specific domain example.
# ---------------------------------------------------------------------------


async def _resolve_spec_llm_fails(monkeypatch, instruction: str) -> dict:
    """Drive ``_resolve_spec`` with the spec LLM stubbed to RAISE (the ~15s timeout /
    provider error the RCA hit), so the deterministic fallback branch runs. A key IS
    present, so the LLM is actually attempted and its FAILURE — not a missing key —
    is what triggers the fallback."""
    async def boom_chat(*_a, **_k):
        raise RuntimeError("spec LLM timed out")

    monkeypatch.setattr(web_ingest_cap, "openrouter_chat", boom_chat)
    ctx = AgentContext(
        tenant_id="demo-tenant", kg_name="kg", neptune=MagicMock(),
        anthropic_key="sk-ant-test", openrouter_key="k",
    )
    return await web_ingest_cap._resolve_spec(ctx, instruction)


async def test_resolve_spec_fallback_keeps_named_fields_on_llm_failure(monkeypatch):
    """The core fix — an explicit field list + a named type survive a spec-LLM
    failure: the fallback spec's suggested attributes carry EVERY named field (not
    the bare [name, description, url] default) and the entity type is the one derived
    from the request (not the WebRecord placeholder)."""
    spec = await _resolve_spec_llm_fails(
        monkeypatch,
        "discover Gadget entities with serial_number, weight_kg, "
        "warranty_months, supplier",
    )
    named = {"serial_number", "weight_kg", "warranty_months", "supplier"}
    # Every named field is in the FETCH set the caller projects to (not dropped).
    assert named <= set(spec["suggested_attributes"])
    # …and in the confirmed floor the LLM path may only EXTEND, never shrink.
    assert named <= set(spec["confirmed_attributes"])
    # NOT the bare generic default that silently thinned the deployed ask.
    assert spec["suggested_attributes"] != ["name", "description", "url"]
    # A real type derived from the request — not the placeholder.
    assert spec["entity_type"] != "WebRecord"
    assert spec["entity_type"] == "Gadget"
    # A field floor was recovered → NOT flagged degraded.
    assert spec.get("degraded") is not True


async def test_resolve_spec_fallback_mechanism_generalizes(monkeypatch):
    """Anti-overfit — the SAME recovery fires for a DIFFERENT marker form ("with
    fields …"), a DIFFERENT invented type, and DIFFERENT invented fields. Asserts the
    mechanism, not one phrasing."""
    spec = await _resolve_spec_llm_fails(
        monkeypatch,
        "Add Sprocket records with fields torque_nm, radius_mm, batch_code",
    )
    assert spec["entity_type"] == "Sprocket"
    assert {"torque_nm", "radius_mm", "batch_code"} <= set(
        spec["suggested_attributes"]
    )
    assert spec["suggested_attributes"] != ["name", "description", "url"]
    assert spec.get("degraded") is not True


async def test_resolve_spec_fallback_weights_current_request_over_stale(monkeypatch):
    """Point 3 — when the accumulated instruction stacks an OLD ask and a NEW one,
    the CURRENT (last) turn's type wins and its fields LEAD the recovered set; the
    stale earlier turn only fills the tail, it never overrides. Accumulated turns are
    newline-joined oldest-first (``_effective_instruction``)."""
    instruction = (
        "discover Alpha entities with old_field_a, old_field_b\n"
        "actually discover Beta records with new_field_x, new_field_y"
    )
    spec = await _resolve_spec_llm_fails(monkeypatch, instruction)
    # The current turn's type wins over the stale earlier one.
    assert spec["entity_type"] == "Beta"
    sugg = spec["suggested_attributes"]
    # The current turn's fields lead the ordering (weighted first)…
    assert sugg[0] == "new_field_x"
    assert {"new_field_x", "new_field_y"} <= set(sugg)
    # …and no explicitly-named field is lost (history fills the tail, never drops).
    assert {"old_field_a", "old_field_b"} <= set(sugg)


async def test_resolve_spec_fallback_surfaces_degraded_note_when_no_fields(monkeypatch):
    """Point 2 — when the LLM fails AND no field list can be parsed, the fallback
    still degrades to name/description but SURFACES it: the spec carries a truthy
    ``degraded`` flag and a non-empty, human-readable ``degraded_note`` so the
    thinning is visible, not silent."""
    spec = await _resolve_spec_llm_fails(
        monkeypatch, "find a list of interesting things from the web"
    )
    assert spec.get("degraded") is True
    note = spec.get("degraded_note") or ""
    assert note and isinstance(note, str)
    # The genuinely-degraded set is the honest name/description floor.
    assert "name" in spec["suggested_attributes"]
    assert "description" in spec["suggested_attributes"]


async def test_resolve_spec_happy_path_unchanged(monkeypatch):
    """Regression guard — when the LLM SUCCEEDS, its normalized spec flows through
    untouched: the fallback recovery never runs and no degraded flag is stamped.
    Same behavior as before the fix."""
    spec_json = json.dumps({
        "entity_type": "Model",
        "key_attribute": "name",
        "query": "OpenRouter models",
        "query_kind": None,
        "confirmed_attributes": ["provider"],
        "suggested_attributes": ["provider", "context_length"],
    })
    out = await _run_resolve_spec(monkeypatch, spec_json, "list of OpenRouter models")
    assert out["entity_type"] == "Model"
    assert out["suggested_attributes"] == ["provider", "context_length"]
    assert out["confirmed_attributes"] == ["provider"]
    # The happy path never stamps the degraded flag.
    assert "degraded" not in out


async def test_plan_surfaces_degraded_note_in_clarify(monkeypatch):
    """END-TO-END (point 2) — when the resolver LLM fails AND no fields can be parsed
    for a brand-new type, plan() still asks (clarify), but the clarify question now
    SURFACES the degraded-planning note so the user learns automated planning fell
    back rather than being silently handed a thin capture."""
    register_web_source(FakeProvider(rows=WIDGET_ROWS))

    async def boom_chat(*_a, **_k):
        raise RuntimeError("spec LLM down")

    async def empty_schema(neptune, tenant_id, type_name):
        return {"attributes": [], "relationships": []}

    monkeypatch.setattr(web_ingest_cap, "openrouter_chat", boom_chat)
    monkeypatch.setattr(web_ingest_cap, "list_type_schema", empty_schema)

    # A key present so the LLM is attempted (and fails); a bare entity ask with no
    # field list and a brand-new type → genuinely degraded → clarify with the note.
    ctx = AgentContext(
        tenant_id="demo-tenant", kg_name="kg", neptune=MagicMock(),
        anthropic_key="sk-ant-test", openrouter_key="k",
        extras={"prior_clarify_count": 0},
    )
    steps = await WebIngestCapability().plan(
        ctx, "find a list of interesting things from the web"
    )
    assert len(steps) == 1
    step = steps[0]
    assert step.action == "clarify"
    assert web_ingest_cap._DEGRADED_NOTE in step.params["question"]


async def test_resolve_spec_fallback_query_ignores_confirm_turn(monkeypatch):
    """Regression guard — on a multi-turn CONFIRM path the recovered search SUBJECT
    must stay the ORIGINAL ask, NEVER the confirmation / chip reply. Turns are joined
    oldest-first with newlines, so the current turn is a bare "yes" or a "Use these:
    …" chip — neither names a subject, so the query must fall back to the first-line
    ask instead of searching the web for the confirmation text (the reviewer-
    reproduced regression: without the type gate, ``query`` became the chip text)."""
    # turn1 = the real ask; turn2 = a bare confirmation (no subject / no type).
    spec = await _resolve_spec_llm_fails(
        monkeypatch, "find some physicians for me\nyes go ahead"
    )
    q = spec["query"].lower()
    assert "physician" in q  # the original subject survived
    assert "yes" not in q and "go ahead" not in q

    # turn2 = a "Use these: …" chip: its FIELDS are recovered, but the chip text is
    # NOT the search subject.
    spec2 = await _resolve_spec_llm_fails(
        monkeypatch,
        "find some physicians for me\nUse these: name, npi, taxonomy, affiliation",
    )
    q2 = spec2["query"].lower()
    assert "physician" in q2
    assert "use these" not in q2 and "npi" not in q2
    # The chip's fields still land as the recovered floor — that half keeps working.
    assert {"npi", "taxonomy", "affiliation"} <= set(spec2["suggested_attributes"])


async def test_resolve_spec_fallback_pivot_turn_still_drives_subject(monkeypatch):
    """The type gate must NOT over-correct: a genuine PIVOT turn (one that names a
    new type/subject) still drives the search subject, so the current request keeps
    winning over a stale earlier one when it actually is a new ask."""
    spec = await _resolve_spec_llm_fails(
        monkeypatch,
        "discover Alpha entities with old_field_a\n"
        "actually discover Beta records with new_field_x, new_field_y",
    )
    q = spec["query"].lower()
    # The pivot ("Beta") drives the subject, not the stale "Alpha" first line.
    assert "beta" in q
    assert "alpha" not in q
    assert spec["entity_type"] == "Beta"


async def test_plan_query_ignores_chip_confirm_turn(monkeypatch):
    """END-TO-END regression (reviewer reproduced at ``params['query']``) — a
    committed discovery job's ``params['query']`` stays the ORIGINAL subject, not the
    "Use these: …" chip text, when the spec LLM fails on the confirm turn."""
    register_web_source(FakeProvider(rows=WIDGET_ROWS))

    async def boom_chat(*_a, **_k):
        raise RuntimeError("spec LLM down")

    async def empty_schema(neptune, tenant_id, type_name):
        return {"attributes": [], "relationships": []}

    monkeypatch.setattr(web_ingest_cap, "openrouter_chat", boom_chat)
    monkeypatch.setattr(web_ingest_cap, "list_type_schema", empty_schema)

    # prior_clarify=1 → the turn commits (already asked once) instead of re-clarifying.
    ctx = AgentContext(
        tenant_id="demo-tenant", kg_name="kg", neptune=MagicMock(),
        anthropic_key="sk-ant-test", openrouter_key="k",
        extras={"prior_clarify_count": 1},
    )
    steps = await WebIngestCapability().plan(
        ctx,
        "find some physicians for me\nUse these: name, npi, taxonomy, affiliation",
    )
    assert len(steps) == 1
    step = steps[0]
    assert step.action == "discover_ingest"
    q = step.params["query"].lower()
    assert "physician" in q
    assert "use these" not in q and "npi" not in q
    # The chip fields still survive into the plan's attributes.
    assert {"npi", "taxonomy", "affiliation"} <= set(step.params["attributes"])

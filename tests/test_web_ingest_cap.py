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

    async def fake_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None):
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

    async def spy_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None):
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

    async def fake_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None):
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


async def test_execute_threads_per_record_source_url(monkeypatch):
    """Each discovered entity carries its own source_url drawn from the provider's
    provenance map, committed through the SAME ingest path (content_type="json")
    — and the plan preview already shows the citation column (preview == commit)."""
    provider = FakeProvider(provenance=True, **RICH)
    register_web_source(provider)
    _patch_preview(monkeypatch, entities=_single_type_entities())

    captured: dict = {}

    async def fake_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None):
        captured.update(content=content, content_type=content_type)
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
    # Preview == commit: the sampled rows already carry source_url.
    assert step.preview["sample_rows"]
    assert all("source_url" in r for r in step.preview["sample_rows"])

    await cap.execute(_ctx(), step)
    await spawned["task"]

    rows_back = json.loads(captured["content"])
    assert rows_back, "rows were committed"
    # Every committed record traces to its OWN page; source_url rides alongside
    # the confirmed attributes (an extra provenance column, not a replacement).
    for i, r in enumerate(rows_back):
        assert r["source_url"] == f"https://src.example/page-{i}"
        assert "name" in r and "context_length" in r


async def test_execute_no_provenance_adds_no_source_url(monkeypatch):
    """A provider that supplies no provenance (e.g. a free source) yields entities
    with NO source_url column — the threading degrades silently, never stamping a
    blank citation."""
    provider = FakeProvider()  # provenance=False
    register_web_source(provider)
    _patch_preview(monkeypatch, entities=_single_type_entities())

    captured: dict = {}

    async def fake_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None):
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

    async def fake_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None):
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

    async def fake_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None):
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

    async def fake_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None):
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

    async def fake_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None):
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

    async def fake_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None):
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

    async def fake_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None):
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

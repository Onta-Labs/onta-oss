"""Scalable registry discovery + selection (ONTA-341).

Covers the three-stage pipeline — structured pre-filter → semantic top-K rank →
deterministic arbitration (+ refute-only tiebreaker) — the decision cache, the
BYO-key / entitlement / geo guardrails, and the flag-gated enrichment wiring
(``apply_registry_selection``: identity when OFF, reshape when ON). All LLM /
embedding seams are injected so these run offline and deterministically.
"""

from __future__ import annotations

import pytest

from cograph_client.api_registry.catalog import (
    ApiSourceCatalog,
    reset_api_source_layers,
)
from cograph_client.api_registry.enrichment import (
    RegistrySourceAdapter,
    apply_registry_selection,
    register_registry_enrichment,
    reset_registry_enrichment,
)
from cograph_client.api_registry import registry_selection as rs
from cograph_client.api_registry.spec import (
    ApiSourceSpec,
    AuthMode,
    AuthSpec,
    AuthorityLevel,
    Coverage,
    EndpointSpec,
    Entitlement,
    ParamSpec,
)
from cograph_client.enrichment.sources.base import register_adapter
from cograph_client.enrichment.tiers import (
    reset_chain_prefix_providers,
    reset_tiers,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # Feature OFF unless a test opts in; clean registry + selector state each run.
    monkeypatch.delenv("COGRAPH_REGISTRY_SELECTION", raising=False)
    monkeypatch.delenv("COGRAPH_REGISTRY_SELECTION_TOP_K", raising=False)
    reset_api_source_layers()
    reset_tiers()
    reset_chain_prefix_providers()
    reset_registry_enrichment()
    rs.reset_source_success_rate_provider()
    rs.clear_selection_cache()
    yield
    reset_api_source_layers()
    reset_tiers()
    reset_chain_prefix_providers()
    reset_registry_enrichment()
    rs.reset_source_success_rate_provider()
    rs.clear_selection_cache()


# --------------------------------------------------------------------------- #
# Spec fixtures
# --------------------------------------------------------------------------- #
def _spec(
    slug: str,
    *,
    kinds: list[str],
    cols: dict[str, str],
    enrich_from: str = "entity_name",
    authority: AuthorityLevel = AuthorityLevel.authoritative,
    cost: float = 0.0,
    verified_at: str = "",
    geo: str = "",
    entitlement: Entitlement = Entitlement.free,
    auth: AuthSpec | None = None,
    example_asks: list[str] | None = None,
    description: str = "",
) -> ApiSourceSpec:
    """A minimal enrichment-ready entry: one endpoint with an ``enrich_from``
    param and the given field-mapping columns + coverage."""
    return ApiSourceSpec(
        slug=slug,
        title=slug,
        description=description or f"{slug} source",
        base_url="https://example.test",
        authority_level=authority,
        cost_per_call=cost,
        verified_at=verified_at,
        entitlement=entitlement,
        auth=auth or AuthSpec(),
        coverage=Coverage(
            entity_kinds=list(kinds),
            attributes=list(cols.keys()),
            geo=geo,
            example_asks=example_asks or [],
        ),
        endpoints=[
            EndpointSpec(
                name="search",
                path="/q",
                params=[ParamSpec(name="q", enrich_from=enrich_from)],
                result_path="results",
                field_mappings=dict(cols),
            )
        ],
    )


def _catalog(*specs: ApiSourceSpec) -> ApiSourceCatalog:
    return ApiSourceCatalog(entries={s.slug: s for s in specs})


def _need(**kw) -> rs.SelectionNeed:
    return rs.SelectionNeed(**kw)


# --------------------------------------------------------------------------- #
# Stage 1a — structured pre-filter
# --------------------------------------------------------------------------- #
def test_prefilter_keeps_only_attribute_and_type_matches():
    a = _spec("nppes", kinds=["physician"], cols={"npi": "number"})
    b = _spec("food", kinds=["food_item"], cols={"barcode": "code"})
    c = _spec("geo", kinds=["place"], cols={"latitude": "lat"})
    cat = _catalog(a, b, c)
    got = rs.structured_prefilter(_need(entity_type="Physician", attribute="npi"), cat.all())
    assert [s.slug for s in got] == ["nppes"]


def test_prefilter_drops_entry_without_enrich_params():
    # An entry that declares the column but has no enrich_from recipe cannot be
    # driven from an entity during enrichment → excluded.
    a = _spec("nppes", kinds=["physician"], cols={"npi": "number"}, enrich_from="")
    got = rs.structured_prefilter(_need(entity_type="Physician", attribute="npi"), [a])
    assert got == []


def test_prefilter_matches_adapter_self_gate_equivalence():
    # The pre-filter admits EXACTLY the entries whose adapter self-gate would not
    # immediately return [] — the equivalence that makes retrieve-top-K safe.
    a = _spec("nppes", kinds=["physician"], cols={"npi": "number"})
    for entity_type, attribute, expect in [
        ("Physician", "npi", True),          # declared + type match
        ("Physician", "favorite_color", False),  # attribute not declared
        ("Company", "npi", False),           # type mismatch
    ]:
        pref = rs.structured_prefilter(
            _need(entity_type=entity_type, attribute=attribute), [a]
        )
        admitted = bool(pref)
        adapter_ok = (
            a.slug in {s.slug for s in pref}
        )
        # Cross-check against the live adapter gate (no network — gate fails first).
        adapter = RegistrySourceAdapter(a)
        gated_out = (
            adapter._fillable_column(attribute) is None
            or not adapter._type_matches(entity_type)
        )
        assert admitted is expect
        assert adapter_ok is expect
        assert (not gated_out) is expect


def test_prefilter_byok_guardrail_excludes_dormant_env_key(monkeypatch):
    # An env-var-keyed entry whose key is unset is dormant → never ranked.
    monkeypatch.delenv("SECRET_SRC_KEY", raising=False)
    keyed = _spec(
        "keyed", kinds=["physician"], cols={"npi": "number"},
        auth=AuthSpec(mode=AuthMode.api_key_query, key_env="SECRET_SRC_KEY", query_key="k"),
    )
    need = _need(entity_type="Physician", attribute="npi")
    assert rs.structured_prefilter(need, [keyed]) == []
    # With the key present it becomes selectable again.
    monkeypatch.setenv("SECRET_SRC_KEY", "abc")
    assert [s.slug for s in rs.structured_prefilter(need, [keyed])] == ["keyed"]


def test_prefilter_entitlement_gate_when_free_only():
    free = _spec("free_src", kinds=["physician"], cols={"npi": "number"})
    paid = _spec(
        "paid_src", kinds=["physician"], cols={"npi": "number"},
        entitlement=Entitlement.paid, cost=0.01,
        auth=AuthSpec(mode=AuthMode.bearer, key_env="PAID_KEY"),
    )
    # allow_paid=False keeps only the free source; the paid one is filtered even
    # aside from dormancy.
    got = rs.structured_prefilter(
        _need(entity_type="Physician", attribute="npi", allow_paid=False), [free, paid]
    )
    assert [s.slug for s in got] == ["free_src"]


def test_prefilter_geo_gate_lenient_on_missing_coverage():
    us = _spec("us_src", kinds=["physician"], cols={"npi": "n"}, geo="United States")
    global_src = _spec("any_src", kinds=["physician"], cols={"npi": "n"}, geo="")
    need = _need(entity_type="Physician", attribute="npi", geo="Canada")
    got = {s.slug for s in rs.structured_prefilter(need, [us, global_src])}
    # us_src excluded (geo token mismatch); the geo-less source stays (global).
    assert got == {"any_src"}


# --------------------------------------------------------------------------- #
# Stage 2 — deterministic arbitration
# --------------------------------------------------------------------------- #
def test_arbitrate_orders_by_authority_then_cost_then_freshness():
    strong = _spec("strong", kinds=["x"], cols={"a": "a"}, authority=AuthorityLevel.source_of_truth)
    weak_cheap = _spec("weak_cheap", kinds=["x"], cols={"a": "a"}, authority=AuthorityLevel.authoritative, cost=0.0)
    weak_pricey = _spec("weak_pricey", kinds=["x"], cols={"a": "a"}, authority=AuthorityLevel.authoritative, cost=1.0)
    order = [s.slug for s in rs.arbitrate([weak_pricey, weak_cheap, strong])]
    # source_of_truth leads; among equal-authority, the cheaper wins.
    assert order == ["strong", "weak_cheap", "weak_pricey"]


def test_arbitrate_freshness_breaks_cost_tie():
    stale = _spec("stale", kinds=["x"], cols={"a": "a"}, verified_at="2020-01-01")
    fresh = _spec("fresh", kinds=["x"], cols={"a": "a"}, verified_at="2026-01-01")
    undated = _spec("undated", kinds=["x"], cols={"a": "a"}, verified_at="")
    order = [s.slug for s in rs.arbitrate([undated, stale, fresh])]
    assert order == ["fresh", "stale", "undated"]


def test_arbitrate_success_rate_breaks_remaining_tie():
    lo = _spec("lo", kinds=["x"], cols={"a": "a"})
    hi = _spec("hi", kinds=["x"], cols={"a": "a"})
    rs.register_source_success_rate_provider(lambda slug: {"hi": 0.9, "lo": 0.1}.get(slug, 0.5))
    order = [s.slug for s in rs.arbitrate([lo, hi])]
    assert order == ["hi", "lo"]


def test_arbitrate_collapses_to_authority_then_slug_when_signals_equal():
    # The load-bearing behavior-preservation property: with equal cost/freshness/
    # success (the OSS seed today), arbitration == (authority_rank, slug).
    b = _spec("bbb", kinds=["x"], cols={"a": "a"})
    a = _spec("aaa", kinds=["x"], cols={"a": "a"})
    order = [s.slug for s in rs.arbitrate([b, a])]
    assert order == ["aaa", "bbb"]


# --------------------------------------------------------------------------- #
# Refute-only LLM tiebreaker
# --------------------------------------------------------------------------- #
def _chat(reply: str):
    async def fn(system: str, user: str) -> str:
        return reply
    return fn


@pytest.mark.asyncio
async def test_refute_tiebreaker_demotes_refuted_leader():
    a = _spec("a", kinds=["x"], cols={"attr": "a"})  # sorts first by slug
    b = _spec("b", kinds=["x"], cols={"attr": "a"})
    cat = _catalog(a, b)
    # LLM says the leader (a) canNOT answer → it is demoted below b.
    slugs = await rs.select_registry_slugs(
        _need(entity_type="X", attribute="attr"), cat,
        chat_fn=_chat('{"can_answer": false, "reason": "no"}'),
    )
    assert slugs == ["b", "a"]


@pytest.mark.asyncio
async def test_refute_tiebreaker_keeps_order_when_leader_confirmed():
    a = _spec("a", kinds=["x"], cols={"attr": "a"})
    b = _spec("b", kinds=["x"], cols={"attr": "a"})
    cat = _catalog(a, b)
    slugs = await rs.select_registry_slugs(
        _need(entity_type="X", attribute="attr"), cat,
        chat_fn=_chat('{"can_answer": true}'),
    )
    assert slugs == ["a", "b"]


@pytest.mark.asyncio
async def test_refute_tiebreaker_skipped_when_authority_differs():
    # Distinct authority ⇒ the deterministic policy decides; the LLM (which would
    # refute the leader) is never consulted, so order is unchanged.
    strong = _spec("strong", kinds=["x"], cols={"attr": "a"}, authority=AuthorityLevel.source_of_truth)
    weak = _spec("weak", kinds=["x"], cols={"attr": "a"}, authority=AuthorityLevel.authoritative)
    cat = _catalog(weak, strong)
    called = {"n": 0}

    async def chat(system, user):
        called["n"] += 1
        return '{"can_answer": false}'

    slugs = await rs.select_registry_slugs(
        _need(entity_type="X", attribute="attr"), cat, chat_fn=chat,
    )
    assert slugs == ["strong", "weak"]
    assert called["n"] == 0


# --------------------------------------------------------------------------- #
# Semantic rank (top-K narrowing)
# --------------------------------------------------------------------------- #
def _positional_embed(counter: dict):
    """Fake embed_fn: query = items[0], candidates = items[1:] in order. Assigns
    strictly descending relevance to candidates so top-K keeps the FIRST k."""
    async def fn(items):
        counter["n"] += 1
        out = [[1.0, 0.0]]  # query vector
        for i in range(len(items) - 1):
            out.append([max(0.0, 1.0 - 0.25 * i), 0.25 * (i + 1)])
        return out
    return fn


@pytest.mark.asyncio
async def test_semantic_rank_narrows_to_top_k_then_arbitrates():
    # 4 eligible; top_k=2 → semantic keeps the two most-relevant (first two by the
    # positional embed), then arbitration orders those two by authority.
    s0 = _spec("s0", kinds=["x"], cols={"a": "a"}, authority=AuthorityLevel.authoritative)
    s1 = _spec("s1", kinds=["x"], cols={"a": "a"}, authority=AuthorityLevel.source_of_truth)
    s2 = _spec("s2", kinds=["x"], cols={"a": "a"})
    s3 = _spec("s3", kinds=["x"], cols={"a": "a"})
    cat = _catalog(s0, s1, s2, s3)
    counter = {"n": 0}
    slugs = await rs.select_registry_slugs(
        _need(entity_type="X", attribute="a"), cat,
        top_k=2, embed_fn=_positional_embed(counter),
    )
    assert counter["n"] == 1                 # embedding path was exercised
    assert set(slugs) == {"s0", "s1"}        # the two most-relevant survived
    assert slugs == ["s1", "s0"]             # source_of_truth arbitrated ahead
    assert "s3" not in slugs                 # least-relevant capped out


@pytest.mark.asyncio
async def test_semantic_rank_skipped_when_eligible_fits_top_k():
    # <= top_k eligible → no embedding round-trip (deterministic small-catalog path).
    s0 = _spec("s0", kinds=["x"], cols={"a": "a"})
    s1 = _spec("s1", kinds=["x"], cols={"a": "a"})
    cat = _catalog(s0, s1)
    counter = {"n": 0}
    slugs = await rs.select_registry_slugs(
        _need(entity_type="X", attribute="a"), cat,
        top_k=8, embed_fn=_positional_embed(counter),
    )
    assert counter["n"] == 0
    assert set(slugs) == {"s0", "s1"}


# --------------------------------------------------------------------------- #
# Decision cache
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_decision_cache_memoizes_by_need():
    s0 = _spec("s0", kinds=["x"], cols={"a": "a"})
    s1 = _spec("s1", kinds=["x"], cols={"a": "a"})
    s2 = _spec("s2", kinds=["x"], cols={"a": "a"})
    cat = _catalog(s0, s1, s2)
    counter = {"n": 0}
    embed = _positional_embed(counter)
    need = _need(entity_type="X", attribute="a")
    first = await rs.select_registry_slugs(need, cat, top_k=2, embed_fn=embed)
    second = await rs.select_registry_slugs(need, cat, top_k=2, embed_fn=embed)
    assert first == second
    assert counter["n"] == 1                 # second call hit the cache, no re-embed
    rs.clear_selection_cache()
    await rs.select_registry_slugs(need, cat, top_k=2, embed_fn=embed)
    assert counter["n"] == 2                 # cleared → recomputed


@pytest.mark.asyncio
async def test_empty_prefilter_returns_empty_and_is_cached():
    a = _spec("a", kinds=["physician"], cols={"npi": "n"})
    cat = _catalog(a)
    slugs = await rs.select_registry_slugs(_need(entity_type="Company", attribute="npi"), cat)
    assert slugs == []


# --------------------------------------------------------------------------- #
# Wiring — apply_registry_selection (flag-gated)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_apply_selection_identity_when_flag_off():
    a = _spec("nppes", kinds=["physician"], cols={"npi": "n"})
    register_registry_enrichment(catalog=_catalog(a))
    chain = ["api:nppes", "wikidata"]
    out = await apply_registry_selection(chain, "Physician", "npi", catalog=_catalog(a))
    assert out == chain                      # OFF → byte-identical


@pytest.mark.asyncio
async def test_apply_selection_reshapes_when_flag_on(monkeypatch):
    monkeypatch.setenv("COGRAPH_REGISTRY_SELECTION", "1")
    good = _spec("nppes", kinds=["physician"], cols={"npi": "n"})
    other = _spec("food", kinds=["food_item"], cols={"barcode": "c"})
    cat = _catalog(good, other)
    register_registry_enrichment(catalog=cat)
    # Chain leads with BOTH registry adapters + wikidata tail. Selection for
    # (Physician, npi) keeps only nppes; the food source is dropped, wikidata
    # tail preserved.
    chain = ["api:nppes", "api:food", "wikidata"]
    out = await apply_registry_selection(chain, "Physician", "npi", catalog=cat)
    assert out == ["api:nppes", "wikidata"]


@pytest.mark.asyncio
async def test_apply_selection_drops_all_registry_when_none_qualify(monkeypatch):
    monkeypatch.setenv("COGRAPH_REGISTRY_SELECTION", "1")
    food = _spec("food", kinds=["food_item"], cols={"barcode": "c"})
    cat = _catalog(food)
    register_registry_enrichment(catalog=cat)
    chain = ["api:food", "wikidata"]
    # No registry source can answer npi for a Physician → registry leads dropped,
    # the non-registry tail still runs (correct: skip N self-gating no-ops).
    out = await apply_registry_selection(chain, "Physician", "npi", catalog=cat)
    assert out == ["wikidata"]


@pytest.mark.asyncio
async def test_apply_selection_preserves_non_registry_names(monkeypatch):
    monkeypatch.setenv("COGRAPH_REGISTRY_SELECTION", "1")
    good = _spec("nppes", kinds=["physician"], cols={"npi": "n"})
    cat = _catalog(good)
    register_registry_enrichment(catalog=cat)
    chain = ["cache", "api:nppes", "wikidata", "some_web_adapter"]
    out = await apply_registry_selection(chain, "Physician", "npi", catalog=cat)
    # Registry lead reshaped to [api:nppes]; every non-api name kept in order.
    assert out == ["api:nppes", "cache", "wikidata", "some_web_adapter"]


@pytest.mark.asyncio
async def test_apply_selection_noop_when_no_registry_in_chain(monkeypatch):
    monkeypatch.setenv("COGRAPH_REGISTRY_SELECTION", "1")
    register_registry_enrichment(catalog=_catalog(_spec("nppes", kinds=["physician"], cols={"npi": "n"})))
    chain = ["cache", "wikidata"]
    out = await apply_registry_selection(chain, "Physician", "npi")
    assert out == chain


@pytest.mark.asyncio
async def test_apply_selection_never_raises_on_bad_catalog(monkeypatch):
    monkeypatch.setenv("COGRAPH_REGISTRY_SELECTION", "1")

    class _Boom:
        def all(self):
            raise RuntimeError("catalog exploded")

    chain = ["api:nppes", "wikidata"]
    out = await apply_registry_selection(chain, "Physician", "npi", catalog=_Boom())
    assert out == chain                      # failure → original chain, no raise


# --------------------------------------------------------------------------- #
# Flag helpers
# --------------------------------------------------------------------------- #
def test_selection_flag_and_top_k_env(monkeypatch):
    assert rs.selection_enabled() is False
    monkeypatch.setenv("COGRAPH_REGISTRY_SELECTION", "TRUE")
    assert rs.selection_enabled() is True
    assert rs.selection_top_k() == 8
    monkeypatch.setenv("COGRAPH_REGISTRY_SELECTION_TOP_K", "3")
    assert rs.selection_top_k() == 3
    monkeypatch.setenv("COGRAPH_REGISTRY_SELECTION_TOP_K", "garbage")
    assert rs.selection_top_k() == 8

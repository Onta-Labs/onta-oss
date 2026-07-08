"""Router tests for the API source registry (ONTA-194, phase 2).

The router's embedding + LLM seams are injected (``embed_fn`` / ``chat_fn``) so
these run offline and deterministically.
"""

from __future__ import annotations

import json

import pytest

from cograph_client.api_registry import (
    MODE_API_ONLY,
    MODE_API_PLUS_WEB,
    MODE_WEB_ONLY,
    make_api_source_catalog,
    route_query,
)
from cograph_client.api_registry.catalog import reset_api_source_layers
from cograph_client.api_registry.router import (
    MODE_API_ONLY as _MODE_API_ONLY,  # noqa: F401 (re-exported for readability)
    RoutingDecision,
    _candidate_block,
    _lexical_rank,
    _param_geo_kind,
)


@pytest.fixture(autouse=True)
def _no_overlays():
    reset_api_source_layers()
    yield
    reset_api_source_layers()


def _catalog():
    return make_api_source_catalog()


def _chat(payload: dict):
    async def fn(system: str, user: str) -> str:
        return json.dumps(payload)
    return fn


# --------------------------------------------------------------------------- #
# choose
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_choose_picks_api_and_drops_bogus_binding():
    dec = await route_query(
        "all cardiologists in San Francisco",
        _catalog(),
        entity_type="healthcare_provider",
        chat_fn=_chat({
            "mode": "api_only",
            "picks": [{"slug": "nppes", "endpoint": "search",
                       "bindings": {"taxonomy_description": "cardiology", "city": "San Francisco",
                                    "state": "CA", "bogus": "x"}}],
            "rationale": "official US clinician registry",
        }),
    )
    assert dec.mode == MODE_API_ONLY
    assert dec.uses_api and not dec.uses_web
    assert len(dec.picks) == 1
    pk = dec.picks[0]
    assert pk.slug == "nppes" and pk.endpoint == "search"
    assert pk.bindings == {"taxonomy_description": "cardiology", "city": "San Francisco", "state": "CA"}
    assert "nppes" in dec.prefilter_slugs


@pytest.mark.asyncio
async def test_choose_drops_hallucinated_slug():
    dec = await route_query(
        "something", _catalog(),
        chat_fn=_chat({"mode": "api_only", "picks": [{"slug": "does_not_exist", "bindings": {}}]}),
    )
    assert dec.mode == MODE_WEB_ONLY
    assert dec.picks == []


@pytest.mark.asyncio
async def test_choose_unknown_endpoint_falls_back_to_primary():
    dec = await route_query(
        "trials", _catalog(),
        chat_fn=_chat({"mode": "api_plus_web",
                       "picks": [{"slug": "clinicaltrials_gov", "endpoint": "nope",
                                  "bindings": {"condition": "cancer"}}]}),
    )
    assert dec.picks and dec.picks[0].endpoint == "studies"  # the real primary endpoint


@pytest.mark.asyncio
async def test_choose_invalid_mode_normalizes_to_web_only_when_no_picks():
    dec = await route_query("x", _catalog(), chat_fn=_chat({"mode": "banana", "picks": []}))
    assert dec.mode == MODE_WEB_ONLY


@pytest.mark.asyncio
async def test_choose_picks_but_web_only_becomes_api_plus_web():
    # LLM contradicts itself (gives picks but says web_only) -> supplement, not drop.
    dec = await route_query(
        "geocode Springfield", _catalog(),
        chat_fn=_chat({"mode": "web_only",
                       "picks": [{"slug": "geonames_search", "bindings": {"q": "Springfield"}}]}),
    )
    assert dec.mode == MODE_API_PLUS_WEB
    assert dec.uses_api and dec.uses_web


@pytest.mark.asyncio
async def test_malformed_json_is_web_only():
    async def bad(system, user):
        return "not json at all"
    dec = await route_query("x", _catalog(), chat_fn=bad)
    assert dec.mode == MODE_WEB_ONLY and dec.picks == []


@pytest.mark.asyncio
async def test_chat_exception_is_web_only_never_raises():
    async def boom(system, user):
        raise RuntimeError("llm down")
    dec = await route_query("x", _catalog(), chat_fn=boom)
    assert dec.mode == MODE_WEB_ONLY


@pytest.mark.asyncio
async def test_fenced_json_is_parsed():
    async def fenced(system, user):
        return "```json\n" + json.dumps({"mode": "api_only", "picks": [{"slug": "nppes", "bindings": {}}]}) + "\n```"
    dec = await route_query("doctors", _catalog(), chat_fn=fenced)
    assert dec.picks and dec.picks[0].slug == "nppes"


# --------------------------------------------------------------------------- #
# prefilter
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_no_key_no_chat_is_web_only():
    # No LLM available at all -> stay on today's behavior (no registry routing).
    dec = await route_query("all cardiologists in SF", _catalog(), openrouter_key="")
    assert dec.mode == MODE_WEB_ONLY


@pytest.mark.asyncio
async def test_empty_query_is_web_only():
    dec = await route_query("   ", _catalog(), chat_fn=_chat({"mode": "api_only", "picks": []}))
    assert dec.mode == MODE_WEB_ONLY


@pytest.mark.asyncio
async def test_prefilter_embedding_ranks_and_limits_topk():
    # Force the embedding path (top_k < catalog size) with a fake embedder that
    # makes the query most similar to whichever entry text contains "clinic".
    seen = {}

    async def embed_fn(texts):
        # texts[0] is the query; rank each entry by whether it shares a keyword.
        q = texts[0].lower()
        seen["n"] = len(texts)
        vecs = []
        for t in texts:
            score = 1.0 if ("clinical" in t.lower() and "clinical" in q) else 0.0
            vecs.append([score, 1.0 - score])
        return vecs

    dec = await route_query(
        "clinical trials for cancer", _catalog(), top_k=1,
        embed_fn=embed_fn,
        chat_fn=_chat({"mode": "api_only",
                       "picks": [{"slug": "clinicaltrials_gov", "bindings": {"condition": "cancer"}}]}),
    )
    assert seen["n"] == len(_catalog().enabled()) + 1  # query + one text per entry
    # Only the top-1 candidate was offered to choose.
    assert dec.prefilter_slugs == ["clinicaltrials_gov"]


def test_lexical_rank_prefers_overlap():
    scores = _lexical_rank("cardiologist doctor npi", ["us clinician npi taxonomy", "food product barcode"])
    assert scores[0] > scores[1]


@pytest.mark.asyncio
async def test_routing_decision_default_is_web_only():
    d = RoutingDecision()
    assert d.mode == MODE_WEB_ONLY and not d.uses_api and d.uses_web


# --------------------------------------------------------------------------- #
# geographic scope-mismatch guard (county-geo-scope bug)
# --------------------------------------------------------------------------- #
def test_candidate_block_includes_param_descriptions():
    """The LLM must see each param's DESCRIPTION, not just its name — otherwise
    it cannot tell a city-granularity param from a county-level one. Assert the
    'name: description' shape appears for a described param."""
    block = _candidate_block(_catalog().enabled())
    # nppes's city param carries a description in its seed spec; it must be shown.
    assert "city: " in block
    # Concretely, the described text (not just the bare name) is present.
    assert "Practice city" in block


def test_param_geo_kind_classifies_by_semantics_not_placename():
    """Pure param-semantics classifier: city/zip => narrow, county/region/radius
    => broad, state/other => neither. No place names involved."""
    from cograph_client.api_registry.spec import ParamSpec

    def kind(name, desc=""):
        return _param_geo_kind(ParamSpec(name=name, description=desc))

    assert kind("city", "Practice city.") == "narrow"
    assert kind("postal_code", "ZIP code.") == "narrow"
    assert kind("county", "County name.") == "broad"
    assert kind("region") == "broad"
    assert kind("radius_miles", "Search radius in miles.") == "broad"
    # A state binding is a legitimate coarsening — not touched by the guard.
    assert kind("state", "Two-letter state code.") == ""
    assert kind("taxonomy_description", "Specialty text.") == ""


@pytest.mark.asyncio
async def test_broad_scope_ask_does_not_silently_bind_same_named_city():
    """The core county-geo-scope fix: a county/region-scoped request against a
    source whose only geo param is city-granularity must NOT silently bind that
    broader place into `city`. The narrowing binding is dropped, api_only is
    demoted to api_plus_web (so web covers the full area), and a note is set.

    Uses an INVENTED county ('Wexford County') so nothing overfits."""
    dec = await route_query(
        "all cardiologists in Wexford County, California",
        _catalog(),
        entity_type="healthcare_provider",
        chat_fn=_chat({
            "mode": "api_only",
            "picks": [{"slug": "nppes", "endpoint": "search",
                       "bindings": {"taxonomy_description": "cardiology",
                                    "city": "Wexford", "state": "CA"}}],
            "rationale": "official US clinician registry",
        }),
    )
    assert dec.picks, "a pick should survive (state is still bindable)"
    pk = dec.picks[0]
    # The greedy city binding is gone — not silently kept.
    assert "city" not in pk.bindings
    # The bindings we CAN honor exactly are preserved.
    assert pk.bindings.get("state") == "CA"
    assert pk.bindings.get("taxonomy_description") == "cardiology"
    # Demoted so web search still runs over the full county.
    assert dec.mode == MODE_API_PLUS_WEB
    assert dec.uses_web and dec.uses_api
    assert dec.scope_notes and any("nppes" in n for n in dec.scope_notes)


@pytest.mark.asyncio
async def test_radius_scope_ask_also_guards_against_city_narrowing():
    """A 'within N miles' radius ask is broader than a city too — the same guard
    applies. Invented town name to avoid overfitting."""
    dec = await route_query(
        "orthopedic surgeons within 25 miles of Zephyrford",
        _catalog(),
        entity_type="healthcare_provider",
        chat_fn=_chat({
            "mode": "api_only",
            "picks": [{"slug": "nppes", "endpoint": "search",
                       "bindings": {"taxonomy_description": "orthopedic",
                                    "city": "Zephyrford"}}],
        }),
    )
    assert dec.picks
    assert "city" not in dec.picks[0].bindings
    assert dec.mode == MODE_API_PLUS_WEB
    assert dec.scope_notes


@pytest.mark.asyncio
async def test_city_scope_ask_is_unchanged_by_the_guard():
    """A plain city-scoped ask must be untouched: the guard is a no-op unless the
    request names a BROADER unit. The city binding survives and api_only holds."""
    dec = await route_query(
        "all cardiologists in Springfield",
        _catalog(),
        entity_type="healthcare_provider",
        chat_fn=_chat({
            "mode": "api_only",
            "picks": [{"slug": "nppes", "endpoint": "search",
                       "bindings": {"taxonomy_description": "cardiology",
                                    "city": "Springfield"}}],
        }),
    )
    assert dec.picks[0].bindings.get("city") == "Springfield"
    assert dec.mode == MODE_API_ONLY
    assert not dec.scope_notes

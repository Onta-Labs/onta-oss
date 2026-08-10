"""Discovery-rail projection tests for the API source registry (ONTA-194, ph2)."""

from __future__ import annotations

import httpx
import pytest

from infona_client.api_registry import (
    MODE_API_ONLY,
    MODE_API_PLUS_WEB,
    RegistryApiSource,
    RegistryDiscoverySource,
    RoutingDecision,
    RoutingPick,
    build_registry_sources,
    make_api_source_catalog,
)
from infona_client.api_registry.catalog import reset_api_source_layers
from infona_client.retrieval import safety as safety_mod


@pytest.fixture(autouse=True)
def _offline_dns(monkeypatch):
    monkeypatch.setattr(safety_mod, "_resolve_ips", lambda host: ["93.184.216.34"])


@pytest.fixture(autouse=True)
def _no_overlays():
    reset_api_source_layers()
    yield
    reset_api_source_layers()


def _nppes_source(handler):
    cat = make_api_source_catalog()
    spec = cat.get("nppes")
    ex = RegistryApiSource(transport=httpx.MockTransport(handler))
    return RegistryDiscoverySource(spec, endpoint="search",
                                   bindings={"taxonomy_description": "cardiology", "state": "CA"},
                                   executor=ex)


@pytest.mark.asyncio
async def test_discover_projects_rows_and_provenance():
    page = {"result_count": 1, "results": [
        {"number": "1", "basic": {"first_name": "A", "last_name": "B"},
         "taxonomies": [{"desc": "Cardiovascular Disease"}],
         "addresses": [{"city": "SAN FRANCISCO", "state": "CA", "telephone_number": "415"}]}]}
    empty = {"result_count": 0, "results": []}

    def handler(req):
        from urllib.parse import parse_qs, urlparse
        skip = parse_qs(urlparse(str(req.url)).query).get("skip", ["0"])[0]
        return httpx.Response(200, json=page if skip == "0" else empty)

    src = _nppes_source(handler)
    assert src.name == "api:nppes"
    assert src.is_source_of_truth is True
    assert src.title == "NPPES NPI Registry"

    dr = await src.discover("cardiologists", sample=False, max_rows=50, hint_columns=None, context={})
    assert [r["last_name"] for r in dr.rows] == ["B"]
    assert list(dr.provenance.values())[0].startswith("https://npiregistry.cms.hhs.gov/api/")


@pytest.mark.asyncio
async def test_discover_projects_request_trace():
    """The executor's per-request trace rides through discover() onto
    DiscoverResult.calls, so the web-ingest capability can persist it."""
    page = {"result_count": 1, "results": [
        {"number": "1", "basic": {"first_name": "A", "last_name": "B"},
         "taxonomies": [{"desc": "Cardiovascular Disease"}],
         "addresses": [{"city": "SF", "state": "CA", "telephone_number": "415"}]}]}
    empty = {"result_count": 0, "results": []}

    def handler(req):
        from urllib.parse import parse_qs, urlparse
        skip = parse_qs(urlparse(str(req.url)).query).get("skip", ["0"])[0]
        return httpx.Response(200, json=page if skip == "0" else empty)

    dr = await _nppes_source(handler).discover(
        "cardiologists", sample=False, max_rows=50, hint_columns=None, context={}
    )
    assert dr.calls, "expected per-request traces to be projected"
    first = dr.calls[0]
    assert first["url"].startswith("https://npiregistry.cms.hhs.gov/api/")
    assert first["status"] == 200
    assert first["records"] == 1
    assert first["params"]["taxonomy_description"] == "cardiology"


@pytest.mark.asyncio
async def test_discover_url_mode_returns_empty():
    src = _nppes_source(lambda r: httpx.Response(200, json={"results": []}))
    dr = await src.discover("x", sample=False, max_rows=10, hint_columns=None, context={},
                            urls=["https://example.com/page"])
    assert dr.rows == []


@pytest.mark.asyncio
async def test_dormant_entry_yields_empty_result(monkeypatch):
    # A paid overlay entry with no key -> dormant -> empty DiscoverResult (falls back
    # to web), never an error that would sink the discovery.
    monkeypatch.delenv("DEMO_TOKEN", raising=False)
    from infona_client.api_registry import ApiSourceSpec
    spec = ApiSourceSpec.from_dict({
        "slug": "paid_demo", "title": "Paid", "base_url": "https://api.demo.test",
        "auth": {"mode": "api_key_query", "key_env": "DEMO_TOKEN", "query_key": "token"},
        "entitlement": "paid",
        "endpoints": [{"name": "s", "path": "/s", "result_path": "results",
                       "field_mappings": {"id": "id"}, "params": [{"name": "q", "target": "q"}],
                       "pagination": {"style": "none"}}],
    })
    called = {"n": 0}

    def handler(req):
        called["n"] += 1
        return httpx.Response(200, json={"results": []})

    src = RegistryDiscoverySource(spec, endpoint="s", bindings={"q": "x"},
                                  executor=RegistryApiSource(transport=httpx.MockTransport(handler)))
    dr = await src.discover("x", sample=False, max_rows=10, hint_columns=None, context={})
    assert dr.rows == [] and dr.error is None
    assert called["n"] == 0  # dormant -> no network


def test_build_registry_sources_skips_missing_and_disabled():
    cat = make_api_source_catalog()
    dec = RoutingDecision(mode=MODE_API_PLUS_WEB, picks=[
        RoutingPick(slug="nppes", endpoint="search", bindings={"state": "CA"}),
        RoutingPick(slug="ghost", bindings={}),  # not in catalog -> skipped
    ])
    srcs = build_registry_sources(cat, dec)
    assert [s.name for s in srcs] == ["api:nppes"]


def test_build_registry_sources_empty_for_web_only():
    cat = make_api_source_catalog()
    assert build_registry_sources(cat, RoutingDecision()) == []


def test_rows_per_call_reflects_pagination_page_size():
    # A paginating source declares records-per-request so the cost estimator
    # prices its pages (not one call for the whole run).
    cat = make_api_source_catalog()
    src = build_registry_sources(
        cat, RoutingDecision(mode=MODE_API_PLUS_WEB,
                             picks=[RoutingPick(slug="nppes", endpoint="search", bindings={})]),
    )[0]
    assert src.rows_per_call == cat.get("nppes").endpoint("search").pagination.page_size


def test_build_registry_sources_skips_dormant_key_gated(monkeypatch):
    # A key-gated entry with its env unset is dormant -> excluded from the
    # ensemble (so api_only doesn't drop web in favor of a source that can't run).
    monkeypatch.delenv("SOME_REGISTRY_KEY", raising=False)
    from infona_client.api_registry import ApiSourceSpec, register_api_source_layer
    paid = ApiSourceSpec.from_dict({
        "slug": "paid_dir", "title": "Paid Directory", "base_url": "https://api.paid.test",
        "auth": {"mode": "api_key_query", "key_env": "SOME_REGISTRY_KEY", "query_key": "token"},
        "entitlement": "paid",
        "endpoints": [{"name": "s", "path": "/s", "result_path": "results",
                       "field_mappings": {"id": "id"}, "params": [{"name": "q", "target": "q"}],
                       "pagination": {"style": "none"}}],
    })
    register_api_source_layer("global_enhanced", [paid])
    cat = make_api_source_catalog()
    dec = RoutingDecision(mode=MODE_API_ONLY, picks=[RoutingPick(slug="paid_dir", bindings={"q": "x"})])
    assert build_registry_sources(cat, dec) == []  # dormant -> skipped

    monkeypatch.setenv("SOME_REGISTRY_KEY", "present")
    assert [s.name for s in build_registry_sources(cat, dec)] == ["api:paid_dir"]


# --------------------------------------------------------------------------- #
# accepts() capability scope (ONTA-461 follow-on — production registry skip)
# --------------------------------------------------------------------------- #


def _openrouter_models_source() -> RegistryDiscoverySource:
    """Real seed catalog entry (openrouter.ai / openrouter_models) — no network."""
    cat = make_api_source_catalog()
    spec = cat.get("openrouter_models")
    assert spec is not None, "openrouter_models seed entry missing from catalog"
    return RegistryDiscoverySource(spec, endpoint="list")


def test_openrouter_models_declares_served_hosts_and_slug():
    """Provider self-knowledge from catalog base_url / slug (not a brand list)."""
    src = _openrouter_models_source()
    assert src.name == "api:openrouter_models"
    assert src.registry_slug == "openrouter_models"
    assert src.served_hosts == frozenset({"openrouter.ai"})


def test_accepts_true_when_context_unconstrained():
    """Ambiguous / empty context → accept (don't over-skip). Query text alone
    never rejects — orchestrator brand ifs stay out of this path."""
    src = _openrouter_models_source()
    assert src.accepts("TTS models on some other platform", {}) is True
    assert src.accepts("OpenRouter TTS models", {}) is True
    assert src.accepts("anything", {"tenant_id": "demo", "kg_name": "k"}) is True
    # Empty constraint containers are not constraints.
    assert src.accepts("q", {"required_hosts": []}) is True
    assert src.accepts("q", {"target_registry_ids": []}) is True
    assert src.accepts("q", {"source_constraint": {}}) is True
    assert src.accepts("q", {"source_constraint": {"hosts": [], "registry_ids": []}}) is True


def test_accepts_false_when_required_hosts_exclude_served():
    """required_hosts set with no intersection → out of scope."""
    src = _openrouter_models_source()
    assert src.accepts("models on other.host", {"required_hosts": ["other.host"]}) is False
    assert src.accepts("models", {"required_hosts": frozenset({"example.com", "api.other.ai"})}) is False
    # Nested preferred shape.
    assert src.accepts(
        "models", {"source_constraint": {"hosts": ["npiregistry.cms.hhs.gov"]}}
    ) is False


def test_accepts_true_when_required_hosts_intersect_served():
    """required_hosts that include our base_url host → accept (www. stripped)."""
    src = _openrouter_models_source()
    assert src.accepts("models", {"required_hosts": ["openrouter.ai"]}) is True
    assert src.accepts("models", {"required_hosts": ["www.openrouter.ai"]}) is True
    assert src.accepts(
        "models",
        {"required_hosts": ["openrouter.ai", "other.example"]},
    ) is True
    assert src.accepts(
        "models",
        {"source_constraint": {"hosts": ["OpenRouter.AI"]}},
    ) is True


def test_accepts_false_when_target_registry_ids_exclude_slug():
    """target_registry_ids / slugs without us → out of scope."""
    src = _openrouter_models_source()
    assert src.accepts("q", {"target_registry_ids": ["nppes"]}) is False
    assert src.accepts("q", {"target_registry_slugs": ["nppes", "census_acs"]}) is False
    assert src.accepts(
        "q", {"source_constraint": {"registry_ids": ["nppes"]}}
    ) is False


def test_accepts_true_when_target_registry_ids_include_slug():
    """Our slug (or api:{slug} form) in the target set → accept."""
    src = _openrouter_models_source()
    assert src.accepts("q", {"target_registry_ids": ["openrouter_models"]}) is True
    assert src.accepts("q", {"target_registry_slugs": ["openrouter_models", "nppes"]}) is True
    # Callers may stamp provider.name (api:slug).
    assert src.accepts("q", {"target_registry_ids": ["api:openrouter_models"]}) is True
    assert src.accepts(
        "q", {"source_constraint": {"registry_ids": ["openrouter_models"]}}
    ) is True


def test_accepts_false_when_either_constraint_excludes():
    """Both host and registry constraints must pass when present."""
    src = _openrouter_models_source()
    # Host matches but registry does not.
    assert src.accepts(
        "q",
        {
            "required_hosts": ["openrouter.ai"],
            "target_registry_ids": ["nppes"],
        },
    ) is False
    # Registry matches but host does not.
    assert src.accepts(
        "q",
        {
            "required_hosts": ["other.host"],
            "target_registry_ids": ["openrouter_models"],
        },
    ) is False


def test_accepts_works_via_provider_accepts_helper():
    """Ensemble path uses provider_accepts (ONTA-461); False must propagate."""
    from infona_client.web_sources.base import provider_accepts

    src = _openrouter_models_source()
    assert provider_accepts(src, "q", {}) is True
    assert provider_accepts(
        src, "q", {"required_hosts": ["not-our-host.example"]}
    ) is False
    assert provider_accepts(
        src, "q", {"target_registry_ids": ["openrouter_models"]}
    ) is True


def test_nppes_accepts_metadata_from_own_base_url():
    """Any registry entry gets the same self-declared scope from its catalog."""
    cat = make_api_source_catalog()
    spec = cat.get("nppes")
    assert spec is not None
    src = RegistryDiscoverySource(spec, endpoint="search")
    assert "npiregistry.cms.hhs.gov" in src.served_hosts
    assert src.registry_slug == "nppes"
    assert src.accepts("cardiologists", {}) is True
    assert src.accepts(
        "cardiologists",
        {"required_hosts": ["openrouter.ai"]},
    ) is False
    assert src.accepts(
        "cardiologists",
        {"required_hosts": ["npiregistry.cms.hhs.gov"]},
    ) is True

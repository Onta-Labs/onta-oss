"""Discovery-rail projection tests for the API source registry (ONTA-194, ph2)."""

from __future__ import annotations

import httpx
import pytest

from cograph_client.api_registry import (
    MODE_API_ONLY,
    MODE_API_PLUS_WEB,
    RegistryApiSource,
    RegistryDiscoverySource,
    RoutingDecision,
    RoutingPick,
    build_registry_sources,
    make_api_source_catalog,
)
from cograph_client.api_registry.catalog import reset_api_source_layers
from cograph_client.retrieval import safety as safety_mod


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
    from cograph_client.api_registry import ApiSourceSpec
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
    from cograph_client.api_registry import ApiSourceSpec, register_api_source_layer
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

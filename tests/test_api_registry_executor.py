"""Executor tests for the API source registry (ONTA-194, phase 1).

Uses ``httpx.MockTransport`` (the repo's convention — no respx/vcr) so the real
request-building, pagination, redirect, and byte-cap machinery runs against
scripted responses. ``_resolve_ips`` is monkeypatched to a public IP by default
so the SSRF guard is deterministic and offline; SSRF tests override it.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from cograph_client.api_registry import (
    ApiSourceSpec,
    RegistryApiSource,
    make_api_source_catalog,
)
from cograph_client.api_registry.catalog import reset_api_source_layers
# SSRF DNS stub now lives in the shared retrieval substrate (ONTA-193).
from cograph_client.retrieval import safety as safety_mod
from cograph_client.research.types import Budget

FIXTURES = Path(__file__).parent / "fixtures" / "nppes"


@pytest.fixture(autouse=True)
def _offline_dns(monkeypatch):
    # Treat every hostname as resolving to a public IP — deterministic + offline.
    monkeypatch.setattr(safety_mod, "_resolve_ips", lambda host: ["93.184.216.34"])


@pytest.fixture(autouse=True)
def _no_overlays():
    reset_api_source_layers()
    yield
    reset_api_source_layers()


def _src(handler) -> RegistryApiSource:
    return RegistryApiSource(transport=httpx.MockTransport(handler))


def _params(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}


def _spec(**overrides) -> ApiSourceSpec:
    """A minimal synthetic entry; overrides are deep-merged shallowly."""
    base = {
        "slug": "demo",
        "title": "Demo",
        "base_url": "https://api.demo.test",
        "auth": {"mode": "none"},
        "endpoints": [
            {
                "name": "search",
                "path": "/v1/search",
                "query": {},
                "params": [{"name": "q", "target": "q"}],
                "result_path": "results",
                "field_mappings": {"id": "id", "name": "name"},
                "pagination": {"style": "none"},
            }
        ],
    }
    base.update(overrides)
    return ApiSourceSpec.from_dict(base)


# --------------------------------------------------------------------------- #
# NPPES recorded-fixture E2E (the flagship, using the real shipped seed spec)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_nppes_fixture_e2e_offset_pagination():
    cat = make_api_source_catalog()
    nppes = cat.get("nppes")
    page0 = json.loads((FIXTURES / "search_cardiology_sf_skip0.json").read_text())
    empty = json.loads((FIXTURES / "search_cardiology_sf_empty.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        skip = _params(request).get("skip", "0")
        return httpx.Response(200, json=page0 if skip == "0" else empty)

    res = await _src(handler).execute(
        nppes,
        {"taxonomy_description": "cardiology", "city": "San Francisco", "state": "CA"},
        max_rows=50,
    )

    assert res.error is None and not res.dormant
    assert res.source == "api:nppes"
    assert len(res.rows) == 2
    first = res.rows[0]
    assert first["npi"] == "1234567893"
    assert first["last_name"] == "GARCIA"
    assert first["primary_taxonomy"] == "Internal Medicine, Cardiovascular Disease"
    assert first["address_city"] == "SAN FRANCISCO"
    assert first["phone"] == "415-353-1000"
    # Every row is attributed to a real NPPES request URL.
    assert all(u.startswith("https://npiregistry.cms.hhs.gov/api/") for u in res.provenance.values())
    # It stopped after the empty second page (offset pagination termination).
    assert res.pages_fetched == 2


@pytest.mark.asyncio
async def test_nppes_projects_enumeration_date():
    """NPPES returns the NPI enumeration date at basic.enumeration_date — the
    recency signal the 'new-to-territory' persona ask depends on. The seed spec
    must map it (else every enumeration_date ask returns 'No results found')."""
    cat = make_api_source_catalog()
    nppes = cat.get("nppes")
    page0 = json.loads((FIXTURES / "search_cardiology_sf_skip0.json").read_text())
    empty = json.loads((FIXTURES / "search_cardiology_sf_empty.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        skip = _params(request).get("skip", "0")
        return httpx.Response(200, json=page0 if skip == "0" else empty)

    res = await _src(handler).execute(
        nppes, {"taxonomy_description": "cardiology", "state": "CA"}, max_rows=50
    )
    assert res.error is None
    # The mapping is present and projects the ISO date verbatim from the body.
    assert res.rows[0]["enumeration_date"] == "2007-05-09"


@pytest.mark.asyncio
async def test_nppes_request_shape_from_seed():
    cat = make_api_source_catalog()
    nppes = cat.get("nppes")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(_params(request))
        return httpx.Response(200, json={"result_count": 0, "results": []})

    await _src(handler).execute(nppes, {"taxonomy_description": "cardiology", "state": "CA"}, sample=True)
    assert seen["version"] == "2.1"          # static query param
    assert seen["taxonomy_description"] == "cardiology"
    assert seen["state"] == "CA"
    assert seen["limit"] == "200"            # page-size param
    assert seen["skip"] == "0"               # offset start


# --------------------------------------------------------------------------- #
# Pagination styles (synthetic specs)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_offset_pagination_walks_multiple_pages():
    spec = _spec()
    spec.endpoints[0].pagination = ApiSourceSpec.from_dict(
        {
            "slug": "x", "title": "x", "base_url": "https://api.demo.test",
            "endpoints": [{
                "name": "s", "path": "/s", "result_path": "results", "field_mappings": {"id": "id"},
                "pagination": {"style": "offset", "limit_param": "limit", "page_size": 2, "offset_param": "skip", "max_pages": 5},
            }],
        }
    ).endpoints[0].pagination

    def handler(request: httpx.Request) -> httpx.Response:
        skip = int(_params(request).get("skip", "0"))
        # Two full pages (2 rows each), then an empty page.
        rows = {0: [{"id": "1"}, {"id": "2"}], 2: [{"id": "3"}, {"id": "4"}]}.get(skip, [])
        return httpx.Response(200, json={"results": rows})

    res = await _src(handler).execute(spec, {"q": "x"}, max_rows=50)
    assert [r["id"] for r in res.rows] == ["1", "2", "3", "4"]
    assert res.pages_fetched == 3  # two data pages + the empty terminator


@pytest.mark.asyncio
async def test_request_trace_captures_url_params_status_records():
    """Every HTTP request is traced with its auth-free URL, query-param payload,
    HTTP status, and per-request record count — including a page that errors."""
    spec = _spec(endpoints=[{
        "name": "s", "path": "/s", "result_path": "results", "field_mappings": {"id": "id"},
        "params": [{"name": "q", "target": "q"}],
        "pagination": {"style": "offset", "limit_param": "limit", "page_size": 2, "offset_param": "skip", "max_pages": 5},
    }])

    def handler(request: httpx.Request) -> httpx.Response:
        skip = int(_params(request).get("skip", "0"))
        if skip == 0:
            return httpx.Response(200, json={"results": [{"id": "1"}, {"id": "2"}]})
        if skip == 2:
            return httpx.Response(200, json={"results": [{"id": "3"}, {"id": "4"}]})
        return httpx.Response(500, text="boom")  # third page errors

    res = await _src(handler).execute(spec, {"q": "x"}, max_rows=50)

    # Two data pages landed; the third (500) truncated the run but is still traced.
    assert [r["id"] for r in res.rows] == ["1", "2", "3", "4"]
    assert res.is_partial is True
    assert len(res.calls) == 3

    c0, c1, c2 = res.calls
    assert c0["status"] == 200 and c0["records"] == 2 and c0["error"] is None
    assert c0["url"].startswith("https://api.demo.test/s?") and "secret" not in c0["url"]
    assert c0["params"]["skip"] == "0" and c0["params"]["limit"] == "2"
    assert c0["params"]["q"] == "x"           # the bound search param (the payload)
    assert c1["params"]["skip"] == "2" and c1["records"] == 2
    # The failed page: status surfaced, zero records, error message.
    assert c2["status"] == 500 and c2["records"] == 0 and c2["error"] == "HTTP 500"


@pytest.mark.asyncio
async def test_request_trace_omits_query_key_secret():
    """A query-key auth secret is applied only to the live fetch, never to the
    traced (display) URL or its parsed params."""
    spec = _spec(
        auth={"mode": "api_key_query", "query_key": "api_key", "key_env": "DEMO_KEY"},
        endpoints=[{
            "name": "s", "path": "/s", "result_path": "results", "field_mappings": {"id": "id"},
            "pagination": {"style": "none"},
        }],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        # The live request DOES carry the secret (auth works)...
        assert _params(request).get("api_key") == "SECRET123"
        return httpx.Response(200, json={"results": [{"id": "1"}]})

    import os
    os.environ["DEMO_KEY"] = "SECRET123"
    try:
        res = await _src(handler).execute(spec, {"q": "x"})
    finally:
        del os.environ["DEMO_KEY"]

    assert len(res.calls) == 1
    # ...but the trace is auth-free: no api_key in the URL or the params.
    assert "SECRET123" not in res.calls[0]["url"]
    assert "api_key" not in res.calls[0]["params"]


@pytest.mark.asyncio
async def test_request_trace_scrubs_query_key_on_same_host_redirect():
    """Defense-in-depth: a same-host redirect that echoes the api_key back in its
    Location must NOT leak the secret into the persisted trace url/params."""
    spec = _spec(
        auth={"mode": "api_key_query", "query_key": "api_key", "key_env": "DEMO_KEY"},
        endpoints=[{
            "name": "s", "path": "/s", "result_path": "results", "field_mappings": {"id": "id"},
            "params": [{"name": "q", "target": "q"}],
            "pagination": {"style": "none"},
        }],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if urlparse(str(request.url)).path == "/s":
            # The upstream reflects the secret back into a same-host redirect.
            return httpx.Response(302, headers={"location": "https://api.demo.test/s2?api_key=SECRET123&q=x"})
        return httpx.Response(200, json={"results": [{"id": "1"}]})

    import os
    os.environ["DEMO_KEY"] = "SECRET123"
    try:
        res = await _src(handler).execute(spec, {"q": "x"})
    finally:
        del os.environ["DEMO_KEY"]

    assert res.rows == [{"id": "1"}]
    assert len(res.calls) == 1
    assert "SECRET123" not in res.calls[0]["url"]
    assert "api_key" not in res.calls[0]["params"]
    assert res.calls[0]["params"]["q"] == "x"  # the real payload is preserved


@pytest.mark.asyncio
async def test_cursor_pagination_follows_token_then_stops():
    spec = _spec(endpoints=[{
        "name": "s", "path": "/s", "result_path": "results", "field_mappings": {"id": "id"},
        "pagination": {"style": "cursor", "limit_param": "pageSize", "page_size": 2,
                       "cursor_param": "pageToken", "cursor_path": "nextPageToken", "max_pages": 5},
    }])

    def handler(request: httpx.Request) -> httpx.Response:
        token = _params(request).get("pageToken")
        if token is None:
            return httpx.Response(200, json={"results": [{"id": "a"}], "nextPageToken": "T2"})
        if token == "T2":
            return httpx.Response(200, json={"results": [{"id": "b"}]})  # no token -> stop
        return httpx.Response(200, json={"results": []})

    res = await _src(handler).execute(spec, max_rows=50)
    assert [r["id"] for r in res.rows] == ["a", "b"]
    assert res.pages_fetched == 2


@pytest.mark.asyncio
async def test_page_pagination_increments_page_number():
    spec = _spec(endpoints=[{
        "name": "s", "path": "/s", "result_path": "results", "field_mappings": {"id": "id"},
        "pagination": {"style": "page", "limit_param": "page_size", "page_size": 1,
                       "page_param": "page", "start_page": 1, "total_path": "count", "max_pages": 5},
    }])

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(_params(request).get("page", "1"))
        rows = {1: [{"id": "p1"}], 2: [{"id": "p2"}]}.get(page, [])
        return httpx.Response(200, json={"count": 2, "results": rows})

    res = await _src(handler).execute(spec, max_rows=50)
    assert [r["id"] for r in res.rows] == ["p1", "p2"]
    assert res.estimated_total == 2


@pytest.mark.asyncio
async def test_next_link_pagination_follows_absolute_url():
    spec = _spec(endpoints=[{
        "name": "s", "path": "/s", "result_path": "results", "field_mappings": {"id": "id"},
        "pagination": {"style": "next_link", "next_link_path": "meta.next", "max_pages": 5},
    }])

    def handler(request: httpx.Request) -> httpx.Response:
        path = urlparse(str(request.url)).path
        if path == "/s":
            return httpx.Response(200, json={"results": [{"id": "1"}], "meta": {"next": "https://api.demo.test/s2"}})
        return httpx.Response(200, json={"results": [{"id": "2"}]})

    res = await _src(handler).execute(spec, max_rows=50)
    assert [r["id"] for r in res.rows] == ["1", "2"]


@pytest.mark.asyncio
async def test_style_none_fetches_single_page():
    spec = _spec()  # pagination none
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"results": [{"id": "1", "name": "a"}]})

    res = await _src(handler).execute(spec, {"q": "x"})
    assert calls["n"] == 1
    assert res.rows == [{"id": "1", "name": "a"}]


@pytest.mark.asyncio
async def test_sample_fetches_one_page_only():
    spec = _spec(endpoints=[{
        "name": "s", "path": "/s", "result_path": "results", "field_mappings": {"id": "id"},
        "pagination": {"style": "offset", "limit_param": "limit", "page_size": 1, "offset_param": "skip", "max_pages": 5},
    }])
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"results": [{"id": str(calls["n"])}]})

    res = await _src(handler).execute(spec, sample=True, max_rows=50)
    assert calls["n"] == 1
    assert res.is_partial  # more may exist upstream


@pytest.mark.asyncio
async def test_max_rows_truncates_and_marks_partial():
    spec = _spec(endpoints=[{
        "name": "s", "path": "/s", "result_path": "results", "field_mappings": {"id": "id"},
        "pagination": {"style": "page", "limit_param": "n", "page_size": 10, "page_param": "page", "max_pages": 5},
    }])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"id": f"a{i}"} for i in range(10)]})

    res = await _src(handler).execute(spec, max_rows=3)
    assert len(res.rows) == 3
    assert res.is_partial


# --------------------------------------------------------------------------- #
# Auth modes
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_api_key_query_injects_secret(monkeypatch):
    monkeypatch.setenv("DEMO_KEY", "secret-123")
    spec = _spec(auth={"mode": "api_key_query", "key_env": "DEMO_KEY", "query_key": "apikey"})
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(_params(request))
        return httpx.Response(200, json={"results": [{"id": "1"}]})

    res = await _src(handler).execute(spec, {"q": "x"})
    assert seen["apikey"] == "secret-123"
    assert res.error is None


@pytest.mark.asyncio
async def test_missing_key_is_dormant(monkeypatch):
    monkeypatch.delenv("DEMO_KEY", raising=False)
    spec = _spec(auth={"mode": "api_key_query", "key_env": "DEMO_KEY", "query_key": "apikey"})
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"results": []})

    res = await _src(handler).execute(spec, {"q": "x"})
    assert res.dormant is True
    assert "DEMO_KEY" in (res.error or "")
    assert called["n"] == 0  # never hit the network


@pytest.mark.asyncio
async def test_api_key_header_injects_secret(monkeypatch):
    monkeypatch.setenv("DEMO_KEY", "hdr-secret")
    spec = _spec(auth={"mode": "api_key_header", "key_env": "DEMO_KEY", "header_name": "X-Api-Key"})
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["h"] = request.headers.get("X-Api-Key")
        return httpx.Response(200, json={"results": [{"id": "1"}]})

    await _src(handler).execute(spec, {"q": "x"})
    assert seen["h"] == "hdr-secret"


@pytest.mark.asyncio
async def test_bearer_injects_secret(monkeypatch):
    monkeypatch.setenv("DEMO_KEY", "tok")
    spec = _spec(auth={"mode": "bearer", "key_env": "DEMO_KEY"})
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["a"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"results": [{"id": "1"}]})

    await _src(handler).execute(spec, {"q": "x"})
    assert seen["a"] == "Bearer tok"


# --------------------------------------------------------------------------- #
# Path templating + required params
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_path_templating():
    spec = _spec(endpoints=[{
        "name": "get", "path": "/v1/product/{gtin}.json",
        "params": [{"name": "gtin", "location": "path", "target": "gtin", "required": True}],
        "result_path": "", "field_mappings": {"id": "code"},
        "pagination": {"style": "none"},
    }])
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = urlparse(str(request.url)).path
        return httpx.Response(200, json=[{"code": "3017620422003"}])

    res = await _src(handler).execute(spec, {"gtin": "3017620422003"})
    assert seen["path"] == "/v1/product/3017620422003.json"
    assert res.rows == [{"id": "3017620422003"}]


@pytest.mark.asyncio
async def test_missing_required_param_errors_without_fetching():
    spec = _spec(endpoints=[{
        "name": "s", "path": "/s",
        "params": [{"name": "q", "target": "q", "required": True}],
        "result_path": "results", "field_mappings": {"id": "id"},
        "pagination": {"style": "none"},
    }])
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"results": []})

    res = await _src(handler).execute(spec, {})
    assert "missing required params" in (res.error or "")
    assert "q" in (res.error or "")
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_default_param_value_used_when_unbound():
    spec = _spec(endpoints=[{
        "name": "s", "path": "/s",
        "params": [{"name": "fmt", "target": "format", "default": "json"}],
        "result_path": "results", "field_mappings": {"id": "id"},
        "pagination": {"style": "none"},
    }])
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(_params(request))
        return httpx.Response(200, json={"results": [{"id": "1"}]})

    await _src(handler).execute(spec, {})
    assert seen["format"] == "json"


# --------------------------------------------------------------------------- #
# SSRF guards
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_refuses_redirect_to_internal_host():
    spec = _spec()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})

    res = await _src(handler).execute(spec, {"q": "x"})
    assert res.error is not None
    assert "blocked" in res.error.lower()
    assert res.rows == []


@pytest.mark.asyncio
async def test_refuses_host_resolving_to_internal(monkeypatch):
    monkeypatch.setattr(safety_mod, "_resolve_ips", lambda host: ["169.254.169.254"])
    spec = _spec()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"id": "1"}]})

    res = await _src(handler).execute(spec, {"q": "x"})
    assert res.error is not None
    assert "blocked" in res.error.lower()


@pytest.mark.asyncio
async def test_follows_public_redirect():
    spec = _spec()

    def handler(request: httpx.Request) -> httpx.Response:
        if urlparse(str(request.url)).path == "/v1/search":
            return httpx.Response(302, headers={"location": "https://api.demo.test/final"})
        return httpx.Response(200, json={"results": [{"id": "ok"}]})

    res = await _src(handler).execute(spec, {"q": "x"})
    assert [r["id"] for r in res.rows] == ["ok"]


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_http_error_on_first_page_is_hard_error():
    spec = _spec()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    res = await _src(handler).execute(spec, {"q": "x"})
    assert res.error == "HTTP 500"
    assert res.rows == []


@pytest.mark.asyncio
async def test_http_error_on_later_page_is_partial():
    spec = _spec(endpoints=[{
        "name": "s", "path": "/s", "result_path": "results", "field_mappings": {"id": "id"},
        "pagination": {"style": "page", "limit_param": "n", "page_size": 1, "page_param": "page", "max_pages": 5},
    }])

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(_params(request).get("page", "1"))
        if page == 1:
            return httpx.Response(200, json={"results": [{"id": "1"}]})
        return httpx.Response(503, text="later boom")

    res = await _src(handler).execute(spec, max_rows=50)
    assert [r["id"] for r in res.rows] == ["1"]
    assert res.is_partial
    assert res.error is None  # partial, not a hard failure


@pytest.mark.asyncio
async def test_non_json_response_errors():
    spec = _spec()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    res = await _src(handler).execute(spec, {"q": "x"})
    assert res.error is not None and "JSON" in res.error


@pytest.mark.asyncio
async def test_disabled_entry_short_circuits():
    spec = _spec(enabled=False)
    res = await _src(lambda r: httpx.Response(200, json={"results": []})).execute(spec, {"q": "x"})
    assert res.error == "disabled"


@pytest.mark.asyncio
async def test_budget_caps_pages():
    spec = _spec(endpoints=[{
        "name": "s", "path": "/s", "result_path": "results", "field_mappings": {"id": "id"},
        "pagination": {"style": "page", "limit_param": "n", "page_size": 1, "page_param": "page", "max_pages": 10},
    }])

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(_params(request).get("page", "1"))
        return httpx.Response(200, json={"results": [{"id": f"p{page}"}]})

    res = await _src(handler).execute(spec, max_rows=100, budget=Budget(max_fetches=2))
    assert res.pages_fetched == 2
    assert res.is_partial


@pytest.mark.asyncio
async def test_result_to_dict_is_serializable():
    spec = _spec()
    res = await _src(lambda r: httpx.Response(200, json={"results": [{"id": "1", "name": "a"}]})).execute(spec, {"q": "x"})
    payload = json.dumps(res.to_dict())  # must not raise
    assert '"api:demo"' in payload


@pytest.mark.asyncio
async def test_never_raises_on_transport_exception():
    spec = _spec()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("kaboom")

    res = await _src(handler).execute(spec, {"q": "x"})  # must NOT raise
    assert res.rows == []
    assert res.error is not None
    assert "kaboom" in res.error


# --------------------------------------------------------------------------- #
# Credential safety (auth must not leak — regression guards for the review)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_api_key_query_secret_absent_from_result(monkeypatch):
    monkeypatch.setenv("DEMO_KEY", "SUPERSECRET")
    spec = _spec(auth={"mode": "api_key_query", "key_env": "DEMO_KEY", "query_key": "apikey"})

    def handler(request: httpx.Request) -> httpx.Response:
        assert _params(request)["apikey"] == "SUPERSECRET"  # secret DID reach the server
        return httpx.Response(200, json={"results": [{"id": "1"}]})

    res = await _src(handler).execute(spec, {"q": "x"})
    blob = json.dumps(res.to_dict())
    assert "SUPERSECRET" not in blob, "secret leaked into the returned result"
    assert all("SUPERSECRET" not in u for u in res.sources)
    assert all("SUPERSECRET" not in u for u in res.provenance.values())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth,leak_header",
    [
        ({"mode": "bearer", "key_env": "DEMO_KEY"}, "Authorization"),
        ({"mode": "api_key_header", "key_env": "DEMO_KEY", "header_name": "X-Api-Key"}, "X-Api-Key"),
    ],
)
async def test_auth_header_not_sent_to_cross_host_redirect(monkeypatch, auth, leak_header):
    monkeypatch.setenv("DEMO_KEY", "SECRETTOKEN")
    spec = _spec(auth=auth)
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = urlparse(str(request.url)).hostname or ""
        seen.append((host, request.headers.get(leak_header, "")))
        if host == "api.demo.test":
            return httpx.Response(302, headers={"location": "https://evil.attacker.test/collect"})
        return httpx.Response(200, json={"results": [{"id": "ok"}]})

    res = await _src(handler).execute(spec, {"q": "x"})
    assert [r["id"] for r in res.rows] == ["ok"]
    by_host = dict(seen)
    assert "SECRETTOKEN" in by_host["api.demo.test"]      # sent to the registered host
    assert "SECRETTOKEN" not in by_host["evil.attacker.test"]  # NOT sent to the redirected host


@pytest.mark.asyncio
async def test_api_key_query_reinjected_on_same_host_next_link(monkeypatch):
    monkeypatch.setenv("DEMO_KEY", "qsecret")
    spec = _spec(auth={"mode": "api_key_query", "key_env": "DEMO_KEY", "query_key": "apikey"},
                 endpoints=[{
                     "name": "s", "path": "/s", "result_path": "results",
                     "field_mappings": {"id": "id"},
                     "pagination": {"style": "next_link", "next_link_path": "meta.next", "max_pages": 5},
                 }])
    seen_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_keys.append(_params(request).get("apikey", ""))
        path = urlparse(str(request.url)).path
        if path == "/s":
            return httpx.Response(200, json={"results": [{"id": "1"}], "meta": {"next": "https://api.demo.test/s2"}})
        return httpx.Response(200, json={"results": [{"id": "2"}]})

    res = await _src(handler).execute(spec, {"q": "x"})
    assert [r["id"] for r in res.rows] == ["1", "2"]
    assert seen_keys == ["qsecret", "qsecret"]  # same-host next_link still authenticated


# --------------------------------------------------------------------------- #
# SSRF via untrusted response body / DNS
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_next_link_to_internal_host_is_blocked():
    spec = _spec(endpoints=[{
        "name": "s", "path": "/s", "result_path": "results", "field_mappings": {"id": "id"},
        "pagination": {"style": "next_link", "next_link_path": "meta.next", "max_pages": 5},
    }])

    def handler(request: httpx.Request) -> httpx.Response:
        if urlparse(str(request.url)).path == "/s":
            return httpx.Response(200, json={"results": [{"id": "1"}], "meta": {"next": "http://169.254.169.254/latest/meta-data"}})
        raise AssertionError("must not fetch the internal next_link")

    res = await _src(handler).execute(spec, {"q": "x"})
    assert [r["id"] for r in res.rows] == ["1"]
    assert res.is_partial  # stopped at the blocked link, honestly partial


@pytest.mark.asyncio
async def test_refuses_redirect_that_dns_resolves_to_internal(monkeypatch):
    # First host is public; the redirect target is a public NAME that resolves internal.
    def resolve(host):
        return ["169.254.169.254"] if host == "public.evil.test" else ["93.184.216.34"]

    monkeypatch.setattr(safety_mod, "_resolve_ips", resolve)
    spec = _spec()

    def handler(request: httpx.Request) -> httpx.Response:
        if urlparse(str(request.url)).hostname == "api.demo.test":
            return httpx.Response(302, headers={"location": "https://public.evil.test/x"})
        raise AssertionError("must not fetch the internally-resolving host")

    res = await _src(handler).execute(spec, {"q": "x"})
    assert res.error is not None
    assert "blocked" in res.error.lower()


# --------------------------------------------------------------------------- #
# Major-3 regression: an all-duplicate middle page must not halt pagination
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_all_duplicate_middle_page_does_not_stop_pagination():
    spec = _spec(endpoints=[{
        "name": "s", "path": "/s", "result_path": "results", "field_mappings": {"id": "id"},
        "pagination": {"style": "offset", "limit_param": "limit", "page_size": 2, "offset_param": "skip", "max_pages": 5},
    }])

    def handler(request: httpx.Request) -> httpx.Response:
        skip = int(_params(request).get("skip", "0"))
        # page @0: 1,2 ; page @2: all dups (1,1) ; page @4: fresh 3,4 ; page @6: empty
        rows = {0: [{"id": "1"}, {"id": "2"}], 2: [{"id": "1"}, {"id": "1"}],
                4: [{"id": "3"}, {"id": "4"}]}.get(skip, [])
        return httpx.Response(200, json={"results": rows})

    res = await _src(handler).execute(spec, max_rows=100)
    # Without the fix, the all-dup page @2 (net-new=0) halts pagination and 3,4
    # are lost. With len(records) passed to next_page, page @4 is reached.
    assert [r["id"] for r in res.rows] == ["1", "2", "3", "4"]


@pytest.mark.asyncio
async def test_nppes_fixture_omits_absent_mapped_fields():
    cat = make_api_source_catalog()
    nppes = cat.get("nppes")
    page0 = json.loads((FIXTURES / "search_cardiology_sf_skip0.json").read_text())
    empty = json.loads((FIXTURES / "search_cardiology_sf_empty.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        skip = _params(request).get("skip", "0")
        return httpx.Response(200, json=page0 if skip == "0" else empty)

    res = await _src(handler).execute(nppes, {"taxonomy_description": "cardiology"}, max_rows=50)
    # rows[1] (CHEN) has no organization_name in the fixture -> column omitted, no filler.
    assert "organization_name" not in res.rows[1]
    assert res.rows[1]["last_name"] == "CHEN"

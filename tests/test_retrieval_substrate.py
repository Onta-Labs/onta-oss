"""Characterization + convergence tests for the shared retrieval substrate
(ONTA-193 P0/P1).

Two jobs:

1. **Freeze the fetch layer's behaviour at its NEW canonical home**
   (:mod:`cograph_client.retrieval`) — the SSRF string guard, the resolve-and-check
   DNS guard, the static fetcher's offline/blocked/redirect handling, the ladder
   registry ordering, and the HTML→text reduction. These mirror the behaviour the
   ONTA-166 hardening suite pinned when the code lived in ``research.fetch``; they
   now assert it against ``cograph_client.retrieval`` so the move is a no-op in
   behaviour, not just in imports.

2. **Freeze the compatibility contract** — the published ``research.fetch`` shim
   and the ``research`` package re-export the *same objects* the substrate defines
   (identity, not just equality). If a future change re-forks the fetch layer, one
   of these identity assertions breaks loudly instead of silently drifting — the
   read-path analogue of ``test_write_path_convergence.py``.

All offline/deterministic (``_resolve_ips`` stubbed on the safety module; httpx
MockTransport), never a real socket.
"""

from __future__ import annotations

import httpx
import pytest

import cograph_client.research as research_pkg
import cograph_client.research.fetch as research_fetch
import cograph_client.research.types as research_types
import cograph_client.retrieval as retrieval
import cograph_client.retrieval.fetch as rfetch
import cograph_client.retrieval.safety as rsafety
import cograph_client.retrieval.types as rtypes
from cograph_client.retrieval import (
    FetchedPage,
    StaticHttpFetcher,
    default_ladder,
    fetcher_cost,
    get_page_fetchers,
    html_to_text,
    is_fetchable_url,
    register_default_fetchers,
    register_page_fetcher,
    reset_page_fetchers,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_page_fetchers()
    yield
    reset_page_fetchers()


@pytest.fixture(autouse=True)
def _offline_dns(monkeypatch):
    # Resolve every host to a fixed PUBLIC ip so StaticHttpFetcher.fetch stays
    # offline + deterministic; DNS-guard tests override this locally.
    monkeypatch.setattr(rsafety, "_resolve_ips", lambda host: ["93.184.216.34"])


def _mock_client_factory(handler):
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    return factory


# --- SSRF string guard -------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "http://2130706433/",       # decimal 127.0.0.1
        "http://0x7f000001/",       # hex
        "http://0177.0.0.1/",       # octal
        "http://127.1/",            # short form
        "http://[::1]/",            # ipv6 loopback
        "http://0.0.0.0/",          # unspecified
        "http://169.254.169.254/",  # cloud metadata
        "http://127.0.0.1./",       # trailing dot
        "http://localhost/x",       # obvious internal name
        "http://svc.internal/x",    # .internal suffix
        "ftp://example.com/x",      # non-http(s) scheme
        "not a url",                # unparseable
    ],
)
def test_is_fetchable_url_blocks(url):
    assert is_fetchable_url(url) is False


@pytest.mark.parametrize("url", ["https://example.com/x", "http://public.example.org/a"])
def test_is_fetchable_url_allows_public(url):
    # Deterministic without DNS — a real hostname is not resolved by the string guard.
    assert is_fetchable_url(url) is True


# --- resolve-and-check DNS guard ---------------------------------------------- #
async def test_static_fetch_refuses_host_resolving_to_internal(monkeypatch):
    monkeypatch.setattr(rsafety, "_resolve_ips", lambda host: ["169.254.169.254"])
    page = await StaticHttpFetcher().fetch("http://imds.attacker.example/latest/meta-data/")
    assert not page.ok
    assert "blocked address" in (page.error or "")


def test_host_dns_blocked_short_circuits_ip_literals(monkeypatch):
    # An IP literal is the string guard's job — the DNS guard must not re-lookup it.
    called = {"n": 0}

    def _boom(host):
        called["n"] += 1
        return ["169.254.169.254"]

    monkeypatch.setattr(rsafety, "_resolve_ips", _boom)
    assert rsafety._host_dns_blocked("127.0.0.1") is False
    assert called["n"] == 0


# --- static fetcher: blocked + happy paths (offline) -------------------------- #
async def test_static_fetch_blocked_url_is_honest():
    page = await StaticHttpFetcher().fetch("http://localhost/x")
    assert not page.ok and page.error and not page.has_content()
    assert page.tier == "static"


async def test_static_fetch_reduces_html_and_passes_through_json(monkeypatch):
    def handler(request):
        if request.url.path == "/page":
            return httpx.Response(
                200,
                text="<html><head><title>T</title><script>junk()</script></head>"
                "<body><p>hello world</p></body></html>",
                headers={"content-type": "text/html"},
            )
        return httpx.Response(
            200, text='{"a": 1}', headers={"content-type": "application/json"}
        )

    monkeypatch.setattr(rfetch.httpx, "AsyncClient", _mock_client_factory(handler))
    html_page = await StaticHttpFetcher().fetch("http://public.example.org/page")
    assert html_page.ok and html_page.title == "T"
    assert "hello world" in html_page.text and "junk" not in html_page.text
    json_page = await StaticHttpFetcher().fetch("http://public.example.org/data.json")
    assert json_page.ok and json_page.text == '{"a": 1}'


# --- ladder registry ---------------------------------------------------------- #
class _Rung:
    def __init__(self, name, tier, is_paid=False, cost=0.0):
        self.name = name
        self.tier = tier
        self.is_paid = is_paid
        self.cost_per_call = cost

    async def fetch(self, url, *, want=""):  # pragma: no cover - not called here
        return FetchedPage(url=url, ok=True, text="x", tier=self.name)


def test_ladder_orders_cheapest_tier_first_and_dedupes_by_name():
    register_page_fetcher(_Rung("render", tier=2))
    register_page_fetcher(_Rung("static", tier=0))
    register_page_fetcher(_Rung("static", tier=0))  # same name replaces
    ladder = get_page_fetchers()
    assert [f.name for f in ladder] == ["static", "render"]


def test_register_default_fetchers_and_default_ladder():
    register_default_fetchers()
    ladder = get_page_fetchers()
    assert len(ladder) == 1 and isinstance(ladder[0], StaticHttpFetcher)
    reset_page_fetchers()
    # default_ladder falls back to a lone static fetcher when nothing is registered.
    fallback = default_ladder()
    assert len(fallback) == 1 and isinstance(fallback[0], StaticHttpFetcher)


def test_fetcher_cost_reads_generically():
    assert fetcher_cost(_Rung("static", tier=0)) == (False, 0.0)
    assert fetcher_cost(_Rung("render", tier=2, is_paid=True, cost=0.02)) == (True, 0.02)
    # a positive cost implies paid even if the flag is unset; malformed cost → free.
    assert fetcher_cost(_Rung("x", tier=1, cost=0.01)) == (True, 0.01)
    assert fetcher_cost(_Rung("y", tier=1, cost="bad")) == (False, 0.0)


def test_html_to_text_never_raises_and_keeps_title():
    title, text = html_to_text("<title>Hi</title><body>a<script>x</script>b</body>")
    assert title == "Hi" and "a" in text and "b" in text and "x" not in text
    assert html_to_text("") == ("", "")


# --- convergence contract: shim + research re-export the SAME objects --------- #
def test_shim_reexports_are_substrate_objects():
    # The published research.fetch shim must BE the substrate (identity), so a
    # future re-fork of the fetch layer trips these instead of drifting silently.
    for name in (
        "PageFetcher",
        "StaticHttpFetcher",
        "default_ladder",
        "fetcher_cost",
        "get_page_fetchers",
        "register_default_fetchers",
        "register_page_fetcher",
        "reset_page_fetchers",
    ):
        assert getattr(research_fetch, name) is getattr(rfetch, name), name
    for name in ("is_fetchable_url", "host_dns_blocked", "html_to_text"):
        assert getattr(research_fetch, name) is getattr(rsafety, name), name
    # The private sync DNS guard reached by older tests stays re-exported too.
    # (``_resolve_ips`` is also re-exported for read-compat, but it is the stub
    # tests monkeypatch — patch it on ``retrieval.safety``, not the shim — so an
    # identity assertion on it is intentionally omitted.)
    assert research_fetch._host_dns_blocked is rsafety._host_dns_blocked


def test_fetchedpage_home_is_substrate_and_reexported():
    assert FetchedPage is rtypes.FetchedPage
    assert research_types.FetchedPage is rtypes.FetchedPage
    assert research_fetch.FetchedPage is rtypes.FetchedPage
    assert FetchedPage.__module__ == "cograph_client.retrieval.types"


def test_research_package_reexports_substrate_ladder():
    for name in (
        "StaticHttpFetcher",
        "PageFetcher",
        "fetcher_cost",
        "get_page_fetchers",
        "register_page_fetcher",
        "reset_page_fetchers",
    ):
        assert getattr(research_pkg, name) is getattr(retrieval, name), name


def test_registry_is_shared_across_shim_and_substrate():
    # One ladder, one registry dict — registering through the shim is visible via
    # the substrate and vice versa (the premium render tier registers via the shim).
    research_fetch.register_page_fetcher(_Rung("render", tier=2))
    assert "render" in {f.name for f in retrieval.get_page_fetchers()}
    retrieval.reset_page_fetchers()
    assert research_fetch.get_page_fetchers() == []

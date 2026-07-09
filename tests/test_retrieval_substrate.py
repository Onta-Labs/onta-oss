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


# --- structured-depth: tables + definition lists keep their shape ------------- #
# The ONTA-193 P4 depth RCA: discovery/enrichment/research land name/desc/url but
# rarely the DECLARED STRUCTURED attributes (pricing, latency, NPI/taxonomy) even
# when asked, because a flat HTML→text dump destroys the row→column / label→value
# association those fields live in. These freeze the reshaping that keeps a record
# coherent for the extractor — asserting the MECHANISM (the " | " row join and the
# "term: value" pair), with INVENTED tokens so they can't pass by memorizing a
# real page. The anti-fabrication half asserts the reducer never invents a value
# the page did not contain (unknown → gap, never a hallucinated cell) — a reducer
# that only REFORMATS text cannot regress ONTA-259.

def test_html_to_text_keeps_table_row_column_association():
    # Invented model/price tokens: a pass requires the reshaping, not recall.
    html = (
        "<html><head><title>Zorptech Pricing</title></head><body>"
        "<nav>Home Docs</nav><h1>Plans</h1>"
        "<table>"
        "<tr><th>Model</th><th>Input/1M</th><th>Latency</th></tr>"
        "<tr><td>qwix-9</td><td>$4.44</td><td>88ms</td></tr>"
        "<tr><td>vmb-3</td><td>$0.07</td><td>512ms</td></tr>"
        "</table></body></html>"
    )
    title, text = html_to_text(html)
    assert title == "Zorptech Pricing"
    lines = text.splitlines()
    # MECHANISM: each record is ONE line with its own cells joined by " | " — the
    # declared price/latency stay bound to their model, not scattered vertically.
    assert "qwix-9 | $4.44 | 88ms" in lines
    assert "vmb-3 | $0.07 | 512ms" in lines
    assert "Model | Input/1M | Latency" in lines  # header row preserved too
    # No cross-row bleed: qwix-9's price never glued to vmb-3's row.
    assert not any("$4.44" in ln and "vmb-3" in ln for ln in lines)


def test_html_to_text_keeps_definition_list_label_value_pairs():
    html = (
        "<html><body><div class='card'><dl>"
        "<dt>NPI</dt><dd>4477881122</dd>"
        "<dt>Taxonomy</dt><dd>Zephyrology</dd>"
        "<dt>Affiliation</dt><dd>Vandelay Clinic</dd>"
        "</dl></div></body></html>"
    )
    _title, text = html_to_text(html)
    lines = text.splitlines()
    # MECHANISM: each definition emits "term: value" on one line, so the declared
    # NPI/taxonomy/affiliation attributes carry their label with them.
    assert "NPI: 4477881122" in lines
    assert "Taxonomy: Zephyrology" in lines
    assert "Affiliation: Vandelay Clinic" in lines


def test_html_to_text_preserves_empty_cell_as_gap_never_fabricates():
    # Middle cell is EMPTY. The declared value is genuinely absent → it must stay a
    # visible gap keeping column alignment, never be back-filled from a neighbour
    # or invented (the anti-fabrication contract, ONTA-259). The invented token
    # ``NEVER_ON_PAGE`` must not appear — a reducer cannot add data.
    html = (
        "<table>"
        "<tr><th>Drug</th><th>Dose</th><th>Route</th></tr>"
        "<tr><td>quixil</td><td></td><td>oral</td></tr>"
        "</table>"
    )
    _title, text = html_to_text(html)
    assert "quixil |  | oral" in text.splitlines()  # empty middle cell preserved
    assert "NEVER_ON_PAGE" not in text
    # The gap is NOT filled with the route or the header value.
    assert "quixil | oral | oral" not in text
    assert "quixil | Dose | oral" not in text


def test_html_to_text_no_structure_no_invented_values():
    # A plain prose page carrying NONE of the declared structured data must yield
    # text WITHOUT any price/id-shaped token — the reducer adds nothing.
    html = (
        "<html><head><title>About Us</title></head><body>"
        "<h1>Our Mission</h1><p>We help teams collaborate.</p></body></html>"
    )
    _title, text = html_to_text(html)
    assert "Our Mission" in text and "collaborate" in text
    assert "$" not in text and " | " not in text  # no fabricated table/price


def test_html_to_text_nested_table_never_raises_and_keeps_all_text():
    # A nested table must not crash the parser or drop any cell's text; the inner
    # table flattens into the outer cell (space-joined, never GLUED).
    html = (
        "<table><tr>"
        "<td>outer1 <table><tr><td>innerA</td><td>innerB</td></tr></table></td>"
        "<td>outer2</td>"
        "</tr></table>"
    )
    _title, text = html_to_text(html)
    for token in ("outer1", "innerA", "innerB", "outer2"):
        assert token in text  # no data loss under nesting
    assert "innerAinnerB" not in text  # inner cells space-separated, not glued
    assert "outer2" in text.split("|")[-1]  # outer row structure survives


def test_html_to_text_block_elements_in_cell_do_not_glue_tokens():
    # display:block elements nested inside a <td> (where <dl> structure is
    # suppressed) must space-break, or adjacent block texts GLUE into a value the
    # page never rendered — the anti-fabrication contract (ONTA-259). Invented
    # numeric tokens, so a corrupted glue (``2``+``50`` -> ``250``) would be an
    # obvious fabrication. INLINE runs (<span>/<b>) must STAY joined — that IS how
    # a browser renders them (``12``+``8k`` -> ``128k`` is real, not fabricated).
    def cell(inner: str) -> str:
        return f"<table><tr><td>{inner}</td></tr></table>"

    # <dl> (dt/dd), <form>, and <details> block children — no glue.
    assert "250" not in html_to_text(cell("<dl><dt>2</dt><dd>50</dd></dl>"))[1]
    assert "128k" not in html_to_text(cell("<form>12</form><form>8k</form>"))[1]
    assert "128k" not in html_to_text(
        cell("<details>12</details><details>8k</details>")
    )[1]
    # The real tokens survive, just un-glued (space-separated).
    npi = html_to_text(cell("<dl><dt>NPI</dt><dd>7788991000</dd></dl>"))[1]
    assert "NPI 7788991000" in npi and "NPI7788991000" not in npi
    # INLINE elements are NOT block — their run correctly joins (browser behavior).
    assert "128k" in html_to_text(cell("<span>12</span><span>8k</span>"))[1]
    assert "$2.50" in html_to_text(cell("$<b>2.50</b>"))[1]


# --- robustness: OMITTED end tags must never lose text ------------------------ #
# <td>/<th>/<tr>/<dt>/<dd> END tags are OPTIONAL per the HTML spec and are
# routinely omitted by real CMS / hand-written markup; HTMLParser does NOT insert
# the implied closes. A reducer that buffered until an end tag would silently DROP
# the value on exactly the structured pages this targets — strictly WORSE than a
# flat dump. These freeze that the boundaries are keyed off the always-present
# START tags (+ the non-omittable </table>/</dl>) and flushed at EOF.

def test_html_to_text_table_with_omitted_end_tags_keeps_rows():
    # No </td> or </tr> anywhere — valid HTML. Every record must survive, bound.
    html = "<table><tr><td>wibblex<td>$3.21<tr><td>flarnk<td>$9.99</table>"
    _title, text = html_to_text(html)
    lines = text.splitlines()
    assert "wibblex | $3.21" in lines
    assert "flarnk | $9.99" in lines


def test_html_to_text_dl_with_omitted_end_tags_keeps_pairs():
    # No </dt> or </dd> — the value must not leak into the term buffer and vanish.
    html = "<dl><dt>NPI<dd>5561234000<dt>Taxonomy<dd>Quaxonomy</dl>"
    _title, text = html_to_text(html)
    lines = text.splitlines()
    assert "NPI: 5561234000" in lines
    assert "Taxonomy: Quaxonomy" in lines


def test_html_to_text_truncated_page_flushes_open_buffers_at_eof():
    # A fetch capped mid-cell (StaticHttpFetcher byte cap) ends with an open
    # table/cell — its text must still be emitted, never dropped on EOF.
    html = "<table><tr><td>zonkid</td><td>partial-val-777"  # abruptly cut off
    _title, text = html_to_text(html)
    assert "zonkid" in text
    assert "partial-val-777" in text


def test_html_to_text_one_unclosed_cell_does_not_poison_whole_table():
    # A single missing </td> must not discard the rest of the table.
    html = (
        "<table>"
        "<tr><td>plonktar</td><td>row1b"       # missing </td> and </tr>
        "<tr><td>grubnix</td><td>row2b</td></tr>"
        "</table>"
    )
    _title, text = html_to_text(html)
    for token in ("plonktar", "row1b", "grubnix", "row2b"):
        assert token in text  # nothing dropped


def test_html_to_text_term_with_empty_value_is_not_lost():
    # A <dt> whose <dd> is empty must still surface the term — the flat dump kept
    # it, so the structured reducer must not silently drop it.
    html = "<dl><dt>Blarnix</dt><dd></dd><dt>Grofnak</dt><dd>present</dd></dl>"
    _title, text = html_to_text(html)
    assert "Blarnix" in text                 # term preserved despite empty value
    assert "Grofnak: present" in text.splitlines()


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
    # Private SSRF internals reached by older tests / reserved for compat stay
    # re-exported as the same objects too, so a future re-fork that drops one from
    # the shim trips here. (``_resolve_ips`` is also re-exported for read-compat,
    # but it is the stub tests monkeypatch — patch it on ``retrieval.safety``, not
    # the shim — so an identity assertion on it is intentionally omitted.)
    for name in ("_host_dns_blocked", "_is_blocked_host", "_host_to_ip",
                 "_BLOCKED_HOST_RE", "_TextExtractor"):
        assert getattr(research_fetch, name) is getattr(rsafety, name), name


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


# --- ONE cost seam (P2): source_cost + the three rail aliases delegate to it --- #
class _Src:
    def __init__(self, is_paid=False, cost_per_call=0.0, rows_per_call=0):
        self.is_paid = is_paid
        self.cost_per_call = cost_per_call
        self.rows_per_call = rows_per_call


def test_source_cost_reads_generically_and_never_raises():
    from cograph_client.retrieval import rows_per_call, source_cost

    assert source_cost(_Src()) == (False, 0.0)
    assert source_cost(_Src(is_paid=True, cost_per_call=0.02)) == (True, 0.02)
    # positive cost implies paid even if the flag is unset; negatives clamp to 0.
    assert source_cost(_Src(cost_per_call=0.01)) == (True, 0.01)
    assert source_cost(_Src(cost_per_call=-5)) == (False, 0.0)
    # malformed cost never raises → free.
    assert source_cost(_Src(cost_per_call="bad")) == (False, 0.0)
    assert rows_per_call(_Src(rows_per_call=20)) == 20
    assert rows_per_call(_Src(rows_per_call="x")) == 0


def test_provider_adapter_fetcher_cost_delegate_to_source_cost():
    # The three per-rail cost reducers must be behavioural aliases of the one seam.
    from cograph_client.enrichment.sources.base import adapter_cost
    from cograph_client.retrieval import fetcher_cost, source_cost
    from cograph_client.web_sources.base import provider_cost

    for src in (_Src(), _Src(is_paid=True, cost_per_call=0.017), _Src(cost_per_call=0.5)):
        want = source_cost(src)
        assert provider_cost(src) == want
        assert adapter_cost(src) == want
        assert fetcher_cost(src) == want


# --- per-request error policy (P1-rest) --------------------------------------- #
def test_fetch_error_policy_semantics_and_hierarchy():
    from cograph_client.retrieval import FetchError, FetchErrorPolicy, RetrievalError

    assert FetchErrorPolicy.RAISE.raises is True
    assert FetchErrorPolicy.DEGRADE.raises is False
    assert FetchErrorPolicy.from_raise_on_error(True) is FetchErrorPolicy.RAISE
    assert FetchErrorPolicy.from_raise_on_error(False) is FetchErrorPolicy.DEGRADE
    # A rail that opts into RAISE catches ONE substrate type regardless of provider.
    assert issubclass(FetchError, RetrievalError)
    assert isinstance(FetchError("x"), RetrievalError)

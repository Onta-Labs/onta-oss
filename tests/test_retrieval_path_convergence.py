"""Drift guard (OSS half): every OSS process that consults the web reaches it
through the ONE shared retrieval substrate (``cograph_client/retrieval/``) — the
fetch ladder + SSRF/HTML safety module + the tolerant web-JSON coercers. No OSS
module may re-implement the page fetch / SSRF guard or the tolerant web-response
JSON coercion outside the substrate + its ONE sanctioned delegating wrapper
(ONTA-193 P5, ADR 0008). This is the read-path mirror of
``test_write_path_convergence.py`` and runs standalone in the OSS repo (which has
only ``cograph_client/`` present).

The premium half of the guard (``onta`` parent repo,
``tests/test_retrieval_path_convergence.py``) additionally scans ``cograph/`` for
re-integrated paid web-search/scrape APIs (M3); that can only run where both trees
are checked out. Here we enforce the two OSS-relevant invariants AND assert the
paid endpoints stay premium-only (they must never appear under ``cograph_client/``).

Shape: a deny-by-default source scan (ADR 0007 §4) — scan ALL of
``cograph_client/`` for bespoke-retrieval markers, fail on any hit outside a small
justified allowlist, plus planted-violation self-tests proving the scan fires.
"""

from __future__ import annotations

import inspect
import io
import pathlib
import re
import tokenize

import cograph_client
import cograph_client.enrichment.extraction as extraction_mod


# --- Markers ----------------------------------------------------------------- #
# M1 — a *definition* of a substrate SSRF/HTML/ladder primitive, or a hand-rolled
# DNS-SSRF resolution. ``def``-anchored so a *call* to the ladder seam never trips.
_M1_DEF = re.compile(
    r"(?m)^\s*(?:async\s+)?def\s+"
    r"(is_fetchable_url|host_dns_blocked|_host_dns_blocked|html_to_text|"
    r"_resolve_ips|_is_blocked_host|_host_to_ip|register_page_fetcher|"
    r"get_page_fetchers)\b"
)
_M1_SOCK = re.compile(r"socket\.(inet_aton|getaddrinfo)\s*\(")

# M2 — a *definition* of a tolerant web-JSON coercer, or the outermost array-slice
# reconstruction (``.find("[")`` + ``.rfind("]")``). The object-``{…}`` slice is a
# generic LLM-parse idiom used platform-wide and is deliberately NOT a marker.
_M2_DEF = re.compile(
    r"(?m)^\s*(?:async\s+)?def\s+"
    r"(parse_json_array|parse_json_object|extract_json_array|_try_parse_json)\b"
)
_M2_ARRAY_FIND = re.compile(r"""\.find\(\s*['"]\[['"]\s*\)""")
_M2_ARRAY_RFIND = re.compile(r"""\.rfind\(\s*['"]\]['"]\s*\)""")

# Paid web-search/scrape ENDPOINT hosts — these must NEVER appear under
# cograph_client/ (they are premium-only integrations in cograph/). ``openrouter.ai``
# is the LLM gateway (legit OSS usage) and is intentionally excluded.
_PAID_HOST = re.compile(
    r"api\.exa\.ai|api\.parallel\.ai|serpapi\.com|api\.perplexity\.ai|"
    r"api\.firecrawl\.dev|places\.googleapis\.com|generativelanguage\.googleapis\.com"
)


def _bespoke_markers(code: str) -> list[str]:
    marks: list[str] = []
    if _M1_DEF.search(code) or _M1_SOCK.search(code):
        marks.append("SSRF/fetch-ladder reimpl")
    if _M2_DEF.search(code) or (
        _M2_ARRAY_FIND.search(code) and _M2_ARRAY_RFIND.search(code)
    ):
        marks.append("web-JSON coercion reimpl")
    return marks


# Deny-by-default: the ONLY OSS modules permitted to construct a retrieval marker.
# The research harness (``research/*``) and API source registry (``api_registry/*``)
# consume the ladder / are a registered source layer and construct NO marker — a
# stronger property than allowlisting, so they are deliberately absent.
_ALLOWLIST: dict[str, str] = {
    "retrieval/safety.py": "the ONE SSRF + HTML-safety module — is_fetchable_url / host_dns_blocked / html_to_text / _resolve_ips live here (ONTA-193 §6).",
    "retrieval/fetch.py": "the ONE fetch ladder — defines register_page_fetcher / get_page_fetchers + StaticHttpFetcher; every rail's page fetch routes here.",
    "retrieval/coerce.py": "the ONE tolerant web-JSON seam — parse_json_array (outermost-[…] slice) + parse_json_object.",
    "enrichment/extraction.py": "enrichment _try_parse_json — a thin delegate to retrieval.coerce.parse_json_object (public name kept for importers).",
}

_PKG_ROOT = pathlib.Path(cograph_client.__file__).parent


def _strip_comments(src: str) -> str:
    """Blank out ``#`` COMMENT spans, preserving structure; keep strings/docstrings."""
    lines = src.splitlines(keepends=True)
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return src
    for tok in toks:
        if tok.type != tokenize.COMMENT:
            continue
        (srow, scol), (erow, ecol) = tok.start, tok.end
        if srow == erow:
            line = lines[srow - 1]
            lines[srow - 1] = line[:scol] + " " * (ecol - scol) + line[ecol:]
    return "".join(lines)


def _iter_sources():
    for path in sorted(_PKG_ROOT.rglob("*.py")):
        rel = path.relative_to(_PKG_ROOT)
        if "tests" in rel.parts or "__pycache__" in rel.parts:
            continue
        yield rel.as_posix(), _strip_comments(path.read_text())


# --- Structural tripwire: deny-by-default scan of cograph_client/ ------------- #


def test_no_bespoke_retrieval_outside_allowlist():
    """Scan ALL of ``cograph_client/`` for bespoke-retrieval markers; fail on any
    hit outside the justified allowlist (deny-by-default, ADR 0008 P5)."""
    violations = [
        f"{rel}: {', '.join(m)}"
        for rel, code in _iter_sources()
        if (m := _bespoke_markers(code)) and rel not in _ALLOWLIST
    ]
    assert not violations, (
        "Bespoke web-retrieval markers found OUTSIDE the retrieval-path convergence "
        "allowlist. Route web access through cograph_client/retrieval/ (fetch ladder "
        "+ safety SSRF guards + coerce.parse_json_*), not a hand-rolled copy. "
        "Offenders:\n  " + "\n  ".join(violations)
    )


def test_allowlist_entries_are_live():
    """Every allowlist entry must still exist AND trip a marker (no rot)."""
    stale = []
    for rel in _ALLOWLIST:
        path = _PKG_ROOT / rel
        if not path.exists():
            stale.append(f"{rel} (file missing)")
        elif not _bespoke_markers(_strip_comments(path.read_text())):
            stale.append(f"{rel} (no retrieval markers — remove from _ALLOWLIST)")
    assert not stale, "Stale retrieval-path allowlist entries:\n  " + "\n  ".join(stale)


def test_no_paid_web_search_host_in_oss():
    """The paid web-search/scrape endpoints are premium-only — none may appear under
    cograph_client/ (they belong in cograph/providers|enrichment|firecrawl). The
    ``openrouter.ai`` LLM gateway is intentionally allowed and never matched."""
    offenders = [rel for rel, code in _iter_sources() if _PAID_HOST.search(code)]
    assert not offenders, (
        "Paid web-search/scrape endpoint host found in OSS — these integrations are "
        "premium-only. Offenders:\n  " + "\n  ".join(offenders)
    )


# --- Behavioral: the OSS coercion wrapper delegates, it doesn't re-fork -------- #


def test_extraction_wrapper_delegates_to_substrate():
    src = inspect.getsource(extraction_mod._try_parse_json)
    assert "parse_json_object(" in src, (
        "enrichment/extraction._try_parse_json must delegate to "
        "retrieval.coerce.parse_json_object"
    )
    assert not (_M2_ARRAY_FIND.search(src) and _M2_ARRAY_RFIND.search(src)), (
        "the wrapper re-inlined the outermost-array slice — delegate instead"
    )


def test_substrate_owns_the_primitives():
    from cograph_client.retrieval import (
        is_fetchable_url,
        parse_json_array,
        parse_json_object,
    )

    assert is_fetchable_url.__module__ == "cograph_client.retrieval.safety"
    assert parse_json_array.__module__ == "cograph_client.retrieval.coerce"
    assert parse_json_object.__module__ == "cograph_client.retrieval.coerce"


# --- Guard self-tests: the scan actually catches planted bespoke retrieval ----- #


def test_guard_flags_planted_ssrf_reimpl():
    planted = (
        "def _guard(host):\n"
        "    for info in socket.getaddrinfo(host, None):\n"
        "        if ipaddress.ip_address(info[4][0]).is_private:\n"
        "            return True\n"
    )
    assert "SSRF/fetch-ladder reimpl" in _bespoke_markers(_strip_comments(planted))
    assert "SSRF/fetch-ladder reimpl" in _bespoke_markers(
        _strip_comments("def html_to_text(html):\n    return html\n")
    )


def test_guard_flags_planted_array_coercion():
    planted = (
        "def rows(text):\n"
        "    s = text.strip()\n"
        "    start, end = s.find('['), s.rfind(']')\n"
        "    return json.loads(s[start:end + 1])\n"
    )
    assert "web-JSON coercion reimpl" in _bespoke_markers(_strip_comments(planted))


def test_guard_ignores_delegation_seam_and_llm_object_json():
    # Delegating to the substrate parser is fine.
    assert _bespoke_markers(_strip_comments("def x(t):\n    return parse_json_object(t)\n")) == []
    # Registering a rung via the seam (a call, not a def) is fine.
    assert _bespoke_markers(_strip_comments("register_page_fetcher(F())\n")) == []
    # A generic object-brace LLM-JSON parse is NOT retrieval drift.
    obj = (
        "def p(text):\n"
        "    start, end = text.find('{'), text.rfind('}')\n"
        "    return json.loads(text[start:end + 1])\n"
    )
    assert _bespoke_markers(_strip_comments(obj)) == []


def test_guard_strips_comment_prose():
    planted = "x = 1  # old code had a def is_fetchable_url and a s.find('[')/s.rfind(']')\n"
    assert _bespoke_markers(_strip_comments(planted)) == []


def test_guard_would_deny_a_new_unallowlisted_rail():
    fake_rel = "web_sources/new_scraper.py"
    fake_src = "def is_fetchable_url(u):\n    return True\n"
    marks = _bespoke_markers(_strip_comments(fake_src))
    assert bool(marks) and fake_rel not in _ALLOWLIST

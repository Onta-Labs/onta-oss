"""The fetch ladder — a cheap→expensive tower of page fetchers, and the ONE way
any retrieval rail touches a page (ONTA-193 §3; ADR 0006 §Fetch).

A single fetch method is never enough: static HTTP misses JS-rendered content
("11 of 21 rows"), and a managed browser is too slow/costly to use for
everything. So fetching is a LADDER of :class:`PageFetcher`\\ s ordered by
``tier`` (0 = cheapest). The harness tries the cheapest rung first and escalates
only when the result looks incomplete.

OSS ships exactly one rung: :class:`StaticHttpFetcher` (tier 0) — a plain
``httpx`` GET with a stdlib HTML→text reduction, no paid vendor, byte-capped and
SSRF-guarded (via :mod:`cograph_client.retrieval.safety`). Premium rungs (a
Browserbase/Firecrawl JS-render fetcher at a higher tier, a structured-API
fetcher) register through :func:`register_page_fetcher` and are dormant without
their keys — the same plugin pattern as the enrichment adapters. Cost is read
generically via :func:`fetcher_cost` (defensive ``getattr``), so the ladder never
hardcodes a paid vendor's name.

This module was factored out of ``cograph_client.research.fetch`` (ONTA-166) so
the ladder becomes the substrate's shared fetch layer that discovery, enrichment,
and research all consume, rather than a research-only capability.
``cograph_client.research.fetch`` re-exports these names for published-package
compatibility.

Boundary: OSS. Imports only stdlib / ``cograph_client.*`` / ``httpx``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from urllib.parse import urlparse

import httpx
import structlog

from cograph_client.retrieval.safety import (
    host_dns_blocked,
    html_to_text,
    is_fetchable_url,
)
from cograph_client.retrieval.types import FetchedPage

logger = structlog.stdlib.get_logger("cograph.retrieval.fetch")

# Cap on bytes read off the wire and chars kept from a page — bounds memory,
# cost, and prompt size. A leaderboard/table answer lives well within this.
_MAX_BYTES = 2_000_000
_MAX_CHARS = 40_000
_DEFAULT_TIMEOUT = 20.0
# Redirects are followed MANUALLY (see StaticHttpFetcher.fetch) so each hop is
# re-validated against the SSRF guard — httpx's own follow_redirects would chase
# a 302 → http://127.0.0.1 past the guard. Bounded to stop redirect loops.
_MAX_REDIRECTS = 5
_UA = "Mozilla/5.0 (compatible; OntaResearchBot/1.0; +https://onta.sh/bot)"


@runtime_checkable
class PageFetcher(Protocol):
    """One rung of the fetch ladder.

    * ``name`` — stable id (``static`` / ``render`` / …).
    * ``tier`` — position on the ladder; lower is cheaper and tried first.
    * ``is_paid`` / ``cost_per_call`` — OPTIONAL cost signal, read via
      :func:`fetcher_cost` (defaulted free). Declared for typing/docs.
    * ``fetch(url, want)`` — retrieve one page. ``want`` is an optional
      free-text hint of what to pull (lets a render/extract fetcher target the
      right region). Must NEVER raise: a failed fetch returns
      ``FetchedPage(ok=False, error=...)``.
    """

    name: str
    tier: int
    is_paid: bool
    cost_per_call: float

    async def fetch(self, url: str, *, want: str = "") -> FetchedPage: ...


# Module-level registry — same shape as register_adapter / register_web_source.
_fetchers: dict[str, PageFetcher] = {}


def register_page_fetcher(fetcher: PageFetcher) -> None:
    """Register (or replace) a fetcher by name. Idempotent — last write wins."""
    _fetchers[fetcher.name] = fetcher


def get_page_fetchers() -> list[PageFetcher]:
    """All registered fetchers, ordered cheapest-tier-first (stable by name on
    ties). This IS the ladder the harness walks."""
    return sorted(
        _fetchers.values(),
        key=lambda f: (int(getattr(f, "tier", 0)), str(getattr(f, "name", ""))),
    )


def reset_page_fetchers() -> None:
    """Clear the registry. For tests."""
    _fetchers.clear()


def register_default_fetchers() -> None:
    """Register the OSS default ladder (the static fetcher at tier 0).

    Called at app boot alongside the other default registrations so a plain OSS
    deployment can fetch pages out of the box. Idempotent. A premium plugin adds
    higher rungs (JS render) on top without disturbing this one.
    """
    register_page_fetcher(StaticHttpFetcher())


def default_ladder() -> list[PageFetcher]:
    """The fetch ladder to use: the registered fetchers, or a lone
    :class:`StaticHttpFetcher` when nothing is registered (so the harness works in
    a bare unit test that never boots the app)."""
    fetchers = get_page_fetchers()
    return fetchers or [StaticHttpFetcher()]


def fetcher_cost(fetcher: PageFetcher) -> tuple[bool, float]:
    """Read a fetcher's cost signal generically (mirrors ``provider_cost``).

    Returns ``(is_paid, cost_per_call)``. Defensive ``getattr`` with free
    defaults; never raises on a malformed ``cost_per_call``.
    """
    try:
        cost = float(getattr(fetcher, "cost_per_call", 0.0) or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    if cost < 0.0:
        cost = 0.0
    is_paid = bool(getattr(fetcher, "is_paid", False)) or cost > 0.0
    return is_paid, cost


class StaticHttpFetcher:
    """OSS default fetcher (tier 0): a plain ``httpx`` GET + stdlib HTML→text.

    Cheapest rung of the ladder. Reads at most ``_MAX_BYTES`` off the wire,
    follows redirects MANUALLY (re-validating each hop against the SSRF guard),
    refuses internal hosts, and reduces HTML to plain text (JSON/plain bodies
    pass through). Never raises — a failure returns ``FetchedPage(ok=False,
    error=...)`` so the harness can escalate or move on.
    """

    name = "static"
    tier = 0
    is_paid = False
    cost_per_call = 0.0

    def __init__(self, timeout: float = _DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout

    async def fetch(self, url: str, *, want: str = "") -> FetchedPage:
        if not is_fetchable_url(url):
            return FetchedPage(
                url=url, tier=self.name, ok=False, error="blocked or non-http(s) URL"
            )
        # Resolve-and-check the host before connecting — catches a public name
        # whose DNS points at an internal/metadata address (SSRF the string guard
        # can't see).
        if await host_dns_blocked(urlparse(url).hostname or ""):
            return FetchedPage(
                url=url, tier=self.name, ok=False, error="host resolves to a blocked address"
            )
        content_type = ""
        body = ""
        truncated = False
        current = url
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,  # followed manually below, re-validated per hop
                headers={"User-Agent": _UA, "Accept": "text/html,application/json,*/*"},
            ) as client:
                for _hop in range(_MAX_REDIRECTS + 1):
                    async with client.stream("GET", current) as resp:
                        # Manual redirect handling: re-run the SSRF guard on the
                        # target so a public URL can't 302 us onto an internal one.
                        if resp.is_redirect:
                            location = resp.headers.get("location", "")
                            nxt = (
                                str(httpx.URL(current).join(location))
                                if location
                                else ""
                            )
                            if not nxt or not is_fetchable_url(nxt):
                                return FetchedPage(
                                    url=url,
                                    tier=self.name,
                                    ok=False,
                                    error="redirect to blocked or missing location",
                                )
                            # Re-resolve the redirect target too — a 302 to a
                            # public name that DNS-maps to an internal address.
                            if await host_dns_blocked(urlparse(nxt).hostname or ""):
                                return FetchedPage(
                                    url=url,
                                    tier=self.name,
                                    ok=False,
                                    error="redirect to a host resolving to a blocked address",
                                )
                            current = nxt
                            continue
                        if resp.status_code >= 400:
                            return FetchedPage(
                                url=url,
                                tier=self.name,
                                ok=False,
                                error=f"HTTP {resp.status_code}",
                            )
                        content_type = resp.headers.get("content-type", "").lower()
                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in resp.aiter_bytes():
                            chunks.append(chunk)
                            total += len(chunk)
                            if total >= _MAX_BYTES:
                                truncated = True
                                break
                        body = b"".join(chunks).decode("utf-8", errors="replace")
                        break
                else:
                    return FetchedPage(
                        url=url, tier=self.name, ok=False, error="too many redirects"
                    )
        except Exception as exc:  # network error, timeout, bad TLS, …
            return FetchedPage(url=url, tier=self.name, ok=False, error=str(exc)[:200])

        if "html" in content_type or (not content_type and "<html" in body[:2000].lower()):
            title, text = html_to_text(body)
        else:
            title, text = "", body  # JSON / plain text / CSV pass through

        if len(text) > _MAX_CHARS:
            text = text[:_MAX_CHARS]
            truncated = True

        return FetchedPage(
            url=url, text=text, title=title, tier=self.name, ok=True, truncated=truncated
        )


__all__ = [
    "FetchedPage",
    "PageFetcher",
    "StaticHttpFetcher",
    "default_ladder",
    "fetcher_cost",
    "get_page_fetchers",
    "register_default_fetchers",
    "register_page_fetcher",
    "reset_page_fetchers",
]

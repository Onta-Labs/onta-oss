"""Compatibility shim — the fetch ladder + SSRF/HTML-safety moved to the shared
retrieval substrate (ONTA-193).

The fetch layer (:class:`StaticHttpFetcher`, the ``register_page_fetcher`` ladder
registry, ``fetcher_cost``) and the SSRF + HTML-safety guards
(``is_fetchable_url`` / ``host_dns_blocked`` / ``html_to_text``) used to live
here, as a research-harness-only capability (ONTA-166 / ADR 0006). They are now
the shared **retrieval substrate** (:mod:`cograph_client.retrieval`) that every
rail — discovery, enrichment, research — consults the web through.

This module re-exports the substrate's names unchanged so any existing importer
of ``cograph_client.research.fetch`` (including the published npm/OSS package and
the premium ``cograph.web_sources.plugin`` render-tier registration) keeps
working with no code change — the ADR-0007 "converge with a shim, never
rename-and-delete" pattern. Internal OSS callers should import from
:mod:`cograph_client.retrieval` directly.

NOTE for test authors: the SSRF DNS stub ``_resolve_ips`` and the sync guard
``_host_dns_blocked`` now live in :mod:`cograph_client.retrieval.safety`.
``monkeypatch`` those on that module (not here) — re-binding this shim's imported
name does not reach the implementation.

Boundary: OSS.
"""

from __future__ import annotations

from cograph_client.research.types import FetchedPage
from cograph_client.retrieval.fetch import (
    PageFetcher,
    StaticHttpFetcher,
    default_ladder,
    fetcher_cost,
    get_page_fetchers,
    register_default_fetchers,
    register_page_fetcher,
    reset_page_fetchers,
)
from cograph_client.retrieval.safety import (
    _BLOCKED_HOST_RE,
    _host_dns_blocked,
    _host_to_ip,
    _is_blocked_host,
    _resolve_ips,
    _TextExtractor,
    host_dns_blocked,
    html_to_text,
    is_fetchable_url,
)

__all__ = [
    "FetchedPage",
    "PageFetcher",
    "StaticHttpFetcher",
    "default_ladder",
    "fetcher_cost",
    "get_page_fetchers",
    "host_dns_blocked",
    "html_to_text",
    "is_fetchable_url",
    "register_default_fetchers",
    "register_page_fetcher",
    "reset_page_fetchers",
]

"""``cograph_client.retrieval`` — the shared web-retrieval substrate (ONTA-193).

Enrichment, web discovery, and the research harness are, at bottom, the same
event: **"consult the web and bring information back with citations."** They rode
parallel retrieval stacks that demonstrably drifted (one external API integrated
twice, divergent Firecrawl failure semantics, three cost seams, three provider
protocols, two JSON-coercion helpers). This package is the read-path counterpart
to the write-path convergence (ADR 0007): the ONE substrate every rail consults
the web through.

Doctrine (mirror of write-path convergence):

* **Shared = HOW the web is consulted.** Source selection, search execution, page
  fetch/scrape, pagination/depth/saturation, fan-out/ensemble, structured
  extraction, citation stamping, SSRF guards, cost metadata, budgets, caching,
  tracing — all live here.
* **Separate = WHAT the evidence is used for.** Discovery mints new entities;
  enrichment fills attributes behind a confidence + conflict-policy + staging
  gate; research answers read-only and never writes. Those *decision* layers are
  the ONLY sanctioned divergence — they do not move here.
* Writes stay on the already-converged write path (``graph/kg_writer.py``);
  nothing here writes to a KG.

Convergence lands in phases, each independently shippable (ONTA-193 P0–P5). This
first slice establishes the package and moves the **fetch layer** in: the fetch
ladder (:mod:`cograph_client.retrieval.fetch`) and the SSRF + HTML-safety module
(:mod:`cograph_client.retrieval.safety`) — item 3 + item 6 of the substrate.
They were factored out of ``cograph_client.research.fetch`` unchanged, which now
re-exports them for published-package compatibility. Later phases fold in the
unified source protocol, the single cost/budget seam, the pagination/fan-out
engine, and the calibrated extraction/citation core, then add a deny-by-default
drift guard.

Boundary: OSS. Every module here imports only stdlib / ``cograph_client.*`` /
``httpx``. No ``from cograph.*`` and no proprietary identifiers.
"""

from __future__ import annotations

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
    host_dns_blocked,
    html_to_text,
    is_fetchable_url,
)
from cograph_client.retrieval.types import FetchedPage

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

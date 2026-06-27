"""URL extraction helper shared by the agent capabilities and planner.

The "URL-targeted" web-extraction feature lets a user hand us one or more
explicit links — in the chat message ("enrich these from https://… and
https://…") or as structured context from the Explorer — and have a premium
scraper/agent (e.g. Firecrawl) parse those pages to ingest new entities or
enrich existing ones.

This module is the single place that turns free text into a clean list of URLs,
so the discovery capability, the enrichment capability, and the planner all
recognise links the same way. It is pure, dependency-free, and vendor-neutral
(no scraper is named here) — the actual fetching lives behind the premium
``WebSourceProvider`` / ``SourceAdapter`` seams.
"""

from __future__ import annotations

import re

# Match http(s) URLs. We stop at whitespace and at the common trailing
# punctuation/brackets that surround a link in prose, then strip a trailing run
# of sentence punctuation so "see https://example.com/x." yields ".../x".
_URL_RE = re.compile(r"https?://[^\s<>\"'`\]\)\}]+", re.IGNORECASE)
_TRAILING_PUNCT = ".,;:!?"


def extract_urls(text: str | None) -> list[str]:
    """Return the http(s) URLs found in ``text``, de-duplicated and in order.

    Trailing sentence punctuation is stripped from each match. Order of first
    appearance is preserved and exact duplicates are dropped. Returns ``[]`` for
    empty/``None`` input.
    """
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in _URL_RE.findall(text):
        url = raw.rstrip(_TRAILING_PUNCT)
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


__all__ = ["extract_urls"]

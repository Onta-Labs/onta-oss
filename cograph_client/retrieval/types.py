"""Retrieval substrate types (ONTA-193).

The evidence/result types every retrieval rail shares. Today that is
:class:`FetchedPage` — the result of fetching one URL through a rung of the fetch
ladder — which was defined in ``cograph_client.research.types`` (ONTA-166) when
the fetch ladder was research-only. It moved here so the substrate owns its own
result type with **no upward dependency on the research layer** (the substrate is
the base; research/discovery/enrichment consume it). ``research.types``
re-exports :class:`FetchedPage` for published-package compatibility.

Later phases add the unified ``RetrievalRequest`` / ``Evidence`` contract here;
``DiscoverResult`` and ``Verdict`` become thin projections of ``Evidence`` at the
rail boundary (existing shapes preserved).

Boundary: OSS. Imports only stdlib.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class FetchedPage:
    """The result of fetching one URL through some rung of the fetch ladder.

    ``tier`` records which rung produced it (``static`` / ``render`` /
    ``structured``) for observability + escalation decisions. ``ok=False`` with an
    ``error`` means the fetch failed (timeout, non-200, blocked) as distinct from
    fetching successfully but finding little text.
    """

    url: str
    text: str = ""
    title: str = ""
    tier: str = ""
    ok: bool = True
    error: Optional[str] = None
    truncated: bool = False

    def has_content(self) -> bool:
        return self.ok and bool(self.text and self.text.strip())

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "title": self.title,
            "tier": self.tier,
            "ok": self.ok,
            "error": self.error,
            "truncated": self.truncated,
            "chars": len(self.text or ""),
        }


__all__ = ["FetchedPage"]

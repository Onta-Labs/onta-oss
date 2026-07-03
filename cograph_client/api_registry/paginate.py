"""Declarative pagination for the API registry executor.

The spec declares *which* style an endpoint uses (``page`` / ``offset`` /
``cursor`` / ``next_link`` / ``none``); this module turns that declaration into
concrete per-page request state. It mirrors the pagination *concepts* the web
``source_first`` provider recognizes heuristically, but here the style is known
up front (from the spec) rather than sniffed from the body — so the engine picks
one style and sticks to it, exactly like ``_crawl_json`` does.

The executor owns the fetch loop and the stop conditions that need I/O results
(max_rows / max_pages / empty page); this module is pure: given the current
state + the last response body, produce the next state or ``None`` to stop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .jsonpath import extract_path
from .spec import PaginationSpec, PaginationStyle


@dataclass
class PageState:
    """Everything needed to issue one page request."""

    index: int = 0                                   # 0-based page number
    query: dict[str, str] = field(default_factory=dict)  # pagination params to merge in
    url: Optional[str] = None                        # next_link style: fetch this absolute URL as-is


def _limit_params(pg: PaginationSpec) -> dict[str, str]:
    """The page-size param, if the endpoint declares one (any style)."""
    if pg.limit_param and pg.page_size > 0:
        return {pg.limit_param: str(pg.page_size)}
    return {}


def first_page(pg: PaginationSpec) -> PageState:
    """State for the first request."""
    query = _limit_params(pg)
    if pg.style is PaginationStyle.page and pg.page_param:
        query[pg.page_param] = str(pg.start_page)
    elif pg.style is PaginationStyle.offset and pg.offset_param:
        query[pg.offset_param] = "0"
    return PageState(index=0, query=query, url=None)


def declared_total(pg: PaginationSpec, payload: Any) -> Optional[int]:
    """Best-effort total-result count for ``estimated_total`` (honest, may be an upper bound)."""
    if not pg.total_path:
        return None
    val = extract_path(payload, pg.total_path)
    try:
        total = int(val)
    except (TypeError, ValueError):
        return None
    return total if total >= 0 else None


def next_page(
    pg: PaginationSpec,
    state: PageState,
    payload: Any,
    *,
    rows_on_page: int,
    rows_so_far: int,
    max_rows: int,
) -> Optional[PageState]:
    """Compute the next page's state, or ``None`` to stop.

    Stop conditions handled here (the executor also stops on transport errors):
      * page budget exhausted (``max_pages``)
      * this page returned zero records (natural end / saturation)
      * we already have enough rows (``max_rows``)
      * the style provides no further cursor/link
    """
    if pg.style is PaginationStyle.none:
        return None
    next_index = state.index + 1
    if next_index >= max(1, pg.max_pages):
        return None
    if rows_on_page <= 0:
        return None
    if max_rows > 0 and rows_so_far >= max_rows:
        return None

    base = _limit_params(pg)

    if pg.style is PaginationStyle.page:
        base[pg.page_param] = str(pg.start_page + next_index)
        return PageState(index=next_index, query=base, url=None)

    if pg.style is PaginationStyle.offset:
        base[pg.offset_param] = str(next_index * pg.page_size)
        return PageState(index=next_index, query=base, url=None)

    if pg.style is PaginationStyle.cursor:
        cursor = extract_path(payload, pg.cursor_path)
        cursor = "" if cursor is None else str(cursor).strip()
        if not cursor:
            return None
        base[pg.cursor_param] = cursor
        return PageState(index=next_index, query=base, url=None)

    if pg.style is PaginationStyle.next_link:
        link = extract_path(payload, pg.next_link_path)
        link = "" if link is None else str(link).strip()
        if not link:
            return None
        return PageState(index=next_index, query={}, url=link)

    return None


__all__ = ["PageState", "first_page", "next_page", "declared_total"]

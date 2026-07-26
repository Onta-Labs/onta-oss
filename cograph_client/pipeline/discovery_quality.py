"""Discovery quality gate (pre-write) — website policy + near-duplicate merge.

After A1 shape validators (:mod:`a1_validators`) drop nav-chrome names and
type-invalid cells, this module applies *semantic* cleanliness that shape checks
cannot:

1. **Website policy** — scrub a ``website`` (or alias) cell when it is clearly the
   list/directory page we scraped (``source_url``), a bare path fragment, or a
   wiki/listicle host that is almost never an entity homepage.
2. **Near-duplicate merge** — collapse rows that share a normalized name *or* the
   same registrable website host, keeping the row with more filled plan attrs
   (authority-by-completeness; first-wins is replaced for near-dups only).

Pure OSS: stdlib only, no I/O, no ``from cograph.*``. Unit-testable in isolation.
Called from ``web_ingest_cap`` AFTER A1 validators and BEFORE the SourceBundle so
garbage never reaches structured write or the soft reifier.

Design notes (hyperresearch / crawl4ai learnings):

* empty > wrong for websites (cite-or-abstain)
* independence: one institution, one row (syndication of the same name on two
  pages should not mint two entities)
* does NOT invent values — only scrub and merge
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

__all__ = [
    "QualityVerdict",
    "apply_discovery_quality_gate",
    "scrub_website_policy",
    "merge_near_duplicates",
    "normalize_entity_key",
    "registrable_host",
    "page_yield_score",
    "page_looks_like_list",
]

# Attribute leaves treated as "entity homepage" for website policy.
_WEBSITE_ATTRS = frozenset({
    "website", "url", "homepage", "webpage", "web_site", "home_page", "site",
})

# Hosts that are almost never a real institution homepage when they appear as the
# website cell of a directory scrape (list pages, wiki, social).
_LIST_LIKE_HOST_MARKERS = (
    "wikipedia.org",
    "wikidata.org",
    "medium.com",
    "blogspot.",
    "substack.com",
    "reddit.com",
    "quora.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "youtube.com",
    "pinterest.com",
)

# Path tokens that mark a *directory / list* URL — if website == this shape, scrub.
_LIST_PATH_TOKENS = (
    "/list",
    "/lists/",
    "/directory",
    "/directories",
    "/registry",
    "/roster",
    "/members",
    "/catalogue",
    "/catalog",
    "/index.php",
    "list_of_",
    "list-of-",
    "/wiki/list_of",
    "/wiki/list_of_",
)


def _norm_ws(value: object) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


def normalize_entity_key(value: object) -> str:
    """Collapse a name for near-dup matching: lower, strip punctuation, drop
    common legal/edu suffixes so 'UBC' stays distinct from 'University of X'
    only when the surface forms truly differ after normalization."""
    s = _norm_ws(value).casefold()
    if not s:
        return ""
    # Drop common trailing legal fluff that creates false near-dups.
    s = re.sub(
        r"\b(inc|llc|ltd|corp|corporation|university|college|institute|"
        r"polytechnic|school|the)\b",
        " ",
        s,
    )
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def registrable_host(url_or_host: object) -> str:
    """Best-effort host (lowercase) from a URL or bare host string."""
    raw = _norm_ws(url_or_host)
    if not raw:
        return ""
    if "://" not in raw and not raw.startswith("//"):
        # Bare host or host/path
        candidate = raw.split("/")[0]
        return candidate.casefold().lstrip("www.")
    try:
        host = (urlsplit(raw if "://" in raw else f"https://{raw}").hostname or "")
    except ValueError:
        return ""
    return host.casefold().lstrip("www.")


def _same_page(a: str, b: str) -> bool:
    """True when two URLs point at the same document (ignore scheme, www, fragment,
    trailing slash, query order loosely)."""
    if not a or not b:
        return False
    def _parts(u: str) -> tuple[str, str]:
        try:
            p = urlsplit(u if "://" in u else f"https://{u}")
        except ValueError:
            return ("", u.casefold())
        host = (p.hostname or "").casefold().lstrip("www.")
        path = (p.path or "/").rstrip("/").casefold() or "/"
        return (host, path)
    return _parts(a) == _parts(b)


def _looks_like_list_page_url(url: str) -> bool:
    low = url.casefold()
    host = registrable_host(url)
    if any(m in host for m in _LIST_LIKE_HOST_MARKERS if not m.endswith(".")):
        # wikipedia etc. — homepage of an entity is rarely these hosts *unless*
        # the path is a single article about the entity. List-of pages always scrub.
        if any(tok in low for tok in _LIST_PATH_TOKENS) or "/wiki/list" in low:
            return True
        # Bare wikipedia.org root or category pages
        if "wikipedia.org" in host and ("/wiki/list" in low or "/wiki/category:" in low):
            return True
    if any(tok in low for tok in _LIST_PATH_TOKENS):
        return True
    return False


def _is_website_attr(attr: str) -> bool:
    leaf = attr.casefold().strip()
    if leaf in _WEBSITE_ATTRS:
        return True
    # token match: home_page_url, primary_website
    tokens = set(re.split(r"[^a-z0-9]+", leaf))
    return bool(tokens & _WEBSITE_ATTRS)


def scrub_website_policy(
    row: dict,
    *,
    source_url: Optional[str] = None,
) -> tuple[dict, list[str]]:
    """Return a copy of ``row`` with bad website cells removed, plus scrub reasons.

    Never mutates the input. Non-website attrs are untouched.
    """
    if not isinstance(row, dict):
        return row, []
    src = source_url if source_url is not None else _norm_ws(row.get("source_url"))
    out = dict(row)
    reasons: list[str] = []
    for attr, raw in list(out.items()):
        if attr == "source_url" or not _is_website_attr(str(attr)):
            continue
        val = _norm_ws(raw)
        if not val:
            continue
        # website is the list page we scraped → never an entity homepage
        if src and _same_page(val, src):
            out.pop(attr, None)
            reasons.append(f"{attr} equals list source_url")
            continue
        if _looks_like_list_page_url(val):
            out.pop(attr, None)
            reasons.append(f"{attr} looks like a list/directory URL")
            continue
        # website host is a known list-like host and equals the source host
        # (e.g. both wikipedia) → scrub; entity sites are rarely wikipedia
        src_host = registrable_host(src)
        val_host = registrable_host(val)
        if src_host and val_host and src_host == val_host:
            if any(m in src_host for m in ("wikipedia.org", "medium.com", "blogspot")):
                out.pop(attr, None)
                reasons.append(f"{attr} host is list-source host {val_host!r}")
                continue
    return out, reasons


def _filled_score(row: dict, plan_attrs: list[str]) -> int:
    """Higher = more plan attributes filled (prefer when merging near-dups)."""
    score = 0
    for a in plan_attrs:
        if a == "source_url":
            continue
        if _norm_ws(row.get(a)):
            score += 1
    # Prefer rows that already have a real website
    for a, v in row.items():
        if _is_website_attr(str(a)) and _norm_ws(v):
            score += 2
            break
    return score


def _is_distinctive_entity_host(host: str) -> bool:
    """True when a website host is safe to use as an identity key.

    Multi-tenant / UGC hosts (wikipedia, medium, social) host MANY unrelated
    entities under one registrable domain — linking by host would collapse an
    entire wiki directory into one row (dogfood 2026-07). Institutional hosts
    (ubc.ca, sfu.ca) are distinctive.
    """
    h = (host or "").casefold()
    if not h or "." not in h:
        return False
    if any(m in h for m in _LIST_LIKE_HOST_MARKERS):
        return False
    # gov / edu list portals often share one host for every institution page
    if h.endswith(".gov") or h.endswith(".gov.bc.ca") or "www2.gov." in h:
        return False
    return True


def merge_near_duplicates(
    rows: list[dict],
    key_attr: str,
    *,
    plan_attrs: Optional[list[str]] = None,
) -> tuple[list[dict], int, list[str]]:
    """Collapse near-duplicate rows; keep the richest row per identity cluster.

    Identity signals (any match → same cluster):
      * identical ``normalize_entity_key(name)`` when non-empty
      * identical ``registrable_host(website)`` ONLY when the host is a
        distinctive institutional domain (not wikipedia/medium/gov portals)

    Returns ``(kept, merged_away_count, reasons)``.
    """
    if not rows:
        return [], 0, []
    attrs = list(plan_attrs or [])
    # cluster id → best row
    clusters: dict[str, dict] = {}
    # map distinctive host → cluster id for website-based linking
    host_to_cid: dict[str, str] = {}
    reasons: list[str] = []
    merged = 0

    for row in rows:
        if not isinstance(row, dict):
            continue
        name_key = normalize_entity_key(row.get(key_attr))
        web_host = ""
        for a, v in row.items():
            if _is_website_attr(str(a)):
                candidate = registrable_host(v)
                if candidate and _is_distinctive_entity_host(candidate):
                    web_host = candidate
                    break

        cid: Optional[str] = None
        if name_key and name_key in clusters:
            cid = name_key
        elif web_host and web_host in host_to_cid:
            cid = host_to_cid[web_host]
        elif name_key:
            cid = name_key
        elif web_host:
            cid = f"host:{web_host}"
        else:
            # no identity — keep as unique singleton
            cid = f"anon:{id(row)}"

        if cid in clusters:
            existing = clusters[cid]
            if _filled_score(row, attrs) > _filled_score(existing, attrs):
                # Prefer new; union non-empty cells from existing into gaps
                merged_row = dict(row)
                for k, v in existing.items():
                    if k not in merged_row or not _norm_ws(merged_row.get(k)):
                        if _norm_ws(v):
                            merged_row[k] = v
                clusters[cid] = merged_row
                reasons.append(
                    f"near-dup merge kept richer row for {cid!r}"
                )
            else:
                # Keep existing; fill gaps from new
                for k, v in row.items():
                    if k not in existing or not _norm_ws(existing.get(k)):
                        if _norm_ws(v):
                            existing[k] = v
                reasons.append(
                    f"near-dup merge dropped weaker row for {cid!r}"
                )
            merged += 1
        else:
            clusters[cid] = dict(row)

        if web_host:
            host_to_cid[web_host] = cid

    # Preserve first-seen order of cluster ids as rows arrived
    # (dict preserves insertion order in Py3.7+)
    kept = list(clusters.values())
    return kept, merged, reasons[:20]


@dataclass
class QualityVerdict:
    """Outcome of the discovery quality gate on one batch."""

    rows: list[dict] = field(default_factory=list)
    websites_scrubbed: int = 0
    near_dups_merged: int = 0
    reasons: list[str] = field(default_factory=list)


def apply_discovery_quality_gate(
    rows: list,
    key_attr: str,
    plan_attrs: list[str],
) -> QualityVerdict:
    """Run website policy + near-dup merge on a post-A1 batch.

    Input rows may be plain dicts (already carrying ``source_url``). Returns a
    :class:`QualityVerdict` with cleaned rows. Never raises.
    """
    if not rows:
        return QualityVerdict()
    scrubbed_count = 0
    reasons: list[str] = []
    cleaned: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        fixed, rs = scrub_website_policy(row)
        if rs:
            scrubbed_count += len(rs)
            for r in rs:
                if r not in reasons and len(reasons) < 20:
                    reasons.append(r)
        cleaned.append(fixed)

    merged_rows, n_merged, merge_reasons = merge_near_duplicates(
        cleaned, key_attr, plan_attrs=plan_attrs
    )
    for r in merge_reasons:
        if r not in reasons and len(reasons) < 20:
            reasons.append(r)
    return QualityVerdict(
        rows=merged_rows,
        websites_scrubbed=scrubbed_count,
        near_dups_merged=n_merged,
        reasons=reasons,
    )


# --------------------------------------------------------------------------- #
# Page yield probe (shared idea with crawl4ai prefetch) — pure, for locate_scrape
# --------------------------------------------------------------------------- #

_PIPE_LINE = re.compile(r"\S+\s+\|\s+\S+")


def page_yield_score(text: str, *, key_attr: str = "name", query: str = "") -> float:
    """Heuristic 0..1: how much this page looks like a dense entity list.

    Used as a pre-extract probe so we don't spend an agent call on a nav shell
    or a single-entity marketing page when the goal is enumeration.

    Pipe-delimited tables are a strong positive signal even at small row counts
    (a 4-row directory is still a list page). Nav shells with no table/bullets
    score near zero.
    """
    raw = text or ""
    if not raw.strip():
        return 0.0
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return 0.0
    pipe_lines = sum(1 for ln in lines if " | " in ln and _PIPE_LINE.search(ln))
    bullet_lines = sum(
        1 for ln in lines if ln.startswith(("- ", "* ", "• ", "· ")) and len(ln) > 8
    )
    long_lines = sum(1 for ln in lines if len(ln) >= 24)
    # Query token hits (weak BM25-ish signal)
    q_hits = 0
    if query:
        q_tokens = [t for t in re.split(r"[^a-z0-9]+", query.casefold()) if len(t) > 3]
        low = raw.casefold()
        q_hits = sum(1 for t in q_tokens if t in low)

    # Pure chrome shell — no table, no bullets, almost no substance.
    if pipe_lines < 2 and bullet_lines < 3 and long_lines < 3:
        return min(0.12, long_lines * 0.03)

    # Pipe tables: ≥2 data-ish lines is enough to treat as a list (header + 1 row
    # already beats a nav shell). Scale gently so bigger tables score higher.
    if pipe_lines >= 2:
        table = min(1.0, 0.40 + 0.08 * pipe_lines)  # 2→0.56, 5→0.80, 8+→1.0
        substance = min(1.0, long_lines / 20.0)
        query_fit = min(1.0, q_hits / 4.0) if query else 0.35
        return max(0.0, min(1.0, 0.55 * table + 0.25 * substance + 0.20 * query_fit))

    # Bullet / prose lists
    density = min(1.0, (bullet_lines * 0.5 + long_lines * 0.1) / 6.0)
    substance = min(1.0, long_lines / 30.0)
    query_fit = min(1.0, q_hits / 4.0) if query else 0.3
    return max(0.0, min(1.0, 0.45 * density + 0.35 * substance + 0.20 * query_fit))


def page_looks_like_list(
    text: str,
    *,
    key_attr: str = "name",
    query: str = "",
    min_score: float = 0.25,
) -> bool:
    """True when :func:`page_yield_score` clears ``min_score`` (default 0.25)."""
    return page_yield_score(text, key_attr=key_attr, query=query) >= min_score

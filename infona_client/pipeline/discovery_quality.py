"""Discovery quality gate (pre-write) — website policy + near-duplicate merge.

After A1 shape validators (:mod:`a1_validators`) drop nav-chrome names and
type-invalid cells, this module applies *semantic* cleanliness that shape checks
cannot:

1. **Website policy** — scrub a ``website`` (or alias) cell when it is clearly the
   list/directory page we scraped (``source_url``), a bare path fragment, or a
   wiki/listicle host that is almost never an entity homepage.
2. **Near-duplicate merge** — collapse rows that share structural identity:
   * identical ``normalize_entity_key(name)``
   * identical distinctive registrable website host
   * **catalog-path identity** (R1): a ``segment/segment…`` key clusters with a
     free-text title whose alnum-normalized form equals the path's slug tail
     (or the full path collapsed). Prefer the catalog-path surface as survivor;
     never merge two distinct catalog paths.

Pure OSS: stdlib only, no I/O, no ``from infona.*``. Unit-testable in isolation.
Called from ``web_ingest_cap`` AFTER A1 validators and BEFORE the SourceBundle so
garbage never reaches structured write or the soft reifier.

Design notes (hyperresearch / crawl4ai learnings):

* empty > wrong for websites (cite-or-abstain)
* independence: one institution, one row (syndication of the same name on two
  pages should not mint two entities)
* does NOT invent values — only scrub and merge
* identity is structural — no brand/product/platform allowlists or denylists
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
    "alnum_identity",
    "catalog_path_segments",
    "catalog_identity_key",
    "catalog_surface_keys",
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

# Path *segments* (or wiki slug prefixes) that mark a directory / list URL.
# Matched on path segments after split, NOT as raw substrings — so "/list" does
# not scrub a real homepage under "/listings" or "/listen".
_LIST_PATH_SEGMENTS = frozenset({
    "list",
    "lists",
    "directory",
    "directories",
    "registry",
    "roster",
    "members",
    "catalogue",
    "catalog",
    "index.php",
})
_LIST_SLUG_PREFIXES = (
    "list_of_",
    "list-of-",
    "list_of",
)


def _norm_ws(value: object) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


def normalize_entity_key(value: object) -> str:
    """Collapse a name for near-dup matching: lower, strip punctuation, drop
    legal-entity fluff only (Inc/LLC/Corp).

    Deliberately does **not** strip educational type words (university, college,
    institute, school, …). Those distinguish real institutions
    (St. Mary's College ≠ St. Mary's University; Columbia College ≠ Columbia
    University). Linking "UBC" to "University of British Columbia" is the job of
    a distinctive website host, not of over-normalizing the name.
    """
    s = _norm_ws(value).casefold()
    if not s:
        return ""
    # Leading "the" is noise; legal suffixes only (not edu type words).
    s = re.sub(r"^\s*the\s+", "", s)
    s = re.sub(
        r"\b(inc|llc|ltd|corp|corporation|co|company|plc)\b\.?",
        " ",
        s,
    )
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def alnum_identity(value: object) -> str:
    """Collapse to lowercase alphanumeric only (no spaces/punctuation).

    Used as the surface form of a catalog slug tail or free-text title so
    ``org/model-slug`` can cluster with display title ``Model Slug``.
    """
    return re.sub(r"[^a-z0-9]+", "", _norm_ws(value).casefold())


def catalog_path_segments(value: object) -> Optional[tuple[str, ...]]:
    """Parse a catalog-path identity key: ``segment/segment…`` (≥1 slash).

    Returns non-empty path segments, or ``None`` when the value is free-text
    (no slash), a URL, or otherwise not a structural catalog id.

    Accepts optional leading ``@`` on a segment (package-scope form
    ``@scope/pkg``). Structural only — no host/brand vocabulary.
    """
    raw = _norm_ws(value)
    if not raw or "/" not in raw:
        return None
    if "://" in raw or raw.startswith("//"):
        return None
    # Drop accidental query/fragment if a path-shaped cell carried them.
    raw = raw.split("?", 1)[0].split("#", 1)[0]
    parts = [p for p in raw.split("/") if p]
    if len(parts) < 2:
        return None
    # Free-text titles almost never embed ``/`` with whitespace segments.
    if any((" " in p or "\t" in p) for p in parts):
        return None
    return tuple(parts)


def catalog_identity_key(segments: tuple[str, ...]) -> str:
    """Canonical full-path identity for catalog segments (alnum per segment).

    ``@scope/pkg`` and ``scope/pkg`` share a key; ``a/b-hd`` ≠ ``a/b-turbo``.
    """
    return "/".join(alnum_identity(p.lstrip("@")) for p in segments)


def catalog_surface_keys(segments: tuple[str, ...]) -> list[str]:
    """Surface forms free-text may match: slug tail, then full path collapsed."""
    keys: list[str] = []
    tail = alnum_identity(segments[-1].lstrip("@"))
    if tail:
        keys.append(tail)
    full = "".join(alnum_identity(p.lstrip("@")) for p in segments)
    if full and full not in keys:
        keys.append(full)
    return keys


def _identity_form_rank(row: dict, key_attr: str) -> int:
    """Stronger identity form wins when merging near-dups.

    Catalog-path keys outrank free-text titles (prefer ``org/slug`` over
    ``Slug Title`` as the surviving name).
    """
    if catalog_path_segments(row.get(key_attr)):
        return 2
    if _norm_ws(row.get(key_attr)):
        return 1
    return 0


def _row_has_catalog_identity(row: dict, key_attr: str) -> bool:
    return catalog_path_segments(row.get(key_attr)) is not None


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


def _path_segments(url: str) -> list[str]:
    try:
        path = urlsplit(url if "://" in url else f"https://{url}").path or ""
    except ValueError:
        path = url
    return [p for p in path.casefold().split("/") if p]


def _looks_like_list_page_url(url: str) -> bool:
    low = url.casefold()
    host = registrable_host(url)
    segs = _path_segments(url)
    # Wiki list/category pages (never an entity homepage).
    if "wikipedia.org" in host or "wikidata.org" in host:
        if any(s.startswith("list_of") or s.startswith("list-of") for s in segs):
            return True
        if any(s.startswith("category:") for s in segs):
            return True
        if "/wiki/list" in low:
            return True
    # Path segment is exactly a directory token (not substring of "listings").
    if any(s in _LIST_PATH_SEGMENTS for s in segs):
        return True
    # Wiki-style "list_of_*" slug anywhere in the path.
    if any(s.startswith(p) for s in segs for p in _LIST_SLUG_PREFIXES):
        return True
    return False


def _is_website_attr(attr: str) -> bool:
    """True for entity-homepage attributes — never for citation ``source_url``.

    ``source_url`` contains the token ``url`` but is the list-page citation, not
    a homepage. Treating it as website caused every row on the same source page
    to share one host and near-dup-merge into a single entity (CI failure on
    model-list fixtures).
    """
    leaf = attr.casefold().strip()
    if not leaf or leaf in ("source_url", "source"):
        return False
    if leaf in _WEBSITE_ATTRS:
        return True
    tokens = set(re.split(r"[^a-z0-9]+", leaf))
    if "source" in tokens:
        return False  # source_*, *_source — provenance, not homepage
    # Genuine homepage compounds: primary_website, home_page_url, official_site
    if tokens & {"website", "homepage", "webpage"}:
        return True
    if "url" in tokens and tokens & {"home", "web", "site", "official", "primary"}:
        return True
    return False


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


def _union_row_attrs(preferred: dict, other: dict) -> dict:
    """Copy of ``preferred`` with empty cells filled from ``other``."""
    out = dict(preferred)
    for k, v in other.items():
        if k not in out or not _norm_ws(out.get(k)):
            if _norm_ws(v):
                out[k] = v
    return out


def _pick_survivor(
    existing: dict,
    incoming: dict,
    *,
    key_attr: str,
    plan_attrs: list[str],
) -> dict:
    """Prefer stronger identity form, then richer plan-attr fill; union attrs."""
    r_in = _identity_form_rank(incoming, key_attr)
    r_ex = _identity_form_rank(existing, key_attr)
    if r_in > r_ex:
        return _union_row_attrs(incoming, existing)
    if r_in < r_ex:
        return _union_row_attrs(existing, incoming)
    if _filled_score(incoming, plan_attrs) > _filled_score(existing, plan_attrs):
        return _union_row_attrs(incoming, existing)
    return _union_row_attrs(existing, incoming)


def merge_near_duplicates(
    rows: list[dict],
    key_attr: str,
    *,
    plan_attrs: Optional[list[str]] = None,
) -> tuple[list[dict], int, list[str]]:
    """Collapse near-duplicate rows; keep the best row per identity cluster.

    Identity signals (any match → same cluster):
      * identical ``normalize_entity_key(name)`` when non-empty
      * identical ``registrable_host(website)`` ONLY when the host is a
        distinctive institutional domain (not wikipedia/medium/gov portals)
      * **catalog-path structural identity**: full path key match, or free-text
        surface (alnum-normalized title) equals another row's catalog slug-tail
        / full-path collapsed form. Catalog-path form preferred as survivor.
        Distinct catalog paths (``a/b-hd`` vs ``a/b-turbo``) never merge.

    Returns ``(kept, merged_away_count, reasons)``.
    """
    if not rows:
        return [], 0, []
    attrs = list(plan_attrs or [])
    # cluster id → best row
    clusters: dict[str, dict] = {}
    # secondary indexes → cluster id
    name_to_cid: dict[str, str] = {}
    host_to_cid: dict[str, str] = {}
    catalog_to_cid: dict[str, str] = {}
    # surface (alnum slug-tail / free-text) → cid; ambiguous tails drop out
    surface_to_cid: dict[str, str] = {}
    surface_ambiguous: set[str] = set()
    reasons: list[str] = []
    merged = 0

    def _register_surface(surf: str, cid: str) -> None:
        if not surf or surf in surface_ambiguous:
            return
        prior = surface_to_cid.get(surf)
        if prior is None:
            surface_to_cid[surf] = cid
        elif prior != cid:
            # Two distinct clusters claim the same surface (e.g. same slug tail
            # under different orgs). Free-text must not auto-join either.
            del surface_to_cid[surf]
            surface_ambiguous.add(surf)

    def _distinctive_host(row: dict) -> str:
        for a, v in row.items():
            if _is_website_attr(str(a)):
                candidate = registrable_host(v)
                if candidate and _is_distinctive_entity_host(candidate):
                    return candidate
        return ""

    def _resolve_cid(
        row_obj: dict,
        *,
        segs: Optional[tuple[str, ...]],
        name_key: str,
        web_host: str,
        free_surface: str,
    ) -> str:
        # 1) Exact catalog full-path (never merges distinct paths).
        if segs is not None:
            ck = catalog_identity_key(segs)
            if ck and ck in catalog_to_cid:
                return catalog_to_cid[ck]
        # 2) Normalized full name.
        if name_key and name_key in name_to_cid:
            return name_to_cid[name_key]
        # 3) Distinctive website host.
        if web_host and web_host in host_to_cid:
            return host_to_cid[web_host]
        # 4) Structural surface: free-text ↔ catalog slug-tail / full path.
        #    Catalog rows may only surface-join a free-text cluster (never
        #    another catalog path via shared tail alone).
        if segs is not None:
            for sk in catalog_surface_keys(segs):
                other = surface_to_cid.get(sk)
                if not other or other not in clusters:
                    continue
                if not _row_has_catalog_identity(clusters[other], key_attr):
                    return other
        elif free_surface and free_surface in surface_to_cid:
            return surface_to_cid[free_surface]
        # New cluster identity.
        if segs is not None:
            ck = catalog_identity_key(segs)
            if ck:
                return f"cat:{ck}"
        if name_key:
            return name_key
        if web_host:
            return f"host:{web_host}"
        return f"anon:{id(row_obj)}"

    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_name = row.get(key_attr)
        segs = catalog_path_segments(raw_name)
        name_key = normalize_entity_key(raw_name)
        web_host = _distinctive_host(row)
        free_surface = "" if segs is not None else alnum_identity(raw_name)

        cid = _resolve_cid(
            row,
            segs=segs,
            name_key=name_key,
            web_host=web_host,
            free_surface=free_surface,
        )

        if cid in clusters:
            clusters[cid] = _pick_survivor(
                clusters[cid], row, key_attr=key_attr, plan_attrs=attrs
            )
            reasons.append(f"near-dup merge for {cid!r}")
            merged += 1
        else:
            clusters[cid] = dict(row)

        # Refresh indexes for the (possibly updated) cluster row.
        survivor = clusters[cid]
        s_segs = catalog_path_segments(survivor.get(key_attr))
        s_name = normalize_entity_key(survivor.get(key_attr))
        s_host = _distinctive_host(survivor) or web_host
        if s_name:
            name_to_cid[s_name] = cid
        if name_key:
            name_to_cid[name_key] = cid
        if s_host:
            host_to_cid[s_host] = cid
        if s_segs is not None:
            ck = catalog_identity_key(s_segs)
            if ck:
                catalog_to_cid[ck] = cid
            for sk in catalog_surface_keys(s_segs):
                _register_surface(sk, cid)
        else:
            surf = alnum_identity(survivor.get(key_attr))
            if surf:
                _register_surface(surf, cid)
        # Also index the incoming row's catalog path even when survivor is free-text
        # briefly (should not happen — catalog ranks higher) or vice versa.
        if segs is not None:
            ck = catalog_identity_key(segs)
            if ck:
                catalog_to_cid[ck] = cid
            for sk in catalog_surface_keys(segs):
                _register_surface(sk, cid)
        elif free_surface:
            _register_surface(free_surface, cid)

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

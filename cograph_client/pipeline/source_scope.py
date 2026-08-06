"""Derive structured source_constraint for discovery sub-queries (ONTA-459).

Plan/execute must populate ``source_constraint`` / ``target_registry_ids`` so
registry providers' :meth:`accepts` can skip out-of-scope ensemble slots.

**Structural only — no brand denylists.** Matching uses each provider's own
declared metadata (``registry_slug``, ``served_hosts``, ``title``). A sub-query
that names a source not present in the live ensemble gets an exclusive
"no registry" constraint so catalog APIs do not run for foreign sources.

The orchestrator never hardcodes platform names; it only merges the returned
dict into the per-sub-query provider context.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

__all__ = [
    "REGISTRY_NONE",
    "derive_source_constraint",
    "provider_identity_tokens",
    "has_named_source_signal",
    "has_weak_source_signal",
    "merge_provider_context",
]

# Sentinel registry id: exclusive "no catalog API" (foreign named source).
# RegistryDiscoverySource.accepts treats this as out-of-scope for every catalog.
REGISTRY_NONE = "__none__"

# STRONG source phrasing: introduces a named platform/catalog. Unmatched strong
# signals yield exclusive REGISTRY_NONE (foreign source → no catalog API).
_STRONG_SOURCE_SIGNAL = re.compile(
    r"""
    \b(?:
        offered\s+by | provided\s+by | published\s+by | listed\s+(?:by|on) |
        sold\s+by | hosted\s+(?:by|on) | available\s+(?:on|from|via|at)
    )\s+
    """,
    re.IGNORECASE | re.VERBOSE,
)

# WEAK prepositions: may *positive*-match a live provider token, but never alone
# force exclusive __none__ (avoids "models from 2024" / "doctors at Mayo" over-skip).
_WEAK_SOURCE_SIGNAL = re.compile(
    r"\b(?:from|via|on|at)\s+",
    re.IGNORECASE,
)

# Stopwords / underspecified slug parts that must not become identity tokens.
_TOKEN_STOP = frozenset({
    "api", "www", "com", "org", "net", "io", "ai", "gov", "models", "model",
    "search", "list", "data", "source", "registry", "the", "and", "for", "with",
    # Underspecified multi-segment slug parts (open_food_facts → open/food/facts)
    "open", "food", "facts", "series", "world", "secure", "public", "free",
    "test", "demo", "prod", "dev", "v1", "v2", "http", "https",
})

# Minimum length for underscored slug *parts* (host labels may be 3+).
_MIN_SLUG_PART_LEN = 5


def _norm_ws(value: object) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


def has_named_source_signal(sub_query: str) -> bool:
    """True when a STRONG source phrase is present (exclusive-none eligible)."""
    return bool(_STRONG_SOURCE_SIGNAL.search(sub_query or ""))


def has_weak_source_signal(sub_query: str) -> bool:
    """True when a weak preposition is present (positive-match only)."""
    return bool(_WEAK_SOURCE_SIGNAL.search(sub_query or ""))


def provider_identity_tokens(provider: Any) -> frozenset[str]:
    """Tokens derived **only** from the provider's self-declared metadata.

    Prefers host registrable labels and compact slugs; underspecified short
    slug parts (``open``, ``series``, …) are dropped so they cannot bind the
    wrong catalog.
    """
    tokens: set[str] = set()

    def _add_part(part: str, *, min_len: int = _MIN_SLUG_PART_LEN) -> None:
        p = (part or "").strip().lower()
        if len(p) >= min_len and p not in _TOKEN_STOP:
            tokens.add(p)

    slug = str(getattr(provider, "registry_slug", "") or "").strip().lower()
    if slug:
        for part in re.split(r"[_\-.]+", slug):
            _add_part(part)
        compact = re.sub(r"[^a-z0-9]+", "", slug)
        if len(compact) >= 4 and compact not in _TOKEN_STOP:
            tokens.add(compact)

    hosts = getattr(provider, "served_hosts", None) or ()
    for h in hosts:
        host = str(h or "").strip().lower().lstrip("www.")
        if not host:
            continue
        # registrable label: openrouter.ai → openrouter (allow len>=3 for hosts)
        label = host.split(".")[0]
        if len(label) >= 3 and label not in _TOKEN_STOP:
            tokens.add(label)

    title = str(getattr(provider, "title", "") or "").strip().lower()
    if title:
        for word in re.split(r"[^a-z0-9]+", title):
            if len(word) >= 4 and word not in _TOKEN_STOP:
                tokens.add(word)
                break

    name = str(getattr(provider, "name", "") or "").strip().lower()
    if name.startswith("api:"):
        for part in re.split(r"[_\-.]+", name[4:]):
            _add_part(part)

    return frozenset(tokens)


def _query_mentions_token(query_cf: str, token: str) -> bool:
    if not token or len(token) < 3:
        return False
    return re.search(rf"\b{re.escape(token)}\b", query_cf) is not None


def _matching_registry_providers(
    sub_query: str, providers: Iterable[Any]
) -> list[Any]:
    q = (sub_query or "").casefold()
    if not q:
        return []
    matched: list[Any] = []
    for p in providers:
        slug = str(getattr(p, "registry_slug", "") or "").strip()
        hosts = getattr(p, "served_hosts", None)
        # Only providers that self-declare catalog scope participate.
        if not slug and not hosts:
            continue
        tokens = provider_identity_tokens(p)
        if any(_query_mentions_token(q, t) for t in tokens):
            matched.append(p)
    return matched


def derive_source_constraint(
    sub_query: str,
    providers: Iterable[Any],
) -> dict[str, Any]:
    """Build a ``source_constraint`` dict for one sub-query, or ``{}``.

    Rules:
    1. ≥1 registry provider matches via its own tokens (strong *or* weak signal
       not required for positive match when tokens appear) → bind those catalogs.
    2. STRONG signal present + zero registry matches →
       ``{registry_ids: [REGISTRY_NONE]}`` (foreign source → web/locate only).
    3. WEAK-only signal + zero matches → ``{}`` (unconstrained; avoid over-skip).
    4. No signal + no token match → ``{}``.
    """
    provs = list(providers or [])
    matched = _matching_registry_providers(sub_query, provs)
    if matched:
        return _constraint_from_providers(matched)

    if has_named_source_signal(sub_query):
        # Strong "offered by X" etc. with no live catalog → exclusive none.
        return {"registry_ids": [REGISTRY_NONE]}

    # Weak prepositions alone never force exclusive none.
    return {}


def _constraint_from_providers(matched: list[Any]) -> dict[str, Any]:
    registry_ids: list[str] = []
    hosts: list[str] = []
    for p in matched:
        slug = str(getattr(p, "registry_slug", "") or "").strip().lower()
        if slug:
            registry_ids.append(slug)
        for h in getattr(p, "served_hosts", None) or ():
            hh = str(h or "").strip().lower().lstrip("www.")
            if hh:
                hosts.append(hh)
    out: dict[str, Any] = {}
    if registry_ids:
        seen: set[str] = set()
        uniq = []
        for r in registry_ids:
            if r not in seen:
                seen.add(r)
                uniq.append(r)
        out["registry_ids"] = uniq
    if hosts:
        seen_h: set[str] = set()
        uniq_h = []
        for h in hosts:
            if h not in seen_h:
                seen_h.add(h)
                uniq_h.append(h)
        out["hosts"] = uniq_h
    return out


def merge_provider_context(
    base: Optional[dict],
    sub_query: str,
    providers: Iterable[Any],
) -> dict:
    """Return ``base`` plus derived ``source_constraint`` for *sub_query*.

    Does not overwrite an explicit ``source_constraint`` / ``target_registry_*``
    already set on *base* (tests and advanced callers win).
    """
    ctx = dict(base or {})
    if ctx.get("source_constraint") or ctx.get("target_registry_ids") or ctx.get(
        "target_registry_slugs"
    ) or ctx.get("required_hosts"):
        return ctx
    sc = derive_source_constraint(sub_query, providers)
    if sc:
        ctx["source_constraint"] = sc
    return ctx

"""Canonical IRI base + derived namespace prefixes for every Onta graph URI.

IRIs are opaque primary keys, not HTTP-dereferenced resources. Historically they
were hard-coded under ``https://cograph.tech/…`` (the pre-rebrand product host).
That brand is gone; new graphs should mint under an Onta-shaped base.

Configuration
-------------
``ONTA_IRI_BASE`` (preferred) or ``COGRAPH_IRI_BASE`` (legacy alias) — a bare
HTTPS origin, no trailing slash. Example::

    ONTA_IRI_BASE=https://graph.onta.sh

Default (when unset): ``https://graph.onta.sh``.

**Hosted deployments that still store pre-rename triples must pin the base**
to the host those triples were minted under (typically
``ONTA_IRI_BASE=https://cograph.tech``) until they re-ingest. Changing the base
without re-minting orphans every existing entity / type / graph IRI.

Derived prefixes (always end with ``/`` where the rest of the code expects a
prefix, never a path segment)::

    types/       type schema + literal attribute declarations
    entities/    instance nodes
    graphs/      named graphs (tenant / kg / global layers)
    onto/        relationship INSTANCE edges + a few housekeeping markers
    er/          entity-resolution internals
    attr_meta/   per-attribute provenance companions
    validity/    valid-time companion graph predicates
    suppression/ suppression-list companion graph predicates
    prov/        fact-level provenance companion nodes
    history/     attribute-history version nodes
    gov/         ontology-governance records
    functions/   computed-function registry
    kgs/         KG registry subjects
    meta/        workspace-level meta subjects
"""


from __future__ import annotations

import os
from functools import lru_cache
from urllib.parse import urlparse

# Brand-aligned default for NEW graphs. Not the live Next.js app host (onta.sh
# apex) — that would collide with real App Router paths like /types/Person.
# ``graph.onta.sh`` is an IRI-namespace host only (never served as product UI).
DEFAULT_IRI_BASE = "https://graph.onta.sh"

# Historical hosts that may still appear in few-shot banks, LLM echoes, or
# pre-rename stores. ``normalize_sparql`` rewrites these onto the live base.
LEGACY_IRI_BASES: tuple[str, ...] = (
    "https://cograph.tech",
    "https://omnix.dev",
)


def _normalize_base(raw: str) -> str:
    base = raw.strip().rstrip("/")
    if not base:
        raise ValueError("IRI base must be a non-empty HTTPS origin")
    if "://" not in base:
        base = f"https://{base}"
    parsed = urlparse(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(
            f"IRI base must be an absolute http(s) origin, got {raw!r}"
        )
    if parsed.path not in ("", "/"):
        # Allow a path prefix (e.g. https://example.com/ns) but strip trailing slash.
        base = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
    else:
        base = f"{parsed.scheme}://{parsed.netloc}"
    return base


@lru_cache(maxsize=1)
def iri_base() -> str:
    """Resolved IRI base for this process (import-time env, then default)."""
    raw = (
        os.environ.get("ONTA_IRI_BASE")
        or os.environ.get("COGRAPH_IRI_BASE")
        or DEFAULT_IRI_BASE
    )
    return _normalize_base(raw)


def _base() -> str:
    return iri_base()


def _prefix(segment: str) -> str:
    return f"{_base()}/{segment.strip('/')}/"


# Module-level aliases kept as CALLABLES that re-read the cached base — tests
# that monkeypatch env before first import get the right value; after first
# resolve, the cache sticks for the process (same as every other env config).
# For the common case (constants imported by the rest of the package), we also
# export frozen strings below computed at import time.


def _freeze() -> dict[str, str]:
    b = iri_base()
    return {
        "IRI_BASE": b,
        "TYPE_URI_PREFIX": f"{b}/types/",
        "ENTITY_URI_PREFIX": f"{b}/entities/",
        "GRAPH_URI_PREFIX": f"{b}/graphs/",
        "ONTO_PRED_PREFIX": f"{b}/onto/",
        "ONTO_BASE": f"{b}/onto",  # no trailing slash (historical OMNIX_ONTO)
        "ER_NS": f"{b}/er/",
        "ATTR_META_NS": f"{b}/attr_meta/",
        "VALIDITY_NS": f"{b}/validity/",
        "SUPPRESSION_NS": f"{b}/suppression/",
        "PROV_NS": f"{b}/prov/",
        "HIST_NS": f"{b}/history/",
        "GOV_NS": f"{b}/gov/",
        "FUNCTIONS_PREFIX": f"{b}/functions/",
        "KGS_PREFIX": f"{b}/kgs/",
        "META_PREFIX": f"{b}/meta/",
        "PUBLIC_GRAPH_URI": f"{b}/graphs/global/public",
        "ENHANCED_GRAPH_URI": f"{b}/graphs/global/enhanced",
        "CHANGELOG_GRAPH_URI": f"{b}/graphs/global/changelog",
    }


_F = _freeze()

IRI_BASE: str = _F["IRI_BASE"]
TYPE_URI_PREFIX: str = _F["TYPE_URI_PREFIX"]
ENTITY_URI_PREFIX: str = _F["ENTITY_URI_PREFIX"]
GRAPH_URI_PREFIX: str = _F["GRAPH_URI_PREFIX"]
ONTO_PRED_PREFIX: str = _F["ONTO_PRED_PREFIX"]
ONTO_BASE: str = _F["ONTO_BASE"]
ER_NS: str = _F["ER_NS"]
ATTR_META_NS: str = _F["ATTR_META_NS"]
VALIDITY_NS: str = _F["VALIDITY_NS"]
SUPPRESSION_NS: str = _F["SUPPRESSION_NS"]
PROV_NS: str = _F["PROV_NS"]
HIST_NS: str = _F["HIST_NS"]
GOV_NS: str = _F["GOV_NS"]
FUNCTIONS_PREFIX: str = _F["FUNCTIONS_PREFIX"]
KGS_PREFIX: str = _F["KGS_PREFIX"]
META_PREFIX: str = _F["META_PREFIX"]
PUBLIC_GRAPH_URI: str = _F["PUBLIC_GRAPH_URI"]
ENHANCED_GRAPH_URI: str = _F["ENHANCED_GRAPH_URI"]
CHANGELOG_GRAPH_URI: str = _F["CHANGELOG_GRAPH_URI"]

# Hosts the SPARQL normalizer rewrites onto the live base. Includes every
# historical brand host PLUS the current default so a custom ONTA_IRI_BASE still
# rewrites bank/LLM echoes that used the stock default.
def known_rewrite_hosts() -> tuple[str, ...]:
    hosts = {urlparse(b).netloc for b in LEGACY_IRI_BASES}
    hosts.add(urlparse(DEFAULT_IRI_BASE).netloc)
    hosts.add(urlparse(IRI_BASE).netloc)
    # Drop empty; stable order for regex compile.
    return tuple(sorted(h for h in hosts if h))


def reset_iri_base_cache() -> None:
    """Test-only: drop the lru_cache so a new env var is observed.

    Production code must not call this — IRI base is process-scoped config,
    same as every other OMNIX_* setting.
    """
    iri_base.cache_clear()
    global IRI_BASE, TYPE_URI_PREFIX, ENTITY_URI_PREFIX, GRAPH_URI_PREFIX
    global ONTO_PRED_PREFIX, ONTO_BASE, ER_NS, ATTR_META_NS, VALIDITY_NS
    global SUPPRESSION_NS, PROV_NS, HIST_NS, GOV_NS, FUNCTIONS_PREFIX
    global KGS_PREFIX, META_PREFIX, PUBLIC_GRAPH_URI, ENHANCED_GRAPH_URI
    global CHANGELOG_GRAPH_URI
    f = _freeze()
    IRI_BASE = f["IRI_BASE"]
    TYPE_URI_PREFIX = f["TYPE_URI_PREFIX"]
    ENTITY_URI_PREFIX = f["ENTITY_URI_PREFIX"]
    GRAPH_URI_PREFIX = f["GRAPH_URI_PREFIX"]
    ONTO_PRED_PREFIX = f["ONTO_PRED_PREFIX"]
    ONTO_BASE = f["ONTO_BASE"]
    ER_NS = f["ER_NS"]
    ATTR_META_NS = f["ATTR_META_NS"]
    VALIDITY_NS = f["VALIDITY_NS"]
    SUPPRESSION_NS = f["SUPPRESSION_NS"]
    PROV_NS = f["PROV_NS"]
    HIST_NS = f["HIST_NS"]
    GOV_NS = f["GOV_NS"]
    FUNCTIONS_PREFIX = f["FUNCTIONS_PREFIX"]
    KGS_PREFIX = f["KGS_PREFIX"]
    META_PREFIX = f["META_PREFIX"]
    PUBLIC_GRAPH_URI = f["PUBLIC_GRAPH_URI"]
    ENHANCED_GRAPH_URI = f["ENHANCED_GRAPH_URI"]
    CHANGELOG_GRAPH_URI = f["CHANGELOG_GRAPH_URI"]

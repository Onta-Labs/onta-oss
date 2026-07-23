"""Structured coverage-match predicates for the registry — the ONE gate (ONTA-341).

Whether a catalog entry can *even try* to fill ``attribute`` on an entity of
``entity_type`` is a purely structural question — does the entry declare a
field-mapping column for the attribute, and does its coverage overlap the type?
This module is the single home for that judgement so the two consumers stay in
lock-step:

* :class:`~cograph_client.api_registry.enrichment.RegistrySourceAdapter` calls it
  as its per-lookup **self-gate** (returns ``[]`` for anything it can't answer);
* the scalable selector (``registry_selection.py``, ONTA-341) calls it as the
  **structured pre-filter** stage — the mandatory deterministic narrowing that
  runs BEFORE any semantic vector rank, so a plausible-but-wrong API can never be
  surfaced by similarity alone.

Because both rails share these exact predicates, the pre-filter is byte-for-byte
the self-gate: the selector can only ever REORDER or CAP the set of entries the
self-gate would have admitted — it can never admit one the self-gate rejects, nor
reject one the self-gate would admit at the same (entity_type, attribute). That
equivalence is what makes replacing the O(N) linear self-gating scan with
retrieve-top-K safe.

Pure data + stdlib — no network, no ``cograph.*`` import, no dependency on the
enrichment tier machinery — so it is importable from either rail without a cycle.
"""

from __future__ import annotations

import re

from .spec import ApiSourceSpec

# --------------------------------------------------------------------------- #
# Tokenization (shared by attribute + type matching)
# --------------------------------------------------------------------------- #
_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Word boundaries INSIDE a camelCase / PascalCase identifier: a lower/digit→upper
# transition ("LineItem" → "Line|Item"), and an acronym→word transition
# ("BLSItem" → "BLS|Item"). Split on these BEFORE lowercasing so a PascalCase
# ontology type name tokenizes to the same words a snake_case coverage kind does
# — otherwise "LineItem" collapses to the single token {"lineitem"}, never
# overlaps "line_item"/"food_item"/…, and the registry source is silently skipped
# for exactly the multi-word type names auto-ontology tends to mint.
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
# Generic tokens that must not, ALONE, make an entity type match an entry's
# coverage — otherwise a bare "Organization" would match "health_organization"
# on the shared "organization" token and fire a spurious API call.
_GENERIC_TYPE_TOKENS = frozenset({
    "organization", "org", "provider", "company", "business", "entity",
    "person", "record", "item", "thing", "group", "service",
})


def norm(s: str) -> str:
    """Collapse a name to a comparison key: lower-cased alphanumerics only."""
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def tokens(s: str) -> set[str]:
    """Word set of ``s``, camelCase/PascalCase-split so a PascalCase entity type
    ("LineItem") and a snake_case coverage kind ("line_item") reduce to the same
    words ``{"line", "item"}``."""
    split = _CAMEL_BOUNDARY_RE.sub(" ", s or "")
    return set(_TOKEN_RE.findall(split.lower()))


# --------------------------------------------------------------------------- #
# The structural predicates
# --------------------------------------------------------------------------- #
def has_enrich_params(spec: ApiSourceSpec) -> bool:
    """True iff any endpoint param carries an ``enrich_from`` recipe — i.e. the
    entry can be driven from an existing entity during enrichment at all."""
    return any(p.enrich_from for ep in spec.endpoints for p in ep.params)


def fillable_columns(spec: ApiSourceSpec) -> dict[str, str]:
    """``normalized(field-mapping column) -> canonical column name`` across every
    endpoint of ``spec``. First occurrence of a normalized key wins (mirrors the
    ``setdefault`` the adapter used)."""
    out: dict[str, str] = {}
    for ep in spec.endpoints:
        for col in ep.field_mappings:
            out.setdefault(norm(col), col)
    return out


def fillable_column(spec: ApiSourceSpec, attribute: str) -> str | None:
    """The canonical field-mapping column that produces ``attribute``, else None.
    Case/separator-insensitive via :func:`norm` (so ``postalCode`` matches a
    ``postal_code`` column)."""
    return fillable_columns(spec).get(norm(attribute))


def type_matches(spec: ApiSourceSpec, entity_type: str) -> bool:
    """True iff ``spec``'s coverage plausibly covers ``entity_type``.

    Missing type → don't over-exclude (ONTA-191): rely on the attribute + binding
    gates. Present type → require a token overlap with a coverage ``entity_kind``
    on a NON-generic token, so a bare "Organization" doesn't match
    "health_organization" (and fire a spurious API call) on the shared generic
    "organization" token alone. A fully-generic type name ("Item",
    "Organization" — the shapes auto-ontology mints for a vocabulary-less
    dataset) matches only a coverage kind declared at the SAME generic level
    (every token of the kind present in the type name) — an explicit author
    opt-in to serve generic types.
    """
    if not entity_type:
        return True
    want = tokens(entity_type)
    if not want:
        return True
    if not (want - _GENERIC_TYPE_TOKENS):
        return any(
            kt and kt <= want
            for kt in (tokens(k) for k in spec.coverage.entity_kinds)
        )
    for kind in spec.coverage.entity_kinds:
        overlap = tokens(kind) & want
        if overlap and (overlap - _GENERIC_TYPE_TOKENS):
            return True
    return False


def covers(spec: ApiSourceSpec, entity_type: str, attribute: str) -> bool:
    """Composite structural gate: the entry declares ``attribute`` as a fillable
    column AND its coverage matches ``entity_type``. This is exactly the pair of
    conditions :meth:`RegistrySourceAdapter.lookup` checks before it will issue a
    call, so the selector's pre-filter and the adapter's self-gate never diverge.
    """
    return (
        fillable_column(spec, attribute) is not None
        and type_matches(spec, entity_type)
    )


__all__ = [
    "norm",
    "tokens",
    "has_enrich_params",
    "fillable_columns",
    "fillable_column",
    "type_matches",
    "covers",
]

"""Predicate normalization — canonicalizes relationship predicates against
existing predicates to prevent semantic duplicates like located_in vs is_located_in.

Uses deterministic fuzzy matching (no LLM calls).
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

import structlog

logger = structlog.stdlib.get_logger("infona.resolver.predicate")

# Common prefixes/suffixes that don't change predicate semantics
_STRIP_PREFIXES = ("is_", "has_", "was_", "does_", "can_", "get_")
_STRIP_SUFFIXES = ("_of", "_by", "_for", "_to", "_from", "_in", "_at")

_SIMILARITY_THRESHOLD = 0.85


def _normalize_name(raw: str) -> str:
    """snake_case normalize a predicate name.

    Splits camelCase / PascalCase before lowercasing so ``manufacturedBy``
    becomes ``manufactured_by`` rather than the opaque ``manufacturedby``
    (Oliver label-compliance regression: Drug edges landed as
    ``manufacturedby`` / ``hascompaniondiagnostic``).
    """
    s = raw.strip()
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", s)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", s)
    s = re.sub(r"[^a-zA-Z0-9]", "_", s)
    s = re.sub(r"_+", "_", s).strip("_").lower()
    return s or "unnamed"


def _strip_affixes(name: str) -> str:
    """Strip common prefixes/suffixes for comparison purposes."""
    stripped = name
    for prefix in _STRIP_PREFIXES:
        if stripped.startswith(prefix) and len(stripped) > len(prefix):
            stripped = stripped[len(prefix):]
            break
    for suffix in _STRIP_SUFFIXES:
        if stripped.endswith(suffix) and len(stripped) > len(suffix):
            stripped = stripped[: -len(suffix)]
            break
    return stripped


def normalize_predicate(raw: str, existing_predicates: set[str]) -> str:
    """Normalize a relationship predicate against existing predicates.

    1. snake_case normalize
    2. Exact match against existing → return existing
    3. Strip affixes and fuzzy match → return existing if similarity >= threshold
    4. Otherwise return normalized form

    Args:
        raw: The proposed predicate name.
        existing_predicates: Set of predicate names already on the source type.

    Returns:
        The canonical predicate name.
    """
    normalized = _normalize_name(raw)

    # Exact match
    if normalized in existing_predicates:
        return normalized

    if not existing_predicates:
        return normalized

    # Fuzzy match with affix stripping. Same-core + both-sides-lost-affix
    # is rejected (created_by ↛ created_at) — mirrors attribute_resolver.
    stripped = _strip_affixes(normalized)
    best_match: str | None = None
    best_ratio = 0.0

    for existing in existing_predicates:
        existing_norm = _normalize_name(existing)
        existing_stripped = _strip_affixes(existing_norm)
        if stripped == existing_stripped and normalized != existing_norm:
            proposed_lost = normalized != stripped
            existing_lost = existing_norm != existing_stripped
            if proposed_lost and existing_lost:
                continue
        ratio = SequenceMatcher(None, stripped, existing_stripped).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_match = existing

    if best_ratio >= _SIMILARITY_THRESHOLD and best_match is not None:
        logger.info(
            "predicate_normalized",
            proposed=normalized,
            matched=best_match,
            ratio=round(best_ratio, 3),
        )
        return best_match

    return normalized

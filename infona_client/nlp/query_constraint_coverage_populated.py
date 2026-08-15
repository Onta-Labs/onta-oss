"""Populated-type / zero-instance coverage against live inventory."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def _normalize_type_set(names: Sequence[str] | None) -> set[str]:
    out: set[str] = set()
    for n in names or ():
        s = str(n or "").strip()
        if s:
            out.add(s)
    return out


def resolve_populated_type_set(
    *,
    populated_types: Sequence[str] | None = None,
    type_counts: Mapping[str, int] | None = None,
) -> set[str] | None:
    """Return the set of type names with entity_count > 0, or None if unknown.

    ``None`` means inventory was not supplied — caller must skip the
    zero-instance gate (preserves hermetic tests that omit inventory).
    """
    if type_counts is not None:
        pops: set[str] = set()
        for name, cnt in type_counts.items():
            s = str(name or "").strip()
            if not s:
                continue
            try:
                n = int(cnt)
            except (TypeError, ValueError):
                n = 0
            if n > 0:
                pops.add(s)
        # Also union explicit populated_types when both given.
        pops |= _normalize_type_set(populated_types)
        return pops
    if populated_types is not None:
        return _normalize_type_set(populated_types)
    return None


def plan_primary_types(
    cypher: str,
    params: dict[str, Any] | None = None,
) -> set[str]:
    """Type names the plan uses as primary INSTANCE_OF / $type_names targets.

    Prefers explicit params (templates / confined generators), then falls back
    to :func:`~infona_client.nlp.empty_type_guard.types_referenced`.
    """
    from infona_client.nlp.empty_type_guard import types_referenced

    return set(types_referenced(cypher or "", params))


def zero_instance_type_coverage(
    question: str,
    cypher: str,
    *,
    params: dict[str, Any] | None = None,
    populated_types: Sequence[str] | None = None,
    type_counts: Mapping[str, int] | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """Detect pollution-type primary targets under live inventory.

    Returns ``(empty_plan_types, matched_populated_alternatives)`` when the
    plan's primary types are **all** zero-instance (or unknown-empty) in this
    KG **and** the question matches at least one *other* populated type.
    Returns ``None`` when inventory is missing or the plan is inventory-safe.

    Anti-overfit: pure string/count logic; no product type hardcodes.
    """
    populated = resolve_populated_type_set(
        populated_types=populated_types, type_counts=type_counts
    )
    if populated is None:
        return None
    # No populated inventory at all → nothing to prefer; skip gate.
    if not populated:
        return None

    plan_types = plan_primary_types(cypher, params)
    if not plan_types:
        return None

    # Case-insensitive membership against live inventory.
    pop_by_norm = {n.lower(): n for n in populated}

    def _is_populated(name: str) -> bool:
        return name.lower() in pop_by_norm

    empty_plan = sorted(t for t in plan_types if not _is_populated(t))
    populated_plan = [t for t in plan_types if _is_populated(t)]
    # Any primary type with instances → plan can return rows; not this class.
    if populated_plan:
        return None
    if not empty_plan:
        return None

    # Question matched populated types that are *not* the empty plan targets.
    from infona_client.nlp.query_build import match_question_types

    hits = match_question_types(question, sorted(populated))
    empty_norm = {t.lower() for t in empty_plan}
    alternatives = tuple(h for h in hits if h.lower() not in empty_norm)
    if not alternatives:
        return None
    return (tuple(empty_plan), alternatives)

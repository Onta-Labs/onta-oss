"""Types, protocols, and constants for constraint coverage.

Looked up on :mod:`infona_client.nlp.query_constraint_coverage` at call time via
``_host()`` when a sibling needs a patchable name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from infona_client.nlp.query_intent import QueryIntentSketch


def _host():
    """Call-time lookup of the public query_constraint_coverage module.

    Tests monkeypatch names on ``infona_client.nlp.query_constraint_coverage``.
    Sibling modules must look these up at call time so patches keep working.
    """
    from infona_client.nlp import query_constraint_coverage as _mod

    return _mod


QueryConfidence = Literal["high", "medium", "low"]


@runtime_checkable
class _DimValueLike(Protocol):
    display: str
    normalized: str


@runtime_checkable
class _DimEntryLike(Protocol):
    leaf: str
    kind: str
    subject_type: str

    @property
    def range_type(self) -> str | None: ...


@runtime_checkable
class DimBindLike(Protocol):
    """Minimal protocol for registry binds (avoids hard circular import shape)."""

    token: str
    dim: _DimEntryLike
    matched_value: _DimValueLike


# Templates that *can* carry a dimension filter when params are populated.
_DIM_FILTER_TEMPLATES = frozenset(
    {
        "literal_values",
        "literal_values_count",
        "literal_compare",
        "related_entity_name_filter",
        "related_entity_name_filter_inverse",
    }
)

# Templates that are pure type / unfiltered aggregate — need dim params or
# free-form filters when the question has filter intent.
_MEASURE_ONLY_TEMPLATES = frozenset(
    {
        "literal_aggregate",
    }
)

_PURE_TYPE_TEMPLATES = frozenset(
    {
        "entities_of_type",
        "entities_of_type_count",
        "entity_count_total",
        "entity_count_by_type",
    }
)

# Param keys that bind a dimension / status / compare filter (not the measure).
_DIM_PARAM_KEYS = (
    "prop_value",
    "needle",
    "target_name",
    "threshold",
    "op",
    "rel_attr",
)

# Aggregate shapes in free-form Cypher.
_AGG_RETURN_RE = re.compile(
    r"(?ix)\bRETURN\b[\s\S]{0,200}\b(?:sum|avg|average|min|max|count)\s*\("
)

# Value equality / compare that is not just "raw IS NOT NULL" / prop selection.
_DIM_VALUE_IN_CYPHER_RE = re.compile(
    r"(?ix)"
    r"("
    r"=\s*\$prop_value\b"
    r"|"
    r"=\s*\$needle\b"
    r"|"
    r"=\s*\$target_name\b"
    r"|"
    r"=\s*\$threshold\b"
    r"|"
    r"\b\$op\b"
    r"|"
    r"\bliteral_value\s*(?:=|<>|!=|<=|>=|<|>|=~)"
    r"|"
    r"\be\.[A-Za-z_][A-Za-z0-9_]*\s*(?:=|<>|!=|<=|>=|<|>|=~|CONTAINS)"
    r"|"
    r"(?:CONTAINS|STARTS\s+WITH|ENDS\s+WITH)\s*\("
    r"|"
    r"(?:CONTAINS|STARTS\s+WITH|ENDS\s+WITH)\s+"
    r"|"
    r"=\s*'[^']+'"
    r"|"
    r'=\s*"[^"]+"'
    r")"
)


@dataclass(frozen=True)
class CoverageResult:
    """Outcome of constraint coverage + confidence assignment."""

    ok: bool
    confidence: QueryConfidence
    reason: str
    unbound_tokens: tuple[str, ...] = ()
    bound_tokens: tuple[str, ...] = ()
    clarification_prompt: str = ""
    fail_closed: bool = False
    sketch: QueryIntentSketch | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    # Registry unique binds that are / aren't applied with the correct leaf.
    unbound_dim_binds: tuple[str, ...] = ()  # "leaf=value" labels
    bound_dim_binds: tuple[str, ...] = ()
    # Live inventory: plan types with 0 entities vs question-matched populated.
    empty_plan_types: tuple[str, ...] = ()
    matched_populated_types: tuple[str, ...] = ()

    def to_timing(self) -> dict[str, float | str]:
        """Sparse timing / debug keys for NLResult.timing."""
        out: dict[str, float | str] = {
            "query_confidence": self.confidence,
            "query_confidence_reason": (self.reason or "")[:500],
        }
        if self.clarification_prompt:
            out["clarification_prompt"] = self.clarification_prompt[:500]
        if self.unbound_tokens:
            out["unbound_filter_tokens"] = ", ".join(self.unbound_tokens)[:300]
        if self.bound_tokens:
            out["bound_filter_tokens"] = ", ".join(self.bound_tokens)[:300]
        if self.fail_closed:
            out["query_constraint_fail_closed"] = 1.0
        if self.unbound_dim_binds:
            out["unbound_dim_binds"] = ", ".join(self.unbound_dim_binds)[:300]
        if self.bound_dim_binds:
            out["bound_dim_binds"] = ", ".join(self.bound_dim_binds)[:300]
        if self.empty_plan_types:
            out["empty_plan_types"] = ", ".join(self.empty_plan_types)[:300]
            out["query_zero_instance_type"] = 1.0
        if self.matched_populated_types:
            out["matched_populated_types"] = ", ".join(
                self.matched_populated_types
            )[:300]
        return out

"""Dim-registry constants, data model, thresholds, and pure construction."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

#: Absolute max distinct values for a registered dim (task: ≤ 50).
MAX_DIM_CARDINALITY = 50

#: Floor used with the ratio when type population is small.
MIN_DIM_FLOOR = 20

#: Fraction of type entity count used for the dynamic cap.
DIM_CARDINALITY_RATIO = 0.05

#: Cap how many values we store / show per dim (may equal MAX_DIM_CARDINALITY).
MAX_VALUES_STORED = 50

#: How many values to list in the LLM "known dimensions" block.
MAX_VALUES_IN_PROMPT = 12

#: Reject near-unique leaves (id-like) even under the absolute cap.
HIGH_UNIQUENESS = 0.9
HIGH_UNIQUENESS_MIN_DISTINCT = 10

#: Ambiguity margin: if top-2 scores differ by less than this, fail closed.
BIND_AMBIGUITY_MARGIN = 0.05

#: Min score to accept a bind candidate at all.
BIND_MIN_SCORE = 0.55

#: Edit-distance budget for short labels only (cheap fuzzy; no embeddings).
EDIT_DISTANCE_MAX = 1
EDIT_DISTANCE_MAX_LEN = 12

# Leaf-name filters — free text / ids, never dims (general patterns only).
_ID_LIKE_LEAF_RE = re.compile(
    r"(?i)(^id$|_id$|^uuid$|_uuid$|^guid$|uri$|_uri$|url$|_url$|"
    r"email$|phone$|ssn$|slug$|external_id$)"
)
_FREE_TEXT_LEAF_RE = re.compile(
    r"(?i)(description|summary|comment|note|text$|body$|content|"
    r"abstract|bio$|narrative|memo$|remarks?$)"
)
_NUMERIC_DTYPES = frozenset(
    {
        "int",
        "integer",
        "long",
        "float",
        "double",
        "decimal",
        "number",
        "numeric",
        "money",
        "currency",
    }
)

# Token extraction for multi-filter bind from a question (cheap).
_TOKEN_RE = re.compile(
    r"(?ix)\b(?:for|in|at|with|having|where|from|by|named|status)\s+"
    r"(?:the\s+|a\s+|an\s+)*"
    r"(?P<tok>['\"][^'\"]+['\"]|[A-Za-z][A-Za-z0-9_]*(?:\s+[A-Za-z0-9_]+){0,2})"
)
_QUOTED_RE = re.compile(r"['\"]([^'\"]+)['\"]")
_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DimValue:
    """One observed dim value with normalized + display forms."""

    normalized: str
    display: str


@dataclass(frozen=True, slots=True)
class DimEntry:
    """One low-cardinality dimension on a subject type."""

    subject_type: str
    leaf: str
    kind: str  # "literal_enum" | "entity_dim"
    range_type: str | None = None
    values: tuple[DimValue, ...] = ()
    distinct_count: int = 0
    coverage: int = 0  # entities (or edges) that carry the dim
    type_entity_count: int = 0
    last_refreshed: str = ""  # ISO-8601

    @property
    def dim_id(self) -> str:
        if self.kind == "entity_dim" and self.range_type:
            return f"{self.subject_type}.{self.leaf}->{self.range_type}"
        return f"{self.subject_type}.{self.leaf}"


@dataclass(frozen=True, slots=True)
class DimRegistry:
    """Per-tenant+KG snapshot of low-cardinality dims."""

    tenant_id: str
    kg: str
    dims: tuple[DimEntry, ...] = ()
    last_refreshed: str = ""

    def for_type(self, type_name: str | None) -> tuple[DimEntry, ...]:
        if not type_name:
            return self.dims
        return tuple(d for d in self.dims if d.subject_type == type_name)


@dataclass(frozen=True, slots=True)
class DimBind:
    """A ranked candidate binding of a filter token to a dim value."""

    token: str
    dim: DimEntry
    matched_value: DimValue
    score: float
    match_kind: str = "exact"  # exact | normalized | fuzzy


@dataclass(frozen=True, slots=True)
class DimBindResult:
    """Outcome of :func:`bind_filter_token` (fail-closed unique)."""

    token: str
    unique: DimBind | None
    candidates: tuple[DimBind, ...]
    ambiguous: bool = False

    @property
    def dim(self) -> DimEntry | None:
        return self.unique.dim if self.unique else None


# ---------------------------------------------------------------------------
# Normalization / thresholds
# ---------------------------------------------------------------------------


def normalize_dim_token(text: str) -> str:
    """Casefold + camelSplit + ``_``/space collapse for dim value matching."""
    if not isinstance(text, str):
        return ""
    s = text.strip()
    if not s:
        return ""
    # Split camelCase / PascalCase before lowercasing.
    s = _CAMEL_SPLIT_RE.sub(" ", s)
    s = s.replace("_", " ").replace("-", " ")
    s = s.casefold()
    s = _NON_ALNUM_RE.sub(" ", s)
    return " ".join(s.split())


def dim_cardinality_threshold(type_entity_count: int) -> int:
    """Max distinct values to still treat a leaf as a low-card dim.

    ``min(MAX_DIM_CARDINALITY, max(MIN_DIM_FLOOR, ceil(RATIO * n)))``.
    For large types the absolute 50-cap wins; for small types the floor
    keeps tiny closed sets (seasons, statuses) registerable while the
    high-uniqueness guard rejects free-text id columns.
    """
    n = max(int(type_entity_count or 0), 0)
    dynamic = max(MIN_DIM_FLOOR, int(DIM_CARDINALITY_RATIO * n + 0.999))
    return min(MAX_DIM_CARDINALITY, dynamic) if n > 0 else MIN_DIM_FLOOR


def is_dim_eligible_leaf(
    leaf: str,
    *,
    kind: str = "literal",
    datatype: str | None = None,
) -> bool:
    """True when a leaf name/datatype is not an obvious free-text / id / numeric."""
    if not leaf or not isinstance(leaf, str):
        return False
    name = leaf.strip()
    if not name:
        return False
    from infona_client.graph.facts import (
        RESERVED_ENTITY_PROPERTY_KEYS,
        is_internal_property_key,
    )

    if name in RESERVED_ENTITY_PROPERTY_KEYS:
        return False
    if is_internal_property_key(name):
        return False
    if _ID_LIKE_LEAF_RE.search(name):
        return False
    if kind == "literal" or kind == "literal_enum":
        if _FREE_TEXT_LEAF_RE.search(name):
            return False
        dt = (datatype or "").strip().lower()
        if dt in _NUMERIC_DTYPES:
            return False
        if dt in {"date", "datetime", "time", "boolean", "bool"}:
            return False
    return True


def passes_cardinality_gates(
    *,
    distinct: int,
    type_entity_count: int,
    coverage: int = 0,
) -> bool:
    """True when distinct count qualifies as a low-card dim under thresholds."""
    if distinct <= 0:
        return False
    thr = dim_cardinality_threshold(type_entity_count)
    if distinct > thr:
        return False
    # High-uniqueness free text on modest types: reject.
    n = max(int(type_entity_count or 0), 0)
    if (
        n > 0
        and distinct > HIGH_UNIQUENESS_MIN_DISTINCT
        and (distinct / n) > HIGH_UNIQUENESS
    ):
        return False
    # Need at least one observation.
    if coverage <= 0 and distinct <= 0:
        return False
    return True


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def make_dim_value(raw: str) -> DimValue:
    display = str(raw).strip()
    return DimValue(normalized=normalize_dim_token(display), display=display)


# ---------------------------------------------------------------------------
# Pure registry construction
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DimInventorySlot:
    """One candidate leaf from ontology/instance inventory (pure input)."""

    subject_type: str
    leaf: str
    kind: str  # "literal" | "relationship"
    range_type: str | None = None
    datatype: str | None = None
    values: tuple[str, ...] = ()
    distinct_count: int = 0
    coverage: int = 0
    type_entity_count: int = 0


def build_registry_from_inventory(
    slots: Sequence[DimInventorySlot],
    *,
    tenant_id: str,
    kg: str,
    last_refreshed: str | None = None,
) -> DimRegistry:
    """Build a :class:`DimRegistry` from pure inventory slots (hermetic-friendly).

    Filters by leaf eligibility + cardinality thresholds. Values are capped
    at :data:`MAX_VALUES_STORED`. No GraphStore I/O.
    """
    ts = last_refreshed or _iso_now()
    dims: list[DimEntry] = []
    seen: set[str] = set()
    for slot in slots or ():
        if not slot.subject_type or not slot.leaf:
            continue
        kind_in = (slot.kind or "literal").lower()
        is_rel = kind_in in {"relationship", "entity_dim", "rel"}
        if not is_dim_eligible_leaf(
            slot.leaf,
            kind="relationship" if is_rel else "literal",
            datatype=None if is_rel else slot.datatype,
        ):
            continue
        distinct = int(slot.distinct_count or 0)
        values_raw = list(slot.values or ())
        if distinct <= 0:
            distinct = len({normalize_dim_token(v) for v in values_raw if str(v).strip()})
        if not passes_cardinality_gates(
            distinct=distinct,
            type_entity_count=int(slot.type_entity_count or 0),
            coverage=int(slot.coverage or 0) or distinct,
        ):
            continue
        # Cap stored values; prefer input order then sort by display for stability.
        capped = values_raw[:MAX_VALUES_STORED]
        dim_vals = tuple(
            make_dim_value(v)
            for v in capped
            if str(v).strip() and normalize_dim_token(str(v))
        )
        # Drop empty / blank-normalized
        dim_vals = tuple(v for v in dim_vals if v.normalized)
        if not dim_vals and distinct > 0:
            # Distinct known but values not provided — still register empty set
            # so planners know the leaf is a closed dim (values may refresh later).
            pass
        entry_kind = "entity_dim" if is_rel else "literal_enum"
        entry = DimEntry(
            subject_type=slot.subject_type,
            leaf=slot.leaf,
            kind=entry_kind,
            range_type=slot.range_type if is_rel else None,
            values=dim_vals,
            distinct_count=distinct,
            coverage=int(slot.coverage or 0),
            type_entity_count=int(slot.type_entity_count or 0),
            last_refreshed=ts,
        )
        if entry.dim_id in seen:
            continue
        seen.add(entry.dim_id)
        dims.append(entry)

    dims.sort(key=lambda d: (d.subject_type.lower(), d.leaf.lower(), d.kind))
    return DimRegistry(
        tenant_id=tenant_id,
        kg=kg,
        dims=tuple(dims),
        last_refreshed=ts,
    )


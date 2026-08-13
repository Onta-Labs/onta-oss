"""Low-cardinality dimension registry for NL planning / filter binding.

Tracks **data-driven** dims (literal enums and entity-range relationships)
per tenant+KG so NL can bind filter tokens ("Fall", "NorthFleet") to the
right type/attr/rel. Complements ontology ``[values: …]`` annotations and
constraint-coverage work — never hardcodes domain strings.

Thresholds (documented, tunable via module constants):

* Absolute distinct cap: ``MAX_DIM_CARDINALITY`` (default 50)
* Dynamic cap: ``max(MIN_DIM_FLOOR, ceil(RATIO * type_entity_count))``
* A leaf is a dim only when ``0 < distinct <= min(absolute, dynamic)``
* High uniqueness (``distinct / type_n > HIGH_UNIQUENESS`` with
  ``distinct > HIGH_UNIQUENESS_MIN_DISTINCT``) is treated as id-like and
  rejected even under the cap (small-type free-text trap)

Refresh: process-scoped cache keyed by ``(tenant_id, kg)``. Invalidated
best-effort from :func:`refresh_after_write`; rebuilt lazily on first
``/ask`` (or explicit :func:`refresh_dim_registry`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Optional, Sequence

import structlog

if TYPE_CHECKING:
    from infona_client.graph.store import GraphStore

logger = structlog.stdlib.get_logger("infona.nlp.dim_registry")

# ---------------------------------------------------------------------------
# Thresholds (product-tunable constants — not env-gated for v1)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Bind API
# ---------------------------------------------------------------------------


def _edit_distance(a: str, b: str) -> int:
    """Classic Levenshtein; only used for short strings (caller gates length)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    # Two-row DP
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            ins = cur[j - 1] + 1
            delete = prev[j] + 1
            sub = prev[j - 1] + (0 if ca == cb else 1)
            cur.append(min(ins, delete, sub))
        prev = cur
    return prev[-1]


def _token_overlap(a: str, b: str) -> float:
    ta = set(a.split())
    tb = set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


def _score_value_match(token_norm: str, value: DimValue) -> tuple[float, str] | None:
    """Score one dim value against a normalized token. None = no match."""
    if not token_norm or not value.normalized:
        return None
    if token_norm == value.normalized:
        return 1.0, "exact"
    # Compact form without spaces (northfleet vs north fleet).
    t_c = token_norm.replace(" ", "")
    v_c = value.normalized.replace(" ", "")
    if t_c and t_c == v_c:
        return 0.98, "normalized"
    # Token overlap for multi-word labels.
    ov = _token_overlap(token_norm, value.normalized)
    if ov >= 1.0:
        return 0.95, "normalized"
    if ov >= 0.67 and min(len(token_norm), len(value.normalized)) >= 3:
        return 0.7 + 0.2 * ov, "normalized"
    # Cheap edit distance for short labels only.
    if (
        len(t_c) <= EDIT_DISTANCE_MAX_LEN
        and len(v_c) <= EDIT_DISTANCE_MAX_LEN
        and abs(len(t_c) - len(v_c)) <= EDIT_DISTANCE_MAX
    ):
        dist = _edit_distance(t_c, v_c)
        if dist <= EDIT_DISTANCE_MAX and max(len(t_c), len(v_c)) >= 3:
            return 0.75 - 0.1 * dist, "fuzzy"
    return None


def rank_filter_token_dims(
    token: str,
    registry: DimRegistry | None,
    *,
    type_hint: str | None = None,
) -> list[DimBind]:
    """Rank all dim-value matches for ``token`` (highest score first)."""
    if registry is None or not token or not str(token).strip():
        return []
    token_norm = normalize_dim_token(token)
    if not token_norm:
        return []
    dims = registry.for_type(type_hint) if type_hint else registry.dims
    out: list[DimBind] = []
    for dim in dims:
        best_for_dim: DimBind | None = None
        for val in dim.values:
            scored = _score_value_match(token_norm, val)
            if scored is None:
                continue
            score, kind = scored
            if score < BIND_MIN_SCORE:
                continue
            cand = DimBind(
                token=str(token).strip(),
                dim=dim,
                matched_value=val,
                score=score,
                match_kind=kind,
            )
            if best_for_dim is None or cand.score > best_for_dim.score:
                best_for_dim = cand
        if best_for_dim is not None:
            out.append(best_for_dim)
    out.sort(
        key=lambda b: (
            -b.score,
            -b.dim.coverage,
            b.dim.subject_type,
            b.dim.leaf,
        )
    )
    return out


def bind_filter_token(
    token: str,
    *,
    tenant_id: str | None = None,
    kg: str | None = None,
    type_hint: str | None = None,
    registry: DimRegistry | None = None,
) -> DimBind | None:
    """Bind a filter token to a unique dim, or ``None`` if none / ambiguous.

    Fail-closed: when the top-2 candidates are within
    :data:`BIND_AMBIGUITY_MARGIN`, returns ``None`` rather than silent pick.

    Prefer passing ``registry=`` in hermetic tests. When omitted, looks up the
    process cache for ``(tenant_id, kg)``.
    """
    result = bind_filter_token_result(
        token,
        tenant_id=tenant_id,
        kg=kg,
        type_hint=type_hint,
        registry=registry,
    )
    return result.unique


def bind_filter_token_result(
    token: str,
    *,
    tenant_id: str | None = None,
    kg: str | None = None,
    type_hint: str | None = None,
    registry: DimRegistry | None = None,
) -> DimBindResult:
    """Full bind result including ambiguity flag and ranked candidates."""
    reg = registry
    if reg is None and tenant_id and kg:
        reg = get_cached_dim_registry(tenant_id, kg)
    ranked = rank_filter_token_dims(token, reg, type_hint=type_hint)
    if not ranked:
        return DimBindResult(token=str(token or ""), unique=None, candidates=())
    if len(ranked) == 1:
        return DimBindResult(
            token=str(token).strip(),
            unique=ranked[0],
            candidates=tuple(ranked),
            ambiguous=False,
        )
    top, second = ranked[0], ranked[1]
    if (top.score - second.score) < BIND_AMBIGUITY_MARGIN:
        return DimBindResult(
            token=str(token).strip(),
            unique=None,
            candidates=tuple(ranked),
            ambiguous=True,
        )
    return DimBindResult(
        token=str(token).strip(),
        unique=top,
        candidates=tuple(ranked),
        ambiguous=False,
    )


def extract_filter_tokens(question: str) -> list[str]:
    """Cheap filter-token candidates from a question (no ontology)."""
    if not question or not isinstance(question, str):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _QUOTED_RE.finditer(question):
        tok = m.group(1).strip()
        key = normalize_dim_token(tok)
        if key and key not in seen:
            seen.add(key)
            out.append(tok)
    for m in _TOKEN_RE.finditer(question):
        raw = m.group("tok") or ""
        tok = raw.strip().strip("'\"").strip()
        # Drop pure stopwords / very short.
        key = normalize_dim_token(tok)
        if not key or len(key) < 2:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(tok)
    return out


def bind_tokens_in_question(
    question: str,
    registry: DimRegistry | None,
    *,
    type_hint: str | None = None,
) -> list[DimBind]:
    """Unique binds for filter tokens found in ``question``."""
    if registry is None:
        return []
    binds: list[DimBind] = []
    for tok in extract_filter_tokens(question):
        b = bind_filter_token(tok, registry=registry, type_hint=type_hint)
        if b is not None:
            binds.append(b)
    return binds


# ---------------------------------------------------------------------------
# Prompt formatting
# ---------------------------------------------------------------------------


def format_dims_for_prompt(
    registry: DimRegistry | None,
    *,
    binds: Sequence[DimBind] = (),
    max_dims: int = 24,
) -> str:
    """Short 'known dimensions' block for the Cypher planning prompt."""
    if registry is None or not registry.dims:
        # Still surface unique binds if provided without a full registry dump.
        if not binds:
            return ""
    lines: list[str] = [
        "Known low-cardinality dimensions (data-driven — bind filter tokens",
        "to these leaves/rels with EXACT stored values; still emit Cypher via LLM;",
        "do NOT invent dim values not listed):",
    ]
    shown = 0
    if registry is not None:
        for dim in registry.dims:
            if shown >= max_dims:
                lines.append(f"  … ({len(registry.dims) - shown} more dims omitted)")
                break
            vals = ", ".join(
                f'"{v.display}"' for v in dim.values[:MAX_VALUES_IN_PROMPT]
            )
            more = ""
            if dim.distinct_count > len(dim.values[:MAX_VALUES_IN_PROMPT]):
                more = f" … ({dim.distinct_count} distinct)"
            if dim.kind == "entity_dim":
                range_bit = f" → {dim.range_type}" if dim.range_type else ""
                lines.append(
                    f"  - {dim.subject_type}.{dim.leaf}{range_bit} "
                    f"(entity_dim, n={dim.distinct_count}): {vals}{more}"
                )
            else:
                lines.append(
                    f"  - {dim.subject_type}.{dim.leaf} "
                    f"(literal_enum, n={dim.distinct_count}): {vals}{more}"
                )
            shown += 1
    if binds:
        lines.append("Bound filter tokens from the question (use these when confident):")
        for b in binds:
            d = b.dim
            target = (
                f"{d.leaf}→{d.range_type}" if d.kind == "entity_dim" else d.leaf
            )
            lines.append(
                f"  - {b.token!r} → {d.subject_type}.{target} "
                f"= {b.matched_value.display!r} "
                f"({b.match_kind}, score={b.score:.2f})"
            )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Process-scoped cache
# ---------------------------------------------------------------------------

_REGISTRY_CACHE: dict[tuple[str, str], DimRegistry] = {}


def get_cached_dim_registry(tenant_id: str, kg: str) -> DimRegistry | None:
    if not tenant_id or not kg:
        return None
    return _REGISTRY_CACHE.get((tenant_id, kg))


def put_cached_dim_registry(registry: DimRegistry) -> None:
    if not registry.tenant_id or not registry.kg:
        return
    _REGISTRY_CACHE[(registry.tenant_id, registry.kg)] = registry


def invalidate_dim_registry(
    tenant_id: str | None = None,
    kg: str | None = None,
) -> None:
    """Drop process cache entries. ``(None, None)`` clears all."""
    if tenant_id is None and kg is None:
        _REGISTRY_CACHE.clear()
        return
    if tenant_id is not None and kg is not None:
        _REGISTRY_CACHE.pop((tenant_id, kg), None)
        return
    # Partial: drop all for tenant or all for kg name.
    drop = [
        k
        for k in _REGISTRY_CACHE
        if (tenant_id is not None and k[0] == tenant_id)
        or (kg is not None and k[1] == kg)
    ]
    for k in drop:
        _REGISTRY_CACHE.pop(k, None)


def reset_dim_registry_for_tests() -> None:
    """Clear process cache (test isolation)."""
    _REGISTRY_CACHE.clear()


# ---------------------------------------------------------------------------
# GraphStore-backed refresh
# ---------------------------------------------------------------------------


async def _distinct_literal_values(
    session: Any,
    *,
    primary_type: str,
    prop_key: str,
    limit: int,
) -> list[str]:
    rows = await session.execute_template(
        "entity_type_prop_distinct",
        {
            "primary_type": primary_type,
            "prop_key": prop_key,
            "limit": int(limit),
        },
    )
    out: list[str] = []
    seen: set[str] = set()
    for r in rows or ():
        d = r.to_dict() if hasattr(r, "to_dict") else dict(r)
        val = d.get("value")
        if val is None:
            continue
        s = str(val).strip()
        if not s:
            continue
        key = normalize_dim_token(s)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


async def _distinct_rel_targets(
    session: Any,
    *,
    primary_type: str,
    rel_attr: str,
    limit: int,
) -> tuple[list[str], str | None]:
    rows = await session.execute_template(
        "entity_type_rel_target_distinct",
        {
            "primary_type": primary_type,
            "rel_attr": rel_attr,
            "limit": int(limit),
        },
    )
    out: list[str] = []
    seen: set[str] = set()
    range_type: str | None = None
    for r in rows or ():
        d = r.to_dict() if hasattr(r, "to_dict") else dict(r)
        val = d.get("value")
        if val is None:
            continue
        s = str(val).strip()
        if not s:
            continue
        key = normalize_dim_token(s)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(s)
        if range_type is None:
            rt = d.get("target_type")
            if rt:
                range_type = str(rt)
    return out, range_type


async def collect_dim_inventory_from_store(
    store: "GraphStore",
    *,
    tenant_id: str,
    kg: str,
    type_names: Sequence[str] | None = None,
) -> list[DimInventorySlot]:
    """Probe instance inventory for dim candidates (capped distinct queries).

    Uses type_summary for leaf inventory + allowlisted distinct templates.
    Does **not** full-scan every entity attribute without a type filter.
    """
    if store is None or not tenant_id or not kg:
        return []

    from infona_client.graph.explore_store import type_summary
    from infona_client.graph.ontology_catalog import schema_types_for_kg
    from infona_client.graph.scope import GraphScope

    slots: list[DimInventorySlot] = []
    try:
        rows = await schema_types_for_kg(
            store, tenant_id=tenant_id, kg=kg, include_attrs=True
        )
    except Exception:
        logger.debug("dim_registry_schema_list_failed", exc_info=True)
        rows = []

    wanted = {n for n in (type_names or ()) if n}
    probe: list[str] = []
    for r in rows or ():
        name = getattr(r, "name", None)
        if not name:
            continue
        if wanted and name not in wanted:
            continue
        # Prefer types with instances; still probe force-wanted.
        ent = int(getattr(r, "entity_count", 0) or 0)
        if ent > 0 or (wanted and name in wanted):
            probe.append(name)
    # Also probe wanted names missing from catalog (instance-only types).
    seen = set(probe)
    for n in wanted:
        if n not in seen:
            probe.append(n)
            seen.add(n)

    if not probe:
        return []

    session = store.session(GraphScope.for_instance(tenant_id, kg))
    for tname in probe:
        try:
            summary = await type_summary(
                store=store,
                tenant_id=tenant_id,
                kg_name=kg,
                type_name=tname,
            )
        except Exception:
            summary = None
        if summary is None:
            continue
        type_n = int(getattr(summary, "entity_count", 0) or 0)
        if type_n <= 0:
            continue
        thr = dim_cardinality_threshold(type_n)
        # Oversample slightly so we can detect "above threshold".
        fetch_limit = min(MAX_DIM_CARDINALITY + 5, thr + 5)

        for attr in getattr(summary, "attributes", ()) or ():
            leaf = getattr(attr, "name", None) or ""
            if not leaf:
                continue
            dt = getattr(attr, "datatype", None) or "string"
            if not is_dim_eligible_leaf(leaf, kind="literal", datatype=dt):
                continue
            coverage = int(getattr(attr, "count", 0) or 0)
            if coverage <= 0:
                continue
            try:
                vals = await _distinct_literal_values(
                    session,
                    primary_type=tname,
                    prop_key=leaf,
                    limit=fetch_limit,
                )
            except Exception:
                logger.debug(
                    "dim_registry_literal_distinct_failed",
                    type=tname,
                    leaf=leaf,
                    exc_info=True,
                )
                continue
            slots.append(
                DimInventorySlot(
                    subject_type=tname,
                    leaf=leaf,
                    kind="literal",
                    datatype=dt,
                    values=tuple(vals),
                    distinct_count=len(vals),
                    coverage=coverage,
                    type_entity_count=type_n,
                )
            )

        for rel in getattr(summary, "relationships", ()) or ():
            leaf = getattr(rel, "name", None) or ""
            if not leaf:
                continue
            if not is_dim_eligible_leaf(leaf, kind="relationship"):
                continue
            coverage = int(getattr(rel, "count", 0) or 0)
            if coverage <= 0:
                continue
            range_type = getattr(rel, "target_type", None)
            try:
                vals, inferred_rt = await _distinct_rel_targets(
                    session,
                    primary_type=tname,
                    rel_attr=leaf,
                    limit=fetch_limit,
                )
            except Exception:
                logger.debug(
                    "dim_registry_rel_distinct_failed",
                    type=tname,
                    leaf=leaf,
                    exc_info=True,
                )
                continue
            if not range_type and inferred_rt:
                range_type = inferred_rt
            slots.append(
                DimInventorySlot(
                    subject_type=tname,
                    leaf=leaf,
                    kind="relationship",
                    range_type=str(range_type) if range_type else None,
                    values=tuple(vals),
                    distinct_count=len(vals),
                    coverage=coverage,
                    type_entity_count=type_n,
                )
            )
    return slots


async def refresh_dim_registry(
    store: "GraphStore",
    *,
    tenant_id: str,
    kg: str,
    type_names: Sequence[str] | None = None,
) -> DimRegistry:
    """Rebuild and cache the dim registry for ``tenant_id``+``kg``."""
    slots = await collect_dim_inventory_from_store(
        store, tenant_id=tenant_id, kg=kg, type_names=type_names
    )
    reg = build_registry_from_inventory(slots, tenant_id=tenant_id, kg=kg)
    put_cached_dim_registry(reg)
    logger.info(
        "dim_registry_refreshed",
        tenant_id=tenant_id,
        kg=kg,
        dims=len(reg.dims),
    )
    return reg


async def ensure_dim_registry(
    store: "GraphStore | None",
    *,
    tenant_id: str,
    kg: str,
    force: bool = False,
) -> DimRegistry | None:
    """Return cached registry or rebuild lazily (best-effort)."""
    if not tenant_id or not kg:
        return None
    if not force:
        cached = get_cached_dim_registry(tenant_id, kg)
        if cached is not None:
            return cached
    if store is None:
        return None
    try:
        return await refresh_dim_registry(store, tenant_id=tenant_id, kg=kg)
    except Exception:
        logger.debug("dim_registry_ensure_failed", exc_info=True)
        return get_cached_dim_registry(tenant_id, kg)


async def planning_dim_binds(
    store: "GraphStore | None",
    *,
    tenant_id: str,
    kg: str,
    question: str,
    type_hint: str | None = None,
) -> list[DimBind]:
    """Ensure registry + return unique :class:`DimBind` list for the question.

    Same bind path as the planning prompt (``bind_tokens_in_question``).
    Callers that only need structured binds for post-gen coverage use this;
    ambiguous tokens are omitted (fail-closed unique only).
    """
    reg = await ensure_dim_registry(store, tenant_id=tenant_id, kg=kg)
    if reg is None or not reg.dims:
        return []
    return bind_tokens_in_question(question, reg, type_hint=type_hint)


async def planning_dim_context(
    store: "GraphStore | None",
    *,
    tenant_id: str,
    kg: str,
    question: str,
    type_hint: str | None = None,
) -> tuple[str, list[DimBind]]:
    """Ensure registry once; return ``(prompt_text, unique_binds)`` for /ask.

    Prefer this over calling :func:`planning_dim_grounding` +
    :func:`planning_dim_binds` separately so bind lists stay consistent
    between prompt grounding and constraint-coverage gates.
    """
    reg = await ensure_dim_registry(store, tenant_id=tenant_id, kg=kg)
    if reg is None or not reg.dims:
        return "", []
    binds = bind_tokens_in_question(question, reg, type_hint=type_hint)
    return format_dims_for_prompt(reg, binds=binds), binds


async def planning_dim_grounding(
    store: "GraphStore | None",
    *,
    tenant_id: str,
    kg: str,
    question: str,
    type_hint: str | None = None,
) -> str:
    """Ensure registry + format prompt block for /ask grounding spine."""
    text, _binds = await planning_dim_context(
        store,
        tenant_id=tenant_id,
        kg=kg,
        question=question,
        type_hint=type_hint,
    )
    return text


__all__ = [
    "BIND_AMBIGUITY_MARGIN",
    "DIM_CARDINALITY_RATIO",
    "DimBind",
    "DimBindResult",
    "DimEntry",
    "DimInventorySlot",
    "DimRegistry",
    "DimValue",
    "MAX_DIM_CARDINALITY",
    "MIN_DIM_FLOOR",
    "bind_filter_token",
    "bind_filter_token_result",
    "bind_tokens_in_question",
    "build_registry_from_inventory",
    "collect_dim_inventory_from_store",
    "dim_cardinality_threshold",
    "ensure_dim_registry",
    "extract_filter_tokens",
    "format_dims_for_prompt",
    "get_cached_dim_registry",
    "invalidate_dim_registry",
    "is_dim_eligible_leaf",
    "normalize_dim_token",
    "passes_cardinality_gates",
    "planning_dim_binds",
    "planning_dim_context",
    "planning_dim_grounding",
    "put_cached_dim_registry",
    "rank_filter_token_dims",
    "refresh_dim_registry",
    "reset_dim_registry_for_tests",
]

"""Deterministic table profiler — ADR 0003 Pass A.

Computes the statistical evidence that schema inference decisions must be
grounded in: per-column completeness/cardinality/uniqueness/value-shape and
cross-column functional dependencies (mutual FD = columns describing one
entity, e.g. code<->title pairs).

Pure function over in-memory rows: stdlib only, no LLM, no I/O — and, by
design, ZERO domain knowledge. Every rule here is structural/statistical;
nothing inspects column names or value vocabulary (ADR 0003 litmus test).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from cograph_client.resolver.models import ColumnProfile, TableProfile, ValueShape

# --- Derived-flag thresholds (structural, validated in ADR 0003) -----------
_KEY_MIN = 0.99          # complete_unique_key: completeness AND uniqueness above this
_INCOMPLETE_MAX = 0.98   # incomplete: completeness below this drops rows if keyed on
_LOW_CARD_MAX = 0.5      # low_cardinality_repeated: card_ratio below this

# --- Functional-dependency bounds -------------------------------------------
# FDs are O(pairs x rows); only dimension-sized columns are candidates.
_FD_MAX_DISTINCT = 400
# Minimum co-non-empty rows for an FD to be evidence rather than coincidence.
_FD_MIN_SUPPORT = 5
# Skip near-unique determinants: a column whose values are (almost) row-unique
# trivially "determines" every other column, which is noise, not structure.
_FD_NEAR_UNIQUE = 0.95

# --- Value-shape heuristics (fractions over non-empty sample values) -------
_DATE_RE = re.compile(
    r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}|^\d{1,2}[-/]\d{1,2}[-/]\d{2,4}"
)
_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_WS_RE = re.compile(r"\s")
_DATE_MIN_FRAC = 0.8
_NUMBER_MIN_FRAC = 0.8
_CODE_MIN_FRAC = 0.9
_CODE_MAX_AVG_LEN = 24
_TEXT_MIN_SPACE_FRAC = 0.5
_TEXT_MIN_AVG_LEN = 25


def profile_table(
    headers: list[str],
    rows: list[dict[str, Any]],
    total_rows: int = 0,
) -> TableProfile:
    """Profile sample rows (whatever the client sent to /ingest/csv/schema).

    `rows` cells may be anything JSON delivers — strings, numbers, booleans,
    None, lists — not just CSV strings. `total_rows` is the declared size of
    the full file, so downstream knows what fraction the sample covers; it
    defaults to len(rows) when unset.
    """
    rows_profiled = len(rows)

    # Normalize once into aligned per-column value lists ('' = empty/missing).
    norm_cols: dict[str, list[str]] = {h: [] for h in headers}
    for row in rows:
        for h in headers:
            norm_cols[h].append(_norm_cell(row.get(h)))

    columns: list[ColumnProfile] = []
    distinct_by_col: dict[str, int] = {}
    for h in headers:
        counts = Counter(v for v in norm_cols[h] if v)
        non_empty = sum(counts.values())
        distinct = len(counts)
        distinct_by_col[h] = distinct
        completeness = non_empty / rows_profiled if rows_profiled else 0.0
        uniqueness = distinct / non_empty if non_empty else 0.0
        card_ratio = distinct / rows_profiled if rows_profiled else 0.0
        columns.append(
            ColumnProfile(
                name=h,
                completeness=completeness,
                distinct=distinct,
                uniqueness=uniqueness,
                card_ratio=card_ratio,
                value_shape=_value_shape(counts, non_empty),
                examples=[v for v, _ in counts.most_common(3)],
                complete_unique_key=completeness > _KEY_MIN and uniqueness > _KEY_MIN,
                incomplete=completeness < _INCOMPLETE_MAX,
                low_cardinality_repeated=(
                    distinct > 1
                    and card_ratio < _LOW_CARD_MAX
                    and non_empty > distinct  # values actually repeat
                ),
            )
        )

    fd_mutual, fd_oneway = _functional_dependencies(headers, norm_cols, distinct_by_col)

    return TableProfile(
        rows_profiled=rows_profiled,
        total_rows=total_rows or rows_profiled,
        columns=columns,
        fd_mutual=fd_mutual,
        fd_oneway=fd_oneway,
    )


def _norm_cell(value: Any) -> str:
    """Normalize one cell to a comparison string; '' means empty/missing.

    Rows arrive from JSON, so cells may be numbers, booleans, None, or lists —
    not just CSV strings."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple, dict)):
        if not value:
            return ""
        try:
            return json.dumps(value, default=str, sort_keys=True)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _is_number(v: str) -> bool:
    try:
        float(v)
    except ValueError:
        return False
    return True


def _value_shape(counts: Counter[str], non_empty: int) -> ValueShape:
    """Classify a column's value shape from its distinct values, weighted by
    frequency (equivalent to scanning every value, but O(distinct))."""
    if non_empty == 0:
        return ValueShape.EMPTY
    date_n = number_n = code_n = space_n = total_len = 0
    for v, c in counts.items():
        if _DATE_RE.match(v):
            date_n += c
        if _is_number(v):
            number_n += c
        if _CODE_RE.match(v):
            code_n += c
        if _WS_RE.search(v):
            space_n += c
        total_len += len(v) * c
    avg_len = total_len / non_empty
    if date_n / non_empty > _DATE_MIN_FRAC:
        return ValueShape.DATE
    if number_n / non_empty > _NUMBER_MIN_FRAC:
        return ValueShape.NUMBER
    if code_n / non_empty > _CODE_MIN_FRAC and avg_len <= _CODE_MAX_AVG_LEN:
        return ValueShape.CODE_ID
    if space_n / non_empty > _TEXT_MIN_SPACE_FRAC and avg_len > _TEXT_MIN_AVG_LEN:
        return ValueShape.TEXT
    return ValueShape.LABEL


def _functional_dependencies(
    headers: list[str],
    norm_cols: dict[str, list[str]],
    distinct_by_col: dict[str, int],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Find cross-column FDs among dimension-sized columns.

    A->B holds when every non-empty A value co-occurs with exactly one
    non-empty B value, over enough rows to be evidence (support >=
    _FD_MIN_SUPPORT), with >1 distinct value on both sides (a constant column
    neither determines nor is meaningfully determined), and A not near-unique
    (a unique column trivially "determines" everything).

    Returns (fd_mutual, fd_oneway): mutual pairs in header order, one-way
    pairs as (determinant, dependent).
    """
    candidates = [h for h in headers if 0 < distinct_by_col[h] <= _FD_MAX_DISTINCT]
    mutual: list[tuple[str, str]] = []
    oneway: list[tuple[str, str]] = []
    for i, a in enumerate(candidates):
        for b in candidates[i + 1 :]:
            a_to_b, b_to_a = _fd_pair(norm_cols[a], norm_cols[b])
            if a_to_b and b_to_a:
                mutual.append((a, b))
            elif a_to_b:
                oneway.append((a, b))
            elif b_to_a:
                oneway.append((b, a))
    return mutual, oneway


def _fd_pair(va: list[str], vb: list[str]) -> tuple[bool, bool]:
    """Check A->B and B->A in one pass over rows where both cells are
    non-empty. Aborts early once both directions are violated."""
    fwd: dict[str, str] = {}
    rev: dict[str, str] = {}
    fwd_ok = rev_ok = True
    support = 0
    for x, y in zip(va, vb):
        if not x or not y:
            continue
        support += 1
        prev = fwd.get(x)
        if prev is None:
            fwd[x] = y
        elif prev != y:
            fwd_ok = False
        prev = rev.get(y)
        if prev is None:
            rev[y] = x
        elif prev != x:
            rev_ok = False
        if not fwd_ok and not rev_ok:
            return False, False
    # len(fwd) / len(rev) = distinct A / B values among supported rows.
    if support < _FD_MIN_SUPPORT or len(fwd) < 2 or len(rev) < 2:
        return False, False
    a_to_b = fwd_ok and len(fwd) < _FD_NEAR_UNIQUE * support
    b_to_a = rev_ok and len(rev) < _FD_NEAR_UNIQUE * support
    return a_to_b, b_to_a

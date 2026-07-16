"""Pure offline scorer for the P1 "Find" component bar (ONTA-343).

Given a **gold roster** and the **rows a discovery run surfaced**, compute the
coverage (recall) / precision quality metrics and the three anti-gaming counters,
then decide pass/fail against a threshold bundle. This is the metric contract the
P1 "Find" fixture-eval asserts (``tests/test_p1_find_eval.py``).

The module is deliberately **pure** — no I/O, no network, and **no cograph
imports** — so it stays dependency-free and OSS-standalone. The one piece of
domain knowledge it needs, the fabrication backstop, is INJECTED by the caller
(``is_fabricated=...``), so this scorer never has to reach into the resolver.
The eval passes the real ``schema_resolver._is_fabricated_placeholder`` in; a
bare call defaults to "nothing is fabricated" so the scorer is usable standalone.

Metric definitions (the contract bar):

* **Coverage (recall)** = ``|gold members present in the result| / |gold roster|``.
  Match on the key attribute, key-normalized (case/whitespace-insensitive; an
  optional per-fixture ``alias_table`` maps variant spellings to the canonical
  key).
* **Precision** = ``|true-member result rows| / |total result rows|``. A false
  positive is a row whose key ∉ gold roster (or fails the fixture's
  ``membership_rule`` for open goals), a fabricated placeholder
  (``is_fabricated`` on the key), or off-type. Near-duplicates do NOT hurt
  precision — a duplicate of a real member is still a true-member row; padding is
  caught by counter (b) instead.
* **Anti-gaming counters** (a run passes ONLY if all hold):
    a. **fabrication + off-membership rate** ≤ ceiling — the share of result rows
       that are fabricated OR not a member (off-membership / off-type).
    b. **near-duplicate collapse rate** ≤ ceiling — the share of result rows that
       collapse onto another under key-normalization (padding coverage with
       restatements of the same entity, or the 92-rows-share-one-fabricated-key
       failure mode).
    c. **$ per net-new true positive** ≤ ceiling — total paid provider spend
       divided by the number of DISTINCT gold members found. Spend is computed by
       the caller from the provider's declared per-call cost
       (``retrieval/cost.py::source_cost``) and passed in as ``total_cost_usd``.

A run PASSES iff ``coverage ≥ floor`` AND ``precision ≥ floor`` AND all three
counters ≤ their ceilings.

Boundary: OSS. Imports only stdlib.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence


# --------------------------------------------------------------------------- #
# Key normalization
# --------------------------------------------------------------------------- #
def normalize_key(value: Any) -> str:
    """Case/whitespace-insensitive normalization of a key value.

    Lower-cases and collapses ALL runs of whitespace to a single space, then
    strips — so ``"Blue  Bottle Coffee"``, ``"blue bottle coffee"`` and
    ``" BLUE BOTTLE COFFEE "`` all normalize to the same key. Punctuation is
    intentionally preserved (an ``alias_table`` handles spelling variants that
    differ by more than case/whitespace). ``None`` → ``""``.
    """
    if value is None:
        return ""
    return " ".join(str(value).strip().casefold().split())


def _build_alias_map(alias_table: Optional[Mapping[str, str]]) -> dict[str, str]:
    """Normalize both sides of the per-fixture alias table once.

    Keys are normalized VARIANT spellings, values the normalized CANONICAL key,
    so a lookup by a row's normalized key resolves the variant to canonical.
    """
    if not alias_table:
        return {}
    return {normalize_key(k): normalize_key(v) for k, v in alias_table.items()}


def _canonical(value: Any, alias_map: Mapping[str, str]) -> str:
    """Normalize ``value`` then map it through the alias table (identity if absent)."""
    nk = normalize_key(value)
    return alias_map.get(nk, nk)


# --------------------------------------------------------------------------- #
# membership_rule — a small declarative predicate for OPEN goals
# --------------------------------------------------------------------------- #
def eval_membership_rule(rule: Any, row: Mapping[str, Any]) -> bool:
    """Evaluate a declarative ``membership_rule`` against one result ``row``.

    Grammar (all keys optional; a leaf names a ``field`` + one operator):

    * ``{"all": [rule, ...]}``   — every subrule holds (AND)
    * ``{"any": [rule, ...]}``   — some subrule holds (OR)
    * ``{"not": rule}``          — negation
    * leaf ``{"field": F, ...}`` with exactly one of:
        - ``"equals": v``        — ``str(row[F]) == str(v)`` (case-insensitive)
        - ``"in": [v, ...]``     — normalized ``row[F]`` in the normalized set
        - ``"regex": r``         — ``re.search(r, str(row[F]))``
        - ``"contains": s``      — normalized ``s`` is a substring of normalized ``row[F]``
        - ``"nonempty": true``   — ``row[F]`` is a non-blank string

    An empty / falsy rule is vacuously True. A malformed rule is False (fail
    closed — a rule that cannot be understood must not silently admit rows).
    """
    if not rule:
        return True
    if not isinstance(rule, Mapping):
        return False
    if "all" in rule:
        subs = rule["all"]
        return isinstance(subs, Sequence) and all(
            eval_membership_rule(s, row) for s in subs
        )
    if "any" in rule:
        subs = rule["any"]
        return isinstance(subs, Sequence) and any(
            eval_membership_rule(s, row) for s in subs
        )
    if "not" in rule:
        return not eval_membership_rule(rule["not"], row)

    field_name = rule.get("field")
    if not field_name:
        return False
    cell = row.get(field_name)
    if "equals" in rule:
        return normalize_key(cell) == normalize_key(rule["equals"])
    if "in" in rule:
        allowed = rule["in"]
        if not isinstance(allowed, Sequence):
            return False
        return normalize_key(cell) in {normalize_key(v) for v in allowed}
    if "regex" in rule:
        try:
            return re.search(str(rule["regex"]), "" if cell is None else str(cell)) is not None
        except re.error:
            return False
    if "contains" in rule:
        return normalize_key(rule["contains"]) in normalize_key(cell)
    if "nonempty" in rule:
        want = bool(rule["nonempty"])
        present = cell is not None and str(cell).strip() != ""
        return present if want else not present
    return False


# --------------------------------------------------------------------------- #
# Thresholds + result bundle
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Thresholds:
    """The pass/fail bar. Floors are lower bounds (≥), ceilings upper bounds (≤)."""

    coverage_floor: float = 0.90
    precision_floor: float = 0.80
    fab_offmembership_ceiling: float = 0.15
    near_dup_ceiling: float = 0.15
    cost_per_tp_ceiling: float = 0.05

    @classmethod
    def from_dict(cls, d: Optional[Mapping[str, Any]]) -> "Thresholds":
        """Build from a (possibly partial) fixture ``thresholds`` block; unknown
        keys are ignored and missing ones fall back to the defaults above."""
        if not d:
            return cls()
        fields = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: float(v) for k, v in d.items() if k in fields})


@dataclass
class FindMetrics:
    """The scored metric bundle for one fixture run."""

    # Headline metrics
    coverage: float
    precision: float
    fab_offmembership_rate: float
    near_dup_collapse_rate: float
    cost_per_net_new_tp: float

    # Supporting counts (for debuggability + test assertions)
    gold_total: int
    result_total: int
    true_member_rows: int
    distinct_true_members: int
    found_gold_members: int
    fabricated_rows: int
    off_membership_rows: int
    near_dup_collapsed_rows: int
    total_cost_usd: float

    # Per-gate verdicts
    coverage_ok: bool
    precision_ok: bool
    fab_offmembership_ok: bool
    near_dup_ok: bool
    cost_ok: bool
    passed: bool

    thresholds: Thresholds
    # Optional per-row classification, in input order: one of
    # "true_member" | "fabricated" | "off_membership" (off-type folds in here).
    row_classes: list[str] = field(default_factory=list)

    def failures(self) -> list[str]:
        """Names of the gates this run violated (empty ⇒ passed)."""
        out = []
        if not self.coverage_ok:
            out.append("coverage")
        if not self.precision_ok:
            out.append("precision")
        if not self.fab_offmembership_ok:
            out.append("fab_offmembership")
        if not self.near_dup_ok:
            out.append("near_dup_collapse")
        if not self.cost_ok:
            out.append("cost_per_tp")
        return out


# --------------------------------------------------------------------------- #
# The scorer
# --------------------------------------------------------------------------- #
def score_find(
    *,
    gold_roster: Sequence[str],
    result_rows: Sequence[Mapping[str, Any]],
    key_attribute: str,
    membership_rule: Any = None,
    alias_table: Optional[Mapping[str, str]] = None,
    expected_type: Optional[str] = None,
    type_key: str = "_type",
    total_cost_usd: float = 0.0,
    is_fabricated: Optional[Callable[[str], bool]] = None,
    thresholds: Optional[Thresholds] = None,
) -> FindMetrics:
    """Score a discovery result against its gold roster. Pure — no I/O.

    ``result_rows`` are the rows the discovery run SURFACED (each a string-keyed
    dict, the CSV/discovery row shape). ``key_attribute`` is the identity column.
    ``membership_rule`` (open goals) decides precision membership when present;
    otherwise a row is a member iff its canonical key is in the gold roster.
    ``expected_type`` + ``type_key`` flag off-type rows. ``total_cost_usd`` is the
    caller-computed paid spend (per-call cost × paid calls). ``is_fabricated`` is
    the injected placeholder backstop applied to each row's KEY value.
    """
    th = thresholds or Thresholds()
    fab = is_fabricated or (lambda _v: False)
    alias_map = _build_alias_map(alias_table)
    has_rule = bool(membership_rule)

    gold_canon = {_canonical(g, alias_map) for g in gold_roster if normalize_key(g)}
    gold_total = len(gold_canon)

    row_classes: list[str] = []
    all_keys: list[str] = []          # canonical keys of every row (for collapse)
    member_keys: set[str] = set()     # canonical keys of member rows (for coverage)
    true_member_rows = 0
    fabricated_rows = 0
    off_membership_rows = 0

    for row in result_rows:
        raw_key = row.get(key_attribute) if isinstance(row, Mapping) else None
        ckey = _canonical(raw_key, alias_map)
        if ckey:
            all_keys.append(ckey)

        # 1) Fabricated key takes precedence — a hallucinated identifier can never
        #    be a genuine member even if it happens to collide with the roster.
        key_str = "" if raw_key is None else str(raw_key)
        if key_str.strip() and fab(key_str):
            fabricated_rows += 1
            row_classes.append("fabricated")
            continue

        # 2) Off-type is a non-member (off-membership).
        if expected_type is not None:
            row_type = row.get(type_key) if isinstance(row, Mapping) else None
            if row_type is not None and normalize_key(row_type) != normalize_key(expected_type):
                off_membership_rows += 1
                row_classes.append("off_membership")
                continue

        # 3) Membership: an open-goal rule, else closed-roster key ∈ gold.
        if has_rule:
            member = bool(ckey) and eval_membership_rule(membership_rule, row)
        else:
            member = bool(ckey) and ckey in gold_canon

        if member:
            true_member_rows += 1
            member_keys.add(ckey)
            row_classes.append("true_member")
        else:
            off_membership_rows += 1
            row_classes.append("off_membership")

    result_total = len(result_rows)
    distinct_true_members = len(member_keys)
    found_gold = len(gold_canon & member_keys)

    # Coverage always measures recall against the KNOWN gold roster (even for open
    # goals, where the rule may admit members not enumerated in the roster).
    coverage = (found_gold / gold_total) if gold_total else 0.0
    precision = (true_member_rows / result_total) if result_total else 0.0

    # (a) fabrication + off-membership rate.
    fab_off_rate = (
        (fabricated_rows + off_membership_rows) / result_total if result_total else 0.0
    )

    # (b) near-duplicate collapse rate: rows whose canonical key repeats a key
    #     seen earlier in the result. Fabricated + off-membership rows count too
    #     (a pile of rows sharing one fabricated key is exactly the padding this
    #     catches). Empty-key rows cannot collapse onto anything.
    collapsed = len(all_keys) - len(set(all_keys))
    near_dup_rate = (collapsed / result_total) if result_total else 0.0

    # (c) $ per net-new true positive.
    cost_per_tp = (
        (total_cost_usd / distinct_true_members)
        if distinct_true_members
        else (math.inf if total_cost_usd > 0 else 0.0)
    )

    coverage_ok = coverage >= th.coverage_floor
    precision_ok = precision >= th.precision_floor
    fab_off_ok = fab_off_rate <= th.fab_offmembership_ceiling
    near_dup_ok = near_dup_rate <= th.near_dup_ceiling
    cost_ok = cost_per_tp <= th.cost_per_tp_ceiling
    passed = coverage_ok and precision_ok and fab_off_ok and near_dup_ok and cost_ok

    return FindMetrics(
        coverage=coverage,
        precision=precision,
        fab_offmembership_rate=fab_off_rate,
        near_dup_collapse_rate=near_dup_rate,
        cost_per_net_new_tp=cost_per_tp,
        gold_total=gold_total,
        result_total=result_total,
        true_member_rows=true_member_rows,
        distinct_true_members=distinct_true_members,
        found_gold_members=found_gold,
        fabricated_rows=fabricated_rows,
        off_membership_rows=off_membership_rows,
        near_dup_collapsed_rows=collapsed,
        total_cost_usd=total_cost_usd,
        coverage_ok=coverage_ok,
        precision_ok=precision_ok,
        fab_offmembership_ok=fab_off_ok,
        near_dup_ok=near_dup_ok,
        cost_ok=cost_ok,
        passed=passed,
        thresholds=th,
        row_classes=row_classes,
    )


__all__ = [
    "FindMetrics",
    "Thresholds",
    "eval_membership_rule",
    "normalize_key",
    "score_find",
]

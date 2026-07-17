"""Pure offline scorer for the P4 "Verify" component bar (ONTA-366).

This IS the Tier-1 definition-of-done gate for P4 (Verify). Given the verdicts a
verifier PREDICTED for a batch of A3 clean facts (as :class:`VerifiedFact`s, or as
lightweight ``(key, verdict)`` pairs / mappings) and a GOLD SET of the correct
verdicts, it computes a small metric bundle and decides pass/fail against a
threshold bundle. Per the QC rule every component bar ships BOTH a metric
definition (numerator / denominator / dataset provenance / adjudication rule) AND
an anti-gaming counter-metric — this module carries both.

Modelled on the P1 "Find" bar (``pipeline/find_metrics.py``): a **pure** function —
no I/O, no network, no KG, deterministic. It imports only
:class:`~cograph_client.verification.types.TruthVerdict` (to normalize verdicts);
everything else is duck-typed, so it accepts a real :class:`VerifiedFact` or any
verdict-carrying stand-in without a hard dependency on the orchestrator.

Metric definitions (the contract bar):

* **Verdict accuracy** — the headline quality metric.
    - numerator: gold facts whose PREDICTED verdict exactly equals the gold verdict.
    - denominator: every fact in the gold set (a gold fact with no prediction is a
      MISS — it counts against accuracy, so silently dropping facts is punished).
    - dataset provenance: a human-adjudicated gold fixture under
      ``tests/fixtures/verification/verify_gold_<name>.json`` — one expected
      :class:`TruthVerdict` per fact, authored by hand (NOT machine-labeled).
    - adjudication rule: EXACT match over the 4-value ``TruthVerdict`` enum
      (SUPPORTED / REFUTED / UNVERIFIABLE / IDENTITY_CONDITIONAL). The four values
      are mutually exclusive; ``IDENTITY_CONDITIONAL`` is a *distinct* correct
      answer for entity-relative (same-name-collision) facts — predicting
      SUPPORTED for one is WRONG, never a partial credit.

* **Anti-gaming counter — the rubber-stamp detector.** Accuracy alone is gameable:
  a verifier that trivially returns SUPPORTED for everything can score decent
  accuracy on a gold set that happens to be mostly supported. TWO counters punish
  that, and a run passes ONLY if BOTH hold:
    a. **false-SUPPORTED rate** ≤ ceiling.
       - numerator: predictions where PREDICTED == SUPPORTED but gold != SUPPORTED
         (the verifier "accepted" a fact independent evidence does NOT corroborate).
       - denominator: total SUPPORTED predictions (0 predictions ⇒ rate 0.0 — a
         verifier that never accepts cannot rubber-stamp).
       A verifier that stamps SUPPORTED everywhere drives this to the gold set's
       non-supported fraction, which the fixtures keep well above the ceiling.
    b. **identity-blindness rate** ≤ ceiling — the same-name-collision guard.
       - numerator: gold ``IDENTITY_CONDITIONAL`` facts predicted SUPPORTED (the
         verifier blindly "corroborated" a fact whose truth hinges on an unresolved
         entity identity — two "Dr. Smith"s).
       - denominator: gold ``IDENTITY_CONDITIONAL`` facts that received a prediction.
       A well-behaved verifier answers ``IDENTITY_CONDITIONAL`` here and scores 0; a
       rubber-stamp answers SUPPORTED and scores 1.0.

* **Verification cost within budget** — given per-fact costs and a budget, report
  the total spend and whether the run stayed within budget. ``budget_usd=None``
  means "no budget declared" → not a gate (``within_budget`` True).

A run PASSES iff ``verdict_accuracy ≥ accuracy_floor`` AND ``false_supported_rate ≤
false_supported_ceiling`` AND ``identity_blindness_rate ≤ identity_blindness_ceiling``
AND the run is within budget.

Boundary: OSS. Imports only stdlib + ``cograph_client.verification.types``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Hashable, Mapping, Optional, Sequence

from cograph_client.verification.types import TruthVerdict

__all__ = [
    "VerifyThresholds",
    "VerifyMetrics",
    "fact_key",
    "score_verification",
]


# --------------------------------------------------------------------------- #
# Key + verdict normalization
# --------------------------------------------------------------------------- #
def fact_key(entity_id: Any, attribute: Any) -> tuple[str, str]:
    """The canonical per-fact identity used to align a prediction with its gold
    verdict: ``(entity_id, attribute)`` as strings.

    A :class:`VerifiedFact` carries both fields verbatim from the A3 clean fact it
    verified, so the same key is derivable from a prediction and from a gold entry.
    Same-name-collision facts stay DISTINCT here as long as their ``entity_id``
    disambiguates them (e.g. ``"Dr. Sarah Smith (NPI 1001)"`` vs ``"(NPI 1002)"``);
    the *collision* is in the evidence, not in the key.
    """
    return (str(entity_id), str(attribute))


def _as_verdict(value: Any) -> TruthVerdict:
    """Coerce a verdict given as a :class:`TruthVerdict`, its ``.value`` string
    (``"supported"``), or its member NAME (``"SUPPORTED"``) into a
    :class:`TruthVerdict`. Unknown values raise ``ValueError`` — a typo in a gold
    fixture must FAIL LOUD, never silently score as a mismatch (or, worse, match)."""
    if isinstance(value, TruthVerdict):
        return value
    if isinstance(value, str):
        norm = value.strip().lower()
        for member in TruthVerdict:
            if norm == member.value or norm == member.name.lower():
                return member
    raise ValueError(f"not a TruthVerdict: {value!r}")


def _extract(item: Any) -> tuple[Hashable, TruthVerdict, Optional[float]]:
    """Pull ``(key, verdict, cost)`` from one predicted item, duck-typed so a real
    :class:`VerifiedFact`, a mapping, or a ``(key, verdict)`` pair all work.

    * VerifiedFact-like (has ``entity_id`` + ``attribute`` + ``verdict``): key is
      :func:`fact_key`; cost is ``envelope.spend_usd`` when an envelope is present.
    * Mapping: ``key`` if present, else :func:`fact_key` from ``entity_id`` /
      ``attribute``; verdict from ``verdict``; cost from ``cost_usd`` / ``spend_usd``.
    * 2-sequence ``(key, verdict)``: taken positionally (no cost).
    """
    # VerifiedFact-like — duck-typed (no hard import of VerifiedFact).
    if hasattr(item, "verdict") and hasattr(item, "entity_id") and hasattr(item, "attribute"):
        cost: Optional[float] = None
        env = getattr(item, "envelope", None)
        spend = getattr(env, "spend_usd", None)
        if spend is not None:
            cost = float(spend)
        return fact_key(item.entity_id, item.attribute), _as_verdict(item.verdict), cost

    if isinstance(item, Mapping):
        if "key" in item:
            key: Hashable = item["key"]
            if isinstance(key, list):
                key = tuple(key)
        else:
            key = fact_key(item.get("entity_id", ""), item.get("attribute", ""))
        raw_cost = item.get("cost_usd", item.get("spend_usd"))
        cost = None if raw_cost is None else float(raw_cost)
        return key, _as_verdict(item["verdict"]), cost

    if isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and len(item) == 2:
        key, verdict = item
        if isinstance(key, list):
            key = tuple(key)
        return key, _as_verdict(verdict), None

    raise TypeError(f"cannot read a (key, verdict) from predicted item: {item!r}")


def _normalize_gold(gold: Mapping[Any, Any]) -> dict[Hashable, TruthVerdict]:
    """Normalize a gold mapping ``key -> verdict`` into ``key -> TruthVerdict``,
    coercing list keys to tuples so a JSON-loaded ``[entity, attr]`` matches a
    :func:`fact_key` tuple."""
    out: dict[Hashable, TruthVerdict] = {}
    for k, v in gold.items():
        key: Hashable = tuple(k) if isinstance(k, list) else k
        out[key] = _as_verdict(v)
    return out


# --------------------------------------------------------------------------- #
# Thresholds + result bundle
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class VerifyThresholds:
    """The pass/fail bar. ``accuracy_floor`` is a lower bound (≥); the two
    anti-gaming rates are upper bounds (≤)."""

    accuracy_floor: float = 0.80
    false_supported_ceiling: float = 0.10
    identity_blindness_ceiling: float = 0.10

    @classmethod
    def from_dict(cls, d: Optional[Mapping[str, Any]]) -> "VerifyThresholds":
        """Build from a (possibly partial) fixture ``thresholds`` block; unknown
        keys are ignored and missing ones fall back to the defaults above."""
        if not d:
            return cls()
        fields = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: float(v) for k, v in d.items() if k in fields})


@dataclass
class VerifyMetrics:
    """The scored metric bundle for one gold-set run."""

    # Headline metrics
    verdict_accuracy: float
    false_supported_rate: float
    identity_blindness_rate: float
    total_cost_usd: float
    within_budget: bool
    budget_usd: Optional[float]

    # Supporting counts (for debuggability + test assertions)
    gold_total: int
    predicted_total: int
    scored_predictions: int          # gold facts that received a prediction
    correct_verdicts: int
    missing_predictions: int         # gold facts with NO prediction
    unexpected_predictions: int      # predictions with no matching gold fact
    supported_predictions: int
    false_supported: int
    identity_conditional_gold: int   # gold IC facts that received a prediction
    identity_blinded: int

    # Per-gate verdicts
    accuracy_ok: bool
    false_supported_ok: bool
    identity_blindness_ok: bool
    budget_ok: bool
    passed: bool

    thresholds: VerifyThresholds
    # gold_verdict -> {predicted_verdict -> count}; "<missing>" for un-predicted gold.
    confusion: dict[str, dict[str, int]] = field(default_factory=dict)

    def failures(self) -> list[str]:
        """Names of the gates this run violated (empty ⇒ passed)."""
        out: list[str] = []
        if not self.accuracy_ok:
            out.append("verdict_accuracy")
        if not self.false_supported_ok:
            out.append("false_supported")
        if not self.identity_blindness_ok:
            out.append("identity_blindness")
        if not self.budget_ok:
            out.append("budget")
        return out


# --------------------------------------------------------------------------- #
# The scorer
# --------------------------------------------------------------------------- #
def score_verification(
    *,
    predicted: Sequence[Any],
    gold: Mapping[Any, Any],
    per_fact_cost: Optional[Mapping[Any, Any]] = None,
    budget_usd: Optional[float] = None,
    thresholds: Optional[VerifyThresholds] = None,
) -> VerifyMetrics:
    """Score predicted verdicts against a gold set. Pure — no I/O.

    ``predicted`` is what the verifier decided: :class:`VerifiedFact`s (the real A4
    artifact), mappings, or ``(key, verdict)`` pairs — each yields a ``(key,
    verdict, cost)`` via duck-typing. ``gold`` maps a fact key (a :func:`fact_key`
    tuple, or any hashable — list keys are coerced to tuples) to its adjudicated
    :class:`TruthVerdict`. ``per_fact_cost`` (a ``key -> cost`` mapping) overrides
    the per-fact cost carried on the predictions; ``budget_usd`` gates the total.
    """
    th = thresholds or VerifyThresholds()
    gold_norm = _normalize_gold(gold)

    pred_verdict: dict[Hashable, TruthVerdict] = {}
    pred_cost: dict[Hashable, float] = {}
    for item in predicted:
        key, verdict, cost = _extract(item)
        pred_verdict[key] = verdict  # last write wins on a duplicate key
        if cost is not None:
            pred_cost[key] = cost

    correct = 0
    supported_predictions = 0
    false_supported = 0
    identity_conditional_gold = 0
    identity_blinded = 0
    scored_predictions = 0
    missing_predictions = 0
    confusion: dict[str, dict[str, int]] = {}

    for key, gold_v in gold_norm.items():
        pred_v = pred_verdict.get(key)
        gcol = confusion.setdefault(gold_v.value, {})
        pkey = pred_v.value if pred_v is not None else "<missing>"
        gcol[pkey] = gcol.get(pkey, 0) + 1

        if pred_v is None:
            missing_predictions += 1
            continue

        scored_predictions += 1
        if pred_v is gold_v:
            correct += 1
        if pred_v is TruthVerdict.SUPPORTED:
            supported_predictions += 1
            if gold_v is not TruthVerdict.SUPPORTED:
                false_supported += 1
        if gold_v is TruthVerdict.IDENTITY_CONDITIONAL:
            identity_conditional_gold += 1
            if pred_v is TruthVerdict.SUPPORTED:
                identity_blinded += 1

    gold_total = len(gold_norm)
    unexpected_predictions = sum(1 for k in pred_verdict if k not in gold_norm)

    verdict_accuracy = (correct / gold_total) if gold_total else 0.0
    false_supported_rate = (
        (false_supported / supported_predictions) if supported_predictions else 0.0
    )
    identity_blindness_rate = (
        (identity_blinded / identity_conditional_gold)
        if identity_conditional_gold
        else 0.0
    )

    # Cost: an explicit per_fact_cost mapping is the run's authoritative cost ledger;
    # otherwise sum the per-fact cost carried on the predictions themselves.
    if per_fact_cost is not None:
        total_cost = float(sum(float(v) for v in per_fact_cost.values()))
    else:
        total_cost = float(sum(pred_cost.values()))
    within_budget = (budget_usd is None) or (total_cost <= float(budget_usd))

    accuracy_ok = verdict_accuracy >= th.accuracy_floor
    false_supported_ok = false_supported_rate <= th.false_supported_ceiling
    identity_blindness_ok = identity_blindness_rate <= th.identity_blindness_ceiling
    budget_ok = within_budget
    passed = accuracy_ok and false_supported_ok and identity_blindness_ok and budget_ok

    return VerifyMetrics(
        verdict_accuracy=verdict_accuracy,
        false_supported_rate=false_supported_rate,
        identity_blindness_rate=identity_blindness_rate,
        total_cost_usd=total_cost,
        within_budget=within_budget,
        budget_usd=(None if budget_usd is None else float(budget_usd)),
        gold_total=gold_total,
        predicted_total=len(pred_verdict),
        scored_predictions=scored_predictions,
        correct_verdicts=correct,
        missing_predictions=missing_predictions,
        unexpected_predictions=unexpected_predictions,
        supported_predictions=supported_predictions,
        false_supported=false_supported,
        identity_conditional_gold=identity_conditional_gold,
        identity_blinded=identity_blinded,
        accuracy_ok=accuracy_ok,
        false_supported_ok=false_supported_ok,
        identity_blindness_ok=identity_blindness_ok,
        budget_ok=budget_ok,
        passed=passed,
        thresholds=th,
        confusion=confusion,
    )

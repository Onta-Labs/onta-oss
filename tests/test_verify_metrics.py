"""P4 "Verify" component bar — offline verdict-accuracy & anti-gaming fixture-eval
(ONTA-366).

This IS the Tier-1 definition-of-done gate for P4 (Verify). For each gold fixture
under ``tests/fixtures/verification/verify_gold_*.json`` it scores a set of
predicted verdicts against the human-adjudicated gold verdicts with
``cograph_client.verification.verify_metrics`` and asserts the metric bundle.

Load-bearing control: a RUBBER-STAMP verifier that returns SUPPORTED for every fact
must FAIL the anti-gaming counter (false-SUPPORTED rate, plus identity-blindness on
the same-name-collision fixture), while a WELL-BEHAVED verifier (the correct gold
verdicts, incl. IDENTITY_CONDITIONAL for the collision facts) PASSES the whole bar.
The contrast proves the counter detects gaming, not just measures accuracy.

Fully offline / deterministic — no network, no live LLM, no store.
"""

from __future__ import annotations

import glob
import json
import os
from types import SimpleNamespace

import pytest

from cograph_client.resolver.models import CleanFact, CleanOutcome
from cograph_client.verification import (
    TruthVerdict,
    VerifierResult,
    register_fact_verifier,
    verify_clean_facts,
)
from cograph_client.verification.verify_metrics import (
    VerifyMetrics,
    VerifyThresholds,
    fact_key,
    score_verification,
)

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "verification")
FIXTURE_PATHS = sorted(glob.glob(os.path.join(FIXTURE_DIR, "verify_gold_*.json")))
# A meaningful bar needs >= 3 goals, at least one a same-name collision.
assert len(FIXTURE_PATHS) >= 3, "P4 verify-eval needs at least 3 gold fixtures"


class VerifyBarNotMet(AssertionError):
    """Raised by :func:`enforce_bar` when a run misses the P4 Verify bar."""


def enforce_bar(m: VerifyMetrics) -> None:
    """The gate. Raise iff accuracy/either anti-gaming counter/budget is violated."""
    if not m.passed:
        raise VerifyBarNotMet(
            f"P4 Verify bar not met: failing gates={m.failures()} "
            f"(accuracy={m.verdict_accuracy:.3f} "
            f"false_supported={m.false_supported_rate:.3f} "
            f"identity_blindness={m.identity_blindness_rate:.3f} "
            f"cost=${m.total_cost_usd:.3f} within_budget={m.within_budget})"
        )


# --------------------------------------------------------------------------- #
# Fixture loading + verifier stand-ins
# --------------------------------------------------------------------------- #
def _load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _gold_from(fixture: dict) -> dict[tuple[str, str], str]:
    """The adjudicated gold verdicts, keyed by ``fact_key(entity_id, attribute)``."""
    return {
        fact_key(f["entity_id"], f["attribute"]): f["gold_verdict"]
        for f in fixture["facts"]
    }


def _cost_from(fixture: dict) -> dict[tuple[str, str], float]:
    return {
        fact_key(f["entity_id"], f["attribute"]): float(f.get("cost_usd", 0.0))
        for f in fixture["facts"]
    }


def _thresholds(fixture: dict) -> VerifyThresholds:
    return VerifyThresholds.from_dict(fixture.get("thresholds"))


def _well_behaved_predictions(fixture: dict) -> list[tuple[tuple[str, str], str]]:
    """The correct verdict for every fact — a perfect verifier."""
    return [
        (fact_key(f["entity_id"], f["attribute"]), f["gold_verdict"])
        for f in fixture["facts"]
    ]


def _rubber_stamp_predictions(fixture: dict) -> list[tuple[tuple[str, str], str]]:
    """SUPPORTED for every fact — the trivial-accept gaming attack."""
    return [
        (fact_key(f["entity_id"], f["attribute"]), "supported")
        for f in fixture["facts"]
    ]


def _score(fixture: dict, predicted) -> VerifyMetrics:
    return score_verification(
        predicted=predicted,
        gold=_gold_from(fixture),
        per_fact_cost=_cost_from(fixture),
        budget_usd=fixture.get("budget_usd"),
        thresholds=_thresholds(fixture),
    )


# --------------------------------------------------------------------------- #
# The eval, parametrized over every gold fixture.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "fixture_path", FIXTURE_PATHS, ids=[os.path.basename(p) for p in FIXTURE_PATHS]
)
def test_p4_verify_fixture(fixture_path):
    fixture = _load(fixture_path)

    # A WELL-BEHAVED verifier clears every gate.
    good = _score(fixture, _well_behaved_predictions(fixture))
    enforce_bar(good)  # must not raise
    assert good.passed, good.failures()
    assert good.verdict_accuracy == pytest.approx(1.0)
    assert good.false_supported_rate == pytest.approx(0.0)
    assert good.identity_blindness_rate == pytest.approx(0.0)
    assert good.within_budget
    assert good.total_cost_usd <= fixture["budget_usd"]

    # The CONTROL: a RUBBER-STAMP verifier trips the anti-gaming counter.
    bad = _score(fixture, _rubber_stamp_predictions(fixture))
    with pytest.raises(VerifyBarNotMet):
        enforce_bar(bad)
    assert not bad.passed
    # The rubber-stamp attack is caught by the false-SUPPORTED counter EVERY time —
    # not by accuracy alone. That is the whole point of the anti-gaming metric.
    assert not bad.false_supported_ok
    assert "false_supported" in bad.failures()
    # Its false-SUPPORTED rate is exactly the non-supported gold fraction.
    non_supported = sum(
        1 for f in fixture["facts"] if f["gold_verdict"] != "supported"
    )
    assert bad.false_supported == non_supported
    assert bad.false_supported_rate == pytest.approx(non_supported / len(fixture["facts"]))


# --------------------------------------------------------------------------- #
# Same-name-collision fixture: the identity-blindness counter is load-bearing.
# --------------------------------------------------------------------------- #
def test_same_name_collision_identity_blindness_counter():
    """On the same-name-collision fixture the correct verdict for the entity-relative
    facts is IDENTITY_CONDITIONAL. A rubber-stamp verifier that returns SUPPORTED is
    maximally identity-blind and MUST trip the identity-blindness counter; the
    well-behaved verifier (IDENTITY_CONDITIONAL for those facts) scores 0 blindness."""
    path = os.path.join(FIXTURE_DIR, "verify_gold_healthcare_providers.json")
    fixture = _load(path)

    ic_facts = [
        f for f in fixture["facts"] if f["gold_verdict"] == "identity_conditional"
    ]
    assert len(ic_facts) >= 1, "the collision fixture must carry IDENTITY_CONDITIONAL facts"

    good = _score(fixture, _well_behaved_predictions(fixture))
    assert good.identity_conditional_gold == len(ic_facts)
    assert good.identity_blinded == 0
    assert good.identity_blindness_rate == pytest.approx(0.0)
    assert good.identity_blindness_ok and good.passed

    bad = _score(fixture, _rubber_stamp_predictions(fixture))
    # Every IDENTITY_CONDITIONAL fact was blindly accepted as SUPPORTED.
    assert bad.identity_blinded == len(ic_facts)
    assert bad.identity_blindness_rate == pytest.approx(1.0)
    assert not bad.identity_blindness_ok
    assert "identity_blindness" in bad.failures()
    # Both anti-gaming counters bite here, and accuracy too.
    assert set(bad.failures()) >= {"verdict_accuracy", "false_supported", "identity_blindness"}


# --------------------------------------------------------------------------- #
# Drive the scorer end-to-end from verify_clean_facts + a registered verifier,
# so the bar demonstrably consumes REAL VerifiedFacts.
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _clear_verifier_registry():
    register_fact_verifier(None)
    yield
    register_fact_verifier(None)


class _GoldVerifier:
    """A well-behaved verifier: returns the gold verdict for each fact (looked up by
    ``(entity_id, attribute)``)."""

    def __init__(self, gold: dict[tuple[str, str], str]) -> None:
        self._gold = gold

    def verify(self, fact: CleanFact, context=None) -> VerifierResult:
        verdict = TruthVerdict(self._gold[fact_key(fact.entity_id, fact.attribute)])
        conf = 0.9 if verdict is TruthVerdict.SUPPORTED else 0.5
        return VerifierResult(verdict=verdict, confidence=conf)


class _RubberStampVerifier:
    """The gaming attack: SUPPORTED for everything, high confidence."""

    def verify(self, fact: CleanFact, context=None) -> VerifierResult:
        return VerifierResult(verdict=TruthVerdict.SUPPORTED, confidence=1.0)


def _clean_facts(fixture: dict) -> list[CleanFact]:
    return [
        CleanFact(
            datatype="string",
            raw_value=str(f["value"]),
            clean_value=str(f["value"]),
            outcome=CleanOutcome.PASSED,
            entity_id=f["entity_id"],
            attribute=f["attribute"],
        )
        for f in fixture["facts"]
    ]


def test_bar_consumes_real_verified_facts_end_to_end():
    """Register a stub verifier, run the real ``verify_clean_facts`` orchestrator, and
    score the emitted A4 ``VerifiedFact``s — the bar reads ``vf.verdict`` verbatim."""
    fixture = _load(
        os.path.join(FIXTURE_DIR, "verify_gold_healthcare_providers.json")
    )
    gold = _gold_from(fixture)
    facts = _clean_facts(fixture)
    on = SimpleNamespace(enabled=True)  # policy that turns verification ON

    # Well-behaved: the orchestrator routes through the registered verifier.
    register_fact_verifier(_GoldVerifier(gold))
    verified = verify_clean_facts(facts, on, workspace_id="ws", run_id="r-verify")
    assert len(verified) == len(facts)
    good = score_verification(
        predicted=verified,  # real VerifiedFacts
        gold=gold,
        per_fact_cost=_cost_from(fixture),
        budget_usd=fixture["budget_usd"],
        thresholds=_thresholds(fixture),
    )
    enforce_bar(good)
    assert good.passed and good.verdict_accuracy == pytest.approx(1.0)
    assert good.scored_predictions == len(facts) and good.missing_predictions == 0
    assert good.unexpected_predictions == 0

    # Rubber-stamp: same pipeline, gamed verifier → the bar fails.
    register_fact_verifier(_RubberStampVerifier())
    verified_bad = verify_clean_facts(facts, on, workspace_id="ws", run_id="r-verify")
    bad = score_verification(
        predicted=verified_bad,
        gold=gold,
        per_fact_cost=_cost_from(fixture),
        budget_usd=fixture["budget_usd"],
        thresholds=_thresholds(fixture),
    )
    with pytest.raises(VerifyBarNotMet):
        enforce_bar(bad)
    assert not bad.false_supported_ok and not bad.identity_blindness_ok


# --------------------------------------------------------------------------- #
# Pure-scorer unit checks — pin the metric contract itself.
# --------------------------------------------------------------------------- #
def test_accuracy_numerator_and_denominator():
    gold = {("e", "a"): "supported", ("e", "b"): "refuted", ("e", "c"): "unverifiable"}
    pred = [(("e", "a"), "supported"), (("e", "b"), "refuted"), (("e", "c"), "supported")]
    m = score_verification(predicted=pred, gold=gold)
    assert m.correct_verdicts == 2 and m.gold_total == 3
    assert m.verdict_accuracy == pytest.approx(2 / 3)


def test_missing_prediction_counts_as_a_miss():
    """A gold fact with no prediction is wrong (accuracy denominator is all gold),
    so silently dropping facts is punished — it never inflates accuracy."""
    gold = {("e", "a"): "supported", ("e", "b"): "supported"}
    m = score_verification(predicted=[(("e", "a"), "supported")], gold=gold)
    assert m.missing_predictions == 1
    assert m.verdict_accuracy == pytest.approx(0.5)
    assert m.confusion["supported"]["<missing>"] == 1


def test_unexpected_prediction_is_surfaced_not_scored():
    gold = {("e", "a"): "supported"}
    pred = [(("e", "a"), "supported"), (("e", "z"), "supported")]
    m = score_verification(predicted=pred, gold=gold)
    assert m.unexpected_predictions == 1
    assert m.verdict_accuracy == pytest.approx(1.0)  # the extra prediction is ignored


def test_false_supported_rate_denominator_is_supported_predictions():
    gold = {("e", "a"): "supported", ("e", "b"): "refuted", ("e", "c"): "refuted"}
    # Two SUPPORTED predictions, one of them false (b's gold is refuted).
    pred = [(("e", "a"), "supported"), (("e", "b"), "supported"), (("e", "c"), "refuted")]
    m = score_verification(predicted=pred, gold=gold)
    assert m.supported_predictions == 2 and m.false_supported == 1
    assert m.false_supported_rate == pytest.approx(0.5)


def test_no_supported_predictions_means_zero_false_supported_rate():
    """A verifier that never accepts cannot rubber-stamp — rate is 0 (not undefined)."""
    gold = {("e", "a"): "refuted", ("e", "b"): "unverifiable"}
    pred = [(("e", "a"), "refuted"), (("e", "b"), "unverifiable")]
    m = score_verification(predicted=pred, gold=gold)
    assert m.supported_predictions == 0
    assert m.false_supported_rate == pytest.approx(0.0) and m.false_supported_ok


def test_budget_gate():
    gold = {("e", "a"): "supported"}
    pred = [(("e", "a"), "supported")]
    within = score_verification(
        predicted=pred, gold=gold, per_fact_cost={("e", "a"): 0.10}, budget_usd=0.20
    )
    assert within.total_cost_usd == pytest.approx(0.10) and within.within_budget
    assert within.budget_ok and within.passed

    over = score_verification(
        predicted=pred, gold=gold, per_fact_cost={("e", "a"): 0.50}, budget_usd=0.20
    )
    assert over.total_cost_usd == pytest.approx(0.50) and not over.within_budget
    assert not over.budget_ok and not over.passed
    assert "budget" in over.failures()


def test_no_budget_declared_is_not_a_gate():
    gold = {("e", "a"): "supported"}
    m = score_verification(
        predicted=[(("e", "a"), "supported")], gold=gold, per_fact_cost={("e", "a"): 9.9}
    )
    assert m.budget_usd is None and m.within_budget and m.budget_ok and m.passed


def test_cost_falls_back_to_predicted_item_cost_when_no_per_fact_cost():
    """With no explicit ledger, the total is summed from the predictions' own costs
    (a mapping's ``cost_usd``, or a VerifiedFact envelope's ``spend_usd``)."""
    gold = {("e", "a"): "supported", ("e", "b"): "supported"}
    pred = [
        {"entity_id": "e", "attribute": "a", "verdict": "supported", "cost_usd": 0.03},
        {"entity_id": "e", "attribute": "b", "verdict": "supported", "cost_usd": 0.04},
    ]
    m = score_verification(predicted=pred, gold=gold, budget_usd=0.10)
    assert m.total_cost_usd == pytest.approx(0.07) and m.within_budget


def test_verdict_normalization_accepts_enum_name_and_value():
    gold = {("e", "a"): TruthVerdict.IDENTITY_CONDITIONAL}
    for spelling in ("identity_conditional", "IDENTITY_CONDITIONAL", TruthVerdict.IDENTITY_CONDITIONAL):
        m = score_verification(predicted=[(("e", "a"), spelling)], gold=gold)
        assert m.verdict_accuracy == pytest.approx(1.0)


def test_unknown_verdict_fails_loud():
    with pytest.raises(ValueError):
        score_verification(predicted=[(("e", "a"), "maybe")], gold={("e", "a"): "supported"})

"""Tier-3 whole-product QC outcome-grader tests (ONTA-283-C).

Asserts the MECHANISM of the pure outcome grader on invented tokens
(Widget / Sprocket / Gadget / Doohickey) — never a real domain string — so the
tests pin behaviour, not the seed corpus's content:

  * planted ``correct`` / ``partial`` / ``wrong`` / ``error`` answers land on the
    right verdict AND the right difficulty tier;
  * the Wilson 95% CI narrows with n and brackets the point estimate;
  * each anti-gaming counter FIRES on a planted bad case and stays clean on the
    good one — the empty-answer guard (even when the gold is empty), the
    false-confident rate, and the citation-fabrication rate.

Fully offline / deterministic — pure scorer, no network, no LLM, no store.
"""

from __future__ import annotations

import glob
import os

import pytest

from cograph_client.qc.tier3_fixture import Tier3Fixture, load_fixture
from cograph_client.qc.tier3_grade import (
    CORRECT,
    ERROR,
    PARTIAL,
    WRONG,
    Tier3Thresholds,
    grade_tier3,
    wilson_interval,
)

TIER3_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "tier3")


# --------------------------------------------------------------------------- #
# Builders (invented tokens only).
# --------------------------------------------------------------------------- #
def _fixture(questions: list[dict], *, fid: str = "unit") -> Tier3Fixture:
    return Tier3Fixture.from_dict(
        {
            "id": fid,
            "goal": "Catalog every widget-family part and answer questions about it.",
            "source_seed": {"kind": "url_list", "urls": ["https://seed.example/parts"]},
            "questions": questions,
        }
    )


def _q(qid: str, tier: str, items: list[str], **extra) -> dict:
    q = {
        "id": qid,
        "question": f"question {qid}",
        "tier": tier,
        "gold_sparql": "SELECT ?x WHERE { ?x a :Widget }",
        "full_expected_items": items,
        "full_result_count": len(items),
    }
    q.update(extra)
    return q


def _graded_by_id(score) -> dict:
    return {g.question_id: g for g in score.graded}


# --------------------------------------------------------------------------- #
# Verdict + tier bucketing.
# --------------------------------------------------------------------------- #
def test_scalar_numeric_correct_and_wrong_bucket_by_tier():
    fx = _fixture([_q("q1", "T1", ["42"]), _q("q2", "T2", ["7"])])
    score = grade_tier3(
        fixture=fx,
        answers=[
            {"question_id": "q1", "answer": "There are 42 widgets in stock."},
            {"question_id": "q2", "answer": "We counted 999 of them."},
        ],
    )
    g = _graded_by_id(score)
    assert g["q1"].verdict == CORRECT
    assert g["q2"].verdict == WRONG
    # Bucketed by the right tier.
    assert score.by_tier["T1"].passed == 1 and score.by_tier["T1"].total == 1
    assert score.by_tier["T2"].wrong == 1 and score.by_tier["T2"].passed == 0
    assert score.by_tier["T1"].accuracy == pytest.approx(1.0)
    assert score.by_tier["T2"].accuracy == pytest.approx(0.0)
    assert score.overall_accuracy == pytest.approx(0.5)
    assert score.passed == 1 and score.total == 2


def test_numeric_count_tolerance_is_two_percent():
    # 100 vs 101 is within +-2%; 100 vs 150 is not (mirrors eval.py counts rule).
    fx = _fixture([_q("q1", "T1", ["100"]), _q("q2", "T1", ["100"])])
    score = grade_tier3(
        fixture=fx,
        answers=[
            {"question_id": "q1", "answer": "101"},
            {"question_id": "q2", "answer": "150"},
        ],
    )
    g = _graded_by_id(score)
    assert g["q1"].verdict == CORRECT
    assert g["q2"].verdict == WRONG


def test_float_average_tolerance_is_five_percent():
    # A non-".0" decimal gold is a float (avg/sum) -> +-5% band.
    fx = _fixture([_q("q1", "T4", ["3.14"]), _q("q2", "T4", ["3.14"])])
    score = grade_tier3(
        fixture=fx,
        answers=[
            {"question_id": "q1", "answer": "about 3.20"},   # within 5%
            {"question_id": "q2", "answer": "roughly 4.00"},  # outside 5%
        ],
    )
    g = _graded_by_id(score)
    assert g["q1"].verdict == CORRECT
    assert g["q2"].verdict == WRONG


def test_set_coverage_correct_partial_wrong():
    fx = _fixture(
        [
            _q("all", "T1", ["Widget", "Sprocket", "Gadget"]),
            _q("some", "T3", ["Widget", "Sprocket", "Gadget"]),
            _q("none", "T3", ["Widget", "Sprocket", "Gadget"]),
        ]
    )
    score = grade_tier3(
        fixture=fx,
        answers=[
            {"question_id": "all", "answer": "The parts are Widget, Sprocket, and Gadget."},
            {"question_id": "some", "answer": "We found a Widget and a Sprocket."},
            {"question_id": "none", "answer": "Only a Doohickey turned up."},
        ],
    )
    g = _graded_by_id(score)
    assert g["all"].verdict == CORRECT
    assert g["some"].verdict == PARTIAL
    assert g["none"].verdict == WRONG


def test_string_scalar_case_insensitive_contains():
    fx = _fixture([_q("q1", "T1", ["Sprocket"])])
    score = grade_tier3(
        fixture=fx, answers=[{"question_id": "q1", "answer": "The answer is SPROCKET."}]
    )
    assert _graded_by_id(score)["q1"].verdict == CORRECT


def test_missing_answer_is_scored_error_not_skipped():
    fx = _fixture([_q("q1", "T1", ["Widget"]), _q("q2", "T2", ["Sprocket"])])
    score = grade_tier3(fixture=fx, answers=[{"question_id": "q1", "answer": "Widget"}])
    g = _graded_by_id(score)
    assert score.missing_answers == 1
    assert g["q2"].verdict == ERROR
    # A dropped answer counts against accuracy — never silently inflates it.
    assert score.overall_accuracy == pytest.approx(0.5)
    assert score.by_tier["T2"].error == 1


def test_unexpected_answer_is_surfaced_not_scored():
    fx = _fixture([_q("q1", "T1", ["Widget"])])
    score = grade_tier3(
        fixture=fx,
        answers=[
            {"question_id": "q1", "answer": "Widget"},
            {"question_id": "ghost", "answer": "Sprocket"},
        ],
    )
    assert score.unexpected_answers == 1
    assert score.total == 1 and score.overall_accuracy == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Counter (a): the empty-answer guard.
# --------------------------------------------------------------------------- #
def test_empty_answer_never_correct_against_nonempty_gold():
    fx = _fixture([_q("q1", "T1", ["Widget"])])
    for blank in ["", "   ", "I don't know", "N/A", "unknown"]:
        score = grade_tier3(fixture=fx, answers=[{"question_id": "q1", "answer": blank}])
        g = _graded_by_id(score)
        assert g["q1"].verdict == ERROR, blank
        assert g["q1"].is_empty_answer
        assert score.empty_answers == 1
        assert score.empty_answer_rate == pytest.approx(1.0)
        # The guard invariant: an empty answer is never scored correct.
        assert score.empty_answer_scored_correct == 0


def test_empty_answer_guard_fires_even_when_gold_is_empty():
    """The load-bearing case: gold is the empty set, so a blank answer WOULD naively
    look correct (empty == empty). The guard must downgrade it and record the block."""
    fx = _fixture([_q("q1", "T2", [])])  # gold-empty question
    score = grade_tier3(fixture=fx, answers=[{"question_id": "q1", "answer": ""}])
    g = _graded_by_id(score)
    assert g["q1"].verdict == ERROR
    assert score.empty_answer_guard_fired == 1
    assert score.empty_answer_scored_correct == 0  # invariant holds
    assert score.empty_guard_ok


def test_explicit_no_results_answer_is_correct_for_empty_gold():
    """A substantive 'no results found' assertion (NOT a blank/idk non-answer) is the
    correct answer to a gold-empty question and is NOT downgraded by the guard."""
    fx = _fixture([_q("q1", "T2", [])])
    score = grade_tier3(
        fixture=fx, answers=[{"question_id": "q1", "answer": "No results found."}]
    )
    g = _graded_by_id(score)
    assert g["q1"].verdict == CORRECT
    assert not g["q1"].is_empty_answer
    assert score.empty_answer_guard_fired == 0
    assert score.by_tier["T2"].passed == 1


# --------------------------------------------------------------------------- #
# Counter (b): false-confident.
# --------------------------------------------------------------------------- #
def test_false_confident_counter_fires_on_confident_wrong():
    fx = _fixture([_q("q1", "T1", ["42"])])
    # Non-empty, non-hedged, wrong -> confident AND wrong.
    score = grade_tier3(
        fixture=fx, answers=[{"question_id": "q1", "answer": "There are exactly 999."}]
    )
    g = _graded_by_id(score)
    assert g["q1"].verdict == WRONG and g["q1"].is_confident
    assert score.false_confident == 1
    assert score.false_confident_rate == pytest.approx(1.0)
    assert not score.false_confident_ok


def test_explicit_confidence_field_drives_false_confident():
    fx = _fixture([_q("q1", "T1", ["42"]), _q("q2", "T1", ["42"])])
    score = grade_tier3(
        fixture=fx,
        answers=[
            {"question_id": "q1", "answer": "999", "confidence": 0.95},  # confident wrong
            {"question_id": "q2", "answer": "999", "confidence": 0.10},  # not confident
        ],
    )
    assert score.confident_answers == 1
    assert score.false_confident == 1
    assert score.false_confident_rate == pytest.approx(1.0)


def test_confident_correct_is_not_false_confident():
    fx = _fixture([_q("q1", "T1", ["42"])])
    score = grade_tier3(
        fixture=fx, answers=[{"question_id": "q1", "answer": "There are 42."}]
    )
    assert score.confident_answers == 1
    assert score.false_confident == 0
    assert score.false_confident_ok


def test_hedged_wrong_answer_is_not_confident():
    fx = _fixture([_q("q1", "T1", ["42"])])
    score = grade_tier3(fixture=fx, answers=[{"question_id": "q1", "answer": "I don't know"}])
    g = _graded_by_id(score)
    assert not g["q1"].is_confident  # hedged -> excluded from the confident denominator
    assert score.false_confident == 0


# --------------------------------------------------------------------------- #
# Counter (c): citation fabrication.
# --------------------------------------------------------------------------- #
def test_citation_fabrication_counter_fires_on_unsupported_source():
    fx = _fixture(
        [_q("q1", "T1", ["Widget"], gold_citations=["https://sources.example/widget-registry"])]
    )
    # Supported citation (matches gold_citations) -> not fabricated.
    good = grade_tier3(
        fixture=fx,
        answers=[
            {
                "question_id": "q1",
                "answer": "Widget",
                "citations": ["https://sources.example/widget-registry"],
            }
        ],
    )
    assert good.total_citations == 1 and good.fabricated_citations == 0
    assert good.citation_fabrication_rate == pytest.approx(0.0)
    assert good.citation_fabrication_ok

    # Fabricated citation (nowhere in gold_citations / seed / gold values) -> flagged.
    bad = grade_tier3(
        fixture=fx,
        answers=[
            {
                "question_id": "q1",
                "answer": "Widget",
                "citations": ["https://made-up.example/fabricated"],
            }
        ],
    )
    assert bad.fabricated_citations == 1
    assert bad.citation_fabrication_rate == pytest.approx(1.0)
    assert not bad.citation_fabrication_ok
    assert "citation_fabrication" in bad.counter_failures()


def test_citation_naming_a_gold_value_is_supported():
    fx = _fixture([_q("q1", "T1", ["Sprocket"])])
    score = grade_tier3(
        fixture=fx,
        answers=[{"question_id": "q1", "answer": "Sprocket", "citations": ["Sprocket spec sheet"]}],
    )
    assert score.total_citations == 1 and score.fabricated_citations == 0


def test_citation_mapping_shape_is_accepted():
    fx = _fixture([_q("q1", "T1", ["Widget"], gold_citations=["https://seed.example/w"])])
    score = grade_tier3(
        fixture=fx,
        answers=[
            {
                "question_id": "q1",
                "answer": "Widget",
                "citations": [{"url": "https://ghost.example/x"}],
            }
        ],
    )
    assert score.total_citations == 1 and score.fabricated_citations == 1


# --------------------------------------------------------------------------- #
# A clean run trips NO counter (the good-case control).
# --------------------------------------------------------------------------- #
def test_clean_run_trips_no_counter():
    fx = _fixture(
        [
            _q("q1", "T1", ["42"], gold_citations=["https://seed.example/a"]),
            _q("q2", "T1", ["Widget", "Sprocket"]),
        ]
    )
    score = grade_tier3(
        fixture=fx,
        answers=[
            {"question_id": "q1", "answer": "42", "citations": ["https://seed.example/a"]},
            {"question_id": "q2", "answer": "Widget and Sprocket"},
        ],
    )
    assert score.overall_accuracy == pytest.approx(1.0)
    assert score.counters_ok and score.counter_failures() == []
    assert score.empty_answers == 0
    assert score.false_confident == 0
    assert score.fabricated_citations == 0


def test_thresholds_from_dict_partial_override():
    th = Tier3Thresholds.from_dict({"false_confident_ceiling": 0.25, "junk": "x"})
    assert th.false_confident_ceiling == pytest.approx(0.25)
    assert th.citation_fabrication_ceiling == pytest.approx(0.10)  # default kept


# --------------------------------------------------------------------------- #
# Wilson 95% CI.
# --------------------------------------------------------------------------- #
def test_wilson_interval_edges_and_bracketing():
    assert wilson_interval(0, 0) == (0.0, 0.0)
    lo, hi = wilson_interval(10, 10)
    assert 0.0 <= lo <= 1.0 and hi == pytest.approx(1.0, abs=1e-9)
    assert lo < 1.0  # even a perfect run has a lower bound below 1
    lo2, hi2 = wilson_interval(5, 10)
    assert lo2 < 0.5 < hi2  # brackets the point estimate


def test_wilson_ci_narrows_with_n():
    # Same accuracy (0.5), larger n -> tighter interval.
    lo_small, hi_small = wilson_interval(1, 2)
    lo_big, hi_big = wilson_interval(50, 100)
    assert (hi_small - lo_small) > (hi_big - lo_big)


def test_score_reports_per_tier_ci():
    fx = _fixture([_q("q1", "T1", ["42"]), _q("q2", "T1", ["7"])])
    score = grade_tier3(
        fixture=fx,
        answers=[
            {"question_id": "q1", "answer": "42"},
            {"question_id": "q2", "answer": "7"},
        ],
    )
    t1 = score.by_tier["T1"]
    assert t1.total == 2 and t1.passed == 2
    assert t1.ci_low <= t1.accuracy <= t1.ci_high
    assert 0.0 <= score.overall_ci_low <= score.overall_accuracy <= score.overall_ci_high <= 1.0


# --------------------------------------------------------------------------- #
# End-to-end round trip over the committed corpus (mechanism only, no domain
# string asserted): feeding each question's own gold back as the answer must
# score every fixture at 100% with no counter tripped.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "fixture_path",
    sorted(glob.glob(os.path.join(TIER3_FIXTURE_DIR, "*.json"))),
    ids=lambda p: os.path.basename(p),
)
def test_gold_roundtrip_scores_full_on_committed_fixtures(fixture_path):
    fx = load_fixture(fixture_path)
    answers = [
        {"question_id": q.id, "answer": ", ".join(q.full_expected_items)}
        for q in fx.questions
    ]
    score = grade_tier3(fixture=fx, answers=answers)
    assert score.overall_accuracy == pytest.approx(1.0), score.fixture_id
    assert score.counters_ok
    assert score.missing_answers == 0 and score.unexpected_answers == 0

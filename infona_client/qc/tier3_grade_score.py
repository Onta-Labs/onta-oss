"""Outcome-grader models and :func:`grade_tier3`.

Implementation sibling of :mod:`infona_client.qc.tier3_grade`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from infona_client.qc.tier3_fixture import TIER_LABELS, Tier3Fixture
from infona_client.qc.tier3_grade_verdict import (
    CORRECT,
    ERROR,
    PARTIAL,
    WRONG,
    _allowed_references,
    _citations_of,
    _is_confident,
    _is_empty_answer,
    _norm,
    _raw_verdict,
    _supported_citation,
    wilson_interval,
)

# --------------------------------------------------------------------------- #
# Thresholds + result bundle
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Tier3Thresholds:
    """Optional pass/fail bar for the anti-gaming counters. The headline accuracy is
    reported (and budgeted per-tier by the separate 283-E error-budget model), so
    these gates cover only the counters that must stay near zero.

    ``empty_answer_scored_correct`` is a hard invariant (must be exactly 0); the two
    rates are upper bounds (≤)."""

    false_confident_ceiling: float = 0.10
    citation_fabrication_ceiling: float = 0.10

    @classmethod
    def from_dict(cls, d: Optional[Mapping[str, Any]]) -> "Tier3Thresholds":
        if not d:
            return cls()
        fields = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: float(v) for k, v in d.items() if k in fields})


@dataclass(frozen=True)
class TierAccuracy:
    """Per-tier accuracy with a Wilson 95% CI and the verdict breakdown."""

    tier: str
    total: int
    passed: int          # verdict == correct
    partial: int
    wrong: int
    error: int
    accuracy: float      # passed / total
    ci_low: float
    ci_high: float


@dataclass(frozen=True)
class GradedAnswer:
    """The grade for one question — the per-answer provenance the bar ships with."""

    question_id: str
    tier: str
    verdict: str
    answer: str
    expected_items: tuple[str, ...]
    detail: str
    is_empty_answer: bool
    is_confident: bool
    citations: tuple[str, ...] = ()
    fabricated_citations: tuple[str, ...] = ()


@dataclass
class Tier3Score:
    """The scored bundle for one fixture run."""

    fixture_id: str

    # Headline
    overall_accuracy: float
    overall_ci_low: float
    overall_ci_high: float
    total: int
    passed: int
    by_tier: dict[str, TierAccuracy]

    # Anti-gaming counter (a): empty-answer guard
    empty_answers: int
    empty_answer_scored_correct: int   # INVARIANT: always 0 (the guard holds)
    empty_answer_guard_fired: int      # times the guard downgraded a would-be-correct empty
    empty_answer_rate: float

    # Anti-gaming counter (b): false-confident
    confident_answers: int
    false_confident: int
    false_confident_rate: float

    # Anti-gaming counter (c): citation fabrication
    answers_with_citations: int
    total_citations: int
    fabricated_citations: int
    citation_fabrication_rate: float

    # Alignment bookkeeping (a dropped answer is punished, never inflated)
    missing_answers: int      # gold questions with no submitted answer
    unexpected_answers: int   # submitted answers with no matching question

    # Per-gate verdicts
    empty_guard_ok: bool
    false_confident_ok: bool
    citation_fabrication_ok: bool
    counters_ok: bool

    thresholds: Tier3Thresholds
    graded: list[GradedAnswer] = field(default_factory=list)

    def counter_failures(self) -> list[str]:
        """Names of the anti-gaming gates this run violated (empty ⇒ clean)."""
        out: list[str] = []
        if not self.empty_guard_ok:
            out.append("empty_answer_scored_correct")
        if not self.false_confident_ok:
            out.append("false_confident")
        if not self.citation_fabrication_ok:
            out.append("citation_fabrication")
        return out


# --------------------------------------------------------------------------- #
# The grader
# --------------------------------------------------------------------------- #
def grade_tier3(
    *,
    fixture: Tier3Fixture,
    answers: Sequence[Mapping[str, Any]],
    thresholds: Optional[Tier3Thresholds] = None,
) -> Tier3Score:
    """Grade produced ``answers`` against a fixture's execution-verified gold. Pure —
    no I/O.

    ``answers`` is a list of records, each ``{"question_id": ..., "answer": ...}``
    with optional ``citations`` (list) and ``confidence`` (float) / ``confident``
    (bool). A record's answer aligns to a gold question by ``question_id`` (falling
    back to ``id``). A gold question with NO answer is scored ``error`` (a dropped
    answer is punished in the accuracy denominator, never silently skipped); an
    answer with no matching question is surfaced as ``unexpected`` and not scored.
    """
    th = thresholds or Tier3Thresholds()

    by_id: dict[str, Mapping[str, Any]] = {}
    for rec in answers:
        if not isinstance(rec, Mapping):
            continue
        qid = str(rec.get("question_id", rec.get("id", ""))).strip()
        if qid:
            by_id[qid] = rec  # last write wins on a duplicate id

    graded: list[GradedAnswer] = []
    # Per-tier tallies.
    tier_counts: dict[str, dict[str, int]] = {
        t: {CORRECT: 0, PARTIAL: 0, WRONG: 0, ERROR: 0} for t in TIER_LABELS
    }

    empty_answers = 0
    empty_answer_scored_correct = 0
    empty_answer_guard_fired = 0
    confident_answers = 0
    false_confident = 0
    answers_with_citations = 0
    total_citations = 0
    fabricated_citations = 0
    missing_answers = 0

    for q in fixture.questions:
        rec = by_id.get(q.id)
        if rec is None:
            # A missing answer is an error against the gold — counted, not skipped.
            missing_answers += 1
            tier_counts[q.tier][ERROR] += 1
            graded.append(
                GradedAnswer(
                    question_id=q.id,
                    tier=q.tier,
                    verdict=ERROR,
                    answer="",
                    expected_items=q.full_expected_items,
                    detail="no answer submitted for this question",
                    is_empty_answer=True,
                    is_confident=False,
                )
            )
            empty_answers += 1
            continue

        answer_text = "" if rec.get("answer") is None else str(rec.get("answer"))
        answer_norm = _norm(answer_text)
        is_empty = _is_empty_answer(answer_norm)

        raw_verdict, detail = _raw_verdict(answer_text, q)

        # Guard (a): an empty/idk answer must never be correct — even for gold-empty.
        verdict = raw_verdict
        if is_empty and raw_verdict == CORRECT:
            verdict = ERROR
            detail = "empty answer downgraded by guard (would have scored correct)"
            empty_answer_guard_fired += 1
        if is_empty:
            empty_answers += 1
            if verdict == CORRECT:  # invariant tripwire — must never happen
                empty_answer_scored_correct += 1

        # Counter (b): confident + wrong.
        confident = _is_confident(rec, answer_norm)
        if confident:
            confident_answers += 1
            if verdict == WRONG:
                false_confident += 1

        # Counter (c): fabricated citations.
        citations = _citations_of(rec)
        fabricated: list[str] = []
        if citations:
            answers_with_citations += 1
            allowed = _allowed_references(fixture, q)
            for c in citations:
                total_citations += 1
                if not _supported_citation(c, allowed):
                    fabricated.append(c)
            fabricated_citations += len(fabricated)

        tier_counts[q.tier][verdict] += 1
        graded.append(
            GradedAnswer(
                question_id=q.id,
                tier=q.tier,
                verdict=verdict,
                answer=answer_text,
                expected_items=q.full_expected_items,
                detail=detail,
                is_empty_answer=is_empty,
                is_confident=confident,
                citations=tuple(citations),
                fabricated_citations=tuple(fabricated),
            )
        )

    unexpected_answers = sum(1 for qid in by_id if qid not in fixture.question_ids)

    # Per-tier accuracy + Wilson CI.
    by_tier: dict[str, TierAccuracy] = {}
    for t in TIER_LABELS:
        c = tier_counts[t]
        total_t = c[CORRECT] + c[PARTIAL] + c[WRONG] + c[ERROR]
        passed_t = c[CORRECT]
        acc = (passed_t / total_t) if total_t else 0.0
        lo, hi = wilson_interval(passed_t, total_t)
        by_tier[t] = TierAccuracy(
            tier=t,
            total=total_t,
            passed=passed_t,
            partial=c[PARTIAL],
            wrong=c[WRONG],
            error=c[ERROR],
            accuracy=acc,
            ci_low=lo,
            ci_high=hi,
        )

    total = len(fixture.questions)
    passed = sum(tc[CORRECT] for tc in tier_counts.values())
    overall_acc = (passed / total) if total else 0.0
    overall_lo, overall_hi = wilson_interval(passed, total)

    empty_answer_rate = (empty_answers / total) if total else 0.0
    false_confident_rate = (
        (false_confident / confident_answers) if confident_answers else 0.0
    )
    citation_fabrication_rate = (
        (fabricated_citations / total_citations) if total_citations else 0.0
    )

    empty_guard_ok = empty_answer_scored_correct == 0
    false_confident_ok = false_confident_rate <= th.false_confident_ceiling
    citation_fabrication_ok = citation_fabrication_rate <= th.citation_fabrication_ceiling
    counters_ok = empty_guard_ok and false_confident_ok and citation_fabrication_ok

    return Tier3Score(
        fixture_id=fixture.id,
        overall_accuracy=overall_acc,
        overall_ci_low=overall_lo,
        overall_ci_high=overall_hi,
        total=total,
        passed=passed,
        by_tier=by_tier,
        empty_answers=empty_answers,
        empty_answer_scored_correct=empty_answer_scored_correct,
        empty_answer_guard_fired=empty_answer_guard_fired,
        empty_answer_rate=empty_answer_rate,
        confident_answers=confident_answers,
        false_confident=false_confident,
        false_confident_rate=false_confident_rate,
        answers_with_citations=answers_with_citations,
        total_citations=total_citations,
        fabricated_citations=fabricated_citations,
        citation_fabrication_rate=citation_fabrication_rate,
        missing_answers=missing_answers,
        unexpected_answers=unexpected_answers,
        empty_guard_ok=empty_guard_ok,
        false_confident_ok=false_confident_ok,
        citation_fabrication_ok=citation_fabrication_ok,
        counters_ok=counters_ok,
        thresholds=th,
        graded=graded,
    )

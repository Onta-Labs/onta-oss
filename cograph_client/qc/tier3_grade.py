"""Pure outcome grader for the Tier-3 whole-product QC capstone (ONTA-283-C).

Given a :class:`~cograph_client.qc.tier3_fixture.Tier3Fixture` and the answers a
pipeline PRODUCED for its questions, score the headline **goal → answer accuracy**
per difficulty tier — with a 95% confidence interval per tier — and the
anti-gaming counter-metrics. This is the A7 outcome half of the capstone
(``docs/specs/onta_283_tier3_capstone.md`` §4); the per-stage attribution and
error-budget halves (283-D/E) are separate later tickets and are NOT here.

The grader is **pure**: no network, no LLM call, no store — deterministic, so the
same (fixture, answers) always score identically. It is the read-side mirror of
the component-bar template (``verify_metrics.py`` / ``find_metrics.py``): the
headline metric ships WITH anti-gaming counters and per-answer provenance.

Verdict vocabulary (reused verbatim from ``eval.py``): every answer is one of
``correct`` / ``partial`` / ``wrong`` / ``error``. Per-tier accuracy counts an
answer as *passed* iff its verdict is ``correct``.

Tolerance rules (mirrored from ``eval.py``'s
``QueryEvaluator._execute_and_fast_judge`` — the fast programmatic judge, which is
a bound method wrapping an HTTP call and so is not cleanly importable as a pure
function; the rules below are the same and this comment is the pointer):

  * a **scalar** gold (one expected item that looks numeric): ``±2%`` for integer
    counts, ``±5%`` for floats (averages / sums);
  * a **scalar string** gold: case-insensitive CONTAINS (either direction), then a
    ≥0.6 word-overlap fallback;
  * a **set** gold (many expected items, e.g. "list all X"): coverage = the
    fraction of expected items found in the answer by case-insensitive substring;
    ``1.0`` ⇒ correct, ``0 < c < 1`` ⇒ partial, ``0`` ⇒ wrong.

Anti-gaming counters (each FIRES on a planted bad case; a run is "clean" only if
all three hold near zero):

  a. **empty-answer-scored-correct guard.** An empty or "I don't know" answer must
     NEVER count as correct — *even when the gold is empty* (the exploit: return
     "" for everything and coast on gold-empty questions). ``empty_answer_scored_
     correct`` is the guard's invariant and is ALWAYS 0; ``empty_answer_guard_
     fired`` counts the times the guard actually downgraded a would-be-correct
     empty answer; ``empty_answer_rate`` surfaces the raw empty share.
  b. **false-confident rate.** A CONFIDENT answer (non-empty, non-hedged, or one
     whose ``confidence`` ≥ threshold) that is nonetheless ``wrong``. Denominator is
     confident answers. Confidently stating a wrong fact is the failure this bar
     punishes beyond plain accuracy.
  c. **citation-fabrication rate.** Of the citations a run produced, the share not
     supported by the fixture (not in the question's ``gold_citations``, not in the
     source seed, and not naming a gold answer value). Fabricating a source to look
     grounded is caught here even when the *answer* happens to be right.

Boundary: OSS. Imports only stdlib + ``cograph_client.qc.tier3_fixture``.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from cograph_client.qc.tier3_fixture import (
    TIER_LABELS,
    Tier3Fixture,
    Tier3GoldQuestion,
)

__all__ = [
    "CORRECT",
    "PARTIAL",
    "WRONG",
    "ERROR",
    "Tier3Thresholds",
    "TierAccuracy",
    "GradedAnswer",
    "Tier3Score",
    "wilson_interval",
    "grade_tier3",
]

# Verdict vocabulary — identical to eval.py's QuestionResult.verdict values.
CORRECT = "correct"
PARTIAL = "partial"
WRONG = "wrong"
ERROR = "error"

# An answer is treated as CONFIDENT when its declared confidence is at least this.
_CONFIDENCE_THRESHOLD = 0.5

# Blank / "I don't know" answers — the empty-answer guard's trigger set. A truly
# substantive "no results found" claim is deliberately NOT here (it is a real
# assertion the grader scores, not a non-answer); this set is only the hedges /
# blanks a gaming agent emits to avoid answering.
_EMPTY_ANSWER_PATTERNS = (
    "i don't know",
    "i dont know",
    "idk",
    "i'm not sure",
    "im not sure",
    "not sure",
    "no answer",
    "n/a",
    "na",
    "unknown",
    "unable to answer",
    "cannot answer",
    "can't answer",
    "cannot determine",
    "could not answer",
)

# Explicit "the result is empty" assertions — the CORRECT answer to a gold-empty
# question, and distinct from a blank/idk non-answer.
_CONVEYS_EMPTY_PATTERNS = (
    "no results",
    "no matching",
    "no records",
    "no rows",
    "none found",
    "nothing found",
    "empty result",
)


# --------------------------------------------------------------------------- #
# Wilson 95% confidence interval (no scipy)
# --------------------------------------------------------------------------- #
def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """The Wilson score interval for a binomial proportion — the same CI method the
    holdout-v2 ``cross_llm_comparison`` report uses, implemented here with only
    ``math`` so OSS carries no scipy dependency.

    ``z=1.96`` is the 95% two-sided normal quantile. ``total == 0`` ⇒ ``(0.0, 0.0)``.
    The bounds are clamped to ``[0, 1]``.
    """
    if total <= 0:
        return (0.0, 0.0)
    phat = successes / total
    z2 = z * z
    denom = 1.0 + z2 / total
    center = (phat + z2 / (2.0 * total)) / denom
    margin = (z * math.sqrt((phat * (1.0 - phat) + z2 / (4.0 * total)) / total)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


# --------------------------------------------------------------------------- #
# Answer normalization + verdict helpers (mirror eval.py fast-judge)
# --------------------------------------------------------------------------- #
def _norm(text: Any) -> str:
    """Lower-case, collapse whitespace, strip. ``None`` → ``""``."""
    if text is None:
        return ""
    return " ".join(str(text).strip().casefold().split())


def _is_empty_answer(answer_norm: str) -> bool:
    """True for a blank or "I don't know"-style non-answer (the guard's trigger)."""
    if not answer_norm:
        return True
    stripped = answer_norm.strip().strip(".!\"'")
    return stripped in _EMPTY_ANSWER_PATTERNS


def _conveys_empty(answer_norm: str) -> bool:
    """True when the answer explicitly asserts an empty result (the correct answer
    to a gold-empty question). A bare ``0`` also counts."""
    if answer_norm.strip().strip(".!\"'") == "0":
        return True
    return any(p in answer_norm for p in _CONVEYS_EMPTY_PATTERNS)


def _numbers(text: str) -> list[float]:
    """Every number in ``text`` (supports sign, decimals, scientific notation)."""
    out: list[float] = []
    for tok in re.findall(r"-?\d+\.?\d*(?:[eE][+-]?\d+)?", text):
        try:
            out.append(float(tok))
        except ValueError:
            pass
    return out


def _looks_numeric(expected: str) -> bool:
    """Whether an expected scalar should be compared numerically — the same
    alpha-ratio heuristic as eval.py (descriptive text is not reduced to a number)."""
    if not expected:
        return False
    alpha = sum(1 for c in expected if c.isalpha())
    return (alpha / max(len(expected), 1)) < 0.3


def _scalar_verdict(answer: str, expected: str) -> tuple[str, str]:
    """Score a single-item gold. Mirrors eval.py's ±2% (counts) / ±5% (floats)
    numeric tolerance and case-insensitive CONTAINS / word-overlap string rules."""
    answer_norm = _norm(answer)
    expected_norm = _norm(expected)

    if _looks_numeric(expected):
        exp_nums = _numbers(expected)
        ans_nums = _numbers(answer)
        if not exp_nums:
            return WRONG, "expected looked numeric but held no number"
        if not ans_nums:
            return WRONG, "answer holds no number to compare"
        e_abs = abs(exp_nums[0])
        a_abs = abs(ans_nums[0])
        if e_abs == 0:
            return (CORRECT, "both zero") if a_abs == 0 else (WRONG, f"{a_abs} vs 0")
        # A non-".0" decimal expected ⇒ a float (avg/sum): ±5%. Else a count: ±2%.
        is_float = "." in expected and expected.split(".")[-1] not in ("", "0")
        tol = 0.05 if is_float else 0.02
        diff = abs(a_abs - e_abs) / max(e_abs, 1e-9)
        if diff <= tol:
            return CORRECT, f"within {tol * 100:.0f}% ({a_abs} vs {e_abs})"
        return WRONG, f"outside {tol * 100:.0f}% ({a_abs} vs {e_abs}, {diff * 100:.1f}%)"

    # String scalar: case-insensitive CONTAINS either direction, then word overlap.
    if expected_norm and (expected_norm in answer_norm or answer_norm in expected_norm):
        return CORRECT, "string contains match"
    exp_words = set(re.findall(r"[a-z0-9]{3,}", expected_norm))
    ans_words = set(re.findall(r"[a-z0-9]{3,}", answer_norm))
    if exp_words and ans_words:
        overlap = len(exp_words & ans_words) / len(exp_words)
        if overlap >= 0.6:
            return CORRECT, f"word overlap {overlap * 100:.0f}%"
        return WRONG, f"word overlap {overlap * 100:.0f}% < 60%"
    return WRONG, "no string match"


def _set_verdict(answer: str, expected_items: Sequence[str]) -> tuple[str, str]:
    """Score a multi-item gold by coverage: the fraction of expected items present
    in the answer by case-insensitive substring. Full ⇒ correct, some ⇒ partial,
    none ⇒ wrong."""
    answer_norm = _norm(answer)
    total = len(expected_items)
    found = sum(1 for item in expected_items if _norm(item) and _norm(item) in answer_norm)
    coverage = found / total if total else 0.0
    if found == total:
        return CORRECT, f"all {total} expected items present"
    if found > 0:
        return PARTIAL, f"{found}/{total} expected items present ({coverage * 100:.0f}%)"
    return WRONG, f"0/{total} expected items present"


def _raw_verdict(answer: str, question: Tier3GoldQuestion) -> tuple[str, str]:
    """The verdict BEFORE the empty-answer guard, so the wrapper can detect a
    would-be-correct empty answer and downgrade it."""
    answer_norm = _norm(answer)
    if question.gold_is_empty:
        # The correct answer is "no results". A blank/idk OR an explicit no-results
        # assertion both convey emptiness — raw-correct here; the guard then blocks
        # the blank/idk case in the wrapper.
        if _is_empty_answer(answer_norm) or _conveys_empty(answer_norm):
            return CORRECT, "gold empty; answer conveys no-results"
        return WRONG, "gold empty but answer asserts content"

    if _is_empty_answer(answer_norm):
        return ERROR, "empty answer, gold non-empty"

    items = question.full_expected_items
    if len(items) == 1:
        return _scalar_verdict(answer, items[0])
    return _set_verdict(answer, items)


# --------------------------------------------------------------------------- #
# Confidence + citation helpers
# --------------------------------------------------------------------------- #
def _is_confident(record: Mapping[str, Any], answer_norm: str) -> bool:
    """Whether an answer is CONFIDENT. An explicit ``confidence`` (float ≥ threshold)
    or ``confident`` (bool) wins; otherwise infer: non-empty and non-hedged."""
    if "confidence" in record and record["confidence"] is not None:
        try:
            return float(record["confidence"]) >= _CONFIDENCE_THRESHOLD
        except (TypeError, ValueError):
            pass
    if "confident" in record and record["confident"] is not None:
        return bool(record["confident"])
    return not _is_empty_answer(answer_norm)


def _citations_of(record: Mapping[str, Any]) -> list[str]:
    """Pull a flat list of citation strings from an answer record. Accepts a list of
    strings or a list of ``{url|source|citation|id: ...}`` mappings."""
    raw = record.get("citations")
    if not raw:
        return []
    if isinstance(raw, (str, bytes)):
        return [str(raw)]
    if not isinstance(raw, Sequence):
        return []
    out: list[str] = []
    for c in raw:
        if isinstance(c, Mapping):
            val = c.get("url") or c.get("source") or c.get("citation") or c.get("id") or ""
            if str(val).strip():
                out.append(str(val))
        elif str(c).strip():
            out.append(str(c))
    return out


def _supported_citation(citation: str, allowed: Sequence[str]) -> bool:
    """A citation is SUPPORTED iff it matches (case-insensitive substring, either
    direction) any allowed reference — the question's ``gold_citations``, the source
    seed, or a gold answer value. Anything else is fabricated."""
    cnorm = _norm(citation)
    if not cnorm:
        return False
    for ref in allowed:
        rnorm = _norm(ref)
        if rnorm and (rnorm in cnorm or cnorm in rnorm):
            return True
    return False


def _allowed_references(fixture: Tier3Fixture, question: Tier3GoldQuestion) -> list[str]:
    """The legitimate citation references for one question: its ``gold_citations``,
    the fixture's source seed (bundled path or pinned URLs), and the gold answer
    values (a citation naming a real answer value is not a fabrication)."""
    refs: list[str] = list(question.gold_citations)
    seed = fixture.source_seed
    if seed.path:
        refs.append(seed.path)
    refs.extend(seed.urls)
    refs.extend(question.full_expected_items)
    return refs


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

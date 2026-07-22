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

Boundary: OSS. Imports only stdlib + ``cograph_client.qc.tier3_fixture`` + the
shared key-normalizer from ``cograph_client.pipeline.find_metrics`` (so coverage
matching cannot drift from the P1 Find bar).

ONTA-384 extension — **enumeration + scoped-schema profile bar**. The outcome
grader above scores A7 answers. The profile grader below scores the *graph the
pipeline produced* for an enumeration goal with a scoped attribute set, against
three independent failure modes that the BC-universities regression compounded:

  1. **coverage** (guards P1 / ONTA-379) — fraction of the expected institution
     set present under key-normalization. Broken profile ≈ 5/40.
  2. **scope-adherence** (guards P2 / ONTA-380+382) — produced attribute leaves
     ⊆ requested ∪ structural. Broken profile ≈ 49 attrs for a 3-field goal.
  3. **fragmentation** (guards P5 / ONTA-383) — distinct types vs allowed set +
     absence of forbidden junk types. Broken profile ≈ 17 types incl. Colour /
     Asset / Online / InstructionMode.

Each metric ships WITH an anti-gaming counter (QC rule). See
:class:`ProfileThresholds` for the documented pass floors/ceilings that fail
today's broken profile and pass after P1/P2/P5 land.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from cograph_client.pipeline.find_metrics import normalize_key
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
    # ONTA-384 profile bar
    "GraphProfileSnapshot",
    "ProfileThresholds",
    "EnumerationProfileScore",
    "grade_enumeration_profile",
    "BROKEN_BC_PROFILE",
    "POST_FIX_PROFILE_THRESHOLDS",
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


# =========================================================================== #
# ONTA-384 — enumeration + scoped-schema graph-profile bar
# =========================================================================== #
#
# Pass thresholds designed so:
#   * today's broken BC-universities profile (~5/40 entities, ~49 attrs,
#     ~17 types with junk Colour/Asset/Online/InstructionMode) FAILS all three
#     headline metrics and their anti-gaming counters;
#   * a post-P1/P2/P5 clean profile (≥50% coverage, attrs ⊆ requested±structural,
#     ≤6 types with no forbidden junk) PASSES.
#
# These numbers are the contract — do not loosen them to green a broken pipeline.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GraphProfileSnapshot:
    """Offline snapshot of what the pipeline produced for an enumeration goal.

    Pure data — the live harness (283-B) or a unit test constructs one from the
    graph / discovery rows. No I/O here.

    * ``entity_keys`` — the identity values of produced entities (the
      ``key_attribute``, typically ``name``). Near-duplicates and off-roster
      noise are kept so the anti-gaming counters can see them.
    * ``attributes`` — distinct attribute *leaves* observed on the produced
      ontology / instance data (not full URIs — ``website``, not
      ``attrs/website``).
    * ``types`` — distinct type labels / local names observed (``University``,
      not the full type URI).
    """

    entity_keys: tuple[str, ...]
    attributes: tuple[str, ...]
    types: tuple[str, ...]

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "GraphProfileSnapshot":
        def _as_str_tuple(key: str) -> tuple[str, ...]:
            raw = d.get(key, ())
            if raw is None:
                return ()
            if isinstance(raw, (str, bytes)):
                return (str(raw),)
            if not isinstance(raw, Sequence):
                return ()
            return tuple(str(x) for x in raw if str(x).strip() or str(x) == "0")

        return cls(
            entity_keys=_as_str_tuple("entity_keys"),
            attributes=_as_str_tuple("attributes"),
            types=_as_str_tuple("types"),
        )


@dataclass(frozen=True)
class ProfileThresholds:
    """Pass/fail bar for the enumeration + scoped-schema profile metrics.

    Floors are lower bounds (≥), ceilings upper bounds (≤). Defaults are the
    **post-P1/P2/P5** pass contract (see module docstring +
    ``POST_FIX_PROFILE_THRESHOLDS``); they deliberately fail the broken profile
    numbers documented as ``BROKEN_BC_PROFILE``.
    """

    # Coverage (P1)
    coverage_floor: float = 0.50
    # Anti-gaming (coverage): off-roster share of produced entities.
    off_roster_ceiling: float = 0.30
    # Anti-gaming (coverage): near-dup collapse rate among produced entity keys.
    near_dup_ceiling: float = 0.15

    # Scope-adherence (P2)
    scope_adherence_floor: float = 0.80
    # Anti-gaming (scope): fraction of requested attrs that must appear at least
    # once — blocks "emit nothing → perfect scope" gaming.
    requested_attr_coverage_floor: float = 0.67
    # Anti-gaming (scope): absolute out-of-scope attr count ceiling.
    max_out_of_scope_attrs: int = 5

    # Fragmentation (P5)
    max_types: int = 6
    # Anti-gaming (fragmentation): forbidden junk types must be absent.
    max_forbidden_types: int = 0
    # Anti-gaming (fragmentation): at least this many allowed types must appear
    # — blocks "emit zero types → type_count=0 passes ceiling" gaming.
    min_allowed_types_present: int = 1

    @classmethod
    def from_dict(cls, d: Optional[Mapping[str, Any]]) -> "ProfileThresholds":
        if not d:
            return cls()
        fields = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        coerced: dict[str, Any] = {}
        for k, v in d.items():
            if k not in fields:
                continue
            if k in ("max_out_of_scope_attrs", "max_types", "max_forbidden_types",
                     "min_allowed_types_present"):
                coerced[k] = int(v)
            else:
                coerced[k] = float(v)
        return cls(**coerced)


# Documented post-fix pass contract (alias of the defaults for test import).
POST_FIX_PROFILE_THRESHOLDS = ProfileThresholds()

# Today's broken BC-universities profile numbers (regression control). A scorer
# that does not fail this snapshot is broken. Counts are approximate midpoints
# of the live job that motivated ONTA-379/380/382/383/384.
BROKEN_BC_PROFILE = GraphProfileSnapshot(
    # ~5 of ~40 expected institutions.
    entity_keys=(
        "University of British Columbia",
        "Simon Fraser University",
        "University of Victoria",
        "British Columbia Institute of Technology",
        "Langara College",
    ),
    # User asked only name/website/type; pipeline emitted ~49 leaves.
    attributes=(
        "name",
        "website",
        "type",
        "label",
        # 45 fabricated / out-of-scope leaves (attr explosion + invention).
        "online_activity_percentage_of_summer_instruction",
        "affordability_ranking",
        "colour",
        "asset_value",
        "instruction_mode",
        "campus_size_hectares",
        "founded_year_approximate",
        "student_body_diversity_index",
        "parking_spaces",
        "dormitory_capacity",
        "endowment_usd_estimate",
        "research_output_score",
        "alumni_network_size",
        "international_student_pct",
        "acceptance_rate_guess",
        "average_class_size",
        "library_volumes",
        "sports_teams_count",
        "mascot_name",
        "school_colors",
        "motto_latin",
        "president_name",
        "board_chair",
        "accreditation_body",
        "qs_rank_world",
        "times_rank_world",
        "maclean_rank_canada",
        "tuition_domestic_cad",
        "tuition_international_cad",
        "application_fee_cad",
        "sat_required",
        "toefl_min",
        "ielts_min",
        "housing_guarantee",
        "meal_plan_available",
        "wifi_coverage_pct",
        "lab_count",
        "patent_count_annual",
        "startup_spinouts",
        "nobel_affiliates",
        "olympic_athletes",
        "climate_action_score",
        "transit_score",
        "bike_racks",
        "cafeteria_rating",
    ),
    # 17 types incl. junk Colour / Asset / Online / InstructionMode.
    types=(
        "University",
        "College",
        "PublicInstitution",
        "PrivateInstitution",
        "Polytechnic",
        "Institute",
        "CommunityCollege",
        "ArtSchool",
        "ResearchUniversity",
        "TeachingUniversity",
        "Colour",
        "Asset",
        "Online",
        "InstructionMode",
        "Campus",
        "Faculty",
        "Program",
    ),
)


@dataclass
class EnumerationProfileScore:
    """Scored bundle for one enumeration + scoped-schema graph profile."""

    fixture_id: str

    # --- Coverage (P1) ---
    coverage: float
    gold_total: int
    found_gold_entities: int
    produced_entity_total: int
    distinct_produced_entities: int
    off_roster_entities: int
    off_roster_rate: float
    near_dup_collapsed_rows: int
    near_dup_collapse_rate: float
    coverage_ok: bool
    off_roster_ok: bool
    near_dup_ok: bool

    # --- Scope-adherence (P2) ---
    scope_adherence: float
    attribute_total: int
    in_scope_attributes: int
    out_of_scope_attributes: int
    out_of_scope_attr_names: tuple[str, ...]
    requested_attr_coverage: float
    requested_present: int
    requested_total: int
    scope_adherence_ok: bool
    requested_attr_coverage_ok: bool
    out_of_scope_count_ok: bool

    # --- Fragmentation (P5) ---
    type_count: int
    allowed_types_present: int
    forbidden_types_present: int
    forbidden_type_names: tuple[str, ...]
    extra_type_names: tuple[str, ...]
    type_count_ok: bool
    forbidden_types_ok: bool
    allowed_presence_ok: bool

    # Aggregate
    passed: bool
    thresholds: ProfileThresholds

    def failures(self) -> list[str]:
        """Names of the gates this profile violated (empty ⇒ passed)."""
        out: list[str] = []
        if not self.coverage_ok:
            out.append("coverage")
        if not self.off_roster_ok:
            out.append("off_roster")
        if not self.near_dup_ok:
            out.append("near_dup_collapse")
        if not self.scope_adherence_ok:
            out.append("scope_adherence")
        if not self.requested_attr_coverage_ok:
            out.append("requested_attr_coverage")
        if not self.out_of_scope_count_ok:
            out.append("out_of_scope_count")
        if not self.type_count_ok:
            out.append("type_count")
        if not self.forbidden_types_ok:
            out.append("forbidden_types")
        if not self.allowed_presence_ok:
            out.append("allowed_type_presence")
        return out


def _build_alias_map(alias_table: Sequence[tuple[str, str]]) -> dict[str, str]:
    """Normalize both sides of variant→canonical so lookups are case/ws free."""
    return {normalize_key(k): normalize_key(v) for k, v in alias_table}


def _canonical(value: Any, alias_map: Mapping[str, str]) -> str:
    nk = normalize_key(value)
    return alias_map.get(nk, nk)


def grade_enumeration_profile(
    *,
    fixture: Tier3Fixture,
    profile: GraphProfileSnapshot,
    thresholds: Optional[ProfileThresholds] = None,
) -> EnumerationProfileScore:
    """Score a produced graph profile against a fixture's ``enumeration_scope``.

    Pure — no I/O. Raises ``ValueError`` if the fixture has no
    ``enumeration_scope`` (the profile bar is only meaningful for enumeration +
    scoped-schema goals).
    """
    scope = fixture.enumeration_scope
    if scope is None:
        raise ValueError(
            f"fixture {fixture.id!r} has no enumeration_scope; cannot grade a profile"
        )
    th = thresholds or ProfileThresholds()
    alias_map = _build_alias_map(scope.alias_table)

    # ------------------------------------------------------------------ #
    # Coverage (P1)
    # ------------------------------------------------------------------ #
    gold_canon = {
        _canonical(g, alias_map) for g in scope.expected_entities if normalize_key(g)
    }
    gold_total = len(gold_canon)

    produced_keys: list[str] = []
    for raw in profile.entity_keys:
        ckey = _canonical(raw, alias_map)
        if ckey:
            produced_keys.append(ckey)

    produced_distinct = set(produced_keys)
    found_gold = len(gold_canon & produced_distinct)
    coverage = (found_gold / gold_total) if gold_total else 0.0

    # Anti-gaming (a): off-roster rate — entities produced that are NOT in gold.
    # Padding the result with noise to look "busy" without covering the roster.
    off_roster = len(produced_distinct - gold_canon)
    produced_entity_total = len(profile.entity_keys)
    distinct_produced = len(produced_distinct)
    off_roster_rate = (off_roster / distinct_produced) if distinct_produced else 0.0

    # Anti-gaming (b): near-dup collapse — restating the same entity under
    # key-normalization to pad raw counts (coverage itself is set-based so this
    # does not inflate the headline; it flags the gaming attempt).
    collapsed = len(produced_keys) - len(produced_distinct)
    near_dup_rate = (collapsed / produced_entity_total) if produced_entity_total else 0.0

    coverage_ok = coverage >= th.coverage_floor
    off_roster_ok = off_roster_rate <= th.off_roster_ceiling
    near_dup_ok = near_dup_rate <= th.near_dup_ceiling

    # ------------------------------------------------------------------ #
    # Scope-adherence (P2)
    # ------------------------------------------------------------------ #
    def _attr_key(a: str) -> str:
        # Accept either a bare leaf ("website") or a trailing URI segment.
        s = str(a).strip()
        if "/" in s:
            s = s.rstrip("/").rsplit("/", 1)[-1]
        return normalize_key(s)

    requested = {_attr_key(a) for a in scope.requested_attributes}
    structural = {_attr_key(a) for a in scope.structural_attributes}
    # The key attribute is always in-scope even if the author forgot it.
    structural.add(_attr_key(scope.key_attribute))
    allowed_attrs = requested | structural

    produced_attrs = [_attr_key(a) for a in profile.attributes if str(a).strip()]
    # Distinct by normalized leaf.
    produced_attr_set = {a for a in produced_attrs if a}
    attribute_total = len(produced_attr_set)
    in_scope = produced_attr_set & allowed_attrs
    out_of_scope = produced_attr_set - allowed_attrs
    # Preserve a stable-ish order for reporting (sorted).
    out_of_scope_names = tuple(sorted(out_of_scope))

    scope_adherence = (len(in_scope) / attribute_total) if attribute_total else 0.0
    # Anti-gaming (a): requested-attr coverage — must surface the fields the
    # user asked for. Emitting zero attrs would otherwise score scope=0 which
    # fails the floor, but emitting only structural attrs (name/label/type)
    # would score high scope while ignoring the goal's field set.
    requested_present = len(requested & produced_attr_set)
    requested_total = len(requested)
    requested_attr_coverage = (
        (requested_present / requested_total) if requested_total else 1.0
    )

    scope_adherence_ok = scope_adherence >= th.scope_adherence_floor
    requested_attr_coverage_ok = (
        requested_attr_coverage >= th.requested_attr_coverage_floor
    )
    out_of_scope_count_ok = len(out_of_scope) <= th.max_out_of_scope_attrs

    # ------------------------------------------------------------------ #
    # Fragmentation (P5)
    # ------------------------------------------------------------------ #
    def _type_key(t: str) -> str:
        s = str(t).strip()
        if "/" in s:
            s = s.rstrip("/").rsplit("/", 1)[-1]
        # Strip a trailing fragment if present.
        if "#" in s:
            s = s.rsplit("#", 1)[-1]
        return normalize_key(s)

    produced_types = {_type_key(t) for t in profile.types if str(t).strip()}
    type_count = len(produced_types)

    allowed_types = {_type_key(t) for t in scope.allowed_types if str(t).strip()}
    forbidden_types = {_type_key(t) for t in scope.forbidden_types if str(t).strip()}

    forbidden_hit = produced_types & forbidden_types
    # Extra = produced types neither allowed nor (if allowed is empty) anything.
    # When the fixture declares no allowed_types, we only gate on count + forbidden.
    if allowed_types:
        extra = produced_types - allowed_types
        allowed_present = len(produced_types & allowed_types)
    else:
        extra = set()
        # No allow-list declared → presence gate is vacuously satisfied once
        # *some* type exists (or min is 0).
        allowed_present = type_count if th.min_allowed_types_present == 0 else (
            type_count if type_count >= th.min_allowed_types_present else type_count
        )
        # When no allow-list, the presence gate only requires type_count >= min
        # if min > 0; we treat "any types present" as the signal.
        if th.min_allowed_types_present > 0:
            allowed_present = type_count  # compared below against min

    type_count_ok = type_count <= th.max_types
    forbidden_types_ok = len(forbidden_hit) <= th.max_forbidden_types
    if allowed_types:
        allowed_presence_ok = allowed_present >= th.min_allowed_types_present
    else:
        # No allow-list: require at least min types present (still blocks empty).
        allowed_presence_ok = type_count >= th.min_allowed_types_present

    passed = (
        coverage_ok
        and off_roster_ok
        and near_dup_ok
        and scope_adherence_ok
        and requested_attr_coverage_ok
        and out_of_scope_count_ok
        and type_count_ok
        and forbidden_types_ok
        and allowed_presence_ok
    )

    return EnumerationProfileScore(
        fixture_id=fixture.id,
        coverage=coverage,
        gold_total=gold_total,
        found_gold_entities=found_gold,
        produced_entity_total=produced_entity_total,
        distinct_produced_entities=distinct_produced,
        off_roster_entities=off_roster,
        off_roster_rate=off_roster_rate,
        near_dup_collapsed_rows=collapsed,
        near_dup_collapse_rate=near_dup_rate,
        coverage_ok=coverage_ok,
        off_roster_ok=off_roster_ok,
        near_dup_ok=near_dup_ok,
        scope_adherence=scope_adherence,
        attribute_total=attribute_total,
        in_scope_attributes=len(in_scope),
        out_of_scope_attributes=len(out_of_scope),
        out_of_scope_attr_names=out_of_scope_names,
        requested_attr_coverage=requested_attr_coverage,
        requested_present=requested_present,
        requested_total=requested_total,
        scope_adherence_ok=scope_adherence_ok,
        requested_attr_coverage_ok=requested_attr_coverage_ok,
        out_of_scope_count_ok=out_of_scope_count_ok,
        type_count=type_count,
        allowed_types_present=allowed_present,
        forbidden_types_present=len(forbidden_hit),
        forbidden_type_names=tuple(sorted(forbidden_hit)),
        extra_type_names=tuple(sorted(extra)),
        type_count_ok=type_count_ok,
        forbidden_types_ok=forbidden_types_ok,
        allowed_presence_ok=allowed_presence_ok,
        passed=passed,
        thresholds=th,
    )

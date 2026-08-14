"""Answer normalization, Wilson CI, and verdict helpers for the Tier-3 grader.

Implementation sibling of :mod:`infona_client.qc.tier3_grade`.
"""

from __future__ import annotations

import math
import re
from typing import Any, Mapping, Sequence

from infona_client.qc.tier3_fixture import Tier3Fixture, Tier3GoldQuestion

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


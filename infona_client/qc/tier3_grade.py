"""Pure outcome grader for the Tier-3 whole-product QC capstone (ONTA-283-C).

Given a :class:`~infona_client.qc.tier3_fixture.Tier3Fixture` and the answers a
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

Boundary: OSS. Imports only stdlib + ``infona_client.qc.tier3_fixture`` + the
shared key-normalizer from ``infona_client.pipeline.find_metrics`` (so coverage
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

Implementation lives in sibling ``tier3_grade_*.py`` modules. Every previously
importable name is re-exported here.
"""

from __future__ import annotations

from infona_client.qc.tier3_grade_profile import (  # noqa: F401 — public re-exports
    BROKEN_BC_PROFILE,
    POST_FIX_PROFILE_THRESHOLDS,
    EnumerationProfileScore,
    GraphProfileSnapshot,
    ProfileThresholds,
    _build_alias_map,
    _canonical,
    grade_enumeration_profile,
)
from infona_client.qc.tier3_grade_score import (  # noqa: F401 — public re-exports
    GradedAnswer,
    Tier3Score,
    Tier3Thresholds,
    TierAccuracy,
    grade_tier3,
)
from infona_client.qc.tier3_grade_verdict import (  # noqa: F401 — public re-exports
    CORRECT,
    ERROR,
    PARTIAL,
    WRONG,
    _CONFIDENCE_THRESHOLD,
    _CONVEYS_EMPTY_PATTERNS,
    _EMPTY_ANSWER_PATTERNS,
    _allowed_references,
    _citations_of,
    _conveys_empty,
    _is_confident,
    _is_empty_answer,
    _looks_numeric,
    _norm,
    _numbers,
    _raw_verdict,
    _scalar_verdict,
    _set_verdict,
    _supported_citation,
    wilson_interval,
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

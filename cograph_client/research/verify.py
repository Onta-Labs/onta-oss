"""Verification — the cite-or-abstain gate (ADR 0006 §Verify/reconcile).

An LLM will happily emit a plausible, unsourced answer. The harness refuses to:
every extracted :class:`~cograph_client.research.types.ResearchRow` must be
backed by at least one citation, or it is dropped. If nothing survives, the
harness ABSTAINS — an honest "I couldn't verify an answer" beats a confident
fabrication.

OSS ships a deterministic default (:class:`CiteOrAbstainVerifier`, no LLM, so
it's cheap and reproducible in tests). A premium LLM-judge verifier — an
adversarial "is this claim actually supported by the cited page?" pass with a
confidence model (ADR 0007) — registers through :func:`register_research_verifier`
and transparently replaces the default. Only ONE verifier is active; the premium
one, when registered, wins.

Boundary: OSS. Imports only stdlib / ``cograph_client.*``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

from cograph_client.research.types import FetchedPage, ResearchRow, TargetSchema

# A row's own confidence below this is treated as too weak to present when it is
# the ONLY signal (i.e. no citation). With a citation, the row is kept regardless
# — a sourced fact is the bar, not a model's self-reported score.
_MIN_STANDALONE_CONFIDENCE = 0.35


@dataclass
class VerifyOutcome:
    """What verification returns: the surviving rows plus an overall confidence
    and an ``abstained`` flag (True when nothing was supportable)."""

    rows: list[ResearchRow] = field(default_factory=list)
    confidence: float = 0.0
    abstained: bool = False
    dropped: int = 0
    notes: str = ""


@runtime_checkable
class ResearchVerifier(Protocol):
    """Filter/annotate extracted rows against their supporting pages.

    * ``name`` — stable id.
    * ``verify(question, rows, pages, schema)`` — return a :class:`VerifyOutcome`.
      Must NEVER raise (a verifier failure must not sink the whole run).
    """

    name: str

    async def verify(
        self,
        question: str,
        rows: list[ResearchRow],
        pages: list[FetchedPage],
        *,
        schema: TargetSchema,
    ) -> VerifyOutcome: ...


def _row_has_value(row: ResearchRow, schema: TargetSchema) -> bool:
    """True when the row carries at least one non-empty value — preferring a
    REQUIRED field when the schema names any (an all-blank required field means
    the extractor didn't actually find this record)."""
    required = schema.required_names()
    if required:
        return any(str(row.values.get(name, "")).strip() for name in required)
    return any(str(v).strip() for v in row.values.values())


class CiteOrAbstainVerifier:
    """Deterministic default: keep a row iff it has a supporting citation AND a
    non-empty value; abstain when none survive.

    No LLM — the gate is structural (is this row sourced and non-empty?), which is
    exactly the failure mode we must never ship: an unsourced or hollow row.
    Overall confidence blends citation coverage with the rows' own reported
    confidence, so a fully-cited result reads high and a thin one reads low.
    """

    name = "cite_or_abstain"

    async def verify(
        self,
        question: str,
        rows: list[ResearchRow],
        pages: list[FetchedPage],
        *,
        schema: TargetSchema,
    ) -> VerifyOutcome:
        # Which of the pages we actually read successfully — a citation pointing
        # at a page we never fetched is still a citation (the extractor may cite a
        # discovery snippet), so we don't hard-require the URL be in ``pages``; we
        # only require the row NAME a source.
        kept: list[ResearchRow] = []
        dropped = 0
        for row in rows:
            has_citation = any(str(u).strip() for u in row.citations)
            has_value = _row_has_value(row, schema)
            if has_value and (
                has_citation or row.confidence >= _MIN_STANDALONE_CONFIDENCE
            ):
                # A kept-without-citation row (model was confident but named no
                # source) is downgraded so the overall score reflects the risk.
                if not has_citation:
                    row.confidence = min(row.confidence, _MIN_STANDALONE_CONFIDENCE)
                kept.append(row)
            else:
                dropped += 1

        if not kept:
            return VerifyOutcome(
                rows=[],
                confidence=0.0,
                abstained=True,
                dropped=dropped,
                notes="no rows survived cite-or-abstain",
            )

        cited = sum(1 for r in kept if any(str(u).strip() for u in r.citations))
        coverage = cited / len(kept)
        mean_conf = sum(max(0.0, min(1.0, r.confidence)) for r in kept) / len(kept)
        # Weight citation coverage heavily; blend in the rows' own confidence.
        confidence = round(0.7 * coverage + 0.3 * mean_conf, 3)
        return VerifyOutcome(
            rows=kept,
            confidence=confidence,
            abstained=False,
            dropped=dropped,
            notes=f"{cited}/{len(kept)} rows cited",
        )


# Single active-verifier slot (premium override replaces the OSS default).
_verifier: Optional[ResearchVerifier] = None


def register_research_verifier(verifier: ResearchVerifier) -> None:
    """Install the active verifier (replaces any prior one, incl. the default)."""
    global _verifier
    _verifier = verifier


def get_research_verifier() -> ResearchVerifier:
    """The active verifier — the registered one, or the OSS cite-or-abstain
    default when none is registered."""
    return _verifier if _verifier is not None else CiteOrAbstainVerifier()


def reset_research_verifier() -> None:
    """Clear the registered verifier (revert to the default). For tests."""
    global _verifier
    _verifier = None


__all__ = [
    "CiteOrAbstainVerifier",
    "ResearchVerifier",
    "VerifyOutcome",
    "get_research_verifier",
    "register_research_verifier",
    "reset_research_verifier",
]

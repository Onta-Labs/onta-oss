"""Match, attribute-resolution, validation, and A3 clean models.

Extracted from ``resolver/models.py``. Public names stay importable from
``infona_client.resolver.models``.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from infona_client.api_registry.spec import AuthorityLevel

# ---------------------------------------------------------------------------
# Type matching
# ---------------------------------------------------------------------------


class MatchVerdict(str, Enum):
    SAME = "SAME"
    SUBTYPE = "SUBTYPE"
    DIFFERENT = "DIFFERENT"
    FLAGGED = "FLAGGED"  # 3-way split, needs user review


class TypeMatch(BaseModel):
    """Result of matching a proposed type against the existing ontology."""

    proposed: str
    resolved: str = Field(description="The resolved type name (existing or new)")
    verdict: MatchVerdict
    confidence: float = Field(ge=0.0, le=1.0)
    is_new: bool = False
    parent_type: str | None = None  # set when verdict is SUBTYPE
    inconclusive: bool = False  # True when the verifier couldn't reach a real decision (e.g. LLM unavailable)


# ---------------------------------------------------------------------------
# Attribute resolution
# ---------------------------------------------------------------------------


class AttrAction(str, Enum):
    REUSE = "REUSE"
    COERCE = "COERCE"
    EXTEND = "EXTEND"
    PROMOTE = "PROMOTE"  # Option D: flat → structured coexistence


class ResolvedAttribute(BaseModel):
    """Result of resolving one attribute against the ontology."""

    name: str
    value: str
    datatype: str
    action: AttrAction
    original_value: str | None = None  # set when coerced
    promoted_type: str | None = None  # set when action is PROMOTE


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class ValidationOutcome(str, Enum):
    OK = "OK"
    COERCED = "COERCED"
    REJECTED = "REJECTED"


class ValidatedTriple(BaseModel):
    """A triple that passed schema-on-write validation — the A4 (Verified) fact.

    ONTA-276: a verified fact optionally carries the trust signals the write-time
    conflict policy (``pipeline/conflict.py``) arbitrates on when this fact
    collides with an existing value on a FUNCTIONAL attribute. Source-of-truth
    priority is set upstream (P1) but dies before the conflict point unless it is
    carried on the fact through A4 — these fields are that carrier. All are
    OPTIONAL with defaults, so every existing ``ValidatedTriple(...)`` construction
    parses and validates unchanged; a fact with no explicit ``authority`` /
    ``confidence`` simply falls to the policy's neutral defaults.
    """

    subject: str
    predicate: str
    object: str
    outcome: ValidationOutcome = ValidationOutcome.OK
    original_value: str | None = None  # set when coerced
    # ONTA-347: the per-attribute SURFACE-FORM companion triple
    # (``<entity> <attr_meta/<Type>/<attr>/surface_form> "<original>"``) built when
    # the A3 clean stage COERCED or CANONICALIZED this value (raw != canonical),
    # else None. It preserves the ORIGINAL pre-clean value in the graph — metadata
    # OF the attribute on the attr_meta namespace, structurally invisible to every
    # user surface (is_internal_predicate) yet queryable — so P4 Verify can compare
    # the stored canonical value against evidence in its original form. The writer
    # threads it into the SAME insert_facts call as ``object`` (never a domain fact
    # about the subject, so it gets no provenance record of its own). Optional +
    # back-compat: every existing ``ValidatedTriple(...)`` construction (and the
    # frozen a4/a5 boundary fixtures, which read explicit fields) is unchanged.
    surface_form_companion: tuple[str, str, str] | None = None
    # Trust signals carried through A4 for write-time conflict resolution (ONTA-276).
    authority: AuthorityLevel | None = Field(
        default=None,
        description=(
            "Source-authority level this fact was verified under (reuses the "
            "AuthorityLevel scale: source_of_truth > authoritative > "
            "supplementary). None = authority unknown; the conflict policy ranks "
            "it weakest."
        ),
    )
    confidence: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description=(
            "Verification confidence in this fact's value (0-1). None lets the "
            "conflict policy fall back to the calibrated confidence implied by "
            "``authority``."
        ),
    )
    source: str = Field(
        default="",
        description="Provenance source label this fact was verified from (carried onto its provenance record).",
    )


class RejectedValue(BaseModel):
    """A value that failed validation."""

    entity_id: str
    attribute: str
    value: str
    expected_datatype: str
    reason: str


# ---------------------------------------------------------------------------
# A3 — the explicit Clean stage (ONTA-344)
# ---------------------------------------------------------------------------


class CleanOutcome(str, Enum):
    """How one A2 candidate value fared in the A3 clean stage — the three-way
    partition every consumed value lands in EXACTLY once (the zero-silent-drops
    ledger).

    Distinct from :class:`ValidationOutcome` (the A4 typing outcome): a value that
    conforms yet is lexically canonicalized (``"True"`` -> ``"true"``) is A3
    ``TRANSFORMED`` but still A4 ``OK`` — A3 records the cleaning A4's typing
    silently hides."""

    PASSED = "passed"  # conforms as-is AND already canonical → written verbatim
    TRANSFORMED = "transformed"  # coerced and/or lexically canonicalized to fit
    DROPPED = "dropped"  # cannot be coerced to the datatype → not written


class CleanFact(BaseModel):
    """One A3 clean fact: a single A2 candidate value after the clean stage.

    ``clean_value`` is the canonical lexical form the A4 typing step
    (``validate_triple``) will stamp with an XSD datatype (``None`` when DROPPED).
    ``conformed`` records whether the value passed ``validate_value`` as-is (no
    coercion needed) — it drives A4's OK vs COERCED outcome, so A3 owns the
    coerce/canonicalize/reject DECISION while A4 owns the typing. Every consumed
    value yields exactly one CleanFact."""

    datatype: str
    raw_value: str
    clean_value: str | None
    outcome: CleanOutcome
    conformed: bool = True
    reason: str = ""
    entity_id: str = ""
    attribute: str = ""


class CleanReport(BaseModel):
    """The A3 ledger: every value the clean stage consumed, partitioned exactly
    once into ``passed`` / ``transformed`` / ``dropped`` — the zero-silent-drops
    guarantee (mirrors ADR 0003 §2 row conservation). ``total`` conserves:
    ``len(inputs) == passed + transformed + dropped``."""

    passed: list[CleanFact] = Field(default_factory=list)
    transformed: list[CleanFact] = Field(default_factory=list)
    dropped: list[CleanFact] = Field(default_factory=list)

    def record(self, fact: CleanFact) -> CleanFact:
        """File one clean fact into its outcome partition and return it."""
        bucket = {
            CleanOutcome.PASSED: self.passed,
            CleanOutcome.TRANSFORMED: self.transformed,
            CleanOutcome.DROPPED: self.dropped,
        }[fact.outcome]
        bucket.append(fact)
        return fact

    @property
    def total(self) -> int:
        return len(self.passed) + len(self.transformed) + len(self.dropped)

    def counts(self) -> dict[str, int]:
        """Partition sizes + total — the count-conservation summary."""
        return {
            "passed": len(self.passed),
            "transformed": len(self.transformed),
            "dropped": len(self.dropped),
            "total": self.total,
        }


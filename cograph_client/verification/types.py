"""A4 (Verify) artifact types — the EPISTEMIC verified fact (ONTA-361).

Onta's pipeline decomposes into a chain of frozen inter-stage artifacts (A0-A10;
see ``pipeline/envelope.py``). This module owns the P4 **Verify** stage's core
artifact: a :class:`VerifiedFact` — the epistemic verdict about whether an A3
clean fact is *corroborated by independent evidence*.

**READ THIS — the A4 naming collision, resolved.** Two very different things are
both informally called "A4":

  * The **mechanical** A4 (``resolver/validator.validate_triple`` →
    :class:`cograph_client.resolver.models.ValidatedTriple`): schema-on-write
    typing/coercion — "is this value a well-formed ``xsd:float`` we may persist?".
    This is what ``qc/boundary.py``'s ``"a4"`` tier freezes.
  * The **epistemic** A4 THIS module owns (:class:`VerifiedFact`): "is this fact
    *true* — does independent evidence support, refute, or fail to corroborate
    it?". A ``ValidatedTriple`` can be perfectly well-typed and still be
    :attr:`TruthVerdict.UNVERIFIABLE` or :attr:`TruthVerdict.REFUTED`.

They are NOT the same artifact and must never be conflated: a well-formed triple
is not a *verified* one. :class:`VerifiedFact` therefore carries a deliberately
distinct name (never ``ValidatedTriple``) and its own frozen characterization
lives OUTSIDE the ``qc/boundary.py`` harness (``tests/test_verification_boundary.py``
+ ``tests/fixtures/verification/``), so the mechanical a2/a3/a4/a5 boundary
fixtures stay byte-identical.

Boundary: OSS. Imports only stdlib + ``cograph_client.*`` — never ``from
cograph.*``. No network anywhere in this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Sequence
from urllib.parse import urlparse

from cograph_client.pipeline.envelope import ArtifactEnvelope
from cograph_client.resolver.models import CleanFact

__all__ = [
    "TruthVerdict",
    "EvidenceRef",
    "VerifierResult",
    "VerifiedFact",
    "VerifyContext",
]


class TruthVerdict(str, Enum):
    """The epistemic verdict P4 assigns an A3 clean fact — its TRUTH status given
    the evidence gathered, NOT its datatype well-formedness (that is the mechanical
    A4 :class:`~cograph_client.resolver.models.ValidationOutcome`, a separate axis).

      * **SUPPORTED** — independent evidence corroborates the fact. A fact is never
        SUPPORTED by its OWN source alone: corroboration must come from at least one
        source distinct from where the fact was ingested.
      * **REFUTED** — independent evidence contradicts the fact's value.
      * **UNVERIFIABLE** — no independent evidence was found either way (the default
        the offline verifier returns; a fact is NOT "verified" until corroborated).
      * **IDENTITY_CONDITIONAL** — the verdict depends on an unresolved entity
        identity ("supported IF this ``City`` is the same San Jose the evidence
        names"). The identity rail (ONTA-365) resolves these; the offline default
        never emits one.
    """

    SUPPORTED = "supported"
    REFUTED = "refuted"
    UNVERIFIABLE = "unverifiable"
    IDENTITY_CONDITIONAL = "identity_conditional"


def _host_of(url: str) -> str:
    """The network host of a source URL, for grouping evidence by origin — a plain
    cosmetic parse (``urlparse().netloc``), NOT a fetch or an SSRF check (those live
    in ``retrieval/safety.py`` and are out of scope for this offline module)."""
    try:
        return urlparse((url or "").strip()).netloc or ""
    except Exception:  # pragma: no cover - defensive; urlparse is very tolerant
        return ""


@dataclass(frozen=True)
class EvidenceRef:
    """One INDEPENDENT piece of evidence a verifier weighed for a fact — minimal but
    real: where the evidence came from and the exact snippet that speaks to the fact.

    ``source_url`` is the citation; ``host`` is its origin (auto-derived from the URL
    when not supplied) — the verifier uses host distinctness to enforce "independent
    of the fact's own source". ``snippet`` is the quoted passage the verdict rests on.
    Evidence gathering itself is a SEPARATE ticket (ONTA-364); this is only the shape
    a gathered ref takes."""

    source_url: str
    snippet: str = ""
    host: str = ""

    def __post_init__(self) -> None:
        if not self.host and self.source_url:
            object.__setattr__(self, "host", _host_of(self.source_url))

    @classmethod
    def from_url(cls, source_url: str, snippet: str = "") -> "EvidenceRef":
        """Build a ref from a URL, deriving ``host`` from it."""
        return cls(source_url=source_url, snippet=snippet, host=_host_of(source_url))

    def to_dict(self) -> dict[str, Any]:
        return {"source_url": self.source_url, "host": self.host, "snippet": self.snippet}


@dataclass(frozen=True)
class VerifierResult:
    """What a :class:`~cograph_client.verification.verifier.FactVerifier` returns for
    ONE fact: the verdict + its supporting evidence + a confidence, WITHOUT any
    pipeline identity.

    Deliberately envelope-free (mirrors how ``auth.AuthVerdict`` is separate from the
    resolved ``TenantContext``): a verifier decides the epistemics; the orchestrator
    (:func:`~cograph_client.verification.verifier.verify_clean_facts`) stamps the
    :class:`ArtifactEnvelope` lineage when it wraps this into a :class:`VerifiedFact`.
    Keeping identity out of the verifier's contract means a premium verifier never
    has to know about ``run_id`` / ``fact_id`` derivation."""

    verdict: TruthVerdict
    confidence: float = 0.0
    evidence: tuple[EvidenceRef, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, tuple):
            object.__setattr__(self, "evidence", tuple(self.evidence))
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"VerifierResult.confidence must be in [0, 1] (got {self.confidence!r})"
            )


@dataclass(frozen=True)
class VerifiedFact:
    """The A4 EPISTEMIC verified fact (ONTA-361) — an A3 clean fact plus a truth
    verdict, its independent evidence, and pipeline lineage.

    NOT :class:`~cograph_client.resolver.models.ValidatedTriple` (the *mechanical* A4:
    schema-on-write typing). A ``ValidatedTriple`` says "well-formed enough to
    persist"; a ``VerifiedFact`` says "corroborated (or not) by independent evidence".
    See the module docstring for the full collision resolution.

    Source fields (``entity_id`` / ``attribute`` / ``datatype`` / ``value`` /
    ``surface_form``) are copied from the consumed A3 :class:`CleanFact`. ``value`` is
    the A3 *clean* (canonical) value; ``surface_form`` is the original pre-clean value
    (ONTA-347) — verification compares evidence against the SURFACE form, not the
    coerced value, so it is carried explicitly.

    ``envelope`` is the universal :class:`ArtifactEnvelope`: its ``fact_id`` is derived
    (``derive_fact_id``, stage ``"A4"``) with the consumed A3 fact's id as the single
    parent, so this fact's lineage threads back to the clean fact it verified."""

    entity_id: str
    attribute: str
    datatype: str
    value: Optional[str]
    verdict: TruthVerdict
    envelope: ArtifactEnvelope
    surface_form: Optional[str] = None
    confidence: float = 0.0
    evidence: tuple[EvidenceRef, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, tuple):
            object.__setattr__(self, "evidence", tuple(self.evidence))
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"VerifiedFact.confidence must be in [0, 1] (got {self.confidence!r})"
            )

    @property
    def fact_id(self) -> str:
        """This verified fact's stable pipeline id (its envelope's ``fact_id``)."""
        return self.envelope.fact_id

    @classmethod
    def from_clean(
        cls,
        clean: CleanFact,
        result: VerifierResult,
        envelope: ArtifactEnvelope,
    ) -> "VerifiedFact":
        """Wrap a verifier's :class:`VerifierResult` for ``clean`` into a
        :class:`VerifiedFact` with the given (already-derived) A4 envelope.

        ``surface_form`` is the A3 ``raw_value`` when the clean stage TRANSFORMED the
        value (``raw_value != clean_value``), else ``None`` — the same ONTA-347 rule
        the writer uses to decide whether a surface-form companion is worth persisting.
        """
        surface = clean.raw_value if clean.raw_value != clean.clean_value else None
        return cls(
            entity_id=clean.entity_id,
            attribute=clean.attribute,
            datatype=clean.datatype,
            value=clean.clean_value,
            surface_form=surface,
            verdict=result.verdict,
            confidence=result.confidence,
            evidence=tuple(result.evidence),
            reason=result.reason,
            envelope=envelope,
        )

    def to_dict(self) -> dict[str, Any]:
        """Full JSON-ready projection, including the envelope (with ``observed_at``).

        For a DETERMINISTIC characterization/diff, drop ``observed_at`` from the
        envelope — it is a wall-clock stamp; see ``tests/test_verification_boundary.py``.
        """
        return {
            "entity_id": self.entity_id,
            "attribute": self.attribute,
            "datatype": self.datatype,
            "value": self.value,
            "surface_form": self.surface_form,
            "verdict": self.verdict.value,
            "confidence": self.confidence,
            "reason": self.reason,
            "evidence": [e.to_dict() for e in self.evidence],
            "envelope": self.envelope.to_dict(),
        }


@dataclass(frozen=True)
class VerifyContext:
    """Optional context threaded to a :class:`FactVerifier` beyond the bare A3 fact —
    what the fact is ABOUT, so a verifier (esp. the future evidence-gathering one,
    ONTA-364) knows what to look for.

    All fields optional/defaulted so the default offline verifier needs none. Kept
    minimal on purpose: ``subject`` is the entity URI the fact is asserted on and
    ``type_name`` its ontology type; ``workspace_id`` / ``run_id`` mirror the envelope
    scope. Richer search hints belong to the evidence-gathering ticket, not here."""

    workspace_id: str = ""
    run_id: str = ""
    subject: str = ""
    type_name: str = ""

"""A10 Machine Re-verification — the machine-correction write path (ONTA-363).

A10 (**Correction & Feedback**) has two authors. The human path
(``pipeline/corrections.py``) lets a person fix a fact in the Explorer. THIS module
is the MACHINE path: a fresh re-verification of a fact ALREADY in the graph —
run by a :class:`~cograph_client.verification.verifier.FactVerifier` over the
current in-graph value — emits an A10 Correction back into P6 when that value turns
out to be stale/wrong.

**The authority contract — the whole point of this module.** A machine re-verify is
stamped at :data:`MACHINE_REVERIFICATION_AUTHORITY`
(``AuthorityLevel.machine_reverification``), which sits STRICTLY BELOW
``user_assertion`` (and below ``source_of_truth``) but ABOVE
``authoritative``/``supplementary`` in the ONE shared authority scale
(``api_registry/spec.py``: ``AUTHORITY_RANK`` / ``AUTHORITY_CONFIDENCE`` — nothing
forks a parallel scale). Two consequences fall out of that rank alone, decided by
the ONTA-276 conflict policy, not by any bespoke logic here:

  * a re-verify SUPERSEDES a stale value that came from a weaker scraped source
    (``authoritative`` / ``supplementary``) — the fresh value wins, the stale one
    is closed deprecated-but-queryable; and
  * a re-verify can NEVER overrule a human's fix: when the current value carries
    ``user_assertion`` authority, the user's value survives and the machine's
    proposed value lands deprecated-but-queryable. This is the validity-interval
    resurrection bug class Waves 3–4 guarded against, held shut here by rank.

**Verdicts ANNOTATE; P6 EXECUTES.** This module does not hand-roll any removal.
When a fresh :class:`VerifierResult` REFUTES the in-graph value and evidence yields
a corrected value distinct from it, the correction is written through the ONE
converged conflict writer
(:func:`~cograph_client.pipeline.mutations.write_with_conflict_resolution`) at the
machine-reverification authority; that writer performs the supersession (closing
the loser's validity interval — never a delete) and keeps the loser queryable with
its provenance. A SUPPORTED / UNVERIFIABLE / IDENTITY_CONDITIONAL verdict, or a
REFUTED verdict with no distinct corrected value, writes NOTHING — the verdict is
an annotation only.

Boundary: OSS. Imports only stdlib / ``cograph_client.*`` — never ``from cograph.*``.
No network anywhere in this module (the verdict is produced upstream by a
``FactVerifier``; this only routes the resulting correction onto the write path).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import structlog

from cograph_client.api_registry.spec import AUTHORITY_CONFIDENCE, AuthorityLevel
from cograph_client.graph.ontology_queries import attr_uri, entity_uri
from cograph_client.pipeline.conflict import (
    DEFAULT_CONFLICT_POLICY,
    ConflictPolicy,
)
from cograph_client.pipeline.envelope import ArtifactEnvelope
from cograph_client.pipeline.mutations import (
    DEFAULT_RECENCY_POLICY,
    ConflictReceipt,
    RecencyPolicy,
    write_with_conflict_resolution,
)
from cograph_client.verification.types import TruthVerdict, VerifierResult

logger = structlog.stdlib.get_logger("cograph.verification.reverify")

Triple = tuple[str, str, str]

# The authority every machine re-verify correction is stamped at — STRICTLY below
# ``user_assertion`` and ``source_of_truth``, ABOVE ``authoritative`` — pulled from
# the ONE shared scale so the ONTA-276 conflict policy ranks it correctly without a
# parallel rank being invented. A re-verify supersedes a stale scrape; a user fix
# survives a re-verify.
MACHINE_REVERIFICATION_AUTHORITY = AuthorityLevel.machine_reverification
_MACHINE_REVERIFICATION_CONFIDENCE = AUTHORITY_CONFIDENCE[
    AuthorityLevel.machine_reverification
]

# Default provenance ``source`` recorded when the caller names no verifier — so a
# re-verify correction is always attributable to the re-verify path, never blank.
_DEFAULT_VERIFIER_SOURCE = MACHINE_REVERIFICATION_AUTHORITY.value

# Parse the leaf ``<Type>`` out of a canonical entity IRI
# (``…/entities/<Type>/<slug>``) — mirrors ``pipeline/corrections.py`` so a
# re-verify can derive the entity's type from its subject URI when not named.
_ENTITY_URI_RE = re.compile(r"^https://cograph\.tech/entities/([^/]+)/.+$")


def _type_from_entity_uri(subject: str) -> str:
    """Return the ``<Type>`` segment of a canonical entity IRI, or ``""``."""
    m = _ENTITY_URI_RE.match(subject or "")
    return m.group(1) if m else ""


class MachineReverificationError(ValueError):
    """Raised when a :class:`MachineReverification` cannot resolve a subject/type."""


@dataclass(frozen=True)
class MachineReverification:
    """One A10 machine re-verification of a fact already in the graph.

    Identify the fact one of two equivalent ways (mirrors
    :class:`~cograph_client.pipeline.corrections.UserAssertion`):

    * ``subject`` — the full canonical entity IRI; or
    * ``type_name`` + ``entity_id`` — the caller-side pair, minted to the SAME
      canonical IRI via the shared :func:`entity_uri` (never re-implemented).

    ``predicate`` is the exact predicate the fact lives on (for the common
    literal-attribute case build it with :func:`literal_attribute_predicate`); the
    model stays predicate-agnostic. ``current_value`` is the in-graph object term
    that was re-verified (exactly as written to the store). ``result`` is the fresh
    :class:`VerifierResult` a ``FactVerifier`` returned for that value.

    ``corrected_value`` is the value the fresh evidence says is right — supplied by
    the caller (extracted from ``result.evidence`` upstream, since a bare
    ``VerifierResult`` carries only the epistemic verdict, not a replacement term).
    A correction is written ONLY when the verdict REFUTES ``current_value`` AND
    ``corrected_value`` is non-empty and distinct from it (:attr:`warrants_correction`).

    ``verifier`` names the re-verifier for provenance (defaults to the
    ``machine_reverification`` label). ``observed_at`` is when the re-verify ran
    (defaults to now at write time); ``reason`` an optional free-text note.
    ``envelope`` is the OPTIONAL universal A1-A10 metadata carrier — when supplied
    its ``run_id`` threads the A6 receipt identity.
    """

    predicate: str
    current_value: str
    result: VerifierResult
    corrected_value: str = ""
    verifier: str = ""
    subject: str = ""
    type_name: str = ""
    entity_id: str = ""
    observed_at: Optional[datetime] = None
    reason: str = ""
    envelope: Optional[ArtifactEnvelope] = None

    def resolved_subject(self) -> str:
        """The canonical entity IRI this re-verify targets — explicit ``subject``,
        else minted from ``type_name`` + ``entity_id`` via the shared
        :func:`entity_uri` (the ONE entity-IRI minter). Raises when neither form is
        available."""
        if self.subject:
            return self.subject
        if self.type_name and self.entity_id:
            return entity_uri(self.type_name, self.entity_id)
        raise MachineReverificationError(
            "MachineReverification needs a subject IRI or (type_name + entity_id)"
        )

    def resolved_type(self) -> str:
        """The entity's type name — explicit ``type_name``, else parsed from the
        subject IRI. Raises when neither yields one (so the conflict writer's
        recency policy + post-write refresh always key on a real type)."""
        if self.type_name:
            return self.type_name
        derived = _type_from_entity_uri(self.resolved_subject())
        if derived:
            return derived
        raise MachineReverificationError(
            f"cannot derive type_name from subject {self.resolved_subject()!r}; "
            "pass type_name explicitly"
        )

    @property
    def warrants_correction(self) -> bool:
        """True iff this re-verify should WRITE a correction: the verdict REFUTES the
        in-graph value AND a distinct ``corrected_value`` is available.

        SUPPORTED (the value is corroborated), UNVERIFIABLE (no evidence either
        way), and IDENTITY_CONDITIONAL (identity unresolved — the ONTA-365 rail's
        job) never write; nor does a REFUTED verdict with no corrected value or one
        equal to the current value. Whether the written correction then WINS is
        decided by the conflict policy on authority — not here."""
        return (
            self.result.verdict is TruthVerdict.REFUTED
            and bool(self.corrected_value)
            and self.corrected_value != self.current_value
        )

    @property
    def effective_confidence(self) -> float:
        """The confidence stamped on the correction: the fresh verdict's own
        confidence when positive, else the calibrated ``machine_reverification``
        confidence. Authority (not confidence) is the load-bearing axis — this only
        matters against an equally-authoritative existing value."""
        c = self.result.confidence
        return c if c > 0.0 else _MACHINE_REVERIFICATION_CONFIDENCE


@dataclass(frozen=True)
class MachineReverificationReceipt:
    """The outcome of a machine re-verify.

    ``applied`` is True when a correction was WRITTEN through the conflict writer (a
    REFUTED verdict with a distinct corrected value); False when the verdict was an
    annotation only (SUPPORTED / UNVERIFIABLE / IDENTITY_CONDITIONAL, or nothing to
    correct) and nothing was written.

    When ``applied``, ``conflict_receipt`` is the
    :class:`~cograph_client.pipeline.mutations.ConflictReceipt` the converged writer
    returned. Its ``winner`` reveals what the authority arbitration decided:
    :attr:`superseded_stale` is True when the machine's corrected value WON (a stale
    scrape retired); :attr:`preserved_existing` is True when an existing
    stronger-authority value (e.g. a ``user_assertion`` human fix) WON and the
    machine's value landed deprecated-but-queryable instead — the invariant that a
    re-verify never clobbers a user fix."""

    op: str  # always "machine_reverify"
    applied: bool
    verdict: TruthVerdict
    reason: str = ""
    current_value: str = ""
    corrected_value: str = ""
    conflict_receipt: Optional[ConflictReceipt] = None

    @property
    def superseded_stale(self) -> bool:
        """True iff a correction was applied AND the machine's corrected value won
        (the stale value was superseded)."""
        r = self.conflict_receipt
        return bool(self.applied and r is not None and r.winner[2] == self.corrected_value)

    @property
    def preserved_existing(self) -> bool:
        """True iff a correction was applied AND an EXISTING value won instead of the
        machine's proposal (e.g. a user fix survived the re-verify)."""
        r = self.conflict_receipt
        return bool(self.applied and r is not None and r.winner[2] != self.corrected_value)


def literal_attribute_predicate(type_name: str, attribute: str) -> str:
    """The predicate a LITERAL attribute value lives on:
    ``types/<Type>/attrs/<attribute>`` — via the shared :func:`attr_uri`.

    Kept here (not inlined at the caller) so the ``attrs/`` vs ``onto/`` predicate
    convention is decided in one place on the write side, mirroring
    ``pipeline/corrections.py``."""
    return attr_uri(type_name, attribute)


async def apply_machine_reverification(
    neptune,
    instance_graph: str,
    reverification: MachineReverification,
    *,
    run_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    kg_name: Optional[str] = None,
    conflict_policy: ConflictPolicy = DEFAULT_CONFLICT_POLICY,
    recency_policy: RecencyPolicy = DEFAULT_RECENCY_POLICY,
    manifest=None,
) -> MachineReverificationReceipt:
    """Apply a machine re-verify as an A10 correction — the P6 write op for a fresh
    machine verdict on an in-graph fact.

    Adds NO new write primitive: when the re-verify
    :attr:`~MachineReverification.warrants_correction` (a REFUTED verdict with a
    distinct corrected value), it routes the corrected value through the ONE
    converged conflict writer
    (:func:`~cograph_client.pipeline.mutations.write_with_conflict_resolution`),
    stamped at :data:`MACHINE_REVERIFICATION_AUTHORITY` with the re-verify's
    :attr:`~MachineReverification.effective_confidence`. That writer:

    * reads the existing current value's authority back from provenance,
    * ranks the machine-reverification authority against it — STRICTLY below any
      ``user_assertion`` human fix (which therefore wins), ABOVE a scraped
      ``authoritative``/``supplementary`` value (which the correction supersedes),
    * writes the winner current and CLOSES the loser's validity interval with
      ``STATUS_DEPRECATED`` (present-but-not-current, never deleted), keeping it
      queryable with its provenance, and
    * runs one ``refresh_after_write``.

    Everything funnels through ``kg_writer`` — no hand-rolled write, no bespoke
    removal. ``run_id`` threads the A6 receipt identity (defaults to the
    reverification's envelope ``run_id`` when one is carried). ``tenant_id`` /
    ``kg_name`` scope the post-write refresh; when omitted, the conflict writer
    parses them from the instance-graph URI.

    When the verdict does NOT warrant a correction, NOTHING is written and a receipt
    with ``applied=False`` is returned — the verdict annotates only.
    """
    subject = reverification.resolved_subject()
    verdict = reverification.result.verdict

    if not reverification.warrants_correction:
        logger.info(
            "apply_machine_reverification.noop",
            subject=subject,
            predicate=reverification.predicate,
            verdict=verdict.value,
        )
        return MachineReverificationReceipt(
            op="machine_reverify",
            applied=False,
            verdict=verdict,
            reason=reverification.result.reason,
            current_value=reverification.current_value,
            corrected_value=reverification.corrected_value,
            conflict_receipt=None,
        )

    type_name = reverification.resolved_type()
    at = reverification.observed_at or datetime.now(timezone.utc)
    effective_run_id = run_id or (
        reverification.envelope.run_id
        if reverification.envelope is not None
        else None
    )

    receipt = await write_with_conflict_resolution(
        neptune,
        instance_graph,
        subject=subject,
        predicate=reverification.predicate,
        type_name=type_name,
        value=reverification.corrected_value,
        authority=MACHINE_REVERIFICATION_AUTHORITY,
        confidence=reverification.effective_confidence,
        source=reverification.verifier or _DEFAULT_VERIFIER_SOURCE,
        observed_at=at,
        run_id=effective_run_id,
        reason=reverification.reason or "machine re-verification (A10)",
        tenant_id=tenant_id,
        kg_name=kg_name,
        conflict_policy=conflict_policy,
        recency_policy=recency_policy,
        manifest=manifest,
    )

    out = MachineReverificationReceipt(
        op="machine_reverify",
        applied=True,
        verdict=verdict,
        reason=receipt.reason,
        current_value=reverification.current_value,
        corrected_value=reverification.corrected_value,
        conflict_receipt=receipt,
    )
    logger.info(
        "apply_machine_reverification",
        subject=subject,
        predicate=reverification.predicate,
        verdict=verdict.value,
        authority=MACHINE_REVERIFICATION_AUTHORITY.value,
        winner=receipt.winner[2],
        superseded_stale=out.superseded_stale,
        preserved_existing=out.preserved_existing,
    )
    return out


__all__ = [
    "MachineReverification",
    "MachineReverificationError",
    "MachineReverificationReceipt",
    "MACHINE_REVERIFICATION_AUTHORITY",
    "apply_machine_reverification",
    "literal_attribute_predicate",
]

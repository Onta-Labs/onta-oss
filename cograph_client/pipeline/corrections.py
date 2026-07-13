"""A10 User Assertions — the human-correction write path (ONTA-281).

A10 (**Correction & Feedback**) is the last artifact in the P0-P9 decomposition:
a human looks at a fact in the Explorer, sees it is wrong, and asserts the right
value. Until now the pipeline had NO write path for a human correction, and the
freshness loop (P8, Wave 4) would silently re-scrape and clobber it — "a design
partner corrects their own phone number and it reverts" is the exact trust-killer
this module exists to prevent.

**How a correction can never be clobbered — the two-part contract.**

1. *Top provenance rank.* The corrected fact is stamped, in the shared companion
   provenance graph, at the NEW top ``AuthorityLevel.user_assertion`` — above
   ``source_of_truth`` in the ONE authority scale (``api_registry/spec.py``:
   ``AUTHORITY_RANK`` / ``AUTHORITY_CONFIDENCE``). Nothing forks a parallel scale.

2. *The ONTA-276 conflict policy does the rest, for free.* Because the user's
   value now carries the strongest authority any fact can have, when a later P8
   refresh writes a contradicting scrape through
   ``write_with_conflict_resolution`` (ONTA-276), that policy reads the existing
   current value's authority back from provenance, ranks ``user_assertion`` above
   the incoming ``source_of_truth`` scrape, and keeps the user's value current —
   the scrape lands deprecated-but-queryable. "A refresh never clobbers a user
   fix" falls out of machinery that already exists; this module writes nothing
   special into the read path.

**The writer is pure orchestration, no new write primitive.** It composes the
ONTA-277 ``supersede_fact`` op (closes the wrong value's validity interval, makes
the corrected value current, emits the A6 ``GraphDelta`` receipt) with the
top-authority provenance built by the shared ``graph/provenance.py`` builder and
threaded through ``supersede_fact``'s existing ``provenance_triples`` seam — so
every write still funnels through ``kg_writer`` (``insert_facts`` /
``refresh_after_write``). It does NOT modify ONTA-277's or ONTA-276's op bodies.

Boundary: OSS. Imports only stdlib / ``cograph_client.*`` — never ``from cograph.*``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import structlog

from cograph_client.api_registry.spec import AUTHORITY_CONFIDENCE, AuthorityLevel
from cograph_client.graph.ontology_queries import attr_uri, entity_uri
from cograph_client.graph.provenance import build_provenance_triples
from cograph_client.pipeline.envelope import ArtifactEnvelope
from cograph_client.pipeline.mutations import (
    DEFAULT_RECENCY_POLICY,
    MutationReceipt,
    RecencyPolicy,
    supersede_fact,
)

logger = structlog.stdlib.get_logger("cograph.pipeline.corrections")

Triple = tuple[str, str, str]

# The top authority every A10 correction is stamped at — the whole point of the
# feature. Pulled from the ONE shared scale so a correction ranks above every
# machine source without a parallel rank being invented.
USER_ASSERTION_AUTHORITY = AuthorityLevel.user_assertion
_USER_ASSERTION_CONFIDENCE = AUTHORITY_CONFIDENCE[AuthorityLevel.user_assertion]

# Parse the leaf ``<Type>`` out of a canonical entity IRI
# (``…/entities/<Type>/<slug>``) — the inverse of ``ontology_queries.entity_uri``'s
# type segment. Used to derive the entity's type from a correction's subject URI
# when the caller doesn't name it, so the recency policy + post-write refresh key
# on the right type.
_ENTITY_URI_RE = re.compile(r"^https://cograph\.tech/entities/([^/]+)/.+$")


def _type_from_entity_uri(subject: str) -> str:
    """Return the ``<Type>`` segment of a canonical entity IRI, or ``""``."""
    m = _ENTITY_URI_RE.match(subject or "")
    return m.group(1) if m else ""


class UserAssertionError(ValueError):
    """Raised when a :class:`UserAssertion` cannot resolve a subject/type."""


@dataclass(frozen=True)
class UserAssertion:
    """One A10 human correction: "this attribute's value is wrong — use this".

    Identify the fact one of two equivalent ways:

    * ``subject`` — the full canonical entity IRI (what the Explorer's record
      row carries as ``rec.id``); or
    * ``type_name`` + ``entity_id`` — the caller-side pair, minted to the SAME
      canonical IRI via the shared :func:`entity_uri` (never re-implemented).

    ``predicate`` is the exact predicate the fact lives on. For the common
    literal-attribute correction the caller builds it with :func:`attr_uri`
    (``types/<Type>/attrs/<leaf>``) — but the model stays predicate-agnostic so a
    future relationship correction can pass an ``onto/<leaf>`` edge unchanged.

    ``value`` is the corrected object term exactly as it should be written
    (typed-literal convention included, matching the store). ``actor`` is the
    authenticated user id who made the correction (set from the auth subject at
    the route, never spoofed by the client). ``observed_at`` is when the
    correction was made (defaults to now at write time); ``reason`` an optional
    free-text note.

    ``envelope`` is an OPTIONAL :class:`ArtifactEnvelope` — the universal A1-A10
    metadata carrier (ADR 0011). When supplied its ``run_id`` threads the A6
    receipt identity; it is otherwise carried for the Wave-4 P8 wiring and never
    required (the schema is still a stub, so the correction route does not
    construct one yet).
    """

    predicate: str
    value: str
    actor: str = ""
    subject: str = ""
    type_name: str = ""
    entity_id: str = ""
    observed_at: Optional[datetime] = None
    reason: str = ""
    envelope: Optional[ArtifactEnvelope] = None

    def resolved_subject(self) -> str:
        """The canonical entity IRI this correction targets.

        Prefers an explicit ``subject``; otherwise mints one from
        ``type_name`` + ``entity_id`` via the shared :func:`entity_uri` (the ONE
        entity-IRI minter — never a local reimplementation). Raises when neither
        form is available."""
        if self.subject:
            return self.subject
        if self.type_name and self.entity_id:
            return entity_uri(self.type_name, self.entity_id)
        raise UserAssertionError(
            "UserAssertion needs a subject IRI or (type_name + entity_id)"
        )

    def resolved_type(self) -> str:
        """The entity's type name — explicit ``type_name``, else parsed from the
        subject IRI. Raises when neither yields one (so the recency policy + the
        post-write refresh always key on a real type)."""
        if self.type_name:
            return self.type_name
        derived = _type_from_entity_uri(self.resolved_subject())
        if derived:
            return derived
        raise UserAssertionError(
            f"cannot derive type_name from subject {self.resolved_subject()!r}; "
            "pass type_name explicitly"
        )


def build_user_assertion_provenance(
    subject: str,
    predicate: str,
    value: str,
    *,
    actor: str = "",
    observed_at: datetime,
    instance_graph: str,
) -> list[Triple]:
    """The top-authority provenance for a corrected fact.

    Built with the shared :func:`build_provenance_triples` (the ONE provenance
    writer) and stamped at :data:`USER_ASSERTION_AUTHORITY` with the calibrated
    ``user_assertion`` confidence, so the ONTA-276 conflict policy — which reads
    this authority back when a later scrape contradicts the value — ranks the
    correction above any machine source. ``source`` records WHO corrected it for
    explainability; the load-bearing field is the authority."""
    return list(
        build_provenance_triples(
            subject,
            predicate,
            value,
            actor or USER_ASSERTION_AUTHORITY.value,
            confidence=_USER_ASSERTION_CONFIDENCE,
            timestamp=observed_at,
            graph_uri=instance_graph,
            authority=USER_ASSERTION_AUTHORITY.value,
        )
    )


async def apply_user_assertion(
    neptune,
    instance_graph: str,
    assertion: UserAssertion,
    *,
    run_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    kg_name: Optional[str] = None,
    recency_policy: RecencyPolicy = DEFAULT_RECENCY_POLICY,
    manifest=None,
) -> MutationReceipt:
    """Apply an A10 user correction — the P6 write op for a human fix.

    Composes two things that already exist, adding no new write primitive:

    1. Build the corrected fact's TOP-authority provenance
       (:func:`build_user_assertion_provenance`) — the ``user_assertion``
       authority is what makes a future refresh unable to clobber it.
    2. Call the ONTA-277 :func:`supersede_fact` op, threading that provenance
       through its ``provenance_triples`` seam. ``supersede_fact`` closes the
       wrong value's validity interval (the corrected value becomes the only
       current one on a functional attribute), writes the new fact + its
       provenance through ``insert_facts``, runs one ``refresh_after_write``, and
       returns the A6 :class:`~cograph_client.pipeline.mutations.MutationReceipt`
       carrying the ``GraphDelta`` receipt of the new fact.

    Everything funnels through ``kg_writer`` — no hand-rolled write. ``run_id``
    threads the A6 receipt identity (defaults to the assertion's envelope
    ``run_id`` when one is carried). ``tenant_id`` / ``kg_name`` scope the
    post-write refresh; when omitted, ``supersede_fact`` parses them from the
    instance-graph URI.

    Returns the ``MutationReceipt`` whose ``superseded`` lists the wrong value(s)
    retired and whose ``graph_delta`` is the A6 receipt of the correction.
    """
    subject = assertion.resolved_subject()
    type_name = assertion.resolved_type()
    at = assertion.observed_at or datetime.now(timezone.utc)
    effective_run_id = run_id or (
        assertion.envelope.run_id if assertion.envelope is not None else None
    )

    provenance = build_user_assertion_provenance(
        subject,
        assertion.predicate,
        assertion.value,
        actor=assertion.actor,
        observed_at=at,
        instance_graph=instance_graph,
    )

    receipt = await supersede_fact(
        neptune,
        instance_graph,
        subject=subject,
        predicate=assertion.predicate,
        new_value=assertion.value,
        type_name=type_name,
        observed_at=at,
        run_id=effective_run_id,
        reason=assertion.reason or "user correction (A10)",
        tenant_id=tenant_id,
        kg_name=kg_name,
        policy=recency_policy,
        provenance_triples=provenance,
        manifest=manifest,
    )

    logger.info(
        "apply_user_assertion",
        subject=subject,
        predicate=assertion.predicate,
        actor=assertion.actor,
        superseded=len(receipt.superseded),
        authority=USER_ASSERTION_AUTHORITY.value,
    )
    return receipt


def literal_attribute_predicate(type_name: str, attribute: str) -> str:
    """The predicate a LITERAL attribute value lives on:
    ``types/<Type>/attrs/<attribute>`` — via the shared :func:`attr_uri`.

    The Explorer's per-field correction affordance sits on literal-attribute rows
    (``DrawerField``), so the correction route mints the predicate this way. Kept
    here (not inlined at the route) so the attrs/ vs onto/ predicate convention is
    decided in one place on the write side, never in the interface layer."""
    return attr_uri(type_name, attribute)


__all__ = [
    "UserAssertion",
    "UserAssertionError",
    "USER_ASSERTION_AUTHORITY",
    "apply_user_assertion",
    "build_user_assertion_provenance",
    "literal_attribute_predicate",
]

"""P4 identity-conditional verdicts + the post-resolution A6 re-check hook (ONTA-365).

Some facts are **entity-relative**: their truth depends on WHICH same-named entity
they are about. "Dr. Smith is affiliated with Stanford" is true of ONE Dr. Smith and
false of another — so the fact cannot get a final verdict until P6 decides identity.
This module gives P4 two things and nothing else:

  1. A DETECTION signal + marker. When a fact's SUBJECT entity is same-name-ambiguous
     (two or more candidate entities share the subject's surface name), the fact is
     marked :attr:`~cograph_client.verification.types.TruthVerdict.IDENTITY_CONDITIONAL`
     PRE-write, deferring the real verdict. The ambiguity is taken as INPUT
     (:class:`IdentityContext`) — this module does NOT itself run name resolution.
  2. A post-resolution RE-CHECK hook (:func:`recheck_after_resolution`). Once P6 has
     written the A6 Graph Delta — the identity decision, via match/mint OR merge/split
     (an ``owl:sameAs`` / ``onto/sameAs`` lineage edge) — the hook READS that delta to
     learn which entity the ambiguous name resolved to, RE-RUNS verification for the
     now-disambiguated entity, and upgrades the ``IDENTITY_CONDITIONAL`` verdict to
     ``SUPPORTED`` / ``REFUTED``.

**ANNOTATE-ONLY (no KG write).** The hook consumes
:class:`~cograph_client.graph.kg_writer.GraphDelta` /
:class:`~cograph_client.pipeline.mutations.MutationReceipt` STRICTLY read-only: it
takes no store / neptune / writer handle and returns plain
:class:`~cograph_client.verification.types.VerifiedFact` values. Any resulting change
to the graph is meant to flow out downstream as an A10 Correction (ONTA-363, owned
elsewhere) — this module never calls it and never writes. Write-path convergence is
trivially satisfied by writing nothing.

Boundary: OSS. Imports only stdlib + ``cograph_client.*`` — never ``from cograph.*``.
No network anywhere in this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable, Mapping, Optional, Sequence

from cograph_client.resolver.models import CleanFact, CleanOutcome
from cograph_client.verification.types import (
    TruthVerdict,
    VerifiedFact,
    VerifyContext,
)
from cograph_client.verification.verifier import FactVerifier, get_fact_verifier

__all__ = [
    "IdentityContext",
    "is_identity_conditional",
    "mark_identity_conditional",
    "mark_all_identity_conditional",
    "RecheckResult",
    "recheck_after_resolution",
]

# The deferred-verdict messaging, fixed so a marked fact's output is deterministic
# + reviewable (mirrors verifier._OFFLINE_REASON's discipline).
_CONDITIONAL_REASON = (
    "verdict deferred: the subject entity is same-name-ambiguous, so this fact is "
    "entity-relative and cannot be finalized until P6 resolves which same-named "
    "entity it is about (re-checked by recheck_after_resolution once the A6 Graph "
    "Delta reveals the identity decision)"
)

# `owl:sameAs` and this repo's instance-edge equivalent (pipeline/mutations.SAME_AS
# == "https://cograph.tech/onto/sameAs"). A merge writes `(canonical, sameAs, merged)`
# on the instance graph, so its object resolves to its subject. Detected by value here
# (not imported from pipeline/mutations) to keep this module's import graph minimal and
# free of the write layer it must never touch.
_ONTO_SAME_AS = "https://cograph.tech/onto/sameAs"
_OWL_SAME_AS = "http://www.w3.org/2002/07/owl#sameAs"


def _entity_id_of(fact_or_id) -> str:
    """The subject entity id, accepting either a :class:`VerifiedFact` (or any object
    with an ``entity_id``) or a bare id string."""
    eid = getattr(fact_or_id, "entity_id", fact_or_id)
    return str(eid or "")


def _name_from_entity_uri(uri: str) -> str:
    """Best-effort surface name of an entity URI — its last path segment
    (``…/entities/Person/Dr_Smith`` → ``Dr_Smith``). A pure structural fallback used
    only when the caller supplied no explicit ``subject_name_by_entity`` mapping; it
    never runs name resolution (that is the caller's job, fed in as the signal)."""
    if not uri:
        return ""
    return uri.rstrip("/").rsplit("/", 1)[-1]


def _is_same_as_predicate(predicate: str) -> bool:
    """True for a redirect/alias edge (``owl:sameAs`` or ``onto/sameAs``, or any
    predicate whose leaf is ``sameAs``) — the merge lineage signal in an A6 delta."""
    if not predicate:
        return False
    if predicate in (_ONTO_SAME_AS, _OWL_SAME_AS):
        return True
    leaf = predicate.rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1]
    return leaf.lower() == "sameas"


# --------------------------------------------------------------------------- #
# P4-time ambiguity signal
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class IdentityContext:
    """The P4-time ambiguity signal driving IDENTITY_CONDITIONAL detection.

    A fact is entity-relative — its truth depends on WHICH same-named entity it is
    about — when its SUBJECT entity is same-name-ambiguous: two or more candidate
    entities share the subject's surface name. This context carries that signal as
    PLAIN DATA (a set/collection), so the marker is deterministic and offline-testable
    and this module never has to implement a name-resolution model itself:

      * ``ambiguous_names`` — the subject surface names that are ambiguous (>= 2
        candidates share each). The primary signal.
      * ``candidates_by_name`` — optional surface name → the candidate entity URIs
        sharing it. Two jobs: (a) any name with >= 2 candidates is *derived* ambiguous
        (see :meth:`from_candidates`); (b) the re-check hook reads it to know the
        candidate set a resolved name could have landed on.
      * ``subject_name_by_entity`` — optional entity_id → its subject surface name, for
        subjects whose URI does not carry the surface name recoverably. When a subject
        is absent here, its name is derived from the entity URI's leaf.
    """

    ambiguous_names: frozenset[str] = frozenset()
    candidates_by_name: Mapping[str, frozenset[str]] = field(default_factory=dict)
    subject_name_by_entity: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Normalize caller-friendly inputs (list/set/dict) to stable internal shapes.
        object.__setattr__(self, "ambiguous_names", frozenset(self.ambiguous_names))
        object.__setattr__(
            self,
            "candidates_by_name",
            {str(k): frozenset(v) for k, v in dict(self.candidates_by_name).items()},
        )
        object.__setattr__(
            self,
            "subject_name_by_entity",
            {str(k): str(v) for k, v in dict(self.subject_name_by_entity).items()},
        )

    @classmethod
    def from_candidates(
        cls,
        candidates_by_name: Mapping[str, Iterable[str]],
        *,
        subject_name_by_entity: Optional[Mapping[str, str]] = None,
        extra_ambiguous_names: Iterable[str] = (),
    ) -> "IdentityContext":
        """Build a context whose ambiguous set is DERIVED — every surface name mapped
        to >= 2 candidate entities is ambiguous by the "multiple candidates share the
        name" litmus, plus any ``extra_ambiguous_names`` the caller forces on."""
        cbn = {str(k): frozenset(v) for k, v in dict(candidates_by_name).items()}
        ambiguous = {name for name, cands in cbn.items() if len(cands) >= 2}
        ambiguous.update(str(n) for n in extra_ambiguous_names)
        return cls(
            ambiguous_names=frozenset(ambiguous),
            candidates_by_name=cbn,
            subject_name_by_entity=dict(subject_name_by_entity or {}),
        )

    def subject_name_for(self, fact_or_id) -> str:
        """The subject surface name of a fact (or bare entity id): the explicit
        mapping when present, else the entity URI's leaf."""
        eid = _entity_id_of(fact_or_id)
        if eid in self.subject_name_by_entity:
            return self.subject_name_by_entity[eid]
        return _name_from_entity_uri(eid)

    def is_ambiguous(self, fact_or_id) -> bool:
        """True iff the subject's surface name is in :attr:`ambiguous_names`."""
        return self.subject_name_for(fact_or_id) in self.ambiguous_names

    def candidates_for(self, fact_or_id) -> frozenset[str]:
        """The candidate entity URIs sharing this subject's surface name (empty when
        no candidate set was supplied for that name)."""
        return self.candidates_by_name.get(self.subject_name_for(fact_or_id), frozenset())


def is_identity_conditional(
    fact,
    context: Optional[IdentityContext] = None,
    *,
    ambiguous_names: Optional[Iterable[str]] = None,
) -> bool:
    """Whether ``fact``'s subject is same-name-ambiguous — the detection predicate.

    Drive it either with a full :class:`IdentityContext` (``is_identity_conditional(
    fact, ctx)``) or, for the simplest case, a bare set of ambiguous surface names
    (``is_identity_conditional(fact, ambiguous_names={...})``). The signal is always
    INPUT — this function runs no name resolution of its own.
    """
    if context is None:
        context = IdentityContext(ambiguous_names=frozenset(ambiguous_names or ()))
    return context.is_ambiguous(fact)


def mark_identity_conditional(
    fact: VerifiedFact,
    context: Optional[IdentityContext] = None,
    *,
    ambiguous_names: Optional[Iterable[str]] = None,
) -> VerifiedFact:
    """Return ``fact`` with verdict :attr:`TruthVerdict.IDENTITY_CONDITIONAL` when its
    subject is same-name-ambiguous, DEFERRING the real verdict; otherwise return it
    unchanged.

    The marker only fires on genuinely ambiguous subjects (the load-bearing contrast:
    an unambiguous fact is returned as-is), and it is idempotent — a fact already
    ``IDENTITY_CONDITIONAL`` is left alone. The deferred fact keeps its lineage
    (envelope, entity_id, attribute, value, surface_form) but resets confidence /
    evidence to nothing, since no true verdict has been reached yet.
    """
    if fact.verdict is TruthVerdict.IDENTITY_CONDITIONAL:
        return fact
    if not is_identity_conditional(fact, context, ambiguous_names=ambiguous_names):
        return fact
    return replace(
        fact,
        verdict=TruthVerdict.IDENTITY_CONDITIONAL,
        confidence=0.0,
        evidence=(),
        reason=_CONDITIONAL_REASON,
    )


def mark_all_identity_conditional(
    facts: Sequence[VerifiedFact], context: IdentityContext
) -> list[VerifiedFact]:
    """Batch :func:`mark_identity_conditional` over a sequence of verified facts."""
    return [mark_identity_conditional(f, context) for f in facts]


# --------------------------------------------------------------------------- #
# A6 resolution view (read-only projection of the Graph Delta / receipt)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _ResolutionView:
    """The identity-relevant projection of an A6 delta/receipt, computed read-only:

      * ``subjects`` — every subject URI present in the delta's facts (the match/mint
        signal: an ambiguous name that resolved by MATCH lands its fact on that URI).
      * ``subject_object_pairs`` — ``(subject, object)`` of every delta fact (used to
        break a tie when >1 candidate is present, by the fact's own value).
      * ``merged_to_canonical`` — ``merged_uri → canonical_uri`` from every ``sameAs``
        lineage edge (the merge signal: an ambiguous name whose candidate was merged
        away resolves to the surviving canonical).
    """

    subjects: frozenset[str]
    subject_object_pairs: frozenset[tuple[str, str]]
    merged_to_canonical: Mapping[str, str]


def _extract_resolution(resolution) -> _ResolutionView:
    """Project a :class:`GraphDelta`, a receipt wrapping one (``MutationReceipt`` /
    ``MergeReceipt`` / ``SplitReceipt`` — anything with a ``graph_delta``), or ``None``
    into a :class:`_ResolutionView`. Purely READ-ONLY: attribute access only, never a
    store call, never a write.
    """
    if resolution is None:
        return _ResolutionView(frozenset(), frozenset(), {})

    # Unwrap a receipt to its A6 delta; a bare GraphDelta is used as-is.
    delta = getattr(resolution, "graph_delta", resolution)

    subjects: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    merged_to_canonical: dict[str, str] = {}

    for fact in getattr(delta, "facts", ()) or ():
        # A delta fact is (fact_id, s, p, o); be defensive about arity.
        if len(fact) == 4:
            _fid, s, p, o = fact
        elif len(fact) == 3:
            s, p, o = fact
        else:
            continue
        if s:
            subjects.add(s)
            pairs.add((s, o))
        if _is_same_as_predicate(p) and s and o:
            # `(canonical, sameAs, merged)` → the object resolved to the subject.
            merged_to_canonical[o] = s

    # A receipt may also expose the sameAs edge directly (MergeReceipt.same_as).
    same_as = getattr(resolution, "same_as", None)
    if isinstance(same_as, (tuple, list)) and len(same_as) == 3:
        s, p, o = same_as
        if _is_same_as_predicate(p) and s and o:
            merged_to_canonical[o] = s

    return _ResolutionView(
        subjects=frozenset(subjects),
        subject_object_pairs=frozenset(pairs),
        merged_to_canonical=dict(merged_to_canonical),
    )


def _resolve_for_fact(
    fact: VerifiedFact, context: IdentityContext, view: _ResolutionView
) -> Optional[str]:
    """The canonical entity URI ``fact``'s ambiguous subject resolved to per the A6
    view, or ``None`` when the delta does NOT resolve it (the hook then leaves the
    verdict IDENTITY_CONDITIONAL).

    Two resolution signals, MERGE first (unambiguous), then MATCH/MINT:

      1. **Merge/split** — a candidate for this name (or the fact's own subject) was
         merged away (``sameAs``); resolve to the surviving canonical.
      2. **Match/mint** — exactly one candidate for this name appears as a subject in
         the delta (the fact landed on it). If several candidates appear, break the tie
         by the fact's own value (the candidate that is the subject of a delta fact
         whose object equals ``fact.value``); still ambiguous → ``None``.
    """
    candidates = set(context.candidates_for(fact))

    # 1. Merge signal — a candidate (or the provisional subject itself) was merged away.
    for cand in candidates | {fact.entity_id}:
        canonical = view.merged_to_canonical.get(cand)
        if canonical:
            return canonical

    # 2. Match/mint signal — the fact landed on a specific candidate subject.
    present = [c for c in candidates if c in view.subjects]
    if len(present) == 1:
        return present[0]
    if len(present) > 1 and fact.value:
        narrowed = [c for c in present if (c, fact.value) in view.subject_object_pairs]
        if len(narrowed) == 1:
            return narrowed[0]
    return None


@dataclass(frozen=True)
class RecheckResult:
    """The outcome of re-checking ONE identity-conditional fact after resolution.

    ``fact`` is the (possibly upgraded) :class:`VerifiedFact`: when the A6 delta
    resolved the identity, its verdict is the re-run verifier's ``SUPPORTED`` /
    ``REFUTED`` and its ``entity_id`` is the resolved canonical URI; when the delta did
    NOT resolve it, ``fact`` is returned UNCHANGED (still ``IDENTITY_CONDITIONAL``).
    ``resolved_entity_id`` is the canonical URI it resolved to (``None`` if unresolved);
    ``upgraded`` is True iff the verdict moved off ``IDENTITY_CONDITIONAL``.
    """

    fact: VerifiedFact
    resolved_entity_id: Optional[str] = None
    upgraded: bool = False


def _clean_from_verified(fact: VerifiedFact, entity_id: str) -> CleanFact:
    """Reconstruct the A3 :class:`CleanFact` a verifier consumes, re-pointed at the
    RESOLVED ``entity_id`` — so the re-run judges the fact as it now stands, about the
    disambiguated entity. ``surface_form`` (when the value was transformed at clean
    time) becomes the ``raw_value`` verification compares evidence against."""
    transformed = fact.surface_form is not None and fact.surface_form != fact.value
    raw = fact.surface_form if fact.surface_form is not None else (fact.value or "")
    return CleanFact(
        datatype=fact.datatype,
        raw_value=raw,
        clean_value=fact.value,
        outcome=CleanOutcome.TRANSFORMED if transformed else CleanOutcome.PASSED,
        conformed=not transformed,
        entity_id=entity_id,
        attribute=fact.attribute,
    )


def recheck_after_resolution(
    conditional_facts: Sequence[VerifiedFact],
    resolution=None,
    *,
    context: IdentityContext,
    verifier: Optional[FactVerifier] = None,
    verify_context: Optional[VerifyContext] = None,
) -> list[RecheckResult]:
    """RE-CHECK identity-conditional facts once P6 has decided identity (ONTA-365).

    Consumes the A6 ``resolution`` — a :class:`GraphDelta` or a receipt wrapping one
    (:class:`~cograph_client.pipeline.mutations.MutationReceipt` / ``MergeReceipt`` /
    ``SplitReceipt``) — STRICTLY read-only, to learn which entity each ambiguous name
    resolved to (match/mint, or merge/split via a ``sameAs`` edge). For each fact still
    ``IDENTITY_CONDITIONAL``:

      * if the delta RESOLVES its subject, RE-RUN verification for the disambiguated
        entity (the ``verifier`` arg, else the registered/offline default via
        :func:`get_fact_verifier`) and upgrade the verdict to the re-run's
        ``SUPPORTED`` / ``REFUTED``, re-pointing ``entity_id`` at the resolved URI;
      * if the delta does NOT resolve its subject (no ``resolution`` at all, or one
        that resolves a DIFFERENT entity), leave the fact ``IDENTITY_CONDITIONAL`` —
        the upgrade is driven by the resolution, never unconditional.

    A fact that is not ``IDENTITY_CONDITIONAL`` to begin with passes through untouched.

    **Annotate-only:** this takes NO store / neptune / writer — it only READS the
    delta and returns plain :class:`VerifiedFact` values inside :class:`RecheckResult`.
    A resulting graph change flows out downstream as an A10 Correction (ONTA-363),
    which this hook neither calls nor depends on.
    """
    active = verifier if verifier is not None else get_fact_verifier()
    view = _extract_resolution(resolution)

    results: list[RecheckResult] = []
    for fact in conditional_facts:
        if fact.verdict is not TruthVerdict.IDENTITY_CONDITIONAL:
            results.append(RecheckResult(fact=fact, resolved_entity_id=None, upgraded=False))
            continue

        resolved = _resolve_for_fact(fact, context, view)
        if resolved is None:
            # Load-bearing: without a resolving delta the verdict STAYS conditional.
            results.append(RecheckResult(fact=fact, resolved_entity_id=None, upgraded=False))
            continue

        result = active.verify(_clean_from_verified(fact, resolved), verify_context)
        upgraded_fact = replace(
            fact,
            entity_id=resolved,
            verdict=result.verdict,
            confidence=result.confidence,
            evidence=tuple(result.evidence),
            reason=result.reason,
        )
        results.append(
            RecheckResult(
                fact=upgraded_fact,
                resolved_entity_id=resolved,
                upgraded=result.verdict is not TruthVerdict.IDENTITY_CONDITIONAL,
            )
        )
    return results

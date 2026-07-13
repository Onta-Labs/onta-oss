"""Write-time conflict resolution on functional (single-valued) attributes (ONTA-276).

Two independently-verified facts can contradict on a FUNCTIONAL attribute — a
company's revenue asserted as $10M by one source and $12M by another, an HQ in
Austin per a fresh filing and in SF per a stale directory. The graph is
append-only, so today both land and the answer layer (P7) nondeterministically
cites one. This module DETERMINISTICALLY picks the WINNER so the "current facts"
read is stable, while the P6 write op (``pipeline/mutations.py``) keeps the LOSER
queryable (its validity interval is CLOSED with ``STATUS_DEPRECATED``, never
deleted) with its provenance intact.

**What lives here vs. the write op.** This module is PURE: it decides *which claim
wins* and *why*, with no I/O and no store access. It never writes — the write op
composes it with ``insert_facts`` / ``refresh_after_write`` on the shared write
path. Same inputs always yield the same winner (a total order, below).

**The precedence — total & deterministic (no unbroken ties).** A claim's rank key
is built axis-by-axis in ``ConflictPolicy.precedence`` order, defaulting to::

    authority  >  confidence  >  recency  >  (value, the final tiebreak)

Justification for that ordering:

* **Authority first.** Authority is an editorial/governance DESIGNATION set
  upstream (P1): a ``source_of_truth`` registry entry is declared canonical, so it
  should beat even a high-confidence guess from a weaker source. This is the whole
  reason authority is carried on facts through A4 — so it survives to this point.
  Reuses the ONE ``AuthorityLevel`` scale (``spec.AUTHORITY_RANK``); never a
  parallel one.
* **Then confidence.** At EQUAL authority, the better-verified value wins — a
  0.9-confidence extraction beats a 0.6 one from an equally-authoritative source.
* **Then recency.** At equal authority AND confidence, the more recently observed
  fact wins — this is what retires the "stale directory" value once a fresh,
  equally-authoritative observation arrives.
* **Finally value.** A pure lexical compare on the object term, appended as the
  LAST key component. Two DISTINCT-valued claims can therefore never tie (they
  differ at least here), so the winner is ALWAYS uniquely determined — the order
  is total. (Two claims that tie on value are the SAME fact, i.e. not a conflict.)

Boundary: OSS. Imports only stdlib / ``cograph_client.*`` (``api_registry.spec``
for the shared authority scale). No ``from cograph.*``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Sequence, Union

from cograph_client.api_registry.spec import (
    AUTHORITY_CONFIDENCE,
    AUTHORITY_RANK,
    AuthorityLevel,
)

# Rank axes (the names a ConflictPolicy.precedence orders). ``value`` is implicit —
# always appended last as the total-order tiebreak, never listed in precedence.
AXIS_AUTHORITY = "authority"
AXIS_CONFIDENCE = "confidence"
AXIS_RECENCY = "recency"
_AXES = frozenset({AXIS_AUTHORITY, AXIS_CONFIDENCE, AXIS_RECENCY})

# The documented default precedence: authority, then confidence, then recency.
DEFAULT_PRECEDENCE: tuple[str, ...] = (AXIS_AUTHORITY, AXIS_CONFIDENCE, AXIS_RECENCY)

# Decision reasons — the axis that DECIDED the winner (or that there was no conflict).
REASON_AUTHORITY = AXIS_AUTHORITY
REASON_CONFIDENCE = AXIS_CONFIDENCE
REASON_RECENCY = AXIS_RECENCY
REASON_VALUE = "value"  # the pure lexical tiebreak decided it
REASON_NO_CONFLICT = "no_conflict"

# An authority level absent from the scale ranks WEAKEST — matches the enrichment
# chain's ``AUTHORITY_RANK.get(level, 9)`` fallback so both rails agree.
_UNKNOWN_AUTHORITY_RANK = 9
# Neutral confidence for a claim carrying neither an explicit confidence nor a
# known authority (so recency/value still break the tie deterministically).
_NEUTRAL_CONFIDENCE = 0.5
# A missing observation time ranks as the OLDEST possible (loses the recency axis).
_OLDEST = float("-inf")


def authority_rank(level: Optional[AuthorityLevel]) -> int:
    """Rank of an authority level — LOWER is STRONGER (source_of_truth == 0).

    Reuses ``spec.AUTHORITY_RANK`` (the ONE scale). ``None`` / unmapped ranks
    weakest, mirroring the enrichment chain's ``.get(level, 9)``.
    """
    if level is None:
        return _UNKNOWN_AUTHORITY_RANK
    return AUTHORITY_RANK.get(level, _UNKNOWN_AUTHORITY_RANK)


def default_confidence_for(level: Optional[AuthorityLevel]) -> float:
    """Calibrated confidence implied by an authority level when a claim carries no
    explicit confidence of its own — reuses ``spec.AUTHORITY_CONFIDENCE``."""
    if level is None:
        return _NEUTRAL_CONFIDENCE
    return AUTHORITY_CONFIDENCE.get(level, _NEUTRAL_CONFIDENCE)


@dataclass(frozen=True)
class FactClaim:
    """One candidate value for a functional attribute, with the trust signals the
    policy arbitrates on. ``value`` is the object term exactly as written to the
    store (typed-literal convention included) so it matches the validity/instance
    triples. Immutable so a decision is a pure function of its inputs.
    """

    value: str
    authority: Optional[AuthorityLevel] = None
    confidence: Optional[float] = None
    observed_at: Optional[datetime] = None
    source: str = ""

    @property
    def effective_confidence(self) -> float:
        """The confidence used for ranking: the explicit one, else the calibrated
        confidence implied by ``authority`` (else neutral)."""
        if self.confidence is not None:
            return float(self.confidence)
        return default_confidence_for(self.authority)

    @property
    def authority_str(self) -> str:
        """The authority as its value string (``""`` when unknown) — for provenance."""
        return self.authority.value if self.authority is not None else ""

    @classmethod
    def from_verified(cls, verified) -> "FactClaim":
        """Adapt an A4 verified fact (``resolver.models.ValidatedTriple`` — or any
        object exposing ``.object``/``.authority``/``.confidence``/``.source``)
        into a claim. This is how the trust signals CARRIED through A4 reach the
        policy: duck-typed to avoid a resolver→pipeline import coupling."""
        value = getattr(verified, "object", None)
        if value is None:
            value = getattr(verified, "value", "")
        return cls(
            value=value,
            authority=getattr(verified, "authority", None),
            confidence=getattr(verified, "confidence", None),
            observed_at=getattr(verified, "observed_at", None),
            source=getattr(verified, "source", "") or "",
        )


@dataclass(frozen=True)
class ConflictDecision:
    """The deterministic outcome of resolving an incoming claim against existing
    current value(s) on a functional attribute.

    ``winner`` is the claim that stays/becomes current; ``loser`` the strongest
    displaced claim (``None`` when there was no conflict). ``losers`` lists EVERY
    non-winning conflicting claim (usually just one on a functional attribute, but
    all are closed if several existing values contradict). ``reason`` is the axis
    that decided it (``REASON_AUTHORITY`` / ``_CONFIDENCE`` / ``_RECENCY`` /
    ``_VALUE``), or ``REASON_NO_CONFLICT``. ``conflict`` is True only when a real
    contradiction was arbitrated. ``winner_is_incoming`` tells the write op whether
    the winner is the newly-arriving fact (open a fresh interval for it) or an
    existing current value (leave its interval alone).
    """

    winner: FactClaim
    loser: Optional[FactClaim]
    reason: str
    conflict: bool
    losers: tuple[FactClaim, ...] = ()
    winner_is_incoming: bool = True


ExistingClaims = Union[None, FactClaim, Sequence[FactClaim]]


def _normalize(existing: ExistingClaims) -> list[FactClaim]:
    if existing is None:
        return []
    if isinstance(existing, FactClaim):
        return [existing]
    return list(existing)


@dataclass(frozen=True)
class ConflictPolicy:
    """Deterministic winner selection on a functional attribute.

    The precedence is configurable (``precedence`` — an ordering over
    ``{authority, confidence, recency}``) but defaults to the documented
    ``authority > confidence > recency``; the object term is ALWAYS the final,
    implicit tiebreak, so the order is total regardless of configuration (no
    unbroken ties). Frozen + stateless → ``resolve`` is a pure function.
    """

    precedence: tuple[str, ...] = DEFAULT_PRECEDENCE

    def __post_init__(self) -> None:
        bad = [ax for ax in self.precedence if ax not in _AXES]
        if bad:
            raise ValueError(
                f"ConflictPolicy.precedence has unknown axes {bad!r}; "
                f"allowed: {sorted(_AXES)}"
            )

    def _axis_value(self, claim: FactClaim, axis: str) -> float:
        if axis == AXIS_AUTHORITY:
            # Negate so STRONGER authority (lower rank) sorts HIGHER.
            return float(-authority_rank(claim.authority))
        if axis == AXIS_CONFIDENCE:
            return claim.effective_confidence
        # AXIS_RECENCY
        return claim.observed_at.timestamp() if claim.observed_at else _OLDEST

    def _key(self, claim: FactClaim) -> tuple:
        """Total-order rank key: bigger wins. The precedence axes first, then the
        object term as the final deterministic tiebreak so distinct values never
        tie."""
        return tuple(self._axis_value(claim, ax) for ax in self.precedence) + (
            claim.value,
        )

    def _deciding_axis(self, winner: FactClaim, runner_up: FactClaim) -> str:
        """Which axis first separates the winner from the runner-up."""
        for axis in self.precedence:
            if self._axis_value(winner, axis) != self._axis_value(runner_up, axis):
                return axis
        return REASON_VALUE

    def resolve(self, existing: ExistingClaims, incoming: FactClaim) -> ConflictDecision:
        """Pick the winner between ``incoming`` and the existing current value(s).

        Same-value existing entries are re-assertions of ``incoming`` (no
        contradiction). If nothing contradicts, ``incoming`` wins with
        ``conflict=False``. Otherwise the winner is the max under :meth:`_key` over
        the contradicting claims plus ``incoming``; every other contradicting claim
        is a loser (the strongest of them surfaced as ``loser``).
        """
        existing_list = _normalize(existing)
        conflicting = [c for c in existing_list if c.value != incoming.value]
        if not conflicting:
            return ConflictDecision(
                winner=incoming,
                loser=None,
                reason=REASON_NO_CONFLICT,
                conflict=False,
                losers=(),
                winner_is_incoming=True,
            )

        candidates = conflicting + [incoming]
        winner = max(candidates, key=self._key)
        losers = tuple(c for c in candidates if c is not winner)
        runner_up = max(losers, key=self._key)
        return ConflictDecision(
            winner=winner,
            loser=runner_up,
            reason=self._deciding_axis(winner, runner_up),
            conflict=True,
            losers=losers,
            winner_is_incoming=(winner is incoming),
        )


# The sensible default: authority > confidence > recency. Callers may inject a
# different precedence, but the winner is always deterministic + total.
DEFAULT_CONFLICT_POLICY = ConflictPolicy()


def resolve(
    existing: ExistingClaims,
    incoming: FactClaim,
    *,
    policy: ConflictPolicy = DEFAULT_CONFLICT_POLICY,
) -> ConflictDecision:
    """Module-level pure resolver — ``policy.resolve(existing, incoming)``.

    Provided so callers can arbitrate without constructing a policy for the
    default ordering. Deterministic: same inputs → same winner.
    """
    return policy.resolve(existing, incoming)


__all__ = [
    "AXIS_AUTHORITY",
    "AXIS_CONFIDENCE",
    "AXIS_RECENCY",
    "DEFAULT_PRECEDENCE",
    "REASON_AUTHORITY",
    "REASON_CONFIDENCE",
    "REASON_RECENCY",
    "REASON_VALUE",
    "REASON_NO_CONFLICT",
    "FactClaim",
    "ConflictDecision",
    "ConflictPolicy",
    "DEFAULT_CONFLICT_POLICY",
    "resolve",
    "authority_rank",
    "default_confidence_for",
]

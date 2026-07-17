"""A4 (Verify) plugin seam + orchestrator (ONTA-361).

The P4 Verify stage decides whether each A3 clean fact is corroborated by
INDEPENDENT evidence. This module owns three things, and nothing else:

  * :class:`FactVerifier` — the plugin PROTOCOL a verifier implements (an A3
    :class:`CleanFact` [+ optional :class:`VerifyContext`] → a
    :class:`VerifierResult`).
  * :func:`register_fact_verifier` / :func:`get_fact_verifier` — a module-global
    plugin hook, mirroring ``auth.api_keys.register_external_verifier`` exactly, so
    a premium verifier (paid fact-check API, LLM-judge, cross-source corroboration)
    attaches without forking OSS. The OSS default stays OFFLINE.
  * :func:`verify_clean_facts` — the orchestrator that runs the active verifier over
    a batch of A3 facts and stamps each result with its :class:`ArtifactEnvelope`.

**The OSS default is deterministic + offline.** Evidence GATHERING (consulting the
web) is a separate ticket (ONTA-364), so with no independent evidence to weigh, the
default :class:`DefaultOfflineVerifier` returns a principled
:attr:`TruthVerdict.UNVERIFIABLE` for every fact — it NEVER returns
:attr:`TruthVerdict.SUPPORTED`, because a fact is not "verified" until a source
DISTINCT from its own corroborates it, and the offline path has no such source.

Boundary: OSS. Imports only stdlib + ``cograph_client.*`` — never ``from cograph.*``.
No network anywhere in the default path.
"""

from __future__ import annotations

from typing import Optional, Protocol, Sequence, runtime_checkable

from cograph_client.pipeline.envelope import ArtifactEnvelope, derive_fact_id
from cograph_client.resolver.models import CleanFact
from cograph_client.verification.types import (
    EvidenceRef,
    TruthVerdict,
    VerifiedFact,
    VerifierResult,
    VerifyContext,
)

__all__ = [
    "FactVerifier",
    "DefaultOfflineVerifier",
    "register_fact_verifier",
    "get_fact_verifier",
    "verify_clean_facts",
]

# Stage labels for `derive_fact_id`. The A4 verified fact's parent is the A3 clean
# fact it consumed; A3 has no envelope of its own yet (envelope wiring lands per-rail
# later — see pipeline/envelope.py), so the orchestrator derives a stable A3 parent id
# from the clean fact's identity, then derives the A4 id as its single-parent child.
_A3_STAGE = "A3"
_A4_STAGE = "A4"

# Envelope scope defaults. `ArtifactEnvelope` requires non-empty workspace_id/run_id;
# real callers thread the actual request scope. These keep `verify_clean_facts(facts)`
# callable in tests / cold plumbing without minting a misleading blank id.
_DEFAULT_WORKSPACE_ID = "local"
_DEFAULT_RUN_ID = "local-verify"

# The offline default's fixed messaging, so its output is deterministic + reviewable.
_OFFLINE_REASON = (
    "no independent evidence available: a fact is not SUPPORTED until a source "
    "distinct from its own corroborates it, and the OSS default verifier is offline "
    "(evidence gathering pending ONTA-364)"
)
_DISABLED_REASON = "verification disabled (policy off/None) — fact passed through unverified"


@runtime_checkable
class FactVerifier(Protocol):
    """The P4 verifier plugin contract: judge ONE A3 clean fact and return a
    :class:`VerifierResult` (verdict + independent evidence + confidence).

    Implementations MUST be side-effect free with respect to the KG (verification is
    read-only; writes stay on the converged write path) and MUST fail closed —
    returning :attr:`TruthVerdict.UNVERIFIABLE` rather than raising — on a provider
    error, so a fact-check outage degrades to "unverified" instead of a 500. Premium
    verifiers register via :func:`register_fact_verifier`."""

    def verify(
        self, fact: CleanFact, context: Optional[VerifyContext] = None
    ) -> VerifierResult:
        ...


class DefaultOfflineVerifier:
    """The OSS default verifier — DETERMINISTIC, OFFLINE, no LLM, no network.

    With evidence gathering not yet built (ONTA-364), there is no independent source
    to corroborate against, so every fact is :attr:`TruthVerdict.UNVERIFIABLE` with
    empty evidence and zero confidence. It NEVER returns
    :attr:`TruthVerdict.SUPPORTED` (that would mean trusting a fact on its own
    source's say-so, which is exactly what verification exists to prevent)."""

    def verify(
        self, fact: CleanFact, context: Optional[VerifyContext] = None
    ) -> VerifierResult:
        return VerifierResult(
            verdict=TruthVerdict.UNVERIFIABLE,
            confidence=0.0,
            evidence=(),
            reason=_OFFLINE_REASON,
        )


_DEFAULT_OFFLINE_VERIFIER = DefaultOfflineVerifier()

# Module-global registered verifier (None = use the offline default). Mirrors
# `auth.api_keys._external_verifier` — a single process-wide plugin slot.
_fact_verifier: Optional[FactVerifier] = None


def register_fact_verifier(verifier: Optional[FactVerifier]) -> None:
    """Register (or clear) the process-wide fact verifier.

    Downstream/premium deployments plug in a paid fact-check API, an LLM judge, or a
    cross-source corroboration verifier here without forking cograph-oss. Pass
    ``None`` to clear and fall back to the offline default. Mirrors
    :func:`cograph_client.auth.api_keys.register_external_verifier`."""
    global _fact_verifier
    _fact_verifier = verifier


def get_fact_verifier() -> FactVerifier:
    """The active verifier: the registered one if any, else the offline default.

    Unlike ``auth.get_external_verifier`` (which returns ``None`` when unset), this
    always returns a USABLE verifier so :func:`verify_clean_facts` never has to
    special-case an empty registry — the OSS default just stays offline."""
    return _fact_verifier if _fact_verifier is not None else _DEFAULT_OFFLINE_VERIFIER


def _policy_enabled(policy: Optional[object]) -> bool:
    """Decide whether a LOOSELY-typed policy turns verification ON.

    Typed ``Optional[object]`` on purpose (ADR/keystone rule): ONTA-362 owns the real
    ``VerifyPolicy``; this module must accept it WITHOUT importing
    ``normalization/policy.py``. So we duck-type the shapes an off/on policy takes:

      * ``None`` → OFF (the default — nothing to verify against).
      * an object with a boolean ``enabled`` → that flag.
      * an object with a string ``mode`` → OFF iff it reads off/none/disabled/empty.
      * any other non-None object → ON (a policy was explicitly supplied)."""
    if policy is None:
        return False
    enabled = getattr(policy, "enabled", None)
    if isinstance(enabled, bool):
        return enabled
    mode = getattr(policy, "mode", None)
    if mode is not None:
        return str(mode).strip().lower() not in {"off", "none", "disabled", ""}
    return bool(policy)


def _clean_fact_key(fact: CleanFact) -> str:
    """A stable per-fact key for `derive_fact_id`. Uses the SAME identity tuple the
    boundary harness sorts clean facts by — (entity_id, attribute, raw_value,
    datatype) — so a fact's derived id is deterministic across replays. Components are
    hashed inside `derive_fact_id`, so the join delimiter can't collide across them."""
    return "\x1f".join(
        (fact.entity_id, fact.attribute, fact.raw_value, fact.datatype)
    )


def _a4_envelope(fact: CleanFact, *, workspace_id: str, run_id: str) -> ArtifactEnvelope:
    """Derive the A4 envelope for one clean fact: an A3 root envelope (parent of this
    fact) then its single-parent A4 child, so ``parent_fact_ids == (A3 fact id,))``."""
    key = _clean_fact_key(fact)
    a3_env = ArtifactEnvelope(
        workspace_id=workspace_id,
        run_id=run_id,
        fact_id=derive_fact_id(run_id=run_id, stage=_A3_STAGE, local_key=key),
    )
    return a3_env.child(stage=_A4_STAGE, local_key=key)


def verify_clean_facts(
    a3_facts: Sequence[CleanFact],
    policy: Optional[object] = None,
    *,
    workspace_id: str = _DEFAULT_WORKSPACE_ID,
    run_id: str = _DEFAULT_RUN_ID,
    verifier: Optional[FactVerifier] = None,
    context: Optional[VerifyContext] = None,
) -> list[VerifiedFact]:
    """P4 orchestrator: run the active verifier over a batch of A3 clean facts and
    return one :class:`VerifiedFact` each, envelope-stamped.

    ``policy`` is typed ``Optional[object]`` so ONTA-362's ``VerifyPolicy`` can be
    passed later without this module importing it (see :func:`_policy_enabled`). When
    the policy is ``None`` or implies OFF, every fact passes through as
    :attr:`TruthVerdict.UNVERIFIABLE` DETERMINISTICALLY and the verifier is not
    consulted — verification is opt-in. When ON, each fact goes through
    ``verifier`` (an explicit override) or :func:`get_fact_verifier` (the registered
    one, else the offline default).

    Every returned fact carries an A4 :class:`ArtifactEnvelope` whose ``fact_id`` is
    derived with the consumed A3 fact's id as its single parent — lineage threads back
    to the clean fact regardless of verdict."""
    enabled = _policy_enabled(policy)
    active = verifier if verifier is not None else get_fact_verifier()

    out: list[VerifiedFact] = []
    for fact in a3_facts:
        envelope = _a4_envelope(fact, workspace_id=workspace_id, run_id=run_id)
        if enabled:
            result = active.verify(fact, context)
        else:
            result = VerifierResult(
                verdict=TruthVerdict.UNVERIFIABLE,
                confidence=0.0,
                evidence=(),
                reason=_DISABLED_REASON,
            )
        out.append(VerifiedFact.from_clean(fact, result, envelope))
    return out

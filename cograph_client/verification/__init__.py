"""P4 **Verify** — the EPISTEMIC verified-fact stage (ONTA-361).

This is the P4 keystone: the core types + plugin seam for deciding whether an A3
clean fact is corroborated by INDEPENDENT evidence. Its public surface (the types
in :mod:`.types`, the protocol + orchestrator in :mod:`.verifier`) is imported by
the sibling P4 tickets (evidence gathering, reverify, identity, policy, eval) and is
meant to stay clean + stable.

Do NOT conflate the EPISTEMIC A4 here (:class:`VerifiedFact`: "is this fact true?")
with the MECHANICAL A4 (``resolver.validator.validate_triple`` →
``ValidatedTriple``: "is this value well-typed enough to persist?"). See
:mod:`cograph_client.verification.types` for the full collision resolution.

Boundary: OSS. Imports only stdlib + ``cograph_client.*`` — never ``from cograph.*``.
The OSS default verifier is deterministic + offline (no network, no LLM); premium
verifiers attach via :func:`register_fact_verifier`.
"""

from __future__ import annotations

from cograph_client.verification.types import (
    EvidenceRef,
    TruthVerdict,
    VerifiedFact,
    VerifierResult,
    VerifyContext,
)
from cograph_client.verification.verifier import (
    DefaultOfflineVerifier,
    FactVerifier,
    get_fact_verifier,
    register_fact_verifier,
    verify_clean_facts,
)

__all__ = [
    # types
    "TruthVerdict",
    "EvidenceRef",
    "VerifierResult",
    "VerifiedFact",
    "VerifyContext",
    # verifier seam + orchestrator
    "FactVerifier",
    "DefaultOfflineVerifier",
    "register_fact_verifier",
    "get_fact_verifier",
    "verify_clean_facts",
]

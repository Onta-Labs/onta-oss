"""Unit tests for the P4 Verify keystone (ONTA-361): the epistemic verified-fact
types, the ``FactVerifier`` plugin seam, and the ``verify_clean_facts`` orchestrator.

Fully deterministic + offline — no LLM, no network, no store."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cograph_client.pipeline.envelope import ArtifactEnvelope, derive_fact_id
from cograph_client.resolver.models import CleanFact, CleanOutcome
from cograph_client.verification import (
    DefaultOfflineVerifier,
    EvidenceRef,
    FactVerifier,
    TruthVerdict,
    VerifiedFact,
    VerifierResult,
    VerifyContext,
    get_fact_verifier,
    register_fact_verifier,
    verify_clean_facts,
)


# --------------------------------------------------------------------------- #
# Registry isolation: never leak a registered verifier across tests.
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _clear_verifier_registry():
    register_fact_verifier(None)
    yield
    register_fact_verifier(None)


def _clean(
    entity_id="W1",
    attribute="rating",
    datatype="float",
    raw_value="4.6",
    clean_value="4",
    outcome=CleanOutcome.TRANSFORMED,
):
    return CleanFact(
        datatype=datatype,
        raw_value=raw_value,
        clean_value=clean_value,
        outcome=outcome,
        entity_id=entity_id,
        attribute=attribute,
    )


# --------------------------------------------------------------------------- #
# TruthVerdict / EvidenceRef / VerifierResult shapes
# --------------------------------------------------------------------------- #
def test_truth_verdict_members():
    assert {v.value for v in TruthVerdict} == {
        "supported",
        "refuted",
        "unverifiable",
        "identity_conditional",
    }


def test_evidence_ref_derives_host_and_serializes():
    ref = EvidenceRef.from_url("https://example.com/a/b?q=1", "a quoted snippet")
    assert ref.host == "example.com"
    assert ref.to_dict() == {
        "source_url": "https://example.com/a/b?q=1",
        "host": "example.com",
        "snippet": "a quoted snippet",
    }
    # __post_init__ derives host even without from_url; an explicit host is kept.
    assert EvidenceRef(source_url="https://h.io/x").host == "h.io"
    assert EvidenceRef(source_url="https://h.io/x", host="override").host == "override"
    assert EvidenceRef(source_url="").host == ""


def test_verifier_result_confidence_bounds():
    VerifierResult(verdict=TruthVerdict.UNVERIFIABLE, confidence=0.0)
    VerifierResult(verdict=TruthVerdict.SUPPORTED, confidence=1.0)
    for bad in (-0.01, 1.01, 2.0):
        with pytest.raises(ValueError):
            VerifierResult(verdict=TruthVerdict.SUPPORTED, confidence=bad)


def test_verifier_result_normalizes_evidence_to_tuple():
    r = VerifierResult(
        verdict=TruthVerdict.SUPPORTED,
        confidence=0.5,
        evidence=[EvidenceRef.from_url("https://a.com", "s")],
    )
    assert isinstance(r.evidence, tuple) and r.evidence[0].host == "a.com"


# --------------------------------------------------------------------------- #
# VerifiedFact construction + fact_id determinism + envelope lineage
# --------------------------------------------------------------------------- #
def test_verified_fact_from_clean_copies_source_fields_and_surface_form():
    clean = _clean()  # TRANSFORMED: raw 4.6 -> clean 4
    env = ArtifactEnvelope(workspace_id="ws", run_id="r", fact_id="fid-1")
    result = VerifierResult(verdict=TruthVerdict.UNVERIFIABLE, confidence=0.0)
    vf = VerifiedFact.from_clean(clean, result, env)

    assert (vf.entity_id, vf.attribute, vf.datatype) == ("W1", "rating", "float")
    assert vf.value == "4"  # the A3 clean value
    # surface_form is the original pre-clean value because the fact was TRANSFORMED.
    assert vf.surface_form == "4.6"
    assert vf.verdict is TruthVerdict.UNVERIFIABLE
    assert vf.fact_id == env.fact_id == "fid-1"


def test_verified_fact_surface_form_none_when_not_transformed():
    passed = _clean(raw_value="hello", clean_value="hello", datatype="string",
                    outcome=CleanOutcome.PASSED)
    env = ArtifactEnvelope(workspace_id="ws", run_id="r", fact_id="fid-2")
    vf = VerifiedFact.from_clean(passed, VerifierResult(TruthVerdict.UNVERIFIABLE), env)
    assert vf.surface_form is None and vf.value == "hello"


def test_fact_id_is_deterministic_and_threads_a3_parent_lineage():
    """The A4 fact_id is a derive_fact_id over the SAME (run, stage, parent, key), so
    two runs of the orchestrator produce identical ids; and the envelope's parent is
    the derived A3 fact id — real lineage back to the consumed clean fact."""
    facts = [_clean(), _clean(entity_id="W2", attribute="name", datatype="string",
                              raw_value="Acme", clean_value="Acme", outcome=CleanOutcome.PASSED)]
    out_a = verify_clean_facts(facts, workspace_id="ws", run_id="run-1")
    out_b = verify_clean_facts(facts, workspace_id="ws", run_id="run-1")
    assert [v.fact_id for v in out_a] == [v.fact_id for v in out_b]

    # Each A4 envelope's single parent is exactly the derived A3 fact id for that fact.
    for v, cf in zip(out_a, facts):
        key = "\x1f".join((cf.entity_id, cf.attribute, cf.raw_value, cf.datatype))
        expected_a3 = derive_fact_id(run_id="run-1", stage="A3", local_key=key)
        assert v.envelope.parent_fact_ids == (expected_a3,)
        expected_a4 = derive_fact_id(
            run_id="run-1", stage="A4", parent_fact_ids=(expected_a3,), local_key=key
        )
        assert v.fact_id == expected_a4

    # A different run_id yields different ids (identity is run-scoped, not content).
    out_c = verify_clean_facts(facts, workspace_id="ws", run_id="run-2")
    assert [v.fact_id for v in out_a] != [v.fact_id for v in out_c]


def test_verified_fact_to_dict_shape():
    clean = _clean()
    env = ArtifactEnvelope(workspace_id="ws", run_id="r", fact_id="fid")
    vf = VerifiedFact.from_clean(
        clean,
        VerifierResult(TruthVerdict.SUPPORTED, 0.8,
                       (EvidenceRef.from_url("https://a.com", "s"),), "why"),
        env,
    )
    d = vf.to_dict()
    assert d["verdict"] == "supported" and d["value"] == "4" and d["surface_form"] == "4.6"
    assert d["evidence"] == [{"source_url": "https://a.com", "host": "a.com", "snippet": "s"}]
    assert d["envelope"]["fact_id"] == "fid"


# --------------------------------------------------------------------------- #
# FactVerifier protocol + register/get round-trip
# --------------------------------------------------------------------------- #
def test_default_offline_verifier_is_the_default():
    assert isinstance(get_fact_verifier(), DefaultOfflineVerifier)


def test_register_and_get_round_trip():
    stub = _SupportingStub()
    register_fact_verifier(stub)
    assert get_fact_verifier() is stub
    register_fact_verifier(None)  # clear → back to the offline default
    assert isinstance(get_fact_verifier(), DefaultOfflineVerifier)


def test_default_offline_verifier_satisfies_protocol_and_never_supports():
    v = DefaultOfflineVerifier()
    assert isinstance(v, FactVerifier)  # runtime_checkable protocol
    for cf in (
        _clean(),
        _clean(datatype="string", raw_value="x", clean_value="x", outcome=CleanOutcome.PASSED),
        _clean(datatype="datetime", raw_value="bad", clean_value=None, outcome=CleanOutcome.DROPPED),
    ):
        r = v.verify(cf)
        assert r.verdict is TruthVerdict.UNVERIFIABLE
        assert r.verdict is not TruthVerdict.SUPPORTED  # never from the fact's own source
        assert r.evidence == () and r.confidence == 0.0


# --------------------------------------------------------------------------- #
# verify_clean_facts — offline default behavior
# --------------------------------------------------------------------------- #
def test_verify_clean_facts_offline_is_deterministic_unverifiable_with_no_evidence():
    facts = [
        _clean(),
        _clean(entity_id="W2", attribute="active", datatype="boolean",
               raw_value="yes", clean_value="true", outcome=CleanOutcome.TRANSFORMED),
    ]
    out = verify_clean_facts(facts)  # policy None → passthrough
    assert len(out) == 2
    for v in out:
        assert v.verdict is TruthVerdict.UNVERIFIABLE
        assert v.evidence == ()  # no gathering yet (ONTA-364)
        assert v.confidence == 0.0
        assert v.envelope.workspace_id and v.envelope.run_id  # envelope always populated


def test_verify_clean_facts_empty_input():
    assert verify_clean_facts([]) == []


# --------------------------------------------------------------------------- #
# LOAD-BEARING control: the registry is actually consulted.
# --------------------------------------------------------------------------- #
class _SupportingStub:
    """A verifier that ALWAYS returns SUPPORTED with evidence — so if the orchestrator
    consults the registry, the output visibly changes from the offline UNVERIFIABLE."""

    def verify(self, fact: CleanFact, context=None) -> VerifierResult:
        return VerifierResult(
            verdict=TruthVerdict.SUPPORTED,
            confidence=0.9,
            evidence=(EvidenceRef.from_url("https://corroborator.example/x", "matches"),),
            reason="stub says supported",
        )


def test_registered_verifier_changes_output_proving_hook_is_consulted():
    facts = [_clean()]
    on = SimpleNamespace(enabled=True)  # a policy that implies ON (duck-typed)

    baseline = verify_clean_facts(facts, on)  # offline default
    assert baseline[0].verdict is TruthVerdict.UNVERIFIABLE

    register_fact_verifier(_SupportingStub())
    changed = verify_clean_facts(facts, on)
    # The verdict CHANGED — proving verify_clean_facts routed through the registry.
    assert changed[0].verdict is TruthVerdict.SUPPORTED
    assert changed[0].confidence == 0.9
    assert changed[0].evidence[0].host == "corroborator.example"
    assert changed[0].verdict != baseline[0].verdict


def test_policy_off_short_circuits_even_with_a_registered_verifier():
    """Verification is opt-in: when the policy implies OFF (or is None), the registered
    verifier is NOT consulted and every fact passes through UNVERIFIABLE."""
    register_fact_verifier(_SupportingStub())
    facts = [_clean()]
    for policy in (None, SimpleNamespace(enabled=False), SimpleNamespace(mode="off")):
        out = verify_clean_facts(facts, policy)
        assert out[0].verdict is TruthVerdict.UNVERIFIABLE, policy
        assert out[0].evidence == ()


def test_explicit_verifier_arg_overrides_registry():
    register_fact_verifier(_SupportingStub())  # registry says SUPPORTED

    class _Refuter:
        def verify(self, fact, context=None):
            return VerifierResult(verdict=TruthVerdict.REFUTED, confidence=1.0, reason="no")

    out = verify_clean_facts([_clean()], SimpleNamespace(enabled=True), verifier=_Refuter())
    assert out[0].verdict is TruthVerdict.REFUTED  # explicit arg won over the registry


def test_context_is_passed_to_the_verifier():
    seen: list = []

    class _Recorder:
        def verify(self, fact, context=None):
            seen.append(context)
            return VerifierResult(verdict=TruthVerdict.UNVERIFIABLE)

    ctx = VerifyContext(workspace_id="ws", run_id="r", subject="uri:1", type_name="Widget")
    verify_clean_facts([_clean()], SimpleNamespace(enabled=True),
                       verifier=_Recorder(), context=ctx)
    assert seen == [ctx]


def test_policy_enabled_duck_typing_variants():
    """A loosely-typed policy (ONTA-362's VerifyPolicy passed WITHOUT importing it):
    on/off is read from `enabled` or `mode`, else any non-None object means ON."""
    facts = [_clean()]

    # `mode="on"` → ON → offline default consulted (still UNVERIFIABLE, but via verifier).
    register_fact_verifier(_SupportingStub())
    assert verify_clean_facts(facts, SimpleNamespace(mode="active"))[0].verdict is TruthVerdict.SUPPORTED
    # a bare truthy object (no enabled/mode) → ON.
    assert verify_clean_facts(facts, object())[0].verdict is TruthVerdict.SUPPORTED

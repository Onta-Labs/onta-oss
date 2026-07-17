"""Unit tests for the P4 identity rail (ONTA-365): identity-conditional verdicts +
the post-resolution A6 re-check hook.

Fully deterministic + offline — no LLM, no network, no store. Drives the ambiguity
signal and the A6 identity decision as plain in-memory data, and the re-check verifier
via a registered stub (reset in teardown).
"""

from __future__ import annotations

import inspect

import pytest

from cograph_client.graph.kg_writer import GraphDelta, build_graph_delta
from cograph_client.pipeline.envelope import ArtifactEnvelope, derive_fact_id
from cograph_client.pipeline.mutations import MutationReceipt
from cograph_client.verification.identity import (
    IdentityContext,
    RecheckResult,
    is_identity_conditional,
    mark_all_identity_conditional,
    mark_identity_conditional,
    recheck_after_resolution,
)
from cograph_client.verification.types import (
    EvidenceRef,
    TruthVerdict,
    VerifiedFact,
    VerifierResult,
)
from cograph_client.verification.verifier import (
    get_fact_verifier,
    register_fact_verifier,
)

# --------------------------------------------------------------------------- #
# Same-name-collision fixture: two "Dr. Smith" entities, one unambiguous "Dr. Jones".
# --------------------------------------------------------------------------- #
GRAPH = "https://omnix.dev/graphs/demo/kg"

SMITH_A = "https://cograph.tech/entities/Person/dr_smith_stanford"
SMITH_B = "https://cograph.tech/entities/Person/dr_smith_mit"
SMITH_PROVISIONAL = "https://cograph.tech/entities/Person/Dr_Smith"  # the ambiguous mint
JONES = "https://cograph.tech/entities/Person/dr_jones"

AFFILIATED = "https://cograph.tech/onto/affiliated_with"
STANFORD = "https://cograph.tech/entities/Org/stanford"


def _collision_context() -> IdentityContext:
    """"Dr. Smith" is ambiguous (2 candidates share the name); "Dr. Jones" is not."""
    return IdentityContext.from_candidates(
        {
            "Dr. Smith": [SMITH_A, SMITH_B],
            "Dr. Jones": [JONES],
        },
        subject_name_by_entity={
            SMITH_PROVISIONAL: "Dr. Smith",
            SMITH_A: "Dr. Smith",
            SMITH_B: "Dr. Smith",
            JONES: "Dr. Jones",
        },
    )


def _vf(
    entity_id=SMITH_PROVISIONAL,
    attribute="affiliated_with",
    datatype="string",
    value=STANFORD,
    verdict=TruthVerdict.UNVERIFIABLE,
    surface_form=None,
    fact_id="fid-1",
) -> VerifiedFact:
    env = ArtifactEnvelope(workspace_id="ws", run_id="r", fact_id=fact_id)
    return VerifiedFact(
        entity_id=entity_id,
        attribute=attribute,
        datatype=datatype,
        value=value,
        surface_form=surface_form,
        verdict=verdict,
        envelope=env,
    )


# --------------------------------------------------------------------------- #
# Registry isolation: never leak a registered verifier across tests.
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _clear_verifier_registry():
    register_fact_verifier(None)
    yield
    register_fact_verifier(None)


# --------------------------------------------------------------------------- #
# Detection + marking (PRE-write)
# --------------------------------------------------------------------------- #
def test_from_candidates_derives_ambiguous_names_by_shared_name():
    ctx = _collision_context()
    assert ctx.ambiguous_names == frozenset({"Dr. Smith"})  # only the shared name
    assert ctx.candidates_for(_vf(entity_id=SMITH_PROVISIONAL)) == frozenset({SMITH_A, SMITH_B})
    assert ctx.candidates_for(_vf(entity_id=JONES)) == frozenset({JONES})


def test_is_identity_conditional_predicate_fires_only_on_ambiguous_subject():
    ctx = _collision_context()
    assert is_identity_conditional(_vf(entity_id=SMITH_PROVISIONAL), ctx) is True
    # LOAD-BEARING contrast: a subject whose name is NOT shared is not conditional.
    assert is_identity_conditional(_vf(entity_id=JONES), ctx) is False


def test_is_identity_conditional_bare_ambiguous_names_form():
    # The simplest call form: a bare set of ambiguous surface names, name derived
    # from the entity URI leaf.
    f = _vf(entity_id="https://cograph.tech/entities/Person/Dr_Smith")
    assert is_identity_conditional(f, ambiguous_names={"Dr_Smith"}) is True
    assert is_identity_conditional(f, ambiguous_names={"Someone_Else"}) is False


def test_mark_identity_conditional_marks_ambiguous_pre_write():
    ctx = _collision_context()
    marked = mark_identity_conditional(_vf(entity_id=SMITH_PROVISIONAL), ctx)
    assert marked.verdict is TruthVerdict.IDENTITY_CONDITIONAL
    assert marked.confidence == 0.0 and marked.evidence == ()
    assert marked.reason  # a deferred-verdict explanation is set
    # Lineage + payload preserved.
    assert marked.entity_id == SMITH_PROVISIONAL
    assert marked.value == STANFORD
    assert marked.fact_id == "fid-1"


def test_mark_identity_conditional_leaves_unambiguous_fact_unchanged():
    """LOAD-BEARING control: the marker only fires on genuinely ambiguous entities.
    An unambiguous fact is returned as-is (same object, verdict untouched)."""
    ctx = _collision_context()
    original = _vf(entity_id=JONES, verdict=TruthVerdict.UNVERIFIABLE)
    out = mark_identity_conditional(original, ctx)
    assert out is original
    assert out.verdict is TruthVerdict.UNVERIFIABLE


def test_mark_identity_conditional_is_idempotent():
    ctx = _collision_context()
    once = mark_identity_conditional(_vf(entity_id=SMITH_PROVISIONAL), ctx)
    twice = mark_identity_conditional(once, ctx)
    assert twice is once  # already conditional → left alone


def test_mark_all_identity_conditional_batch():
    ctx = _collision_context()
    facts = [_vf(entity_id=SMITH_PROVISIONAL, fact_id="a"), _vf(entity_id=JONES, fact_id="b")]
    out = mark_all_identity_conditional(facts, ctx)
    assert out[0].verdict is TruthVerdict.IDENTITY_CONDITIONAL
    assert out[1].verdict is TruthVerdict.UNVERIFIABLE


# --------------------------------------------------------------------------- #
# Re-check driven by an entity-aware stub verifier.
# --------------------------------------------------------------------------- #
class _EntityAwareStub:
    """SUPPORTED only when the resolved entity is SMITH_A, REFUTED otherwise — so the
    finalized verdict is provably a function of WHICH entity the identity resolved to,
    not unconditional."""

    def verify(self, fact, context=None) -> VerifierResult:
        if fact.entity_id == SMITH_A:
            return VerifierResult(
                verdict=TruthVerdict.SUPPORTED,
                confidence=0.9,
                evidence=(EvidenceRef.from_url("https://stanford.edu/faculty", "listed"),),
                reason="Dr. Smith (Stanford) is affiliated with Stanford",
            )
        return VerifierResult(
            verdict=TruthVerdict.REFUTED,
            confidence=0.8,
            reason="this Dr. Smith is not affiliated with Stanford",
        )


def _conditional_fact() -> VerifiedFact:
    return mark_identity_conditional(_vf(entity_id=SMITH_PROVISIONAL), _collision_context())


def test_recheck_match_resolution_finalizes_verdict():
    """P6 MATCH: the affiliation fact landed on SMITH_A in the A6 delta → the ambiguous
    'Dr. Smith' resolved to SMITH_A → verdict finalizes to SUPPORTED (the stub's verdict
    for SMITH_A)."""
    register_fact_verifier(_EntityAwareStub())
    ctx = _collision_context()
    fact = _conditional_fact()

    delta = build_graph_delta(GRAPH, [(SMITH_A, AFFILIATED, STANFORD)], run_id="r")
    [res] = recheck_after_resolution([fact], delta, context=ctx)

    assert res.resolved_entity_id == SMITH_A
    assert res.upgraded is True
    assert res.fact.verdict is TruthVerdict.SUPPORTED
    assert res.fact.entity_id == SMITH_A  # re-pointed at the disambiguated entity
    assert res.fact.confidence == 0.9
    assert res.fact.evidence[0].host == "stanford.edu"


def test_recheck_resolution_to_other_entity_refutes():
    """Same fixture, but the delta lands the fact on SMITH_B → resolved to SMITH_B →
    the stub REFUTES. Proves the finalized verdict tracks the resolved identity."""
    register_fact_verifier(_EntityAwareStub())
    ctx = _collision_context()
    fact = _conditional_fact()

    delta = build_graph_delta(GRAPH, [(SMITH_B, AFFILIATED, STANFORD)], run_id="r")
    [res] = recheck_after_resolution([fact], delta, context=ctx)

    assert res.resolved_entity_id == SMITH_B
    assert res.fact.verdict is TruthVerdict.REFUTED
    assert res.fact.entity_id == SMITH_B


def test_recheck_merge_resolution_via_sameas_edge():
    """P6 MERGE: new evidence shows the two Dr. Smith candidates are the SAME entity;
    the A6 delta carries `(canonical, sameAs, merged)`. The merged candidate resolves
    to the surviving canonical (SMITH_A) → verdict finalizes."""
    register_fact_verifier(_EntityAwareStub())
    ctx = _collision_context()
    fact = _conditional_fact()

    # SMITH_B merged into SMITH_A (canonical) — the sameAs lineage edge in the delta.
    same_as = (SMITH_A, "https://cograph.tech/onto/sameAs", SMITH_B)
    delta = build_graph_delta(GRAPH, [same_as], run_id="r")
    [res] = recheck_after_resolution([fact], delta, context=ctx)

    assert res.resolved_entity_id == SMITH_A
    assert res.fact.verdict is TruthVerdict.SUPPORTED
    assert res.fact.entity_id == SMITH_A


def test_recheck_consumes_mutation_receipt_wrapping_a_delta():
    """The hook accepts a receipt that WRAPS a GraphDelta (MutationReceipt), not only a
    bare delta — it reads `.graph_delta` read-only."""
    register_fact_verifier(_EntityAwareStub())
    ctx = _collision_context()
    fact = _conditional_fact()

    delta = build_graph_delta(GRAPH, [(SMITH_A, AFFILIATED, STANFORD)], run_id="r")
    receipt = MutationReceipt(op="supersede", graph_delta=delta)
    [res] = recheck_after_resolution([fact], receipt, context=ctx)

    assert res.resolved_entity_id == SMITH_A
    assert res.fact.verdict is TruthVerdict.SUPPORTED


# --------------------------------------------------------------------------- #
# LOAD-BEARING controls: the upgrade is DRIVEN by the resolution, not unconditional.
# --------------------------------------------------------------------------- #
def test_recheck_without_delta_stays_identity_conditional():
    register_fact_verifier(_EntityAwareStub())
    ctx = _collision_context()
    fact = _conditional_fact()

    [res] = recheck_after_resolution([fact], None, context=ctx)
    assert res.resolved_entity_id is None
    assert res.upgraded is False
    assert res.fact.verdict is TruthVerdict.IDENTITY_CONDITIONAL
    assert res.fact is fact  # returned unchanged


def test_recheck_non_resolving_delta_stays_identity_conditional():
    """A delta that resolves a DIFFERENT, unrelated entity (not a candidate for this
    name, no merge of a candidate) must NOT finalize the verdict."""
    register_fact_verifier(_EntityAwareStub())
    ctx = _collision_context()
    fact = _conditional_fact()

    other = "https://cograph.tech/entities/Person/dr_unrelated"
    delta = build_graph_delta(GRAPH, [(other, AFFILIATED, STANFORD)], run_id="r")
    [res] = recheck_after_resolution([fact], delta, context=ctx)

    assert res.resolved_entity_id is None
    assert res.upgraded is False
    assert res.fact.verdict is TruthVerdict.IDENTITY_CONDITIONAL


def test_recheck_ambiguous_both_candidates_present_without_value_match_stays_conditional():
    """If BOTH candidates appear as delta subjects and neither is disambiguated by the
    fact's value, the match signal is still ambiguous → stays conditional."""
    register_fact_verifier(_EntityAwareStub())
    ctx = _collision_context()
    fact = _conditional_fact()

    # Both candidates present as subjects, but via a DIFFERENT predicate/object than the
    # fact's value (STANFORD), so the value tie-break can't pick one.
    other_org = "https://cograph.tech/entities/Org/mit"
    delta = build_graph_delta(
        GRAPH,
        [(SMITH_A, AFFILIATED, other_org), (SMITH_B, AFFILIATED, other_org)],
        run_id="r",
    )
    [res] = recheck_after_resolution([fact], delta, context=ctx)
    assert res.resolved_entity_id is None
    assert res.fact.verdict is TruthVerdict.IDENTITY_CONDITIONAL


def test_recheck_value_tiebreak_when_multiple_candidates_present():
    """When >1 candidate is a delta subject, the fact's own value breaks the tie: the
    candidate that is subject of a fact whose object == the fact's value wins."""
    register_fact_verifier(_EntityAwareStub())
    ctx = _collision_context()
    fact = _conditional_fact()  # value == STANFORD

    other_org = "https://cograph.tech/entities/Org/mit"
    delta = build_graph_delta(
        GRAPH,
        [(SMITH_A, AFFILIATED, STANFORD), (SMITH_B, AFFILIATED, other_org)],
        run_id="r",
    )
    [res] = recheck_after_resolution([fact], delta, context=ctx)
    assert res.resolved_entity_id == SMITH_A
    assert res.fact.verdict is TruthVerdict.SUPPORTED


def test_recheck_passes_through_non_conditional_facts_untouched():
    ctx = _collision_context()
    plain = _vf(entity_id=SMITH_A, verdict=TruthVerdict.SUPPORTED)
    delta = build_graph_delta(GRAPH, [(SMITH_A, AFFILIATED, STANFORD)], run_id="r")
    [res] = recheck_after_resolution([plain], delta, context=ctx)
    assert res.fact is plain
    assert res.upgraded is False
    assert res.resolved_entity_id is None


def test_recheck_uses_registered_verifier_when_no_explicit_arg():
    """With no explicit `verifier=`, the hook routes through get_fact_verifier() — the
    registered stub, proving the seam is consulted."""
    register_fact_verifier(_EntityAwareStub())
    assert get_fact_verifier().__class__.__name__ == "_EntityAwareStub"
    ctx = _collision_context()
    fact = _conditional_fact()
    delta = build_graph_delta(GRAPH, [(SMITH_A, AFFILIATED, STANFORD)], run_id="r")
    [res] = recheck_after_resolution([fact], delta, context=ctx)
    assert res.fact.verdict is TruthVerdict.SUPPORTED


def test_recheck_explicit_verifier_overrides_registry():
    register_fact_verifier(_EntityAwareStub())  # registry SUPPORTS SMITH_A

    class _AlwaysUnverifiable:
        def verify(self, fact, context=None):
            return VerifierResult(verdict=TruthVerdict.UNVERIFIABLE, reason="no evidence")

    ctx = _collision_context()
    fact = _conditional_fact()
    delta = build_graph_delta(GRAPH, [(SMITH_A, AFFILIATED, STANFORD)], run_id="r")
    [res] = recheck_after_resolution([fact], delta, context=ctx, verifier=_AlwaysUnverifiable())
    # Resolved, so re-checked; but the explicit verifier's UNVERIFIABLE is NOT an
    # upgrade off conditional in the SUPPORTED/REFUTED sense — it's still moved off
    # IDENTITY_CONDITIONAL though (the explicit arg won over the registry).
    assert res.resolved_entity_id == SMITH_A
    assert res.fact.verdict is TruthVerdict.UNVERIFIABLE


# --------------------------------------------------------------------------- #
# Annotate-only: the hook performs NO KG write.
# --------------------------------------------------------------------------- #
def test_recheck_hook_is_annotate_only_no_store_parameter():
    """The hook takes NO store / neptune / writer handle — it can only READ the delta
    and return plain VerifiedFacts, so it structurally cannot write to a KG."""
    params = set(inspect.signature(recheck_after_resolution).parameters)
    for banned in ("neptune", "store", "writer", "instance_graph", "kg", "session"):
        assert banned not in params, banned
    # Its inputs are exactly: the conditional facts, the (read-only) resolution, and
    # in-memory context / verifier / verify_context.
    assert "conditional_facts" in params and "resolution" in params and "context" in params


def test_recheck_returns_plain_verified_facts():
    register_fact_verifier(_EntityAwareStub())
    ctx = _collision_context()
    fact = _conditional_fact()
    delta = build_graph_delta(GRAPH, [(SMITH_A, AFFILIATED, STANFORD)], run_id="r")
    results = recheck_after_resolution([fact], delta, context=ctx)
    assert all(isinstance(r, RecheckResult) for r in results)
    assert all(isinstance(r.fact, VerifiedFact) for r in results)


def test_recheck_does_not_import_or_touch_the_write_path():
    """identity.py must not pull in a KG writer — annotate-only means no write seam
    is even reachable from the module."""
    import cograph_client.verification.identity as identity_mod

    src = inspect.getsource(identity_mod)
    # No write-path primitives and no store round-trips anywhere in the module source.
    for banned in (
        "insert_facts",
        "delete_facts",
        "rewrite_subject",
        "refresh_after_write",
        "neptune.update",
        "neptune.query",
    ):
        assert banned not in src, banned

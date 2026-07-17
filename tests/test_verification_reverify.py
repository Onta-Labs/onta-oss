"""ONTA-363 acceptance: a MACHINE re-verification of an in-graph fact emits an A10
Correction stamped at ``AuthorityLevel.machine_reverification`` — an authority that
can SUPERSEDE a stale scraped value but can NEVER overrule a human's fix.

Three layers, mirroring the Wave-4 refresh-vs-correction e2e:

1. Authority ordering (pure): ``machine_reverification`` ranks STRICTLY below
   ``user_assertion`` and STRICTLY above ``authoritative`` in the ONE shared scale,
   with every pre-existing level keeping its relative order + a calibrated
   confidence between ``source_of_truth`` and ``authoritative``.
2. THE load-bearing conflict e2e over a pyoxigraph store:
     (a) re-verify a fact whose current value came from a low-authority scraped
         source (``authoritative``) → the machine re-verify SUPERSEDES it (corrected
         value wins, stale value closed deprecated-but-queryable); and
     (b) the CONTROL — re-verify a fact whose current value is a human fix
         (``user_assertion``, written via the REAL A10 correction path) → the SAME
         machine re-verify does NOT win; the user's value survives and the machine's
         proposal lands deprecated-but-queryable. A re-verify never clobbers a user
         fix — the invariant.
3. Write-path convergence: the correction is written through the ONE converged
   conflict writer (``write_with_conflict_resolution``), never a raw
   ``insert_triples`` / hand-rolled DELETE.
"""
from __future__ import annotations

import inspect
import json
import re

import pytest

from cograph_client.api_registry.spec import (
    AUTHORITY_CONFIDENCE,
    AUTHORITY_RANK,
    AuthorityLevel,
)
from cograph_client.graph.kg_writer import GraphDelta, insert_facts
from cograph_client.graph.provenance import fetch_provenance
from cograph_client.graph.queries import kg_graph_uri
from cograph_client.graph.validity import (
    STATUS_DEPRECATED,
    current_objects_query,
    fetch_history,
)
from cograph_client.pipeline.conflict import REASON_AUTHORITY
from cograph_client.pipeline.corrections import UserAssertion, apply_user_assertion
from cograph_client.verification.reverify import (
    MACHINE_REVERIFICATION_AUTHORITY,
    MachineReverification,
    MachineReverificationReceipt,
    apply_machine_reverification,
    literal_attribute_predicate,
)
from cograph_client.verification.types import TruthVerdict, VerifierResult

TENANT, KG = "onta363", "corp"
INSTANCE_GRAPH = kg_graph_uri(TENANT, KG)
ACME = "https://cograph.tech/entities/Company/acme"
PHONE = literal_attribute_predicate("Company", "phone")

SCRAPED_PHONE = "+1-555-0001"      # the stale scraped value
CORRECTED_PHONE = "+1-555-9999"    # what a fresh re-verify says is right
USER_PHONE = "+1-555-2222"         # a human fix


# --------------------------------------------------------------------------- #
# 1. Authority ordering — pure, no store
# --------------------------------------------------------------------------- #
def test_machine_reverification_ranks_below_user_and_above_authoritative():
    """The load-bearing rank invariant: strictly weaker than a human assertion,
    strictly stronger than a scraped authoritative source."""
    mr = AUTHORITY_RANK[AuthorityLevel.machine_reverification]
    # STRICTLY below (weaker than / higher rank number) user_assertion.
    assert mr > AUTHORITY_RANK[AuthorityLevel.user_assertion]
    # STRICTLY above (stronger than / lower rank number) authoritative + supplementary.
    assert mr < AUTHORITY_RANK[AuthorityLevel.authoritative]
    assert mr < AUTHORITY_RANK[AuthorityLevel.supplementary]
    # Recommended placement: also below source_of_truth.
    assert mr > AUTHORITY_RANK[AuthorityLevel.source_of_truth]


def test_existing_levels_keep_their_relative_order():
    """Inserting a new level must not reorder any pre-existing pair."""
    order = [
        AuthorityLevel.user_assertion,
        AuthorityLevel.source_of_truth,
        AuthorityLevel.authoritative,
        AuthorityLevel.supplementary,
    ]
    ranks = [AUTHORITY_RANK[l] for l in order]
    assert ranks == sorted(ranks), "existing levels must stay strictly increasing in rank"
    # And no two levels collide on a rank.
    all_ranks = list(AUTHORITY_RANK.values())
    assert len(all_ranks) == len(set(all_ranks)), "every level must have a distinct rank"


def test_machine_reverification_confidence_is_calibrated_between():
    """Its calibrated confidence sits between source_of_truth and authoritative."""
    c = AUTHORITY_CONFIDENCE[AuthorityLevel.machine_reverification]
    assert (
        AUTHORITY_CONFIDENCE[AuthorityLevel.authoritative]
        < c
        < AUTHORITY_CONFIDENCE[AuthorityLevel.source_of_truth]
    )


def test_authority_constant_is_the_shared_level():
    """The module stamps the ONE shared scale's level — no parallel authority."""
    assert MACHINE_REVERIFICATION_AUTHORITY is AuthorityLevel.machine_reverification


# --------------------------------------------------------------------------- #
# Store-backed layers
# --------------------------------------------------------------------------- #
pyoxigraph = pytest.importorskip("pyoxigraph")
from pyoxigraph import QueryResultsFormat, Store  # noqa: E402


class PyoxiNeptune:
    """Minimal async NeptuneClient shim over an in-process pyoxigraph Store —
    the same fixture the conflict-policy e2e uses."""

    def __init__(self) -> None:
        self.store = Store()

    async def query(self, sparql: str) -> dict:
        results = self.store.query(sparql, use_default_graph_as_union=True)
        return json.loads(results.serialize(format=QueryResultsFormat.JSON))

    async def update(self, sparql: str) -> None:
        self.store.update(sparql)


@pytest.fixture(autouse=True)
def _quiet_housekeeping(monkeypatch):
    """Silence the shared refresh_after_write internals so the e2e isolates the
    conflict/authority mechanism — the op STILL calls refresh_after_write."""
    import cograph_client.api.routes.explore as explore_mod
    import cograph_client.nlp.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod.NLQueryPipeline, "invalidate_cache", lambda g: None)
    monkeypatch.setattr(pipeline_mod, "get_embedding_service", lambda: None)
    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)


async def _current(n: PyoxiNeptune, subject: str, predicate: str) -> set[str]:
    """The "current facts" projection — objects with no CLOSED validity interval."""
    raw = await n.query(current_objects_query(INSTANCE_GRAPH, subject, predicate))
    return {b["o"]["value"] for b in raw["results"]["bindings"]}


async def _seed_scraped(
    n: PyoxiNeptune, value: str, *, authority: AuthorityLevel
) -> None:
    """Seed an initial current fact WITH its trust signals persisted in provenance,
    via the SAME conflict-resolving write path — exactly how an upstream A4 fact
    lands, and what makes the seeded value's authority readable at re-verify time."""
    from cograph_client.pipeline.mutations import write_with_conflict_resolution

    await insert_facts(
        n,
        INSTANCE_GRAPH,
        [(ACME, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
          "https://cograph.tech/types/Company")],
    )
    await write_with_conflict_resolution(
        n, INSTANCE_GRAPH,
        subject=ACME, predicate=PHONE, type_name="Company", value=value,
        authority=authority, confidence=AUTHORITY_CONFIDENCE[authority],
        source="scraper", run_id="seed",
    )


def _refuted(current: str, corrected: str) -> VerifierResult:
    """A fresh REFUTED verdict — independent evidence contradicts the in-graph value
    and points at ``corrected``."""
    return VerifierResult(
        verdict=TruthVerdict.REFUTED,
        confidence=0.88,
        reason=f"independent evidence refutes {current!r}; correct value is {corrected!r}",
    )


# --------------------------------------------------------------------------- #
# 2a. THE acceptance bar — re-verify supersedes a stale scraped value
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_reverify_supersedes_stale_scraped_value():
    """Current value is a scraped ``authoritative`` phone. A fresh re-verify REFUTES
    it and supplies the corrected value → the correction (machine_reverification)
    WINS: current = {corrected}, the stale value is deprecated-but-queryable."""
    n = PyoxiNeptune()
    await _seed_scraped(n, SCRAPED_PHONE, authority=AuthorityLevel.authoritative)
    assert await _current(n, ACME, PHONE) == {SCRAPED_PHONE}

    reverif = MachineReverification(
        predicate=PHONE,
        current_value=SCRAPED_PHONE,
        corrected_value=CORRECTED_PHONE,
        result=_refuted(SCRAPED_PHONE, CORRECTED_PHONE),
        subject=ACME,
        verifier="reverify-bot",
    )
    receipt = await apply_machine_reverification(
        n, INSTANCE_GRAPH, reverif, run_id="run-363",
    )

    # A correction was applied and the MACHINE value superseded the stale scrape.
    assert isinstance(receipt, MachineReverificationReceipt)
    assert receipt.applied is True
    assert receipt.superseded_stale is True
    assert receipt.preserved_existing is False
    assert receipt.conflict_receipt is not None
    assert receipt.conflict_receipt.winner == (ACME, PHONE, CORRECTED_PHONE)
    assert receipt.conflict_receipt.reason == REASON_AUTHORITY

    # (a) current-facts cites ONLY the corrected value now.
    assert await _current(n, ACME, PHONE) == {CORRECTED_PHONE}

    # (b) the stale value stays queryable, closed + DEPRECATED, pointing at the winner.
    history = await fetch_history(n, INSTANCE_GRAPH, ACME, PHONE)
    by_obj = {h.obj: h for h in history}
    assert set(by_obj) == {SCRAPED_PHONE, CORRECTED_PHONE}
    assert not by_obj[SCRAPED_PHONE].is_current and by_obj[SCRAPED_PHONE].valid_to
    assert by_obj[SCRAPED_PHONE].status == STATUS_DEPRECATED
    assert by_obj[CORRECTED_PHONE].is_current

    # The corrected value's provenance is stamped machine_reverification.
    prov = await fetch_provenance(n, INSTANCE_GRAPH, ACME, PHONE)
    by_val = {p.obj: p for p in prov}
    assert by_val[CORRECTED_PHONE].authority == "machine_reverification"
    assert by_val[CORRECTED_PHONE].source == "reverify-bot"

    # An A6 GraphDelta receipt was produced, carrying the winning fact.
    assert isinstance(receipt.conflict_receipt.graph_delta, GraphDelta)
    assert receipt.conflict_receipt.graph_delta.run_id == "run-363"


# --------------------------------------------------------------------------- #
# 2b. THE load-bearing control — a re-verify can NEVER clobber a user fix
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_reverify_cannot_clobber_user_fix():
    """CONTROL: the current value is a HUMAN fix, written via the REAL A10 correction
    path (``apply_user_assertion`` → ``user_assertion`` authority). The SAME machine
    re-verify — REFUTED with a corrected value — does NOT win: the user's value
    survives current, and the machine's proposal lands deprecated-but-queryable.

    Same code path as the supersede case, opposite outcome — decided by authority
    rank alone (machine_reverification STRICTLY below user_assertion)."""
    n = PyoxiNeptune()
    # Start from a scraped value, then a human corrects it to USER_PHONE.
    await _seed_scraped(n, SCRAPED_PHONE, authority=AuthorityLevel.authoritative)
    await apply_user_assertion(
        n, INSTANCE_GRAPH,
        UserAssertion(predicate=PHONE, value=USER_PHONE, subject=ACME,
                      type_name="Company", actor="user-42"),
        run_id="user-fix",
    )
    assert await _current(n, ACME, PHONE) == {USER_PHONE}

    # A machine re-verify disagrees with the human value and proposes CORRECTED_PHONE.
    reverif = MachineReverification(
        predicate=PHONE,
        current_value=USER_PHONE,
        corrected_value=CORRECTED_PHONE,
        result=_refuted(USER_PHONE, CORRECTED_PHONE),
        subject=ACME,
        verifier="reverify-bot",
    )
    receipt = await apply_machine_reverification(
        n, INSTANCE_GRAPH, reverif, run_id="run-363b",
    )

    # The correction WAS written (verdict warranted it), but the USER value WON.
    assert receipt.applied is True
    assert receipt.preserved_existing is True, "the user fix must survive the re-verify"
    assert receipt.superseded_stale is False
    assert receipt.conflict_receipt is not None
    assert receipt.conflict_receipt.winner == (ACME, PHONE, USER_PHONE)
    assert receipt.conflict_receipt.reason == REASON_AUTHORITY

    # THE INVARIANT: current is STILL the user's value — never clobbered.
    assert await _current(n, ACME, PHONE) == {USER_PHONE}

    # The machine's proposal is present-but-not-current (deprecated), still queryable.
    history = await fetch_history(n, INSTANCE_GRAPH, ACME, PHONE)
    by_obj = {h.obj: h for h in history}
    assert USER_PHONE in by_obj and CORRECTED_PHONE in by_obj
    assert by_obj[USER_PHONE].is_current, "the human fix stays current"
    assert not by_obj[CORRECTED_PHONE].is_current
    assert by_obj[CORRECTED_PHONE].status == STATUS_DEPRECATED

    # The user's value keeps its user_assertion authority in provenance.
    prov = await fetch_provenance(n, INSTANCE_GRAPH, ACME, PHONE)
    by_val = {p.obj: p for p in prov}
    assert by_val[USER_PHONE].authority == "user_assertion"
    # And the machine's deprecated proposal retains ITS machine_reverification stamp.
    assert by_val[CORRECTED_PHONE].authority == "machine_reverification"


# --------------------------------------------------------------------------- #
# 3. Annotate-only verdicts write nothing
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_supported_verdict_writes_nothing():
    """A SUPPORTED verdict corroborates the in-graph value — no correction written."""
    n = PyoxiNeptune()
    await _seed_scraped(n, SCRAPED_PHONE, authority=AuthorityLevel.authoritative)

    reverif = MachineReverification(
        predicate=PHONE,
        current_value=SCRAPED_PHONE,
        corrected_value=SCRAPED_PHONE,  # even if echoed, SUPPORTED never writes
        result=VerifierResult(verdict=TruthVerdict.SUPPORTED, confidence=0.9),
        subject=ACME,
    )
    receipt = await apply_machine_reverification(n, INSTANCE_GRAPH, reverif)

    assert receipt.applied is False
    assert receipt.conflict_receipt is None
    assert await _current(n, ACME, PHONE) == {SCRAPED_PHONE}


@pytest.mark.asyncio
async def test_unverifiable_verdict_writes_nothing():
    """UNVERIFIABLE (offline default) never writes — even with a corrected_value set,
    the verdict must be REFUTED to warrant a correction."""
    n = PyoxiNeptune()
    await _seed_scraped(n, SCRAPED_PHONE, authority=AuthorityLevel.authoritative)

    reverif = MachineReverification(
        predicate=PHONE,
        current_value=SCRAPED_PHONE,
        corrected_value=CORRECTED_PHONE,
        result=VerifierResult(verdict=TruthVerdict.UNVERIFIABLE, confidence=0.0),
        subject=ACME,
    )
    receipt = await apply_machine_reverification(n, INSTANCE_GRAPH, reverif)

    assert receipt.applied is False
    assert receipt.conflict_receipt is None
    assert await _current(n, ACME, PHONE) == {SCRAPED_PHONE}


@pytest.mark.asyncio
async def test_refuted_without_distinct_corrected_value_writes_nothing():
    """A REFUTED verdict with no corrected value (or one equal to the current value)
    has nothing concrete to write — the verdict annotates only."""
    n = PyoxiNeptune()
    await _seed_scraped(n, SCRAPED_PHONE, authority=AuthorityLevel.authoritative)

    # No corrected value.
    r1 = MachineReverification(
        predicate=PHONE, current_value=SCRAPED_PHONE, corrected_value="",
        result=_refuted(SCRAPED_PHONE, ""), subject=ACME,
    )
    assert (await apply_machine_reverification(n, INSTANCE_GRAPH, r1)).applied is False

    # Corrected value equals the current value (not a change).
    r2 = MachineReverification(
        predicate=PHONE, current_value=SCRAPED_PHONE, corrected_value=SCRAPED_PHONE,
        result=_refuted(SCRAPED_PHONE, SCRAPED_PHONE), subject=ACME,
    )
    assert (await apply_machine_reverification(n, INSTANCE_GRAPH, r2)).applied is False

    assert await _current(n, ACME, PHONE) == {SCRAPED_PHONE}


# --------------------------------------------------------------------------- #
# 4. Write-path convergence — routes through the converged conflict writer
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_write_routes_through_conflict_writer(monkeypatch):
    """The correction is written through ``write_with_conflict_resolution`` — the ONE
    converged conflict writer — stamped at machine_reverification authority."""
    import cograph_client.verification.reverify as reverify_mod
    from unittest.mock import AsyncMock

    from cograph_client.pipeline.mutations import ConflictReceipt

    fake_delta = GraphDelta(run_id="spy", instance_graph=INSTANCE_GRAPH, facts=())
    spy = AsyncMock(return_value=ConflictReceipt(
        op="conflict",
        graph_delta=fake_delta,
        winner=(ACME, PHONE, CORRECTED_PHONE),
        reason=REASON_AUTHORITY,
        conflict=True,
    ))
    monkeypatch.setattr(reverify_mod, "write_with_conflict_resolution", spy)

    reverif = MachineReverification(
        predicate=PHONE, current_value=SCRAPED_PHONE, corrected_value=CORRECTED_PHONE,
        result=_refuted(SCRAPED_PHONE, CORRECTED_PHONE), subject=ACME,
    )
    await apply_machine_reverification(None, INSTANCE_GRAPH, reverif)

    spy.assert_awaited_once()
    kwargs = spy.await_args.kwargs
    assert kwargs["authority"] is AuthorityLevel.machine_reverification
    assert kwargs["value"] == CORRECTED_PHONE
    assert kwargs["subject"] == ACME
    assert kwargs["predicate"] == PHONE


def test_reverify_module_has_no_bespoke_write_markers():
    """Structural: reverify.py hand-rolls NO instance write — no ``insert_triples(``
    and no raw SPARQL DELETE. It only orchestrates the converged writer."""
    src = inspect.getsource(__import__(
        "cograph_client.verification.reverify", fromlist=["x"]
    ))
    assert re.search(r"(?<![\w.])insert_triples\(", src) is None
    assert re.search(r"DELETE\s*\{|DELETE\s+WHERE|DELETE\s+DATA", src) is None
    assert "write_with_conflict_resolution" in src

"""ONTA-279 acceptance: the enrichment REFRESH rail is wired to the shipped P6
mutation lifecycle (supersession + conflict policy + suppression list).

Until now the supersession/conflict machinery (ONTA-276/277/281) was built but the
REAL refresh rail never used it: enrichment's apply phase wrote with raw
``insert_facts`` (blind append) or ``delete_facts``+``insert_facts`` (hard delete),
so an ``overwrite`` refresh CLOBBERED a ``user_assertion`` correction — the exact
trust-killer ONTA-281 was meant to prevent (its e2e only "passed" because it
hand-called ``write_with_conflict_resolution``). These tests drive the REAL
enrichment rail (``EnrichmentExecutor`` + a fake verdict source + an in-memory job
store) over a REAL in-process pyoxigraph store — never hand-calling the write op —
and assert STATEFUL SEQUENCES with load-bearing controls:

  1. a refresh with a newer value SUPERSEDES the old (closes its interval), never
     blind-appends — and the old instance triple still EXISTS (control);
  2. a user correction SURVIVES a contradicting refresh (completing ONTA-281's
     e2e), while a DIFFERENT non-corrected attribute IS updated on the SAME refresh
     (control that the refresh actually ran);
  3. a retracted/suppressed value is NOT re-acquired by a refresh, while a
     non-retracted value on the same entity IS (control);
  4. an A→B→A oscillation lands A current at the end and B closed (inherited
     ``reopen_facts`` resurrection).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from cograph_client.api_registry.spec import AuthorityLevel
from cograph_client.enrichment.cache import EnrichmentCache
from cograph_client.enrichment.executor import EnrichmentExecutor
from cograph_client.enrichment.job_store import InMemoryJobStore
from cograph_client.enrichment.models import (
    ConflictPolicy,
    EnrichJob,
    EnrichmentTier,
    JobStatus,
    Verdict,
)
from cograph_client.graph.kg_writer import insert_facts
from cograph_client.graph.ontology_queries import attr_uri
from cograph_client.graph.queries import kg_graph_uri
from cograph_client.graph.suppression import is_suppressed
from cograph_client.graph.validity import current_objects_query, fetch_history
from cograph_client.pipeline.corrections import UserAssertion, apply_user_assertion
from cograph_client.pipeline.mutations import retract_fact, write_with_conflict_resolution

from tests._enrichment_prov_helpers import FakeWikidata

TENANT, KG = "onta279", "corp"
INSTANCE_GRAPH = kg_graph_uri(TENANT, KG)
TYPE = "Company"
ACME = "https://cograph.tech/entities/Company/acme"
LABEL = "Acme"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
COMPANY_TYPE = "https://cograph.tech/types/Company"

PHONE = attr_uri(TYPE, "phone")
HQ = attr_uri(TYPE, "hq")
REV = attr_uri(TYPE, "rev")

# Fixed timestamps so the recency axis is deterministic (never depends on the wall
# clock being distinct between two sub-second writes).
T_OLD = datetime(2020, 1, 1, tzinfo=timezone.utc)
T1 = datetime(2021, 1, 1, tzinfo=timezone.utc)
T2 = datetime(2022, 1, 1, tzinfo=timezone.utc)
T3 = datetime(2023, 1, 1, tzinfo=timezone.utc)


pyoxigraph = pytest.importorskip("pyoxigraph")
from pyoxigraph import QueryResultsFormat, Store  # noqa: E402


class PyoxiNeptune:
    """Minimal NeptuneClient shim over an in-process pyoxigraph Store — async
    query()/update() returning SPARQL-1.1 JSON, union-of-named-graphs default."""

    def __init__(self) -> None:
        self.store = Store()

    async def query(self, sparql: str) -> dict:
        results = self.store.query(sparql, use_default_graph_as_union=True)
        return json.loads(results.serialize(format=QueryResultsFormat.JSON))

    async def update(self, sparql: str) -> None:
        self.store.update(sparql)


@pytest.fixture(autouse=True)
def _quiet_housekeeping(monkeypatch):
    """Silence the shared refresh_after_write internals (cache-invalidate / embed /
    stats recompute) so these tests isolate the refresh/supersession/suppression
    mechanism — exactly as tests/test_user_assertion.py + tests/test_validity_
    resurrection.py do. The real rail STILL calls refresh_after_write."""
    import cograph_client.api.routes.explore as explore_mod
    import cograph_client.nlp.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod.NLQueryPipeline, "invalidate_cache", lambda g: None)
    monkeypatch.setattr(pipeline_mod, "get_embedding_service", lambda: None)
    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
async def _seed_entity(n: PyoxiNeptune) -> None:
    """The typed, labelled entity the enrichment SELECT must find."""
    await insert_facts(
        n,
        INSTANCE_GRAPH,
        [(ACME, RDF_TYPE, COMPANY_TYPE), (ACME, RDFS_LABEL, LABEL)],
    )


async def _seed_current(
    n: PyoxiNeptune,
    predicate: str,
    value: str,
    *,
    authority: AuthorityLevel = AuthorityLevel.source_of_truth,
    confidence: float = 0.9,
    observed_at: datetime = T_OLD,
) -> None:
    """Seed a CURRENT fact WITH its authority persisted in provenance — via the SAME
    conflict-resolving op an upstream machine fact lands through (no existing value,
    so it is a plain current write). This is what makes the seed's authority
    readable when the refresh later arbitrates against it."""
    await write_with_conflict_resolution(
        n,
        INSTANCE_GRAPH,
        subject=ACME,
        predicate=predicate,
        type_name=TYPE,
        value=value,
        authority=authority,
        confidence=confidence,
        source="seed",
        observed_at=observed_at,
        run_id="seed",
    )


async def _current(n: PyoxiNeptune, predicate: str) -> set[str]:
    """The "current facts" projection — objects with no CLOSED validity interval."""
    raw = await n.query(current_objects_query(INSTANCE_GRAPH, ACME, predicate))
    return {b["o"]["value"] for b in raw["results"]["bindings"]}


async def _instance_objects(n: PyoxiNeptune, predicate: str) -> set[str]:
    """EVERY object of (ACME, predicate) in the instance graph, regardless of
    validity — so we can prove supersession CLOSED an interval but never DELETED the
    edge."""
    raw = await n.query(
        f"SELECT ?o WHERE {{ GRAPH <{INSTANCE_GRAPH}> {{ <{ACME}> <{predicate}> ?o }} }}"
    )
    return {b["o"]["value"] for b in raw["results"]["bindings"]}


def _verdict(value: str, *, at: datetime, confidence: float = 0.95) -> Verdict:
    return Verdict(
        value=value,
        confidence=confidence,
        source="scraper",
        source_url="https://scrape.example/x",
        retrieved_at=at,
    )


async def _run_refresh(
    n: PyoxiNeptune,
    verdicts: dict,
    attributes: list[str],
    *,
    policy: ConflictPolicy = ConflictPolicy.overwrite,
) -> EnrichJob:
    """Drive ONE real enrichment refresh over the store (fresh executor + cache so a
    prior run's verdict never serves from cache). ``verdicts`` maps
    ``(label, attribute) -> [Verdict, …]`` — the fake source's answers."""
    executor = EnrichmentExecutor(
        n, InMemoryJobStore(), EnrichmentCache(), FakeWikidata(verdicts)
    )
    job = EnrichJob(
        id=f"job-{policy.value}-{'-'.join(attributes)}-{len(verdicts)}",
        tenant_id=TENANT,
        kg_name=KG,
        type_name=TYPE,
        attributes=attributes,
        tier=EnrichmentTier.lite,
        status=JobStatus.queued,
        created_at=datetime.now(timezone.utc),
        conflict_policy=policy,
        entity_uris=[ACME],
    )
    await executor._jobs.create(job)
    await executor.run(job, TENANT)
    return await executor._jobs.get(job.id)


# --------------------------------------------------------------------------- #
# 1. A refresh with a newer value SUPERSEDES the old (never blind-appends).
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_refresh_supersedes_stale_value():
    """Seed a current fact (phone="111", source_of_truth, older + lower confidence).
    Run a REAL refresh producing "222" (higher confidence). "222" must become
    current and "111" must land in HISTORY WITH a valid_to (superseded, not deleted).
    Load-bearing control: the old value's instance triple still EXISTS."""
    n = PyoxiNeptune()
    await _seed_entity(n)
    await _seed_current(n, PHONE, "111", confidence=0.9, observed_at=T_OLD)
    assert await _current(n, PHONE) == {"111"}

    final = await _run_refresh(n, {(LABEL, "phone"): [_verdict("222", at=T1)]}, ["phone"])
    assert final.status == JobStatus.applied
    assert [r.action for r in final.results] == ["conflict"]

    # "222" is current; "111" superseded (present in history WITH a valid_to).
    assert await _current(n, PHONE) == {"222"}
    hist = {h.obj: h for h in await fetch_history(n, INSTANCE_GRAPH, ACME, PHONE)}
    assert set(hist) == {"111", "222"}
    assert hist["222"].is_current, "the fresh value must be current"
    assert not hist["111"].is_current and hist["111"].valid_to, (
        "the stale value must be SUPERSEDED (closed interval), not appended-beside"
    )

    # LOAD-BEARING CONTROL: supersession closes an interval, it never DELETES the
    # edge — both instance triples still exist in the graph.
    assert await _instance_objects(n, PHONE) == {"111", "222"}, (
        "supersession must keep the old edge (history), only close its interval"
    )


# --------------------------------------------------------------------------- #
# 2. A user correction SURVIVES a real refresh (completes ONTA-281's e2e).
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_user_correction_survives_a_real_refresh():
    """Correct phone to "user_phone" (A10, top authority). Seed hq="old_hq"
    (machine). Then run a REAL refresh asserting a CONTRADICTING phone="scrape_phone"
    AND a fresh hq="new_hq". The user's phone must STILL be current and the scrape
    deprecated-but-queryable; the DIFFERENT non-corrected hq IS updated — proving the
    refresh ran and WOULD have clobbered phone were it not for the authority
    protection."""
    n = PyoxiNeptune()
    await _seed_entity(n)
    await _seed_current(n, HQ, "old_hq", confidence=0.9, observed_at=T_OLD)
    await apply_user_assertion(
        n,
        INSTANCE_GRAPH,
        UserAssertion(predicate=PHONE, value="user_phone", subject=ACME, actor="user-1"),
        run_id="fix",
    )
    assert await _current(n, PHONE) == {"user_phone"}

    final = await _run_refresh(
        n,
        {
            (LABEL, "phone"): [_verdict("scrape_phone", at=T3)],
            (LABEL, "hq"): [_verdict("new_hq", at=T3)],
        },
        ["phone", "hq"],
    )
    assert final.status == JobStatus.applied

    # The user fix survives — a refresh must NEVER clobber a user correction.
    assert await _current(n, PHONE) == {"user_phone"}
    phone_hist = {h.obj: h for h in await fetch_history(n, INSTANCE_GRAPH, ACME, PHONE)}
    assert set(phone_hist) == {"user_phone", "scrape_phone"}
    assert phone_hist["user_phone"].is_current, "the user fix stays current"
    assert not phone_hist["scrape_phone"].is_current, (
        "the contradicting scrape must land DEPRECATED-but-queryable, not current"
    )

    # LOAD-BEARING CONTROL: the SAME refresh DID update a different, non-corrected
    # attribute — so the refresh genuinely ran (it would have clobbered phone
    # without the user_assertion authority protection).
    assert await _current(n, HQ) == {"new_hq"}, (
        "the refresh must update a non-corrected attribute (proof it actually ran)"
    )
    hq_hist = {h.obj: h for h in await fetch_history(n, INSTANCE_GRAPH, ACME, HQ)}
    assert not hq_hist["old_hq"].is_current, "the stale hq is superseded by the refresh"


# --------------------------------------------------------------------------- #
# 3. Suppression blocks re-acquisition of a retracted value.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_suppression_blocks_reacquisition():
    """Retract phone="gone" (writes a sticky suppression marker + closes its
    interval). A REAL refresh that re-scrapes the SAME "gone" value must NOT bring it
    back (it stays suppressed / not-current). Load-bearing control: a
    non-retracted attribute (hq) IS acquired by the same refresh."""
    n = PyoxiNeptune()
    await _seed_entity(n)
    await _seed_current(n, PHONE, "gone", confidence=0.9, observed_at=T_OLD)
    assert await _current(n, PHONE) == {"gone"}

    # Retract → closes the interval AND writes the sticky suppression marker.
    await retract_fact(
        n, INSTANCE_GRAPH, subject=ACME, predicate=PHONE, type_name=TYPE, value="gone"
    )
    assert await _current(n, PHONE) == set(), "retraction removes currency"
    assert await is_suppressed(n, INSTANCE_GRAPH, ACME, PHONE, "gone"), (
        "retraction must write a suppression marker"
    )

    final = await _run_refresh(
        n,
        {
            (LABEL, "phone"): [_verdict("gone", at=T3)],  # re-scrapes the retracted value
            (LABEL, "hq"): [_verdict("fresh_hq", at=T3)],  # control
        },
        ["phone", "hq"],
    )
    assert final.status == JobStatus.applied

    # The suppressed value is NOT re-acquired — a refresh cannot resurrect a
    # retracted value (unlike a bare validity closure, which reopen_facts clears).
    assert await _current(n, PHONE) == set(), (
        "a suppressed value must NOT be re-acquired by a refresh"
    )
    assert await is_suppressed(n, INSTANCE_GRAPH, ACME, PHONE, "gone"), (
        "the suppression marker is sticky — the refresh must not clear it"
    )

    # LOAD-BEARING CONTROL: a non-retracted attribute IS acquired by the SAME
    # refresh — so the refresh genuinely ran and only the suppressed value was held.
    assert await _current(n, HQ) == {"fresh_hq"}, (
        "a non-suppressed value must still be acquired by the refresh"
    )


# --------------------------------------------------------------------------- #
# 4. Oscillation A → B → A resurrection (inherited reopen_facts).
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_oscillation_A_B_A_resurrects_A():
    """Three real refreshes to rev: A (T1), then B (T2), then A again (T3). A must be
    current at the end and B closed — proving the refresh inherits ONTA-277's
    reopen_facts resurrection (the stale closure on A is cleared when it wins again)."""
    n = PyoxiNeptune()
    await _seed_entity(n)

    # A lands (fill).
    await _run_refresh(n, {(LABEL, "rev"): [_verdict("rev_a", at=T1)]}, ["rev"])
    assert await _current(n, REV) == {"rev_a"}

    # B wins on recency (equal authority + confidence, newer) → A deprecated.
    await _run_refresh(n, {(LABEL, "rev"): [_verdict("rev_b", at=T2)]}, ["rev"])
    assert await _current(n, REV) == {"rev_b"}

    # A wins AGAIN on recency (T3 > T2) → its stale closure must be cleared so it is
    # current once more (the resurrection this inherits for free via reopen_facts).
    await _run_refresh(n, {(LABEL, "rev"): [_verdict("rev_a", at=T3)]}, ["rev"])
    assert await _current(n, REV) == {"rev_a"}, "A must resurrect as current"

    hist = {h.obj: h for h in await fetch_history(n, INSTANCE_GRAPH, ACME, REV)}
    assert set(hist) == {"rev_a", "rev_b"}
    assert hist["rev_a"].is_current, "A is current at the end"
    assert not hist["rev_b"].is_current and hist["rev_b"].valid_to, "B is closed"

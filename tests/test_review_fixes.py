"""Wave-4 review-fix regressions (branch ``wave4/review-fixes``).

Two independent correctness fixes on the enrichment refresh rail, each pinned by a
focused, deterministic test:

  FIX 1 — the batched refresh path must do O(1) housekeeping, not O(N). The per-row
  ``write_with_conflict_resolution`` gained a ``refresh=False`` knob so the caller's
  ONE final ``refresh_after_write`` is the only housekeeping pass; previously each of
  the N per-row ops ran its own refresh (Neptune query + re-embed + stats) on top of
  the caller's final one, so a bulk refresh did ~N+1 passes.

  FIX 3 — ``EnrichmentExecutor._verdict_authority`` must NEVER return
  ``user_assertion``. A machine scrape stamped with the top human-correction
  authority would rank rank-0 and could tie/beat a real user fix at arbitration —
  the exact thing the refresh rail is supposed to protect. The level is clamped down
  to ``REFRESH_AUTHORITY`` regardless of what the verdict claims.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from cograph_client.api_registry.spec import AuthorityLevel
from cograph_client.enrichment.cache import EnrichmentCache
from cograph_client.enrichment.executor import REFRESH_AUTHORITY, EnrichmentExecutor
from cograph_client.enrichment.job_store import InMemoryJobStore
from cograph_client.enrichment.models import (
    ConflictPolicy,
    EnrichJob,
    EnrichmentTier,
    JobStatus,
    Verdict,
)
from cograph_client.graph.kg_writer import insert_facts
from cograph_client.graph.ontology_queries import attr_uri, entity_uri
from cograph_client.graph.queries import kg_graph_uri
from cograph_client.graph.validity import current_objects_query

from tests._enrichment_prov_helpers import FakeWikidata

TENANT, KG = "onta_reviewfix", "corp"
INSTANCE_GRAPH = kg_graph_uri(TENANT, KG)
TYPE = "Company"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
COMPANY_TYPE = "https://cograph.tech/types/Company"
PHONE = attr_uri(TYPE, "phone")

T1 = datetime(2021, 1, 1, tzinfo=timezone.utc)


pyoxigraph = pytest.importorskip("pyoxigraph")
from pyoxigraph import QueryResultsFormat, Store  # noqa: E402

from cograph_client.pipeline import mutations as mutations_mod  # noqa: E402
import cograph_client.enrichment.executor as executor_mod  # noqa: E402
from cograph_client.pipeline.mutations import (  # noqa: E402
    write_with_conflict_resolution,
)


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


@pytest.fixture
def refresh_spy(monkeypatch):
    """Replace ``refresh_after_write`` at BOTH call sites (the per-row op in
    ``pipeline.mutations`` and the caller's final pass in ``enrichment.executor``)
    with ONE counting no-op, so the test sees the TOTAL number of housekeeping
    passes across the whole refresh. Replacing it entirely is safe here: the derived
    -index housekeeping it drives (cache-invalidate / re-embed / stats recompute) is
    orthogonal to the data writes, which land via ``insert_facts`` regardless."""
    calls = {"n": 0}

    async def _spy(*a, **k):
        calls["n"] += 1

    monkeypatch.setattr(mutations_mod, "refresh_after_write", _spy)
    monkeypatch.setattr(executor_mod, "refresh_after_write", _spy)
    return calls


async def _current(n: PyoxiNeptune, subject: str, predicate: str) -> set[str]:
    raw = await n.query(current_objects_query(INSTANCE_GRAPH, subject, predicate))
    return {b["o"]["value"] for b in raw["results"]["bindings"]}


# --------------------------------------------------------------------------- #
# FIX 1a — the op's ``refresh`` knob directly gates its own housekeeping pass.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_write_op_refresh_false_skips_housekeeping(refresh_spy):
    """``write_with_conflict_resolution(refresh=False)`` must NOT call
    ``refresh_after_write``; the default (``refresh=True``) must call it exactly
    once. Same op, same store, same scope — only the knob differs."""
    n = PyoxiNeptune()
    subj = entity_uri(TYPE, "acme")

    # refresh=False → the per-row op defers housekeeping to the caller.
    await write_with_conflict_resolution(
        n, INSTANCE_GRAPH,
        subject=subj, predicate=PHONE, type_name=TYPE, value="111",
        source="seed", observed_at=T1, run_id="r1", refresh=False,
    )
    assert refresh_spy["n"] == 0, "refresh=False must skip the housekeeping pass"
    # The data still landed — refresh only gates housekeeping, not the write.
    assert await _current(n, subj, PHONE) == {"111"}

    # refresh=True (default) → the op runs its own single housekeeping pass.
    await write_with_conflict_resolution(
        n, INSTANCE_GRAPH,
        subject=subj, predicate=PHONE, type_name=TYPE, value="222",
        source="seed", observed_at=T1, run_id="r2",  # refresh defaults to True
    )
    assert refresh_spy["n"] == 1, "the default refresh=True must run exactly one pass"


# --------------------------------------------------------------------------- #
# FIX 1b — a bulk (N-row) enrichment refresh does ONE housekeeping pass, not N+1.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_bulk_refresh_does_one_housekeeping_pass(refresh_spy):
    """Drive the REAL enrichment refresh rail over N entities of one type. The whole
    run must invoke ``refresh_after_write`` exactly ONCE (the executor's single final
    pass) — not N per-row passes + 1. Before FIX 1 this was N+1 (== 4 for N=3)."""
    n = PyoxiNeptune()
    N = 3
    entities = [entity_uri(TYPE, f"acme{i}") for i in range(N)]
    labels = [f"Acme {i}" for i in range(N)]

    # Seed N typed, labelled entities (no prior phone value → each row is a fresh
    # fill the refresh applies). Direct insert_facts does not call refresh_after_write.
    triples = []
    for uri, label in zip(entities, labels):
        triples.append((uri, RDF_TYPE, COMPANY_TYPE))
        triples.append((uri, RDFS_LABEL, label))
    await insert_facts(n, INSTANCE_GRAPH, triples)
    assert refresh_spy["n"] == 0, "seeding must not trigger housekeeping"

    # Each entity's fake source answer for `phone`.
    verdicts = {
        (label, "phone"): [
            Verdict(value=f"num-{i}", confidence=0.95, source="scraper",
                    source_url="https://scrape.example/x", retrieved_at=T1)
        ]
        for i, label in enumerate(labels)
    }

    executor = EnrichmentExecutor(
        n, InMemoryJobStore(), EnrichmentCache(), FakeWikidata(verdicts)
    )
    job = EnrichJob(
        id="bulk-refresh-job",
        tenant_id=TENANT,
        kg_name=KG,
        type_name=TYPE,
        attributes=["phone"],
        tier=EnrichmentTier.lite,
        status=JobStatus.queued,
        created_at=datetime.now(timezone.utc),
        conflict_policy=ConflictPolicy.overwrite,  # a refresh policy
        entity_uris=entities,
    )
    await executor._jobs.create(job)
    await executor.run(job, TENANT)
    final = await executor._jobs.get(job.id)

    assert final.status == JobStatus.applied
    # THE ASSERTION: O(1), not O(N). Exactly one housekeeping pass for the whole run.
    assert refresh_spy["n"] == 1, (
        f"a bulk refresh over {N} rows must do ONE housekeeping pass, got "
        f"{refresh_spy['n']} (pre-fix it was N+1 = {N + 1})"
    )
    # Control: the refresh actually ran — every entity got its fresh value current.
    for i, uri in enumerate(entities):
        assert await _current(n, uri, PHONE) == {f"num-{i}"}


# --------------------------------------------------------------------------- #
# FIX 3 — _verdict_authority never yields the human-only ``user_assertion`` slot.
# --------------------------------------------------------------------------- #
def _verdict_with_authority(authority):
    return Verdict(value="x", confidence=0.9, source="scraper", authority=authority)


def test_verdict_authority_clamps_user_assertion():
    """A machine verdict carrying ``authority="user_assertion"`` is downgraded to
    ``REFRESH_AUTHORITY`` — never the top human-correction slot, so it can neither
    tie nor beat a real user fix at write-time arbitration."""
    got = EnrichmentExecutor._verdict_authority(
        _verdict_with_authority("user_assertion")
    )
    assert got == REFRESH_AUTHORITY
    assert got != AuthorityLevel.user_assertion


def test_verdict_authority_passes_through_curated_and_defaults():
    """A curated ``source_of_truth`` verdict threads through verbatim; an
    absent/blank or garbage authority defaults to ``REFRESH_AUTHORITY`` — the
    non-user levels are unaffected by the clamp."""
    assert (
        EnrichmentExecutor._verdict_authority(
            _verdict_with_authority("source_of_truth")
        )
        == AuthorityLevel.source_of_truth
    )
    # Absent authority → default.
    assert (
        EnrichmentExecutor._verdict_authority(_verdict_with_authority(None))
        == REFRESH_AUTHORITY
    )
    # Unparseable authority string → default (ValueError path).
    assert (
        EnrichmentExecutor._verdict_authority(_verdict_with_authority("not_a_level"))
        == REFRESH_AUTHORITY
    )

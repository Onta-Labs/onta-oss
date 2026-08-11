"""The semantic-index write hook in graph/kg_writer.py (ONTA-181) — currently DEAD.

**Read this before adding to this file (ONTA-527).** The write hook is not
wired into the production write path and has not been since the Neo4j cutover.
``kg_writer._insert_facts_store`` — the only insert path there is now — calls
``_index_spatiotemporal`` and deliberately does NOT call ``_index_semantic``
(there is a GAP comment saying so at the call site). ``_index_semantic`` itself
is unreachable for a second, independent reason: it rebuilds each touched
entity's docs from a RE-READ of the graph (the ONTA-173 completeness contract)
and that re-read is SPARQL, so it could not serve a property-graph store even
if something called it. Nothing in this file was broken by ONTA-527; ONTA-527
only made it visible.

So: the FRESHNESS half of the ONTA-173 consistency model does not exist in
production. Nothing indexes free text at write time. What is left is the
CORRECTNESS half — the claim-based reconciler
(``semantic/reconciler.py``, covered by ``test_semantic_reconciler.py``) — and
even that only re-indexes attributes carrying a durable ``textKind`` marker,
which is itself unpersistable on the store path (see the strict xfail in
``test_semantic_reconciler.py``). The removal side is NOT dead: whole-subject
deletes and ER rewrites still evict semantic rows through
``refresh_after_write`` → ``_deindex_secondary``. So the index can currently
lose rows on the write path but never gain them.

What this file is now
---------------------

Five ``xfail(strict=True)`` cases stating the capability at the PRODUCTION
seam — a write through :func:`insert_facts` against a :class:`MemoryGraphStore`
— so that porting the hook flips them XPASS and forces whoever does it to come
back here. Plus one live test of the isolation guarantee (a broken index must
never fail a KG write), which holds vacuously today and must keep holding after
the port. Free-text markers are seeded through the ``text_markers`` tenant
cache rather than a SPARQL fake, so these cases stay valid for any port that
keeps using the shared ``get_free_text_map`` helper.

Coverage deliberately DROPPED (all of it was assertions about the SPARQL the
dead hook emitted or about behaviour that is now vacuously true — restore it
alongside the port, not before):

* gating (``INFONA_SEMANTIC_INDEX_ENABLED`` off by default,
  ``INFONA_SEMANTIC_IDENTITY_INDEX`` off, unmarked predicates ignored, the
  durable ``not_text`` decided-no honoured, non-KG graphs skipped) — every one
  asserted "no rows were indexed" and/or a Neptune query count, and every one
  is now true no matter what the hook would do;
* the bounded-cost contract (no entity re-read for an unmarked write;
  ``INFONA_SEMANTIC_HOOK_MAX_ENTITIES`` capping the fetch) — the assertions
  read the ``VALUES ?e { … }`` clause of the re-read, i.e. exactly the query the
  port has to replace;
* re-read failure handling (index skipped, never degraded to partial docs;
  marker-map fetch failure) — the failure mode is a SPARQL read that no longer
  exists;
* hook/reconciler dedup parity for a mirrored attribute — it compared two
  SPARQL fakes; the reconciler half is covered in ``test_semantic_reconciler.py``.
"""

from __future__ import annotations

import asyncio
import time

import pytest

import infona_client.graph.text_markers as tm
import infona_client.semantic.reconciler as rec
from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.queries import kg_graph_uri
from infona_client.graph.store import configure_graph_store
from infona_client.scheduling.store import get_schedule_store, reset_schedule_store
from infona_client.semantic.extract import (
    canonicalize_values,
    content_hash,
    extract_semantic_chunks,
)
from infona_client.semantic.memory import InMemorySemanticIndex
from infona_client.semantic.protocol import IDENTITY_ATTR, SemanticChunk
from infona_client.semantic.registry import (
    register_semantic_index,
    reset_semantic_index,
)

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"

TENANT = "t1"
KG = "kg1"
GRAPH = kg_graph_uri(TENANT, KG)
DOC_TYPE = "https://graph.infona.ai/types/Doc"
DESC_PRED = "https://graph.infona.ai/types/Doc/attrs/description"
NOTES_PRED = "https://graph.infona.ai/types/Doc/attrs/notes"
ENTITY = "https://graph.infona.ai/entities/Doc/e1"
NOTES = "Some standing notes."
PROSE = (
    "The committee heard extensive testimony about the proposed changes to the "
    "watershed management plan and debated the funding formula for well over "
    "two hours before adjourning without a final vote on the matter."
)
PROSE_TAIL = (
    "A follow-up session was scheduled for the next quarter, where the revised "
    "funding formula and the amended watershed boundaries will be put to a "
    "binding vote of the full committee."
)

DEAD_HOOK = (
    "LOST CAPABILITY (pre-dates ONTA-527, surfaced by it): the semantic-index "
    "WRITE hook does not run. graph/kg_writer.py::_insert_facts_store — the "
    "only insert path since the Neo4j cutover — calls _index_spatiotemporal "
    "and skips _index_semantic (see the GAP comment at that call site), and "
    "_index_semantic could not serve a property-graph store anyway because its "
    "completeness re-read (_fetch_touched_entity_triples) is SPARQL. So "
    "nothing indexes free text at write time; freshness rests entirely on the "
    "claim-based reconciler in semantic/reconciler.py. Not fixed here: the "
    "port needs a GraphStore re-read, which is out of ONTA-527's scope. "
)


@pytest.fixture(autouse=True)
def _clean_state():
    reset_semantic_index()
    tm.reset_for_tests()
    rec.reset_for_tests()
    reset_schedule_store()
    yield
    reset_semantic_index()
    tm.reset_for_tests()
    rec.reset_for_tests()
    reset_schedule_store()


@pytest.fixture
def store():
    """The GraphStore the write lands in (conftest installs one too; this makes
    the instance the assertions read back from explicit)."""
    st = MemoryGraphStore()
    configure_graph_store(st)
    return st


@pytest.fixture
def index():
    idx = InMemorySemanticIndex()
    register_semantic_index(idx)
    return idx


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setenv("INFONA_SEMANTIC_INDEX_ENABLED", "true")


def _mark_free_text(*predicates: str) -> None:
    """Declare ``predicates`` free-text for this tenant.

    Seeded straight into the ``text_markers`` tenant cache (the read the hook
    makes, via ``get_free_text_map``, returns a cached map without touching any
    backend inside the TTL) so the marker declaration carries no transport of
    its own — the pre-ONTA-527 version of this file spelled markers as SPARQL
    query results from a fake Neptune.
    """
    tm._cache[TENANT] = (time.monotonic(), {p: True for p in predicates})


def _seed_index_doc(idx: InMemorySemanticIndex, pred: str, *values: str) -> None:
    """Put the doc the hook WOULD have written into the index directly.

    Built with the hook's own extractor so the seeded rows are byte-identical
    to hook output; used by the cases that test what a LATER write does to an
    already-indexed doc.
    """
    triples = [(ENTITY, pred, v) for v in values]
    chunks = extract_semantic_chunks(
        triples, tenant_id=TENANT, kg_name=KG, marked_predicates={pred}
    )
    assert chunks, "seed produced no chunks"
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        idx.upsert_chunks(chunks)
    )


async def _docs(idx: InMemorySemanticIndex) -> set[tuple[str, str]]:
    return {(d[0], d[1]) for d in await idx.list_docs(TENANT, kg_name=KG)}


# --- the write hook's capability, stated at the production seam ---------------


@pytest.mark.xfail(reason=DEAD_HOOK, strict=True)
def test_write_indexes_marked_free_text(store, index):
    """A write of a marked free-text attribute lands a chunk in the same
    request, with ``embedding=None`` (the durable queue) and lexically
    searchable immediately."""
    _mark_free_text(DESC_PRED)

    async def run():
        await insert_facts(
            None,
            GRAPH,
            [(ENTITY, RDF_TYPE, DOC_TYPE), (ENTITY, DESC_PRED, PROSE)],
            store=store,
        )
        rows = await index.fetch_pending(limit=100)
        assert [(r.entity_uri, r.attr) for r in rows] == [(ENTITY, "description")]
        assert rows[0].embedding is None
        hits = await index.search(TENANT, "watershed management", kg_name=KG)
        assert [h.entity_uri for h in hits.hits] == [ENTITY]

    asyncio.run(run())


@pytest.mark.xfail(reason=DEAD_HOOK, strict=True)
def test_write_indexes_the_identity_doc_for_a_name_only_write(store, index):
    """ONTA-421: a write carrying only a name, on a tenant with ZERO markers,
    must still make the entity findable by that name.

    The identity row is deliberately invisible to the embed queue, so the
    assertion goes through ``list_docs`` rather than ``fetch_pending``.
    """

    async def run():
        await insert_facts(
            None,
            GRAPH,
            [(ENTITY, RDF_TYPE, DOC_TYPE), (ENTITY, RDFS_LABEL, "Acme Corporation")],
            store=store,
        )
        assert await _docs(index) == {(ENTITY, IDENTITY_ATTR)}
        assert await index.fetch_pending(limit=100) == []  # never embedded
        hits = await index.search(TENANT, "Acme Corporation", kg_name=KG)
        assert [h.entity_uri for h in hits.hits] == [ENTITY]

    asyncio.run(run())


@pytest.mark.xfail(reason=DEAD_HOOK, strict=True)
def test_appending_a_value_merges_the_doc_instead_of_wiping_the_tail(store, index):
    """THE partial-doc-wipe regression (ONTA-173).

    A second write carrying ONLY the appended value (what schema_resolver's
    duplicate-merge path emits) must yield the MERGED doc: upsert is
    replace-per-doc, so a doc built from the write's own triples would wipe the
    already-indexed text. The doc has to come from the entity's full state,
    which the store does hold — a repeated literal predicate accumulates
    values rather than overwriting.
    """
    _mark_free_text(DESC_PRED)
    _seed_index_doc(index, DESC_PRED, PROSE)

    async def run():
        await insert_facts(None, GRAPH, [(ENTITY, DESC_PRED, PROSE)], store=store)
        await insert_facts(None, GRAPH, [(ENTITY, DESC_PRED, PROSE_TAIL)], store=store)
        rows = await index.fetch_pending(limit=100)
        assert {(r.entity_uri, r.attr) for r in rows} == {(ENTITY, "description")}
        merged = canonicalize_values([PROSE, PROSE_TAIL])
        assert "".join(r.chunk_text for r in sorted(rows, key=lambda r: r.chunk_ix)) == merged
        assert rows[0].content_hash == content_hash(merged)

    asyncio.run(run())


@pytest.mark.xfail(reason=DEAD_HOOK, strict=True)
def test_emptying_a_marked_attr_deletes_only_that_attrs_doc(store, index):
    """ONTA-175 empty-doc contract: when a touched entity's re-read doc is
    EMPTY (its values were replaced with whitespace), that attr's rows are
    deleted explicitly — an empty doc has no chunk rows to carry its key
    through upsert — and the entity's OTHER marked docs are untouched."""
    _mark_free_text(DESC_PRED, NOTES_PRED)
    _seed_index_doc(index, DESC_PRED, PROSE)
    _seed_index_doc(index, NOTES_PRED, NOTES)

    async def run():
        assert await _docs(index) == {(ENTITY, "description"), (ENTITY, "notes")}
        # The KG's current state for description is whitespace-only (a
        # normalization replaced the prose); notes still holds real text.
        await insert_facts(
            None,
            GRAPH,
            [(ENTITY, DESC_PRED, "   "), (ENTITY, NOTES_PRED, NOTES)],
            store=store,
        )
        assert await _docs(index) == {(ENTITY, "notes")}

    asyncio.run(run())


@pytest.mark.xfail(reason=DEAD_HOOK, strict=True)
def test_write_ensures_the_kgs_reconcile_schedule(store, index):
    """The hook memoizes an ensure of the KG's recurring reconcile row — how a
    write-active KG gets periodic ghost repair with no operator action. With
    the hook dead, a KG that is only ever WRITTEN to never gets one."""
    _mark_free_text(DESC_PRED)

    async def run():
        await insert_facts(None, GRAPH, [(ENTITY, DESC_PRED, PROSE)], store=store)
        schedule = await get_schedule_store().get(rec.reconcile_schedule_id(TENANT, KG))
        assert schedule is not None
        assert schedule.action == "semantic-reconcile"
        assert schedule.kg_name == KG
        assert schedule.next_run is not None  # first run = the backfill

    asyncio.run(run())


# --- isolation: a derived-index hiccup must never fail the KG write -----------


class _HangingIndex(InMemorySemanticIndex):
    """upsert_chunks hangs — a partitioned/hung index backend."""

    async def upsert_chunks(self, chunks):  # noqa: ANN001
        await asyncio.sleep(30)


class _ExplodingIndex(InMemorySemanticIndex):
    async def upsert_chunks(self, chunks):  # noqa: ANN001
        raise RuntimeError("index backend down")


@pytest.mark.parametrize("backend", [_HangingIndex, _ExplodingIndex])
def test_a_broken_semantic_index_never_fails_the_write(store, monkeypatch, backend):
    """The KG write survives an index backend that hangs or raises.

    Currently VACUOUS — the write never touches the semantic index at all (see
    the module docstring) — and kept anyway, because it is the invariant a port
    of the hook is most likely to break: the timeout knob
    (``INFONA_SEMANTIC_UPSERT_TIMEOUT_S``) exists precisely so a hung backend
    becomes a caught TimeoutError instead of a request that never returns. The
    outer ``wait_for`` is what makes the hanging case a real assertion rather
    than a 30-second test.
    """
    monkeypatch.setenv("INFONA_SEMANTIC_UPSERT_TIMEOUT_S", "0.05")
    _mark_free_text(DESC_PRED)
    register_semantic_index(backend())

    async def run():
        await asyncio.wait_for(
            insert_facts(
                None,
                GRAPH,
                [(ENTITY, RDF_TYPE, DOC_TYPE), (ENTITY, DESC_PRED, PROSE)],
                store=store,
            ),
            timeout=5,
        )

    asyncio.run(run())
    # The primary write went through.
    assert [e["id"] for e in store.snapshot_entities()] == [ENTITY]


def test_seeded_chunks_are_what_the_extractor_produces(index):
    """Guard for this file's own seeding helper.

    :func:`_seed_index_doc` stands in for hook output in two cases above; if it
    ever drifted from the real extractor those cases would be xfailing for the
    wrong reason. Pinned here so the seed is checked independently of the dead
    hook.
    """
    _seed_index_doc(index, DESC_PRED, PROSE)

    async def check():
        rows = await index.fetch_pending(limit=100)
        assert [(r.entity_uri, r.attr) for r in rows] == [(ENTITY, "description")]
        assert isinstance(rows[0], SemanticChunk)
        assert rows[0].content_hash == content_hash(canonicalize_values([PROSE]))

    asyncio.run(check())

"""Tests for the shared KG write path (graph/kg_writer.py).

This module is the single insertion + post-write housekeeping path that BOTH
ingestion and enrichment must use, so these tests pin the behaviors that keep the
two from drifting: every fact landing through one primitive, provenance kept in
companion records (never mixed into instance data), and the post-write refreshes
(cache-invalidate, re-embed, triple-count invalidate, recompute stats) running
best-effort.

**Ported by ONTA-527.** ``insert_facts`` / ``delete_facts`` / ``rewrite_subject``
are GraphStore-only now; their SPARQL tails are deleted and the leading
``neptune`` argument is vestigial (ignored). The write-shape assertions here used
to read the emitted SPARQL — ``DELETE DATA`` batches of 500, ``VALUES (?s ?p)``,
``DELETE { <old> ?p ?o } INSERT { <new> ?p ?o }``, a companion provenance NAMED
GRAPH — and none of those strings exist any more. They are replaced by assertions
on what the store actually holds afterwards, which is what the strings stood in
for, plus ``neptune.update.assert_not_awaited()`` so "the SPARQL path is gone" is
pinned rather than assumed.

Two contract details are worth stating because they CHANGED, not just moved:

* the removal count is now the number of STORE ROWS removed, and ADR 0013 keeps a
  datatype/object ``Assertion`` alongside each property or relationship, so
  clearing one literal reports 2 (the property + its Assertion). It is derived
  from real deletions rather than a best-effort ``COUNT(*)`` query, so it is
  exact in a way the SPARQL path's pattern-delete count was not;
* provenance is ``:ProvEvent`` companion records written inside the store path,
  gated by ``INFONA_PROVENANCE_ENABLED`` exactly as the named-graph writes were.
  RDF ``provenance_triples`` handed to ``insert_facts`` are IGNORED (see the
  module docstring of ``kg_writer``), so a caller still passing them writes
  nothing — pinned below so that silence is deliberate rather than discovered.
"""

import asyncio
from unittest.mock import AsyncMock

import infona_client.api.routes.explore as explore_mod
import infona_client.nlp.pipeline as pipeline_mod
from infona_client.graph.iri import IRI_BASE
from infona_client.graph.kg_writer import (
    delete_facts,
    insert_facts,
    refresh_after_write,
    rewrite_subject,
)
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_queries import entity_uri
from infona_client.spatiotemporal.memory import InMemorySpatioTemporalIndex
from infona_client.spatiotemporal.protocol import SpatioTemporalFact
from infona_client.spatiotemporal.registry import (
    register_spatiotemporal_index,
    reset_spatiotemporal_index,
)

#: A per-KG instance graph. A TENANT graph URI (``…/graphs/t``) no longer
#: resolves — the store path derives (tenant, kg) from this URI and raises
#: ``GraphScopeError`` rather than writing nowhere.
GRAPH = f"{IRI_BASE}/graphs/t/kg/k"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
LABEL_PRED = "http://www.w3.org/2000/01/rdf-schema#label"
WIDGET_TYPE = f"{IRI_BASE}/types/Widget"
PRICE_ATTR = f"{IRI_BASE}/types/Widget/attrs/price"
RELATED_PRED = f"{IRI_BASE}/onto/relatedTo"


def _props(store: MemoryGraphStore, entity_id: str) -> dict:
    for row in store.snapshot_entities():
        if row["id"] == entity_id:
            return row
    return {}


def _prov_events(store: MemoryGraphStore, event_type: str) -> list[dict]:
    return [p for p in store.snapshot_prov() if p["event_type"] == event_type]


def test_insert_facts_writes_every_fact_through_the_store():
    """A large write lands in full through the ONE insertion primitive.

    The 1200-triple size is inherited from the batching test this replaces: the
    SPARQL path had to chunk at 500 to stay under Neptune's per-statement limit,
    and the property-graph path has no such limit, so what survives the port is
    the property the batching existed to guarantee — nothing is dropped — plus
    the proof that no SPARQL is emitted at all.
    """

    async def run():
        store = MemoryGraphStore()
        neptune = AsyncMock()
        instance_triples = []
        for i in range(1200):
            uri = entity_uri("Widget", f"w{i}")
            instance_triples.append((uri, RDF_TYPE, WIDGET_TYPE))
            instance_triples.append((uri, PRICE_ATTR, f"v{i}"))

        await insert_facts(neptune, GRAPH, instance_triples, store=store)

        neptune.update.assert_not_awaited()
        neptune.query.assert_not_awaited()
        assert store.entity_count(tenant_id="t", kg="k") == 1200
        assert _props(store, entity_uri("Widget", "w0"))["props"] == {"price": "v0"}
        assert _props(store, entity_uri("Widget", "w1199"))["props"] == {"price": "v1199"}

    asyncio.run(run())


def test_insert_facts_routes_provenance_to_companion_records(monkeypatch):
    """Provenance never mixes into instance data.

    The named-graph version of this assertion was "the provenance triples name
    the companion graph and the instance statements do not". Its property-graph
    equivalent: assert events are ``:ProvEvent`` companion rows, the entities
    carry only their own attributes, and the RDF ``provenance_triples`` payload
    creates no entity of its own (it is ignored — the store path derives its
    provenance from the facts).
    """

    async def run():
        monkeypatch.setenv("INFONA_PROVENANCE_ENABLED", "1")
        store = MemoryGraphStore()
        subject = entity_uri("Widget", "w1")
        prov_stmt = f"{IRI_BASE}/prov/stmt/abc"

        await insert_facts(
            None,
            GRAPH,
            [(subject, RDF_TYPE, WIDGET_TYPE), (subject, PRICE_ATTR, "10")],
            provenance_triples=[(prov_stmt, f"{IRI_BASE}/prov/source", "csv")],
            store=store,
        )

        asserts = _prov_events(store, "assert")
        assert [(p["subject_id"], p["attr"]) for p in asserts] == [(subject, "price")]
        # The instance side carries the attribute and nothing provenance-shaped.
        assert _props(store, subject)["props"] == {"price": "10"}
        assert prov_stmt not in {row["id"] for row in store.snapshot_entities()}

    asyncio.run(run())


def test_insert_facts_writes_no_provenance_when_the_gate_is_off(monkeypatch):
    async def run():
        monkeypatch.delenv("INFONA_PROVENANCE_ENABLED", raising=False)
        monkeypatch.delenv("INFONA_PROVENANCE_STORE_ALWAYS", raising=False)
        store = MemoryGraphStore()
        await insert_facts(
            None,
            GRAPH,
            [(entity_uri("Widget", "w1"), PRICE_ATTR, "10")],
            store=store,
        )
        assert store.snapshot_prov() == []

    asyncio.run(run())


def test_insert_facts_noop_on_empty():
    async def run():
        store = MemoryGraphStore()
        neptune = AsyncMock()
        await insert_facts(neptune, GRAPH, [], provenance_triples=None, store=store)
        neptune.update.assert_not_awaited()
        assert store.snapshot_entities() == []

    asyncio.run(run())


def test_refresh_after_write_runs_all_three(monkeypatch):
    """Cache-invalidate, re-embed affected types, drop stored triple count, and
    recompute stats all fire with the right args — the housekeeping enrichment
    used to skip entirely."""

    async def run():
        calls = {
            "invalidate": [],
            "embed": [],
            "recompute": [],
            "triple_count": [],
        }

        monkeypatch.setattr(
            pipeline_mod.NLQueryPipeline,
            "invalidate_cache",
            lambda graph: calls["invalidate"].append(graph),
        )

        class FakeSvc:
            async def embed_types(self, graph, types, neptune):
                calls["embed"].append((graph, list(types)))

        monkeypatch.setattr(pipeline_mod, "get_embedding_service", lambda: FakeSvc())
        monkeypatch.setattr(
            explore_mod,
            "schedule_recompute",
            lambda neptune, tenant_id, kg_name: calls["recompute"].append((tenant_id, kg_name)),
        )

        async def fake_invalidate_triple_count(client, tenant_id, name):
            calls["triple_count"].append((tenant_id, name))

        monkeypatch.setattr(
            "infona_client.api.routes.knowledge_graphs.invalidate_triple_count",
            fake_invalidate_triple_count,
        )

        neptune = AsyncMock()
        await refresh_after_write(
            neptune, tenant_id="t", kg_name="k", affected_types={"Company"},
        )

        onto = "https://graph.infona.ai/graphs/t"
        assert calls["invalidate"] == [onto]
        assert calls["embed"] == [(onto, ["Company"])]
        assert calls["recompute"] == [("t", "k")]
        # Stored kg_triple_count must be dropped so list_kgs does not serve a
        # sticky pre-ingest 0 after this write.
        assert calls["triple_count"] == [("t", "k")]

    asyncio.run(run())


def test_refresh_after_write_skips_embed_without_types(monkeypatch):
    """No affected types → no embed call (but cache-invalidate + recompute still run)."""

    async def run():
        embedded = []
        recomputed = []
        monkeypatch.setattr(pipeline_mod.NLQueryPipeline, "invalidate_cache", lambda graph: None)

        class FakeSvc:
            async def embed_types(self, graph, types, neptune):
                embedded.append(types)

        monkeypatch.setattr(pipeline_mod, "get_embedding_service", lambda: FakeSvc())
        monkeypatch.setattr(
            explore_mod, "schedule_recompute",
            lambda neptune, tenant_id, kg_name: recomputed.append(kg_name),
        )

        await refresh_after_write(AsyncMock(), tenant_id="t", kg_name="k", affected_types=set())
        assert embedded == []
        assert recomputed == ["k"]

    asyncio.run(run())


def test_refresh_after_write_is_best_effort(monkeypatch):
    """An embedding failure must NOT propagate, and must not block the stats
    recompute that follows it."""

    async def run():
        recomputed = []
        monkeypatch.setattr(pipeline_mod.NLQueryPipeline, "invalidate_cache", lambda graph: None)

        class BadSvc:
            async def embed_types(self, graph, types, neptune):
                raise RuntimeError("embedding backend down")

        monkeypatch.setattr(pipeline_mod, "get_embedding_service", lambda: BadSvc())
        monkeypatch.setattr(
            explore_mod, "schedule_recompute",
            lambda neptune, tenant_id, kg_name: recomputed.append(kg_name),
        )

        # Should not raise.
        await refresh_after_write(AsyncMock(), tenant_id="t", kg_name="k", affected_types={"X"})
        assert recomputed == ["k"]

    asyncio.run(run())


def test_refresh_after_write_skips_recompute_without_kg(monkeypatch):
    """No kg_name (tenant-graph-only write) → no stats recompute or triple-count invalidate."""

    async def run():
        recomputed = []
        triple_counts = []
        monkeypatch.setattr(pipeline_mod.NLQueryPipeline, "invalidate_cache", lambda graph: None)
        monkeypatch.setattr(pipeline_mod, "get_embedding_service", lambda: None)
        monkeypatch.setattr(
            explore_mod, "schedule_recompute",
            lambda neptune, tenant_id, kg_name: recomputed.append(kg_name),
        )

        async def fake_invalidate_triple_count(client, tenant_id, name):
            triple_counts.append((tenant_id, name))

        monkeypatch.setattr(
            "infona_client.api.routes.knowledge_graphs.invalidate_triple_count",
            fake_invalidate_triple_count,
        )
        await refresh_after_write(AsyncMock(), tenant_id="t", kg_name=None, affected_types={"X"})
        assert recomputed == []
        assert triple_counts == []

    asyncio.run(run())


def test_refresh_after_write_invalidates_triple_count_even_without_recompute(monkeypatch):
    """Triple-count invalidation is independent of recompute_stats.

    An attribute update / rewrite that skips Explorer type-stats still changes
    the graph's triple cardinality, so list_kgs must not keep a stale stored
    count.
    """

    async def run():
        triple_counts = []
        recomputed = []
        monkeypatch.setattr(pipeline_mod.NLQueryPipeline, "invalidate_cache", lambda graph: None)
        monkeypatch.setattr(pipeline_mod, "get_embedding_service", lambda: None)
        monkeypatch.setattr(
            explore_mod, "schedule_recompute",
            lambda neptune, tenant_id, kg_name: recomputed.append(kg_name),
        )

        async def fake_invalidate_triple_count(client, tenant_id, name):
            triple_counts.append((tenant_id, name))

        monkeypatch.setattr(
            "infona_client.api.routes.knowledge_graphs.invalidate_triple_count",
            fake_invalidate_triple_count,
        )
        await refresh_after_write(
            AsyncMock(),
            tenant_id="t",
            kg_name="k",
            affected_types=set(),
            recompute_stats=False,
        )
        assert triple_counts == [("t", "k")]
        assert recomputed == []

    asyncio.run(run())


def test_refresh_after_write_triple_count_invalidate_is_best_effort(monkeypatch):
    """A triple-count invalidation failure must not fail the write path."""

    async def run():
        recomputed = []
        monkeypatch.setattr(pipeline_mod.NLQueryPipeline, "invalidate_cache", lambda graph: None)
        monkeypatch.setattr(pipeline_mod, "get_embedding_service", lambda: None)
        monkeypatch.setattr(
            explore_mod, "schedule_recompute",
            lambda neptune, tenant_id, kg_name: recomputed.append(kg_name),
        )

        async def boom(*_a, **_k):
            raise RuntimeError("metadata graph down")

        monkeypatch.setattr(
            "infona_client.api.routes.knowledge_graphs.invalidate_triple_count",
            boom,
        )
        await refresh_after_write(AsyncMock(), tenant_id="t", kg_name="k")
        # recompute still ran after the failed invalidate
        assert recomputed == ["k"]

    asyncio.run(run())


# --- delete_facts: batching, counting, provenance tombstone (ADR 0007) ---------


def test_delete_facts_removes_every_concrete_triple_and_counts_exactly():
    """Concrete-triple deletes remove exactly what they name, with no COUNT query.

    The count is now derived from the rows the store actually removed rather
    than a best-effort ``SELECT (COUNT(*))``: each cleared literal reports its
    Entity property AND its ADR 0013 datatype ``Assertion``, i.e. 2 per triple.
    Pinned as an exact number so a future change to what a removal touches has
    to be deliberate.
    """

    async def run():
        store = MemoryGraphStore()
        neptune = AsyncMock()
        triples = []
        for i in range(1200):
            uri = entity_uri("Widget", f"w{i}")
            triples.append((uri, PRICE_ATTR, f"v{i}"))
        await insert_facts(None, GRAPH, triples, store=store)

        removed = await delete_facts(neptune, GRAPH, triples=triples, store=store)

        assert removed == 2400  # 1200 properties + 1200 Assertion rows
        neptune.update.assert_not_awaited()
        neptune.query.assert_not_awaited()  # exact count, no COUNT query
        assert all(row["props"] == {} for row in store.snapshot_entities())

    asyncio.run(run())


def test_delete_facts_predicate_scoped_clear_removes_the_whole_predicate():
    """An object=None triple is a predicate-scoped clear: every value goes.

    The SPARQL shape (``VALUES (?s ?p)`` + a COUNT round-trip) is gone; what
    matters is that naming (subject, predicate) with no object removes that
    attribute entirely, for both an ``attrs/`` literal and an ``onto/``
    relationship.
    """

    async def run():
        store = MemoryGraphStore()
        subject = entity_uri("Widget", "w1")
        target = entity_uri("Widget", "w2")
        await insert_facts(
            None,
            GRAPH,
            [
                (subject, RDF_TYPE, WIDGET_TYPE),
                (subject, PRICE_ATTR, "10"),
                (target, RDF_TYPE, WIDGET_TYPE),
                (subject, RELATED_PRED, target),
            ],
            store=store,
        )
        assert store.rel_count(tenant_id="t", kg="k") == 1

        removed = await delete_facts(
            None,
            GRAPH,
            triples=[(subject, PRICE_ATTR, None), (subject, RELATED_PRED, None)],
            store=store,
        )

        assert removed > 0
        assert _props(store, subject)["props"] == {}
        assert store.rel_count(tenant_id="t", kg="k") == 0
        # The predicate-scoped clear is scoped to its subject: the peer survives.
        assert _props(store, target)["id"] == target

    asyncio.run(run())


def test_delete_facts_whole_subject_removes_the_entity_and_its_edges():
    """``subjects=`` removes the entity and every relationship incident to it."""

    async def run():
        store = MemoryGraphStore()
        e1 = entity_uri("Widget", "w1")
        e2 = entity_uri("Widget", "w2")
        await insert_facts(
            None,
            GRAPH,
            [
                (e1, RDF_TYPE, WIDGET_TYPE),
                (e1, PRICE_ATTR, "10"),
                (e2, RDF_TYPE, WIDGET_TYPE),
                (e2, RELATED_PRED, e1),  # INCOMING edge on e1
            ],
            store=store,
        )

        removed = await delete_facts(None, GRAPH, subjects=[e1], store=store)

        assert removed > 0
        assert {row["id"] for row in store.snapshot_entities()} == {e2}
        # The incoming edge went with the node — no dangling endpoint.
        assert store.rel_count(tenant_id="t", kg="k") == 0

    asyncio.run(run())


def test_delete_facts_noop_on_empty():
    async def run():
        store = MemoryGraphStore()
        neptune = AsyncMock()
        removed = await delete_facts(neptune, GRAPH, store=store)
        assert removed == 0
        neptune.update.assert_not_awaited()

    asyncio.run(run())


def test_delete_facts_writes_tombstone_when_provenance_enabled(monkeypatch):
    """With INFONA_PROVENANCE_ENABLED=1 a tombstone lands as a companion record."""

    async def run():
        monkeypatch.setenv("INFONA_PROVENANCE_ENABLED", "1")
        store = MemoryGraphStore()
        subj = entity_uri("Widget", "w1")
        await insert_facts(
            None, GRAPH, [(subj, RDF_TYPE, WIDGET_TYPE)], store=store
        )

        await delete_facts(
            None, GRAPH, subjects=[subj], reason="unit-delete", store=store
        )

        tombstones = _prov_events(store, "tombstone")
        assert tombstones, "a tombstone must be recorded for a removal"
        assert any(
            p["subject_id"] == subj and p["reason"] == "unit-delete"
            for p in tombstones
        )
        # Companion records are not instance data: the entity is gone, and the
        # tombstone did not resurrect it as a node.
        assert store.snapshot_entities() == []

    asyncio.run(run())


def test_delete_facts_no_tombstone_when_provenance_disabled(monkeypatch):
    async def run():
        monkeypatch.delenv("INFONA_PROVENANCE_ENABLED", raising=False)
        monkeypatch.delenv("INFONA_PROVENANCE_STORE_ALWAYS", raising=False)
        store = MemoryGraphStore()
        subj = entity_uri("Widget", "w1")
        await insert_facts(None, GRAPH, [(subj, RDF_TYPE, WIDGET_TYPE)], store=store)
        await delete_facts(None, GRAPH, subjects=[subj], reason="x", store=store)
        assert store.snapshot_prov() == []

    asyncio.run(run())


# --- rewrite_subject: two-direction move + rewrite provenance -------------------


def test_rewrite_subject_moves_both_directions_and_records_event(monkeypatch):
    """An ER merge re-keys the node — one event, not delete+insert.

    The SPARQL version asserted the two-direction ``DELETE { <old> ?p ?o }`` /
    ``INSERT { <new> ?p ?o }`` pair. The property-graph equivalent is stronger
    because it is checked against the graph rather than the statement: the
    entity carries the NEW id with its properties intact, and an edge that
    POINTED AT the old id now points at the new one (the "incoming" half, which
    is the direction a naive delete+insert loses).
    """

    async def run():
        monkeypatch.setenv("INFONA_PROVENANCE_ENABLED", "1")
        store = MemoryGraphStore()
        old = entity_uri("Widget", "old")
        new = entity_uri("Widget", "new")
        peer = entity_uri("Widget", "peer")
        await insert_facts(
            None,
            GRAPH,
            [
                (old, RDF_TYPE, WIDGET_TYPE),
                (old, PRICE_ATTR, "10"),
                (peer, RDF_TYPE, WIDGET_TYPE),
                (peer, RELATED_PRED, old),  # incoming edge, must follow the merge
            ],
            store=store,
        )

        await rewrite_subject(None, GRAPH, old, new, reason="er-merge", store=store)

        ids = {row["id"] for row in store.snapshot_entities()}
        assert new in ids and old not in ids
        assert _props(store, new)["props"] == {"price": "10"}
        assert [r["end_id"] for r in store.snapshot_rels()] == [new]

        rewrites = _prov_events(store, "rewrite")
        assert rewrites, "an ER merge must record a rewrite event"
        assert rewrites[0]["old_id"] == old and rewrites[0]["new_id"] == new
        # A rewrite is NOT a delete: no tombstone is recorded for the loser.
        assert _prov_events(store, "tombstone") == []

    asyncio.run(run())


def test_rewrite_subject_noop_on_same_uri():
    async def run():
        store = MemoryGraphStore()
        neptune = AsyncMock()
        await rewrite_subject(neptune, GRAPH, "same", "same", store=store)
        neptune.update.assert_not_awaited()
        assert store.snapshot_entities() == []

    asyncio.run(run())


# --- refresh_after_write: derived-index eviction / re-key (ADR 0007) ------------


def _quiet_housekeeping(monkeypatch):
    """Silence the ontology-cache / embed / stats steps so a refresh test isolates
    the derived-index maintenance."""
    monkeypatch.setattr(pipeline_mod.NLQueryPipeline, "invalidate_cache", lambda g: None)
    monkeypatch.setattr(pipeline_mod, "get_embedding_service", lambda: None)
    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)


def test_refresh_after_write_evicts_deleted_subjects(monkeypatch):
    async def run():
        _quiet_housekeeping(monkeypatch)
        index = InMemorySpatioTemporalIndex()
        register_spatiotemporal_index(index)
        try:
            await index.upsert(
                SpatioTemporalFact(entity_uri="E1", tenant_id="t", kg_name="k", lon=1.0, lat=2.0)
            )
            await index.upsert(
                SpatioTemporalFact(entity_uri="E2", tenant_id="t", kg_name="k", lon=3.0, lat=4.0)
            )
            await refresh_after_write(
                AsyncMock(), tenant_id="t", kg_name="k", deleted_subjects=["E1"]
            )
            hits = await index.query_bbox("t", -180, -90, 180, 90, kg_name="k")
            assert {h.entity_uri for h in hits} == {"E2"}
        finally:
            reset_spatiotemporal_index()

    asyncio.run(run())


def test_refresh_after_write_rekeys_rewritten_subjects(monkeypatch):
    async def run():
        _quiet_housekeeping(monkeypatch)
        index = InMemorySpatioTemporalIndex()
        register_spatiotemporal_index(index)
        try:
            await index.upsert(
                SpatioTemporalFact(entity_uri="loser", tenant_id="t", kg_name="k", lon=1.0, lat=2.0)
            )
            await refresh_after_write(
                AsyncMock(), tenant_id="t", kg_name="k", rewritten_subjects={"loser": "canon"},
            )
            hits = await index.query_bbox("t", -180, -90, 180, 90, kg_name="k")
            assert {h.entity_uri for h in hits} == {"canon"}
        finally:
            reset_spatiotemporal_index()

    asyncio.run(run())


def test_refresh_after_write_evicts_deleted_subjects_from_semantic_index(monkeypatch):
    """The ONTA-173 half of the _deindex_secondary seam: deletes and rewrites
    evict semantic docs exactly like spatiotemporal rows (rewrites evict the
    stale key; re-indexing the new key is the hook/reconciler's job). Gated:
    with the env gate off the semantic backend must not even be touched."""
    from infona_client.semantic.memory import InMemorySemanticIndex
    from infona_client.semantic.protocol import SemanticChunk
    from infona_client.semantic.registry import (
        register_semantic_index,
        reset_semantic_index,
    )

    async def run():
        _quiet_housekeeping(monkeypatch)
        monkeypatch.setenv("INFONA_SEMANTIC_INDEX_ENABLED", "true")
        sem = InMemorySemanticIndex()
        register_semantic_index(sem)
        try:
            for uri in ("E1", "E2", "loser"):
                await sem.upsert_chunks(
                    [
                        SemanticChunk(
                            tenant_id="t",
                            kg_name="k",
                            entity_uri=uri,
                            attr="desc",
                            chunk_ix=0,
                            chunk_text=f"text of {uri}",
                            content_hash=f"h-{uri}",
                        )
                    ]
                )
            await refresh_after_write(
                AsyncMock(),
                tenant_id="t",
                kg_name="k",
                deleted_subjects=["E1"],
                rewritten_subjects={"loser": "canon"},
            )
            remaining = {e for e, _a, _h, _at in await sem.list_docs("t", kg_name="k")}
            assert remaining == {"E2"}

            # Gate off: the backend must not be touched at all.
            monkeypatch.delenv("INFONA_SEMANTIC_INDEX_ENABLED", raising=False)

            class Exploding:
                def __getattr__(self, name):
                    raise AssertionError("semantic backend touched with gate off")

            register_semantic_index(Exploding())
            await refresh_after_write(
                AsyncMock(), tenant_id="t", kg_name="k", deleted_subjects=["E2"]
            )
        finally:
            reset_semantic_index()

    asyncio.run(run())


def test_refresh_after_write_deindex_is_noop_without_removals(monkeypatch):
    """No deleted/rewritten subjects → the derived-index step touches nothing."""

    async def run():
        _quiet_housekeeping(monkeypatch)

        class BoomIndex(InMemorySpatioTemporalIndex):
            async def delete(self, *a, **k):  # pragma: no cover - must not be called
                raise AssertionError("delete must not run without deleted_subjects")

            async def rekey(self, *a, **k):  # pragma: no cover
                raise AssertionError("rekey must not run without rewritten_subjects")

        register_spatiotemporal_index(BoomIndex())
        try:
            # Should not raise — the deindex step early-returns.
            await refresh_after_write(AsyncMock(), tenant_id="t", kg_name="k")
        finally:
            reset_spatiotemporal_index()

    asyncio.run(run())

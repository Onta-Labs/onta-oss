"""Hermetic GraphStore ER blocking + citation fold + rewrite_subject ER path.

Covers the remaining write-path Neo4j pieces:

1. ER ``index_triples`` land as literal Assertions on the store path
2. ``GraphStoreBlocker`` / dual-path ``SparqlBlocker`` find candidates by block key
3. attr_meta companions fold onto Assertion provenance (not just AttrCitation)
4. ``rewrite_subject`` rebinds ER block keys / signals onto the survivor
"""

from __future__ import annotations

import asyncio

import pytest

from infona_client.graph.assertion_model import property_uri
from infona_client.graph.facts import Fact, classify_triple, triples_to_facts
from infona_client.graph.iri import ATTR_META_NS, ER_NS, IRI_BASE
from infona_client.graph.kg_writer import insert_facts, rewrite_subject
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.store import (
    configure_graph_store,
    reset_graph_store_for_tests,
)
from infona_client.resolver.er.blocking import (
    GraphStoreBlocker,
    SparqlBlocker,
    generate_block_keys,
)
from infona_client.resolver.er.normalize import DefaultNormalizer
from infona_client.resolver.er.types import EntitySignals


@pytest.fixture
def store(monkeypatch):
    reset_graph_store_for_tests()
    s = MemoryGraphStore()
    configure_graph_store(s)
    yield s
    asyncio.run(s.close())
    reset_graph_store_for_tests()
    monkeypatch.delenv("INFONA_GRAPH_BACKEND", raising=False)


def _graph(tenant: str = "demo-tenant", kg: str = "guests") -> str:
    return f"{IRI_BASE}/graphs/{tenant}/kg/{kg}"


def _person(pid: str) -> str:
    return entity_uri("Person", pid)


# ---------------------------------------------------------------------------
# classify_triple / index_triples → Facts
# ---------------------------------------------------------------------------


def test_classify_triple_maps_er_block_key_and_signals():
    sid = _person("alice")
    assert classify_triple(sid, f"{ER_NS}blockKey", "email_local:alice") is not None
    f = classify_triple(sid, f"{ER_NS}blockKey", "email_local:alice")
    assert f.kind == "literal" and f.key == "blockKey"
    assert f.value == "email_local:alice"

    # Angle-bracket form (index_triples output)
    f2 = classify_triple(
        f"<{sid}>", f"<{ER_NS}erSignal_email>", "alice@example.com"
    )
    assert f2 is not None
    assert f2.subject_id == sid
    assert f2.key == "erSignal_email"
    assert f2.value == "alice@example.com"


def test_index_triples_roundtrip_via_triples_to_facts():
    N = DefaultNormalizer()
    norm = N.normalize(
        EntitySignals(name="John Smith", email="john@x.com", phone="+12125550001")
    )
    keys = generate_block_keys(norm)
    sid = _person("john")
    triples = SparqlBlocker.index_triples(sid, norm, keys)
    facts = triples_to_facts(triples)
    leaves = {f.key for f in facts}
    assert "blockKey" in leaves
    assert "erSignal_email" in leaves
    assert "erSignal_phone_e164" in leaves or "erSignal_name" in leaves
    # No angle brackets on subject_id after classify
    assert all(not f.subject_id.startswith("<") for f in facts)


# ---------------------------------------------------------------------------
# GraphStoreBlocker candidate lookup
# ---------------------------------------------------------------------------


def test_graph_store_blocker_finds_candidates_by_block_key(store, monkeypatch):
    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neo4j")

    async def run():
        N = DefaultNormalizer()
        alice_n = N.normalize(
            EntitySignals(name="Alice Smith", email="alice@x.com", phone="+12125551111")
        )
        bob_n = N.normalize(
            EntitySignals(name="Bob Jones", email="bob@y.com", phone="+12125552222")
        )
        alice = _person("alice")
        bob = _person("bob")
        type_uri = f"{IRI_BASE}/types/Person"
        g = _graph()

        for sid, norm in ((alice, alice_n), (bob, bob_n)):
            keys = generate_block_keys(norm)
            triples = [
                (
                    sid,
                    "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
                    type_uri,
                ),
                *SparqlBlocker.index_triples(sid, norm, keys),
            ]
            await insert_facts(None, g, triples, store=store)

        blocker = GraphStoreBlocker(store)
        # Lookup with Alice's keys only → should return Alice, not Bob
        keys_a = generate_block_keys(alice_n)
        found = await blocker.candidates_with_signals(g, type_uri, keys_a)
        assert alice in found
        assert bob not in found
        assert found[alice].email == "alice@x.com" or found[alice].email_local

        # Dual-path SparqlBlocker with store configured
        dual = SparqlBlocker(neptune=None, store=store)
        found2 = await dual.candidates_with_signals(g, type_uri, keys_a)
        assert alice in found2

        # all_entities_with_signals returns both
        all_ents = await blocker.all_entities_with_signals(g, type_uri)
        assert set(all_ents) == {alice, bob}

    asyncio.run(run())


def test_sparql_blocker_dual_path_via_env_store(store, monkeypatch):
    """When INFONA_GRAPH_BACKEND=neo4j, SparqlBlocker without explicit store
    still uses the process GraphStore for lookups."""
    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neo4j")

    async def run():
        N = DefaultNormalizer()
        norm = N.normalize(
            EntitySignals(name="Carol Lee", email="carol@z.com", phone="+12125553333")
        )
        sid = _person("carol")
        type_uri = f"{IRI_BASE}/types/Person"
        g = _graph()
        keys = generate_block_keys(norm)
        triples = [
            (sid, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", type_uri),
            *SparqlBlocker.index_triples(sid, norm, keys),
        ]
        await insert_facts(None, g, triples, store=store)

        dual = SparqlBlocker(neptune=None)  # store from resolve_optional_graph_store
        found = await dual.candidates_with_signals(g, type_uri, keys)
        assert sid in found

    asyncio.run(run())


# ---------------------------------------------------------------------------
# AttrCitation fold onto Assertion provenance
# ---------------------------------------------------------------------------


def test_attr_meta_companions_fold_onto_assertion_provenance(store):
    async def run():
        sid = _person("cite-fold")
        g = _graph()
        triples = [
            (
                sid,
                "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
                f"{IRI_BASE}/types/Person",
            ),
            (sid, f"{IRI_BASE}/types/Person/attrs/email", "fold@example.com"),
            (
                sid,
                f"{ATTR_META_NS}Person/email/source_url",
                "https://example.com/fold",
            ),
            (
                sid,
                f"{ATTR_META_NS}Person/email/provenance",
                "enrichment",
            ),
            (
                sid,
                f"{ATTR_META_NS}Person/email/verified_at",
                "2026-08-09T12:00:00Z",
            ),
        ]
        await insert_facts(None, g, triples, store=store)

        # Assertion SoT carries the citation fields
        email_rows = [
            a
            for a in store.snapshot_assertions()
            if a["subject_id"] == sid
            and a["property_id"] == property_uri("email")
        ]
        assert len(email_rows) == 1
        row = email_rows[0]
        assert row["literal_value"] == "fold@example.com"
        assert row["source_url"] == "https://example.com/fold"
        assert row["provenance"] == "enrichment"
        assert row["verified_at"] == "2026-08-09T12:00:00Z"

        # Residual AttrCitation still written (secondary)
        cites = [c for c in store.snapshot_citations() if c["attr"] == "email"]
        assert cites
        assert cites[0]["source_url"] == "https://example.com/fold"

    asyncio.run(run())


def test_structured_fact_provenance_not_clobbered_by_empty_citation(store):
    """Fact IR source_url wins when attr_meta is absent."""

    async def run():
        sid = _person("fact-prov")
        await insert_facts(
            None,
            _graph(),
            facts=[
                Fact(subject_id=sid, kind="type", key="Person"),
                Fact(
                    subject_id=sid,
                    kind="literal",
                    key="email",
                    value="p@x.com",
                    source_url="https://primary.example",
                    verified_at="2026-01-01T00:00:00Z",
                ),
            ],
            store=store,
        )
        rows = [
            a
            for a in store.snapshot_assertions()
            if a["subject_id"] == sid and a["property_id"] == property_uri("email")
        ]
        assert rows[0]["source_url"] == "https://primary.example"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# rewrite_subject rebinds ER index Assertions
# ---------------------------------------------------------------------------


def test_rewrite_subject_rebinds_er_block_keys_and_signals(store):
    async def run():
        N = DefaultNormalizer()
        norm = N.normalize(
            EntitySignals(name="Dana West", email="dana@x.com", phone="+12125554444")
        )
        loser = _person("dana-dup")
        survivor = _person("dana-canonical")
        type_uri = f"{IRI_BASE}/types/Person"
        g = _graph()
        keys = generate_block_keys(norm)

        # Seed loser with type + ER index
        await insert_facts(
            None,
            g,
            [
                (loser, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", type_uri),
                *SparqlBlocker.index_triples(loser, norm, keys),
            ],
            store=store,
        )
        # Survivor shell (merge-into-existing)
        await insert_facts(
            None,
            g,
            facts=[Fact(subject_id=survivor, kind="type", key="Person")],
            store=store,
        )

        await rewrite_subject(None, g, loser, survivor, store=store, reason="er-merge")

        assert ("demo-tenant", "guests", loser) not in store._entities
        assert ("demo-tenant", "guests", survivor) in store._entities

        # Assertions re-keyed onto survivor
        for a in store.snapshot_assertions():
            assert a["subject_id"] != loser
            assert a.get("object_id") != loser

        blocker = GraphStoreBlocker(store)
        found = await blocker.candidates_with_signals(g, type_uri, keys)
        assert survivor in found
        assert loser not in found
        assert found[survivor].email == "dana@x.com" or found[survivor].email_local

    asyncio.run(run())


def test_rewrite_subject_free_id_rebinds_assertion_subject(store):
    """Free new_id path also moves Assertion.subject_id (Memory path)."""

    async def run():
        old = _person("free-old")
        new = _person("free-new")
        g = _graph()
        await insert_facts(
            None,
            g,
            facts=[
                Fact(subject_id=old, kind="type", key="Person"),
                Fact(subject_id=old, kind="literal", key="blockKey", value="email_local:x"),
                Fact(
                    subject_id=old,
                    kind="literal",
                    key="erSignal_email",
                    value="x@y.com",
                ),
            ],
            store=store,
        )
        await rewrite_subject(None, g, old, new, store=store)
        assert ("demo-tenant", "guests", old) not in store._entities
        assert ("demo-tenant", "guests", new) in store._entities
        assert all(a["subject_id"] == new for a in store.snapshot_assertions())

    asyncio.run(run())

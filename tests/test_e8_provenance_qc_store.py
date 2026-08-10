"""E8 — provenance companions + structural QC on GraphStore (Memory hermetic).

Covers:
* ProvEvent assert / tombstone / rewrite with fact_hash + ABOUT subject_id
* INFONA_PROVENANCE_ENABLED and INFONA_PROVENANCE_STORE_ALWAYS gates
* AttrCitation write helper (enrichment source_url style) + attr_meta parse
* Store-path check_invariants (missing primary_type, orphan rel, unscoped rel)
* ADR 0013 dual-write skew (INSTANCE_OF without type Assertion)
* SPARQL QC path still importable / unchanged catalogue
"""

from __future__ import annotations

import asyncio

import pytest

from infona_client.graph.facts import Fact
from infona_client.graph.iri import ATTR_META_NS, IRI_BASE
from infona_client.graph.kg_writer import (
    _provenance_enabled,
    delete_facts,
    insert_facts,
    rewrite_subject,
)
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_queries import entity_uri, type_uri
from infona_client.graph import pg_ops
from infona_client.graph.scope import GraphScope
from infona_client.qc import (
    INVARIANTS,
    STORE_INVARIANT_INSTANCE_OF_NO_TYPE_ASSERTION,
    STORE_INVARIANT_MISSING_PRIMARY_TYPE,
    STORE_INVARIANT_ORPHAN_REL_TARGET,
    STORE_INVARIANT_REL_MISSING_SCOPE,
    check_assertion_cache_skew,
    check_invariants,
)
from infona_client.qc.invariants_store import check_store_invariants


@pytest.fixture
def store():
    s = MemoryGraphStore()
    yield s
    asyncio.run(s.close())


def _graph(tenant: str = "demo-tenant", kg: str = "bookstore") -> str:
    return f"{IRI_BASE}/graphs/{tenant}/kg/{kg}"


def test_provenance_gate_store_always(monkeypatch):
    monkeypatch.delenv("INFONA_PROVENANCE_ENABLED", raising=False)
    monkeypatch.delenv("INFONA_PROVENANCE_STORE_ALWAYS", raising=False)
    assert _provenance_enabled() is False
    assert _provenance_enabled(store_path=True) is False

    monkeypatch.setenv("INFONA_PROVENANCE_STORE_ALWAYS", "1")
    assert _provenance_enabled() is False
    assert _provenance_enabled(store_path=True) is True

    monkeypatch.setenv("INFONA_PROVENANCE_ENABLED", "1")
    assert _provenance_enabled(store_path=False) is True


def test_assert_tombstone_rewrite_prov_events(store, monkeypatch):
    monkeypatch.setenv("INFONA_PROVENANCE_ENABLED", "1")

    async def run():
        sid = entity_uri("Person", "e8-alice")
        org = entity_uri("Organization", "e8-acme")
        g = _graph()

        await insert_facts(
            None,
            g,
            facts=[
                Fact(subject_id=sid, kind="type", key="Person"),
                Fact(
                    subject_id=sid,
                    kind="literal",
                    key="email",
                    value="a@x.com",
                    source="csv",
                ),
                Fact(subject_id=org, kind="type", key="Organization"),
                Fact(subject_id=sid, kind="rel", key="works_at", value=org),
            ],
            store=store,
        )
        asserts = [p for p in store.snapshot_prov() if p["event_type"] == "assert"]
        assert asserts, "assert ProvEvents expected when provenance enabled"
        assert all(p.get("fact_hash") for p in asserts)
        assert any(p["attr"] == "email" and p["object_repr"] == "a@x.com" for p in asserts)
        assert any(p["attr"] == "works_at" and p["object_repr"] == org for p in asserts)

        # Concrete tombstone
        await delete_facts(
            None,
            g,
            triples=[(sid, f"{IRI_BASE}/types/Person/attrs/email", "a@x.com")],
            store=store,
        )
        tombs = [p for p in store.snapshot_prov() if p["event_type"] == "tombstone"]
        assert any(p.get("attr") == "email" for p in tombs)

        # Rewrite event
        neu = entity_uri("Person", "e8-alice-canonical")
        await rewrite_subject(None, g, sid, neu, store=store)
        rewrites = [p for p in store.snapshot_prov() if p["event_type"] == "rewrite"]
        assert rewrites
        assert rewrites[-1]["old_id"] == sid
        assert rewrites[-1]["new_id"] == neu
        assert rewrites[-1]["subject_id"] == neu
        assert rewrites[-1].get("fact_hash")

        # Whole-subject tombstone must not resurrect the entity
        await delete_facts(None, g, subjects=[neu], store=store)
        assert ("demo-tenant", "bookstore", neu) not in store._entities
        subject_tombs = [
            p
            for p in store.snapshot_prov()
            if p["event_type"] == "tombstone" and p["subject_id"] == neu and not p.get("attr")
        ]
        assert subject_tombs

    asyncio.run(run())


def test_store_always_writes_assert_without_global_flag(store, monkeypatch):
    monkeypatch.delenv("INFONA_PROVENANCE_ENABLED", raising=False)
    monkeypatch.setenv("INFONA_PROVENANCE_STORE_ALWAYS", "1")

    async def run():
        sid = entity_uri("Person", "always-on")
        await insert_facts(
            None,
            _graph(),
            facts=[
                Fact(subject_id=sid, kind="type", key="Person"),
                Fact(subject_id=sid, kind="literal", key="email", value="z@x.com"),
            ],
            store=store,
        )
        assert any(p["event_type"] == "assert" for p in store.snapshot_prov())

    asyncio.run(run())


def test_attr_citation_helper_and_attr_meta_parse(store):
    async def run():
        sid = entity_uri("Person", "cite1")
        session = store.session(GraphScope.for_instance("demo-tenant", "bookstore"))
        await pg_ops.merge_entity(session, sid, primary_type="Person")
        await pg_ops.upsert_attr_citation(
            session,
            sid,
            "email",
            source_url="https://example.com/a",
            provenance="wikidata",
            verified_at="2026-08-01T00:00:00Z",
            value="a@x.com",
        )
        cites = store.snapshot_citations()
        assert len(cites) == 1
        c = cites[0]
        assert c["entity_id"] == sid
        assert c["attr"] == "email"
        assert c["source_url"] == "https://example.com/a"
        assert c["provenance"] == "wikidata"
        assert c["value_hash"]  # derived from value

        # attr_meta triples → citations via insert_facts store path
        sid2 = entity_uri("Person", "cite2")
        triples = [
            (
                sid2,
                f"http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
                f"{IRI_BASE}/types/Person",
            ),
            (
                sid2,
                f"{ATTR_META_NS}Person/phone/source_url",
                "https://example.com/phone",
            ),
            (
                sid2,
                f"{ATTR_META_NS}Person/phone/provenance",
                "enrichment",
            ),
            (
                sid2,
                f"{ATTR_META_NS}Person/phone/verified_at",
                "2026-08-02T00:00:00Z^^http://www.w3.org/2001/XMLSchema#dateTime",
            ),
        ]
        await insert_facts(None, _graph(), triples, store=store)
        phones = [x for x in store.snapshot_citations() if x["attr"] == "phone"]
        assert phones
        assert phones[0]["source_url"] == "https://example.com/phone"
        assert phones[0]["provenance"] == "enrichment"
        assert phones[0]["verified_at"] == "2026-08-02T00:00:00Z"

        # parse helper unit
        specs = pg_ops.parse_attr_meta_citations(triples)
        assert len(specs) == 1
        assert specs[0].attr == "phone"

    asyncio.run(run())


def test_qc_missing_primary_type_and_orphan(store):
    async def run():
        session = store.session(GraphScope.for_instance("demo-tenant", "bookstore"))
        # Clean graph → no structural violations
        clean = await check_invariants(store=store, tenant_id="demo-tenant", kg_name="bookstore")
        assert clean == []

        # Entity without primary_type
        bare = entity_uri("Person", "bare")
        await pg_ops.merge_entity(session, bare)  # no primary_type
        vs = await check_store_invariants(session)
        assert any(v.invariant == STORE_INVARIANT_MISSING_PRIMARY_TYPE for v in vs)
        assert any(bare in v.detail for v in vs)

        # Inject orphan rel (bypass merge_rel which auto-creates endpoints)
        from infona_client.graph.memory_store import _RelRow

        store._rels[("demo-tenant", "bookstore", bare, "urn:missing", "WORKS_AT")] = _RelRow(
            tenant_id="demo-tenant",
            kg="bookstore",
            start_id=bare,
            end_id="urn:missing",
            rel_type="WORKS_AT",
            attr="works_at",
        )
        vs2 = await check_invariants(
            session=session,
            include={STORE_INVARIANT_ORPHAN_REL_TARGET},
        )
        assert any(v.invariant == STORE_INVARIANT_ORPHAN_REL_TARGET for v in vs2)

        # Unscoped rel
        store._rels[("", "", bare, bare, "SELF")] = _RelRow(
            tenant_id="",
            kg="",
            start_id=bare,
            end_id=bare,
            rel_type="SELF",
            attr="self",
        )
        vs3 = await check_invariants(
            store=store,
            tenant_id="demo-tenant",
            kg_name="bookstore",
            include={STORE_INVARIANT_REL_MISSING_SCOPE},
        )
        assert any(v.invariant == STORE_INVARIANT_REL_MISSING_SCOPE for v in vs3)

    asyncio.run(run())


def test_sparql_qc_path_still_present():
    """Do not delete SPARQL QC — catalogue and runner shape unchanged."""
    assert len(INVARIANTS) == 5
    assert INVARIANTS[0].name == "node_edge_on_attrs_predicate"
    # Runner still works with a fake SPARQL client.
    class _Empty:
        async def query(self, sparql: str) -> dict:
            return {"results": {"bindings": []}}

    async def run():
        vs = await check_invariants(_Empty())
        assert vs == []

    asyncio.run(run())


def test_assertion_cache_skew_clean_after_insert_facts(store):
    """Proper dual-write via insert_facts → no INSTANCE_OF / Assertion skew."""

    async def run():
        sid = entity_uri("Person", "skew-clean")
        await insert_facts(
            None,
            _graph(),
            facts=[
                Fact(subject_id=sid, kind="type", key="Person"),
                Fact(subject_id=sid, kind="literal", key="email", value="c@x.com"),
            ],
            store=store,
        )
        session = store.session(GraphScope.for_instance("demo-tenant", "bookstore"))
        vs = await check_assertion_cache_skew(session)
        assert vs == []
        # Also via the full store-path suite (include filter).
        vs2 = await check_store_invariants(
            session,
            include={STORE_INVARIANT_INSTANCE_OF_NO_TYPE_ASSERTION},
        )
        assert vs2 == []

    asyncio.run(run())


def test_assertion_cache_skew_planted_instance_of(store):
    """Planted INSTANCE_OF without type Assertion is reported as dual-write skew."""

    async def run():
        session = store.session(GraphScope.for_instance("demo-tenant", "bookstore"))
        sid = entity_uri("Person", "skew-orphan-io")
        class_id = type_uri("Person")

        # Entity + Class + derived INSTANCE_OF only — no type Assertion (SoT gap).
        await pg_ops.merge_entity(session, sid, primary_type="Person")
        from infona_client.graph.rdf_model import merge_class

        await merge_class(session, class_id, name="Person")
        await session.write_instance_of(sid, class_id)

        # Standalone skew check.
        vs = await check_assertion_cache_skew(session)
        assert any(
            v.invariant == STORE_INVARIANT_INSTANCE_OF_NO_TYPE_ASSERTION for v in vs
        )
        assert any(sid in v.detail and class_id in v.detail for v in vs)
        assert all(v.severity == "error" for v in vs)

        # Wired into E8 store-path invariants (default include set).
        vs_all = await check_store_invariants(session)
        assert any(
            v.invariant == STORE_INVARIANT_INSTANCE_OF_NO_TYPE_ASSERTION
            for v in vs_all
        )

        # Via dual-backend check_invariants(store=...).
        vs3 = await check_invariants(
            store=store,
            tenant_id="demo-tenant",
            kg_name="bookstore",
            include={STORE_INVARIANT_INSTANCE_OF_NO_TYPE_ASSERTION},
        )
        assert len(vs3) >= 1
        assert vs3[0].binding.get("entity_id") == sid
        assert vs3[0].binding.get("class_id") == class_id

        # Repair: write the type Assertion (+ dual-write is fine); skew clears.
        from infona_client.graph.rdf_model import AssertionFact, assert_fact

        await assert_fact(
            session,
            AssertionFact(subject_id=sid, kind="type", value="Person"),
            dual_write_cache=True,
        )
        vs_fixed = await check_assertion_cache_skew(session)
        assert vs_fixed == []

    asyncio.run(run())


def test_prov_fact_hash_stable():
    a = pg_ops.prov_fact_hash("s", "email", "x", "csv")
    b = pg_ops.prov_fact_hash("s", "email", "x", "csv")
    assert a == b and len(a) == 40
    assert a != pg_ops.prov_fact_hash("s", "email", "y", "csv")

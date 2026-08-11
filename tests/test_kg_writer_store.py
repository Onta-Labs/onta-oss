"""E3 — property-graph write path via Memory GraphStore (no live Neo4j).

Pins insert_facts / delete_facts / rewrite_subject when an explicit store is
passed, plus Fact IR mapping from legacy triples.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from infona_client.graph.facts import (
    RESERVED_ENTITY_PROPERTY_KEYS,
    Fact,
    classify_triple,
    sanitize_prop_key,
    sanitize_rel_type,
    triples_to_facts,
)
from infona_client.graph.iri import IRI_BASE
from infona_client.graph.kg_writer import (
    delete_facts,
    graph_backend,
    insert_facts,
    refresh_after_write,
    rewrite_subject,
)
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.scope import GraphScope, GraphScopeError
from infona_client.graph.store import configure_graph_store, reset_graph_store_for_tests


@pytest.fixture
def store():
    s = MemoryGraphStore()
    yield s
    asyncio.run(s.close())


def _graph(tenant: str = "demo-tenant", kg: str = "bookstore") -> str:
    return f"{IRI_BASE}/graphs/{tenant}/kg/{kg}"


def test_graph_backend_default_neo4j(monkeypatch):
    monkeypatch.delenv("INFONA_GRAPH_BACKEND", raising=False)
    assert graph_backend() == "neo4j"


def test_sanitize_prop_key_and_reserved():
    assert sanitize_prop_key("email") == "email"
    assert sanitize_prop_key("city/town") == "city_town"
    assert sanitize_prop_key("2fa") == "T_2fa"
    with pytest.raises(GraphScopeError, match="reserved"):
        sanitize_prop_key("tenant_id")
    assert "id" in RESERVED_ENTITY_PROPERTY_KEYS


def test_sanitize_rel_type():
    assert sanitize_rel_type("works_at") == "WORKS_AT"
    assert sanitize_rel_type("city/town") == "CITY_TOWN"


def test_triples_to_facts_type_literal_rel():
    person = entity_uri("Person", "alice")
    org = entity_uri("Organization", "acme")
    triples = [
        (person, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/Person"),
        (person, "http://www.w3.org/2000/01/rdf-schema#label", "Alice"),
        (person, f"{IRI_BASE}/types/Person/attrs/email", "a@example.com"),
        (person, f"{IRI_BASE}/onto/works_at", org),
        (person, f"{IRI_BASE}/onto/source", "csv"),
    ]
    facts = triples_to_facts(triples)
    kinds = {(f.kind, f.key) for f in facts}
    assert ("type", "Person") in kinds
    assert ("literal", "name") in kinds
    assert ("literal", "email") in kinds
    assert ("rel", "works_at") in kinds
    assert ("literal", "source") in kinds


def test_insert_facts_store_entity_literal_rel(store):
    async def run():
        person = entity_uri("Person", "alice")
        org = entity_uri("Organization", "acme")
        graph = _graph()
        triples = [
            (person, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/Person"),
            (person, "http://www.w3.org/2000/01/rdf-schema#label", "Alice"),
            (person, f"{IRI_BASE}/types/Person/attrs/email", "a@example.com"),
            (person, f"{IRI_BASE}/types/Person/attrs/tag", "vip"),
            (person, f"{IRI_BASE}/types/Person/attrs/tag", "founding"),
            (org, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/Organization"),
            (person, f"{IRI_BASE}/onto/works_at", org),
        ]
        await insert_facts(None, graph, triples, store=store)

        assert store.entity_count(tenant_id="demo-tenant", kg="bookstore") == 2
        row = store._entities[("demo-tenant", "bookstore", person)]
        assert row.primary_type == "Person"
        assert row.name == "Alice"
        assert "Person" in row.labels
        assert row.props.get("email") == "a@example.com"
        # multi-value list union
        tags = row.props.get("tag")
        assert tags == ["vip", "founding"] or set(tags) == {"vip", "founding"}

        assert store.rel_count(tenant_id="demo-tenant", kg="bookstore") == 1
        rels = store.snapshot_rels()
        assert rels[0]["rel_type"] == "WORKS_AT"
        assert rels[0]["attr"] == "works_at"
        assert rels[0]["start_id"] == person
        assert rels[0]["end_id"] == org

    asyncio.run(run())


def test_insert_facts_structured_fact_ir(store):
    async def run():
        sid = entity_uri("Book", "b1")
        facts = [
            Fact(subject_id=sid, kind="type", key="Book"),
            Fact(subject_id=sid, kind="literal", key="name", value="Dune"),
            Fact(subject_id=sid, kind="literal", key="pages", value=412),
        ]
        await insert_facts(None, _graph(), facts=facts, store=store)
        row = store._entities[("demo-tenant", "bookstore", sid)]
        assert row.primary_type == "Book"
        assert row.name == "Dune"
        assert row.props["pages"] == 412

    asyncio.run(run())


def test_insert_facts_fail_closed_missing_id(store):
    async def run():
        with pytest.raises(GraphScopeError, match="subject_id|id"):
            Fact(subject_id="", kind="literal", key="x", value=1)

    asyncio.run(run())


def test_delete_facts_subject(store):
    async def run():
        sid = entity_uri("Person", "bob")
        await insert_facts(
            None,
            _graph(),
            facts=[
                Fact(subject_id=sid, kind="type", key="Person"),
                Fact(subject_id=sid, kind="literal", key="name", value="Bob"),
            ],
            store=store,
        )
        assert store.entity_count() == 1
        n = await delete_facts(None, _graph(), subjects=[sid], store=store)
        assert n >= 1
        assert store.entity_count() == 0

    asyncio.run(run())


def test_delete_facts_predicate_scoped_literal(store):
    async def run():
        sid = entity_uri("Person", "carol")
        await insert_facts(
            None,
            _graph(),
            facts=[
                Fact(subject_id=sid, kind="type", key="Person"),
                Fact(subject_id=sid, kind="literal", key="email", value="c@x.com"),
            ],
            store=store,
        )
        pred = f"{IRI_BASE}/types/Person/attrs/email"
        await delete_facts(
            None,
            _graph(),
            triples=[(sid, pred, None)],
            store=store,
        )
        row = store._entities[("demo-tenant", "bookstore", sid)]
        assert "email" not in row.props

    asyncio.run(run())


def test_rewrite_subject_free_id_rekeys_entity_and_rel(store):
    """P0: free new_id — re-key node; outbound rel endpoints move with id."""

    async def run():
        old = entity_uri("Person", "dup")
        new = entity_uri("Person", "canonical")
        org = entity_uri("Organization", "co")
        await insert_facts(
            None,
            _graph(),
            facts=[
                Fact(subject_id=old, kind="type", key="Person"),
                Fact(subject_id=old, kind="literal", key="name", value="Dup"),
                Fact(subject_id=org, kind="type", key="Organization"),
                Fact(subject_id=old, kind="rel", key="works_at", value=org),
            ],
            store=store,
        )
        await rewrite_subject(None, _graph(), old, new, store=store)
        assert ("demo-tenant", "bookstore", old) not in store._entities
        assert ("demo-tenant", "bookstore", new) in store._entities
        assert store._entities[("demo-tenant", "bookstore", new)].name == "Dup"
        rels = store.snapshot_rels()
        assert len(rels) == 1
        assert rels[0]["start_id"] == new
        assert rels[0]["end_id"] == org

    asyncio.run(run())


def test_rewrite_subject_into_existing_rebinds_rels_and_prov(store, monkeypatch):
    """P0: new_id already exists — rebind outbound+inbound onto survivor, drop loser."""

    monkeypatch.setenv("INFONA_PROVENANCE_ENABLED", "1")

    async def run():
        loser = entity_uri("Person", "loser")
        survivor = entity_uri("Person", "survivor")
        org = entity_uri("Organization", "co")
        city = entity_uri("City", "sf")
        await insert_facts(
            None,
            _graph(),
            facts=[
                Fact(subject_id=loser, kind="type", key="Person"),
                Fact(subject_id=loser, kind="literal", key="name", value="Loser"),
                Fact(subject_id=loser, kind="literal", key="email", value="l@x.com"),
                Fact(subject_id=survivor, kind="type", key="Person"),
                Fact(subject_id=survivor, kind="literal", key="name", value="Survivor"),
                Fact(subject_id=org, kind="type", key="Organization"),
                Fact(subject_id=city, kind="type", key="City"),
                # Outbound from loser, inbound to loser.
                Fact(subject_id=loser, kind="rel", key="works_at", value=org),
                Fact(subject_id=city, kind="rel", key="hosts", value=loser),
            ],
            store=store,
        )
        # Pre-existing edge on survivor (same B4 key as rebind) must stay single.
        await insert_facts(
            None,
            _graph(),
            facts=[
                Fact(subject_id=survivor, kind="rel", key="works_at", value=org),
            ],
            store=store,
        )

        await rewrite_subject(None, _graph(), loser, survivor, store=store)

        assert ("demo-tenant", "bookstore", loser) not in store._entities
        assert ("demo-tenant", "bookstore", survivor) in store._entities
        row = store._entities[("demo-tenant", "bookstore", survivor)]
        assert row.name == "Survivor"  # survivor wins on conflict
        # Loser's unique prop filled onto survivor.
        assert row.props.get("email") == "l@x.com"

        rels = store.snapshot_rels()
        starts_ends = {(r["start_id"], r["end_id"], r["attr"]) for r in rels}
        assert (survivor, org, "works_at") in starts_ends
        assert (city, survivor, "hosts") in starts_ends
        # No leftover endpoints on loser.
        assert all(r["start_id"] != loser and r["end_id"] != loser for r in rels)
        # Exactly one WORKS_AT after merge-into-existing (B4 key dedupe).
        works = [r for r in rels if r["attr"] == "works_at"]
        assert len(works) == 1

        # ProvEvent subject_ids re-keyed off the loser.
        for p in store.snapshot_prov():
            assert p["subject_id"] != loser

    asyncio.run(run())


def test_scope_isolation_across_kgs(store):
    async def run():
        sid = entity_uri("Person", "x")
        await insert_facts(
            None,
            _graph(kg="kg_a"),
            facts=[Fact(subject_id=sid, kind="type", key="Person")],
            store=store,
        )
        await insert_facts(
            None,
            _graph(kg="kg_b"),
            facts=[Fact(subject_id=sid, kind="type", key="Person")],
            store=store,
        )
        assert store.entity_count(tenant_id="demo-tenant", kg="kg_a") == 1
        assert store.entity_count(tenant_id="demo-tenant", kg="kg_b") == 1
        await delete_facts(None, _graph(kg="kg_a"), subjects=[sid], store=store)
        assert store.entity_count(tenant_id="demo-tenant", kg="kg_a") == 0
        assert store.entity_count(tenant_id="demo-tenant", kg="kg_b") == 1

    asyncio.run(run())


def test_neptune_path_untouched_without_store():
    """Without store/session and default backend, still hits Neptune client."""

    async def run():
        from unittest.mock import AsyncMock

        neptune = AsyncMock()
        person = entity_uri("Person", "z")
        triples = [
            (person, f"{IRI_BASE}/types/Person/attrs/email", "z@x.com"),
        ]
        await insert_facts(neptune, _graph(), triples)
        assert neptune.update.await_count >= 1
        sparql = neptune.update.await_args_list[0].args[0]
        assert "INSERT" in sparql.upper() or "DATA" in sparql.upper()

    asyncio.run(run())


def test_refresh_after_write_store_skips_neptune_registration(store, monkeypatch):
    async def run():
        calls = []

        async def boom(*a, **k):
            calls.append("ensure")
            raise AssertionError("should not register on store path")

        monkeypatch.setattr(
            "infona_client.graph.kg_writer.ensure_kg_registered", boom
        )
        await refresh_after_write(
            None,
            tenant_id="demo-tenant",
            kg_name="bookstore",
            affected_types=set(),
            store=store,
        )
        assert calls == []

    asyncio.run(run())


def test_provenance_assert_when_enabled(store, monkeypatch):
    monkeypatch.setenv("INFONA_PROVENANCE_ENABLED", "1")

    async def run():
        sid = entity_uri("Person", "prov1")
        await insert_facts(
            None,
            _graph(),
            facts=[
                Fact(subject_id=sid, kind="type", key="Person"),
                Fact(subject_id=sid, kind="literal", key="email", value="p@x.com"),
            ],
            store=store,
        )
        prov = store.snapshot_prov()
        assert any(p["event_type"] == "assert" for p in prov)

    asyncio.run(run())


def test_entity_uri_used_as_id(store):
    """Entity.id is exactly entity_uri() string (B5) — no second sanitizer."""

    async def run():
        sid = entity_uri("Person", "raw id!")
        await insert_facts(
            None,
            _graph(),
            facts=[Fact(subject_id=sid, kind="type", key="Person")],
            store=store,
        )
        assert sid in {e["id"] for e in store.snapshot_entities()}
        assert sid == f"{IRI_BASE}/entities/Person/raw_id_"

    asyncio.run(run())


def test_session_instance_graph_scope_mismatch():
    """P2: explicit session + instance_graph must agree on tenant/kg."""
    from infona_client.graph.kg_writer import _resolve_graph_session

    mem = MemoryGraphStore()
    session = mem.session(GraphScope.for_instance("demo-tenant", "bookstore"))
    with pytest.raises(GraphScopeError, match="does not match"):
        _resolve_graph_session(
            session=session,
            instance_graph=_graph(tenant="other-tenant", kg="bookstore"),
        )
    # Matching scopes are fine.
    gs = _resolve_graph_session(session=session, instance_graph=_graph())
    assert gs is session
    asyncio.run(mem.close())


def test_neo4j_rewrite_free_id_and_existing_mock():
    """P0 hermetic Neo4j mock: free-id SETs id; existing rebinds then DETACH DELETEs."""
    from infona_client.graph.neo4j_store import Neo4jGraphSession
    from infona_client.graph.store import GraphRecord

    class _FakeNeo4jStore:
        def __init__(self) -> None:
            self.writes: list[tuple[str, dict]] = []
            self.reads: list[tuple[str, dict]] = []
            # State toggled by test phases.
            self.old_exists = True
            self.new_exists = False
            self.rel_rows: list[dict] = []

        async def _run(self, cypher, bound, writing=False, database=None):
            params = dict(bound)
            if writing:
                self.writes.append((cypher, params))
                return [GraphRecord(data={"id": params.get("new_id") or params.get("id")})]
            self.reads.append((cypher, params))
            # Existence probes look for id: $old_id / id: $new_id single MATCH RETURN.
            compact = " ".join(cypher.split())
            if "id: $old_id" in cypher and "RETURN e.id" in compact:
                if self.old_exists:
                    return [GraphRecord(data={"id": params["old_id"]})]
                return []
            if "id: $new_id" in cypher and "RETURN e.id" in compact and "UNION" not in cypher:
                if self.new_exists:
                    return [GraphRecord(data={"id": params["new_id"]})]
                return []
            if "UNION" in cypher and "type(r)" in cypher:
                return [GraphRecord(data=dict(r)) for r in self.rel_rows]
            return []

    async def free_id_path():
        fake = _FakeNeo4jStore()
        fake.old_exists = True
        fake.new_exists = False
        session = Neo4jGraphSession(fake, GraphScope.for_instance("t", "k"))  # type: ignore[arg-type]
        await session.write_rewrite_entity_id("urn:old", "urn:new")
        write_text = "\n".join(c for c, _ in fake.writes)
        assert "SET old.id = $new_id" in write_text
        assert "DETACH DELETE" not in write_text
        # Prov subject_id rebind always runs on free path too.
        assert any("ProvEvent" in c and "p.subject_id" in c for c, _ in fake.writes)

    async def existing_path():
        fake = _FakeNeo4jStore()
        fake.old_exists = True
        fake.new_exists = True
        fake.rel_rows = [
            {
                "start_id": "urn:old",
                "end_id": "urn:org",
                "rel_type": "WORKS_AT",
                "attr": "works_at",
            },
            {
                "start_id": "urn:city",
                "end_id": "urn:old",
                "rel_type": "HOSTS",
                "attr": "hosts",
            },
        ]
        session = Neo4jGraphSession(fake, GraphScope.for_instance("t", "k"))  # type: ignore[arg-type]
        await session.write_rewrite_entity_id("urn:old", "urn:new")
        write_text = "\n---\n".join(c for c, _ in fake.writes)
        # Outbound + inbound re-merged onto survivor (MERGE edges).
        merge_writes = [c for c, p in fake.writes if "MERGE (a)-[r:" in c]
        assert len(merge_writes) >= 2
        # Params of merges must use survivor id not loser as endpoint of rebind.
        merge_params = [p for c, p in fake.writes if "MERGE (a)-[r:" in c]
        starts_ends = {(p["start_id"], p["end_id"]) for p in merge_params}
        assert ("urn:new", "urn:org") in starts_ends
        assert ("urn:city", "urn:new") in starts_ends
        # ABOUT rebind + DETACH DELETE loser (after rebind).
        assert "ABOUT" in write_text
        assert "DETACH DELETE old" in write_text
        # DETACH must come after MERGE rebinds.
        first_merge = next(i for i, (c, _) in enumerate(fake.writes) if "MERGE (a)-[r:" in c)
        detach_i = next(i for i, (c, _) in enumerate(fake.writes) if "DETACH DELETE old" in c)
        assert first_merge < detach_i

    asyncio.run(free_id_path())
    asyncio.run(existing_path())

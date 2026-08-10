"""ADR 0013 RDF-semantic / assertion-centric model — hermetic tests.

No Neo4j process required. Pins:
* Assertion write with provenance → read via helpers
* Subclass: Person ⊑ Agent → entities_of_type(Agent, include_subclasses=True)
* Subproperty closure
* Multi-value: two Assertions same s,p different o
* Entity id is full entity_uri IRI
* Default Neptune path still works without store
"""

from __future__ import annotations

import asyncio

import pytest

from infona_client.graph.assertion_model import (
    property_uri,
    type_membership_property_id,
)
from infona_client.graph.facts import Fact
from infona_client.graph.iri import IRI_BASE
from infona_client.graph.kg_writer import graph_backend, insert_facts
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_queries import entity_uri, type_uri
from infona_client.graph.rdf_model import (
    AssertionFact,
    assert_fact,
    class_iri,
    make_assertion_id,
    mint_assertion_id,
    set_subclass_of,
    set_subproperty_of,
)
from infona_client.graph.rdfs_helpers import (
    descendants_of,
    session_assertions_for_subject,
    session_entities_of_type,
    session_literal_values,
    session_object_values,
    subclass_closure,
    subproperty_closure,
)
from infona_client.graph.schema_bootstrap import SCHEMA_STATEMENTS, bootstrap_schema_statements
from infona_client.graph.scope import GraphScope


@pytest.fixture
def store():
    s = MemoryGraphStore()
    yield s
    asyncio.run(s.close())


def _session(store: MemoryGraphStore, tenant: str = "demo-tenant", kg: str = "bookstore"):
    return store.session(GraphScope.for_instance(tenant, kg))


def _graph(tenant: str = "demo-tenant", kg: str = "bookstore") -> str:
    return f"{IRI_BASE}/graphs/{tenant}/kg/{kg}"


def test_schema_bootstrap_includes_assertion_labels():
    names = {n for n, _ in bootstrap_schema_statements()}
    assert "entity_tenant_kg_id_unique" in names
    assert "class_tenant_kg_id_unique" in names
    assert "property_tenant_kg_id_unique" in names
    assert "assertion_tenant_kg_id_unique" in names
    assert "assertion_subject_lookup" in names
    # Constraint cypher mentions labels
    body = "\n".join(c for _, c in SCHEMA_STATEMENTS)
    assert ":Class" in body and ":Property" in body and ":Assertion" in body


def test_mint_assertion_id_deterministic():
    a = mint_assertion_id("s1", "p1", "o1")
    b = make_assertion_id("s1", "p1", "o1")
    assert a == b
    assert len(a) >= 32
    # source discriminator changes id
    c = make_assertion_id("s1", "p1", "o1", source_discriminator="http://src")
    assert c != a


def test_entity_id_is_full_entity_uri_iri():
    eid = entity_uri("Person", "alice")
    assert eid.startswith(f"{IRI_BASE}/entities/Person/")
    assert "alice" in eid
    assert class_iri("Person") == type_uri("Person")
    assert type_uri("Person").startswith(f"{IRI_BASE}/types/")


def test_write_assertion_with_provenance_and_read(store):
    async def run():
        session = _session(store)
        person = entity_uri("Person", "alice")
        prop = property_uri("email")
        fact = AssertionFact(
            subject_id=person,
            kind="literal",
            property_leaf="email",
            property_id=prop,
            value="a@example.com",
            source_url="https://example.com/source",
            verified_at="2026-08-01T00:00:00Z",
            run_id="run-1",
            confidence=0.91,
            provenance="enrichment",
        )
        out = await assert_fact(session, fact, dual_write_cache=True)
        assert out["assertion_id"]
        assert out["source_url"] == "https://example.com/source"

        rows = await session_assertions_for_subject(session, person)
        assert len(rows) == 1
        assert rows[0]["literal_value"] == "a@example.com"
        assert rows[0]["source_url"] == "https://example.com/source"
        assert rows[0]["run_id"] == "run-1"
        assert rows[0]["confidence"] == 0.91
        assert rows[0]["property_id"] == prop

        lits = await session_literal_values(session, person, prop)
        assert lits == ["a@example.com"]

        # Entity cache dual-write
        ent = store._entities[("demo-tenant", "bookstore", person)]
        assert ent.props.get("email") == "a@example.com"
        # Full IRI identity
        assert ent.id == person
        assert person.startswith("http")

    asyncio.run(run())


def test_subclass_entities_of_type(store):
    async def run():
        session = _session(store)
        agent_id = type_uri("Agent")
        person_id = type_uri("Person")
        # Person ⊑ Agent
        await set_subclass_of(session, person_id, agent_id)

        alice = entity_uri("Person", "alice")
        await assert_fact(
            session,
            AssertionFact(
                subject_id=alice,
                kind="type",
                value="Person",
            ),
            dual_write_cache=True,
        )

        # Closure includes Person under Agent
        closure = await subclass_closure(session, agent_id)
        assert agent_id in closure
        assert person_id in closure

        # entities_of_type(Agent, include_subclasses=True) includes alice
        ents = await session_entities_of_type(
            session, agent_id, include_subclasses=True
        )
        assert alice in ents

        # Without subclasses, only exact Agent membership
        ents_exact = await session_entities_of_type(
            session, agent_id, include_subclasses=False
        )
        assert alice not in ents_exact

        # entity id is full entity_uri
        assert alice == entity_uri("Person", "alice")

    asyncio.run(run())


def test_subproperty_closure(store):
    async def run():
        session = _session(store)
        parent = property_uri("contact")
        child = property_uri("email")
        await set_subproperty_of(
            session, child, parent, child_kind="datatype", parent_kind="datatype"
        )
        ids = await subproperty_closure(session, parent)
        assert parent in ids
        assert child in ids

    asyncio.run(run())


def test_multi_value_two_assertions_same_sp(store):
    async def run():
        session = _session(store)
        person = entity_uri("Person", "alice")
        prop = property_uri("email")
        await assert_fact(
            session,
            AssertionFact(
                subject_id=person,
                kind="literal",
                property_id=prop,
                property_leaf="email",
                value="a@example.com",
            ),
            dual_write_cache=True,
        )
        await assert_fact(
            session,
            AssertionFact(
                subject_id=person,
                kind="literal",
                property_id=prop,
                property_leaf="email",
                value="a@work.com",
            ),
            dual_write_cache=True,
        )
        rows = await session_assertions_for_subject(session, person, prop_id=prop)
        assert len(rows) == 2
        values = {r["literal_value"] for r in rows}
        assert values == {"a@example.com", "a@work.com"}
        # Distinct assertion ids
        assert rows[0]["assertion_id"] != rows[1]["assertion_id"]

    asyncio.run(run())


def test_object_assertion_and_shortcut_rel(store):
    async def run():
        session = _session(store)
        person = entity_uri("Person", "alice")
        org = entity_uri("Organization", "acme")
        prop = property_uri("works_at")
        await assert_fact(
            session,
            AssertionFact(
                subject_id=person,
                kind="object",
                property_id=prop,
                property_leaf="works_at",
                value=org,
            ),
            dual_write_cache=True,
        )
        objs = await session_object_values(session, person, prop)
        assert objs == [org]
        assert store.rel_count(tenant_id="demo-tenant", kg="bookstore") == 1

    asyncio.run(run())


def test_insert_facts_writes_assertions(store):
    async def run():
        person = entity_uri("Person", "bob")
        org = entity_uri("Organization", "acme")
        triples = [
            (
                person,
                "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
                f"{IRI_BASE}/types/Person",
            ),
            (person, f"{IRI_BASE}/types/Person/attrs/email", "b@example.com"),
            (person, f"{IRI_BASE}/onto/works_at", org),
            (
                org,
                "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
                f"{IRI_BASE}/types/Organization",
            ),
        ]
        await insert_facts(None, _graph(), triples, store=store)
        assert store.assertion_count(tenant_id="demo-tenant", kg="bookstore") >= 3
        # type + email + works_at (+ org type)
        session = _session(store)
        rows = await session_assertions_for_subject(session, person)
        kinds_or_props = {r["property_id"] for r in rows}
        assert property_uri("email") in kinds_or_props
        assert property_uri("works_at") in kinds_or_props
        assert type_membership_property_id() in kinds_or_props
        # entity still full IRI as store key
        assert ("demo-tenant", "bookstore", person) in store._entities
        assert store._entities[("demo-tenant", "bookstore", person)].id == person

    asyncio.run(run())


def test_insert_facts_structured_fact_ir_assertions(store):
    async def run():
        sid = entity_uri("Book", "b1")
        facts = [
            Fact(subject_id=sid, kind="type", key="Book"),
            Fact(subject_id=sid, kind="literal", key="pages", value=412),
        ]
        await insert_facts(None, _graph(), facts=facts, store=store)
        assert store.assertion_count(tenant_id="demo-tenant", kg="bookstore") >= 2
        session = _session(store)
        lits = await session_literal_values(session, sid, property_uri("pages"))
        assert 412 in lits or "412" in {str(x) for x in lits}

    asyncio.run(run())


def test_insert_facts_memory_assertion_per_literal_rel_type(store):
    """M5: insert_facts Memory writes one Assertion for each literal/rel/type Fact."""
    async def run():
        person = entity_uri("Person", "carol")
        org = entity_uri("Organization", "acme")
        facts = [
            Fact(subject_id=person, kind="type", key="Person"),
            Fact(subject_id=person, kind="literal", key="email", value="c@example.com"),
            Fact(subject_id=person, kind="rel", key="works_at", value=org),
            Fact(subject_id=org, kind="type", key="Organization"),
        ]
        await insert_facts(None, _graph(), facts=facts, store=store)
        session = _session(store)
        person_rows = await session_assertions_for_subject(session, person)
        props = {r["property_id"] for r in person_rows}
        assert type_membership_property_id() in props
        assert property_uri("email") in props
        assert property_uri("works_at") in props
        assert len(person_rows) == 3
        # INSTANCE_OF dual-written for type
        ents = await session_entities_of_type(
            session, type_uri("Person"), include_subclasses=False
        )
        assert person in ents
        # object Assertion present
        objs = await session_object_values(session, person, property_uri("works_at"))
        assert objs == [org]

    asyncio.run(run())


def test_entities_of_type_via_instance_of_with_subclass(store):
    """M5: entities_of_type reads INSTANCE_OF + Class SUBCLASS_OF, not primary_type only."""
    async def run():
        session = _session(store)
        agent_id = type_uri("Agent")
        person_id = type_uri("Person")
        await set_subclass_of(session, person_id, agent_id)

        alice = entity_uri("Person", "alice-io")
        await insert_facts(
            None,
            _graph(),
            facts=[Fact(subject_id=alice, kind="type", key="Person")],
            store=store,
        )
        # Semantic helper path
        ents = await session_entities_of_type(
            session, agent_id, include_subclasses=True
        )
        assert alice in ents
        # Template path ($type_names expanded to parent∪child leaves)
        rows = await session.execute_template(
            "entities_of_type",
            {
                "type_names": ["Agent", "Person"],
                "after_id": None,
                "limit": 50,
            },
        )
        ids = {r.get("id") for r in rows}
        assert alice in ids
        # Exact Agent only — no direct INSTANCE_OF Agent
        exact = await session.execute_template(
            "entities_of_type",
            {"type_names": ["Agent"], "after_id": None, "limit": 50},
        )
        assert alice not in {r.get("id") for r in exact}

    asyncio.run(run())


def test_fact_full_provenance_lands_on_assertion(store):
    """M5: Fact source_url / verified_at / run_id / confidence land on Assertion."""
    async def run():
        sid = entity_uri("Person", "prov-person")
        facts = [
            Fact(
                subject_id=sid,
                kind="type",
                key="Person",
            ),
            Fact(
                subject_id=sid,
                kind="literal",
                key="email",
                value="p@example.com",
                source_url="https://example.com/cite",
                verified_at="2026-08-09T12:00:00Z",
                run_id="run-m5",
                confidence=0.87,
            ),
        ]
        # source alias also maps
        assert facts[1].source == "https://example.com/cite"
        await insert_facts(None, _graph(), facts=facts, store=store)
        session = _session(store)
        rows = await session_assertions_for_subject(
            session, sid, prop_id=property_uri("email")
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["literal_value"] == "p@example.com"
        assert row["source_url"] == "https://example.com/cite"
        assert row["verified_at"] == "2026-08-09T12:00:00Z"
        assert row["run_id"] == "run-m5"
        assert row["confidence"] == 0.87

    asyncio.run(run())


def test_apply_facts_requires_write_assertion(store):
    """M1: soft-skip of write_assertion is gone — missing surface fails closed."""
    from infona_client.graph.pg_ops import apply_facts
    from infona_client.graph.scope import GraphScopeError

    class _NoAssertSession:
        scope = GraphScope.for_instance("demo-tenant", "bookstore")

        async def write_merge_entity(self, **kwargs):
            return []

    async def run():
        with pytest.raises(GraphScopeError, match="write_assertion"):
            await apply_facts(
                _NoAssertSession(),  # type: ignore[arg-type]
                [Fact(subject_id=entity_uri("Person", "x"), kind="type", key="Person")],
            )

    asyncio.run(run())


def test_delete_literals_removes_assertions_not_swallowed(store):
    """M4: delete_literals removes matching Assertions; no silent except:pass."""
    from infona_client.graph import pg_ops

    async def run():
        sid = entity_uri("Person", "del-lit")
        await insert_facts(
            None,
            _graph(),
            facts=[
                Fact(subject_id=sid, kind="type", key="Person"),
                Fact(subject_id=sid, kind="literal", key="email", value="x@y.z"),
            ],
            store=store,
        )
        session = _session(store)
        before = await session_assertions_for_subject(
            session, sid, prop_id=property_uri("email")
        )
        assert len(before) == 1
        n = await pg_ops.delete_literals(session, sid, ["email"])
        assert n >= 1
        after = await session_assertions_for_subject(
            session, sid, prop_id=property_uri("email")
        )
        assert after == []

    asyncio.run(run())


def test_instance_of_evicted_when_type_assertion_deleted(store):
    """Derived INSTANCE_OF is removed when the backing type Assertion is deleted."""
    from infona_client.graph.rdf_model import delete_assertions_for_subject

    async def run():
        sid = entity_uri("Person", "evict-me")
        await insert_facts(
            None,
            _graph(),
            facts=[
                Fact(subject_id=sid, kind="type", key="Person"),
                Fact(subject_id=sid, kind="type", key="Employee"),
            ],
            store=store,
        )
        session = _session(store)
        person_iri = type_uri("Person")
        employee_iri = type_uri("Employee")
        ios = store._instance_of.get(("demo-tenant", "bookstore", sid), set())
        assert person_iri in ios and employee_iri in ios

        # Delete only the Person type Assertion → INSTANCE_OF Person goes away.
        n = await delete_assertions_for_subject(
            session,
            sid,
            property_id=type_membership_property_id(),
            object_key=person_iri,
        )
        assert n == 1
        ios2 = store._instance_of.get(("demo-tenant", "bookstore", sid), set())
        assert person_iri not in ios2
        assert employee_iri in ios2

        # Delete remaining type Assertions → no INSTANCE_OF left.
        n2 = await delete_assertions_for_subject(
            session, sid, property_id=type_membership_property_id()
        )
        assert n2 >= 1
        ios3 = store._instance_of.get(("demo-tenant", "bookstore", sid), set())
        assert not ios3

    asyncio.run(run())


def test_neptune_default_path_without_store(monkeypatch):
    """Default backend remains neptune; insert_facts without store does not require GraphStore."""
    monkeypatch.delenv("COGRAPH_GRAPH_BACKEND", raising=False)
    assert graph_backend() == "neptune"

    class _FakeNeptune:
        def __init__(self):
            self.updates: list[str] = []

        async def update(self, sparql: str):
            self.updates.append(sparql)

        async def query(self, sparql: str):
            return {"results": {"bindings": []}}

    async def run():
        nep = _FakeNeptune()
        person = entity_uri("Person", "x")
        triples = [
            (
                person,
                "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
                f"{IRI_BASE}/types/Person",
            ),
        ]
        # No store → SPARQL path (may no-op companions); must not raise GraphConfigError
        await insert_facts(nep, _graph(), triples)
        assert len(nep.updates) >= 1
        assert "INSERT" in nep.updates[0].upper() or "insert" in nep.updates[0].lower() or "DATA" in nep.updates[0]

    asyncio.run(run())


def test_descendants_of_helper():
    child_to_parent = {"Person": "Agent", "Employee": "Person"}
    assert descendants_of("Agent", child_to_parent) == ["Agent", "Person", "Employee"] or set(
        descendants_of("Agent", child_to_parent)
    ) == {"Agent", "Person", "Employee"}


def test_assertion_memory_golden_helpers_still_importable():
    """Golden harness AssertionMemoryStore helpers remain available."""
    from infona_client.graph.assertion_memory import AssertionMemoryStore
    from infona_client.graph.golden_fixture import build_mini_people
    from infona_client.graph.rdfs_helpers import entities_of_type, count_entities_of_type

    fx = build_mini_people()
    rows = entities_of_type(
        fx.store, "Agent", tenant_id=fx.tenant_id, kg=fx.kg, include_subclasses=True
    )
    assert any(r["entity_id"] for r in rows)
    cnt = count_entities_of_type(
        fx.store, "Person", tenant_id=fx.tenant_id, kg=fx.kg, include_subclasses=True
    )
    assert cnt[0]["count"] >= 1

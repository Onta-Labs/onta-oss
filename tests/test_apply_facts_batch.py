"""Batched apply_facts: Memory round-trip + Neo4j UNWIND query count."""

from __future__ import annotations

import asyncio
import math

import pytest

from infona_client.graph.assertion_model import property_uri
from infona_client.graph.facts import Fact
from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.neo4j_store_batch import Neo4jBatchMixin, _UNWIND_CHUNK
from infona_client.graph.ontology_queries import entity_uri, type_uri
from infona_client.graph.pg_ops import apply_facts
from infona_client.graph.iri import IRI_BASE
from infona_client.graph.rdfs_helpers import (
    session_entities_of_type,
    session_literal_values,
    session_object_values,
)
from infona_client.graph.scope import GraphScope, GraphScopeError
from infona_client.graph.store import (
    GraphRecord,
    assert_cypher_is_scoped,
    merge_scope_params,
)


@pytest.fixture
def store():
    s = MemoryGraphStore()
    yield s
    asyncio.run(s.close())


def _session(store: MemoryGraphStore, tenant: str = "demo-tenant", kg: str = "firms"):
    return store.session(GraphScope.for_instance(tenant, kg))


def _graph(tenant: str = "demo-tenant", kg: str = "firms") -> str:
    return f"{IRI_BASE}/graphs/{tenant}/kg/{kg}"


def _firm_facts(n: int, *, with_rel: bool = False) -> list[Fact]:
    facts: list[Fact] = []
    hq = entity_uri("Place", "boston")
    if with_rel:
        facts.append(Fact(subject_id=hq, kind="type", key="Place"))
        facts.append(
            Fact(subject_id=hq, kind="literal", key="place_name", value="Boston")
        )
    for i in range(n):
        sid = entity_uri("Firm", f"firm-{i}")
        facts.append(Fact(subject_id=sid, kind="type", key="Firm"))
        facts.append(
            Fact(subject_id=sid, kind="literal", key="firm_name", value=f"Firm {i}")
        )
        facts.append(
            Fact(subject_id=sid, kind="literal", key="hq_geo", value="Boston, MA")
        )
        facts.append(
            Fact(subject_id=sid, kind="literal", key="name", value=f"Firm {i}")
        )
        if with_rel:
            facts.append(
                Fact(subject_id=sid, kind="rel", key="headquartered_in", value=hq)
            )
    return facts


def test_apply_facts_batch_round_trip_memory(store):
    async def run():
        session = _session(store)
        facts = _firm_facts(8, with_rel=True)
        n = await apply_facts(session, facts)
        assert n == len(facts)
        alice = entity_uri("Firm", "firm-0")
        assert await session_literal_values(
            session, alice, property_uri("firm_name")
        ) == ["Firm 0"]
        row = store._entities[("demo-tenant", "firms", alice)]
        assert row.props.get("firm_name") == "Firm 0"
        assert row.name == "Firm 0"
        assert "firm_name" in row.props
        assert "name" not in row.props
        assert "id" not in row.props
        assert "Firm" in row.labels
        types = await session_entities_of_type(session, type_uri("Firm"))
        assert len(types) == 8
        hq = entity_uri("Place", "boston")
        assert await session_object_values(
            session, alice, property_uri("headquartered_in")
        ) == [hq]

    asyncio.run(run())


def test_insert_facts_uses_batch_on_memory(store):
    async def run():
        session = _session(store)
        facts = _firm_facts(10)
        await insert_facts(
            None, _graph(), facts=facts, store=store, session=session
        )
        types = await session_entities_of_type(session, type_uri("Firm"))
        assert len(types) == 10

    asyncio.run(run())


def test_batch_matches_sequential_assert_fact(store):
    sequential = MemoryGraphStore()

    async def run():
        facts = _firm_facts(5, with_rel=True)
        batched = _session(store)
        seq = sequential.session(GraphScope.for_instance("demo-tenant", "firms"))
        seq.write_fact_batch = None  # type: ignore[method-assign]
        await apply_facts(batched, facts)
        await apply_facts(seq, facts)
        for i in range(5):
            sid = entity_uri("Firm", f"firm-{i}")
            b = store._entities[("demo-tenant", "firms", sid)]
            s = sequential._entities[("demo-tenant", "firms", sid)]
            assert b.props.get("firm_name") == s.props.get("firm_name")
            assert b.name == s.name
            assert b.labels == s.labels
            assert b.primary_type == s.primary_type

    asyncio.run(run())
    asyncio.run(sequential.close())


class _CaptureSession(Neo4jBatchMixin):
    def __init__(self):
        self.writes: list[tuple[str, dict]] = []
        self._scope = GraphScope.for_instance("demo-tenant", "firms")

    async def execute_write(self, cypher: str, params=None):
        assert_cypher_is_scoped(cypher, privileged=self._scope.privileged)
        bound = merge_scope_params(self._scope, params, for_write=True)
        assert bound["tenant_id"] == "demo-tenant"
        assert bound["kg"] == "firms"
        self.writes.append((cypher, bound))
        n = 0
        if "rows" in bound and isinstance(bound["rows"], list):
            n = len(bound["rows"])
        elif "ids" in bound and isinstance(bound["ids"], list):
            n = len(bound["ids"])
        return [GraphRecord(data={"n": n})]


def test_neo4j_batch_is_unwind_not_per_fact():
    async def run():
        session = _CaptureSession()
        facts = _firm_facts(80, with_rel=True)
        n = await apply_facts(session, facts)
        assert n == len(facts)
        assert any("UNWIND" in cy for cy, _ in session.writes)
        assert any(":Assertion" in cy for cy, _ in session.writes)
        assert any("SET e += row.props" in cy for cy, _ in session.writes)
        assert any("DELETE old_o" in cy for cy, _ in session.writes)
        props_maps = [
            p["rows"]
            for cy, p in session.writes
            if "SET e += row.props" in cy
        ]
        for rows in props_maps:
            for row in rows:
                assert "name" not in row["props"]
                assert "id" not in row["props"]
        label_writes = [cy for cy, _ in session.writes if "SET e:`Firm`" in cy]
        assert len(label_writes) == 1
        # Round-trips scale with UNWIND chunks, not per fact.
        n_assert = sum(1 for f in facts if True)
        expected_assert_chunks = math.ceil(n_assert / _UNWIND_CHUNK)
        assert len(session.writes) < expected_assert_chunks + 20

    asyncio.run(run())


def test_apply_facts_requires_assertion_surface(store):
    class _Bare:
        pass

    async def run():
        with pytest.raises(GraphScopeError, match="write_assertion"):
            await apply_facts(_Bare(), _firm_facts(1))  # type: ignore[arg-type]

    asyncio.run(run())

"""Batched apply_facts: Memory equivalence + Neo4j UNWIND query count."""

from __future__ import annotations

import asyncio

import pytest

from infona_client.graph.assertion_model import property_uri
from infona_client.graph.facts import Fact
from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.neo4j_store_batch import Neo4jBatchMixin
from infona_client.graph.ontology_queries import entity_uri, type_uri
from infona_client.graph.pg_ops import apply_facts
from infona_client.graph.iri import IRI_BASE
from infona_client.graph.rdfs_helpers import (
    session_assertions_for_subject,
    session_entities_of_type,
    session_literal_values,
)
from infona_client.graph.scope import GraphScope, GraphScopeError


@pytest.fixture
def store():
    s = MemoryGraphStore()
    yield s
    asyncio.run(s.close())


def _session(store: MemoryGraphStore, tenant: str = "demo-tenant", kg: str = "firms"):
    return store.session(GraphScope.for_instance(tenant, kg))


def _graph(tenant: str = "demo-tenant", kg: str = "firms") -> str:
    return f"{IRI_BASE}/graphs/{tenant}/kg/{kg}"


def _firm_facts(n: int) -> list[Fact]:
    facts: list[Fact] = []
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
            Fact(
                subject_id=sid,
                kind="literal",
                key="check_size_min",
                value="500000",
            )
        )
    return facts


def test_apply_facts_batch_round_trip_memory(store):
    async def run():
        session = _session(store)
        facts = _firm_facts(25)
        n = await apply_facts(session, facts)
        assert n == len(facts)
        alice = entity_uri("Firm", "firm-0")
        assert await session_literal_values(
            session, alice, property_uri("firm_name")
        ) == ["Firm 0"]
        assert await session_literal_values(
            session, alice, property_uri("hq_geo")
        ) == ["Boston, MA"]
        types = await session_entities_of_type(session, type_uri("Firm"))
        assert len(types) == 25
        rows = await session_assertions_for_subject(session, alice)
        assert len(rows) >= 3

    asyncio.run(run())


def test_insert_facts_uses_batch_on_memory(store):
    async def run():
        session = _session(store)
        facts = _firm_facts(10)
        graph = _graph()
        await insert_facts(
            None, graph, facts=facts, store=store, session=session
        )
        types = await session_entities_of_type(session, type_uri("Firm"))
        assert len(types) == 10

    asyncio.run(run())


class _CaptureSession(Neo4jBatchMixin):
    def __init__(self):
        self.writes: list[tuple[str, dict]] = []
        self._scope = GraphScope.for_instance("demo-tenant", "firms")

    async def execute_write(self, cypher: str, params=None):
        self.writes.append((cypher, dict(params or {})))
        return []


def test_neo4j_batch_7419_firms_is_constant_query_count():
    """A full InvestorMatch-sized page must not grow Bolt round-trips with row count."""

    async def run():
        session = _CaptureSession()
        facts = _firm_facts(7419)
        n = await apply_facts(session, facts)
        assert n == len(facts)
        assert len(session.writes) < 20

    asyncio.run(run())


def test_neo4j_batch_is_unwind_not_per_fact():
    async def run():
        session = _CaptureSession()
        facts = _firm_facts(80)
        n = await apply_facts(session, facts)
        assert n == len(facts)
        # 80 firms × 4 facts would be 300+ per-fact writes. UNWIND is O(kinds).
        assert len(session.writes) < 20
        assert any("UNWIND" in cy for cy, _ in session.writes)
        assert any(":Assertion" in cy for cy, _ in session.writes)
        assert any("SET e += row.props" in cy for cy, _ in session.writes)
        # One label query for Firm, not 80.
        label_writes = [cy for cy, _ in session.writes if "SET e:`Firm`" in cy]
        assert len(label_writes) == 1

    asyncio.run(run())


def test_apply_facts_requires_assertion_surface(store):
    class _Bare:
        pass

    async def run():
        with pytest.raises(GraphScopeError, match="write_assertion"):
            await apply_facts(_Bare(), _firm_facts(1))  # type: ignore[arg-type]

    asyncio.run(run())

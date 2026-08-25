"""ER merge must retarget ``:ValidityInterval.subject`` (and re-key interval_id).

Conflict-write then ``rewrite_subject`` used to leave closed intervals on the
loser URI, so ``fetch_current`` on the winner missed the closure.

Hermetic MemoryGraphStore. Invented Widget schema — no real customer data.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from infona_client.graph.kg_writer import insert_facts, rewrite_subject
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.memory_store_session import MemoryGraphSession
from infona_client.graph.ontology_queries import attr_uri, entity_uri
from infona_client.graph.queries import kg_graph_uri
from infona_client.graph.scope import GraphScope
from infona_client.graph.store import GraphRecord, get_graph_store
from infona_client.graph.validity import (
    STATUS_SUPERSEDED,
    _interval_uri,
    build_closed_interval_triples,
    fetch_current_object_terms,
    statement_id,
)

TENANT, KG = "test-tenant", "kg"
GRAPH = kg_graph_uri(TENANT, KG)
TYPE = "Widget"
LOSER = entity_uri(TYPE, "loser")
WINNER = entity_uri(TYPE, "winner")
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
TYPE_URI = "https://graph.infona.ai/types/Widget"
HQ = attr_uri(TYPE, "headquarters")
SF, AUSTIN = "San Francisco", "Austin"
AT = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_rewrite_subject_rekeys_closed_interval_onto_winner():
    """Closed interval on the loser URI must exclude that object on the winner."""

    async def run():
        await insert_facts(
            None,
            GRAPH,
            [
                (LOSER, RDF_TYPE, TYPE_URI),
                (WINNER, RDF_TYPE, TYPE_URI),
                (LOSER, HQ, SF),
                (WINNER, HQ, AUSTIN),
            ],
            validity_triples=build_closed_interval_triples(
                LOSER, HQ, SF, valid_to=AT, status=STATUS_SUPERSEDED, graph_uri=GRAPH
            ),
        )
        assert SF not in set(await fetch_current_object_terms(None, GRAPH, LOSER, HQ))
        await rewrite_subject(None, GRAPH, LOSER, WINNER)
        current = set(await fetch_current_object_terms(None, GRAPH, WINNER, HQ))
        assert SF not in current
        assert AUSTIN in current
        rows = get_graph_store().snapshot_validity()
        assert not any(r.get("subject") == LOSER for r in rows)
        closed = [
            r
            for r in rows
            if r.get("subject") == WINNER and r.get("object_repr") == SF
        ]
        assert len(closed) == 1
        assert closed[0]["valid_to"]
        assert closed[0]["interval_id"] == _interval_uri(WINNER, HQ, SF)
        assert closed[0]["statement_id"] == statement_id(WINNER, HQ, SF)

    _run(run())


def test_validity_rekey_failure_does_not_fail_uri_rewrite(monkeypatch):
    """A down validity native must not roll back the Entity re-key."""

    async def boom(*_a, **_k):
        raise RuntimeError("validity native down")

    monkeypatch.setattr(MemoryGraphSession, "rewrite_validity_subject", boom)

    async def run():
        await insert_facts(
            None,
            GRAPH,
            [(LOSER, RDF_TYPE, TYPE_URI), (WINNER, RDF_TYPE, TYPE_URI)],
        )
        await rewrite_subject(None, GRAPH, LOSER, WINNER)
        store = get_graph_store()
        assert isinstance(store, MemoryGraphStore)
        assert (TENANT, KG, LOSER) not in store._entities
        assert (TENANT, KG, WINNER) in store._entities

    _run(run())


def test_neo4j_rewrite_validity_subject_sets_new_interval_id():
    """Free destination: SET subject + interval_id from sha1(new|p|o)."""
    from infona_client.graph.neo4j_store import Neo4jGraphSession

    old_iid = _interval_uri(LOSER, HQ, SF)
    new_iid = _interval_uri(WINNER, HQ, SF)

    class _Fake:
        def __init__(self) -> None:
            self.writes: list[tuple[str, dict]] = []
            self.reads: list[tuple[str, dict]] = []

        async def _run(self, cypher, bound, writing=False, database=None):
            params = dict(bound)
            if writing:
                self.writes.append((cypher, params))
                return [GraphRecord(data={"interval_id": params.get("new_interval_id")})]
            self.reads.append((cypher, params))
            if "v.subject AS subject" in cypher:
                return [
                    GraphRecord(
                        data={
                            "interval_id": old_iid,
                            "subject": LOSER,
                            "predicate": HQ,
                            "object_repr": SF,
                            "valid_to": AT.isoformat(),
                            "statement_id": statement_id(LOSER, HQ, SF),
                        }
                    )
                ]
            return []

    async def run():
        fake = _Fake()
        session = Neo4jGraphSession(fake, GraphScope.for_instance("t", "k"))  # type: ignore[arg-type]
        await session.rewrite_validity_subject(LOSER, WINNER)
        write_text = "\n".join(c for c, _ in fake.writes)
        assert "SET v.subject = $new_subject" in write_text
        assert "v.interval_id = $new_interval_id" in write_text
        assert "DELETE old" not in write_text
        params = fake.writes[0][1]
        assert params["old_interval_id"] == old_iid
        assert params["new_interval_id"] == new_iid
        assert params["new_subject"] == WINNER
        assert params["new_statement_id"] == statement_id(WINNER, HQ, SF)

    _run(run())


def test_neo4j_rewrite_validity_subject_merges_occupied_interval_id():
    """Occupied sha1(new|p|o): coalesce closures onto survivor, DELETE loser node."""
    from infona_client.graph.neo4j_store import Neo4jGraphSession

    old_iid = _interval_uri(LOSER, HQ, SF)
    new_iid = _interval_uri(WINNER, HQ, SF)

    class _Fake:
        def __init__(self) -> None:
            self.writes: list[tuple[str, dict]] = []

        async def _run(self, cypher, bound, writing=False, database=None):
            params = dict(bound)
            if writing:
                self.writes.append((cypher, params))
                return [GraphRecord(data={"interval_id": new_iid})]
            if "v.subject AS subject" in cypher:
                return [
                    GraphRecord(
                        data={
                            "interval_id": old_iid,
                            "subject": LOSER,
                            "predicate": HQ,
                            "object_repr": SF,
                            "valid_to": AT.isoformat(),
                        }
                    )
                ]
            if "interval_id: $new_interval_id" in cypher:
                return [GraphRecord(data={"interval_id": new_iid})]
            return []

    async def run():
        fake = _Fake()
        session = Neo4jGraphSession(fake, GraphScope.for_instance("t", "k"))  # type: ignore[arg-type]
        await session.rewrite_validity_subject(LOSER, WINNER)
        write_text = "\n".join(c for c, _ in fake.writes)
        assert "DELETE old" in write_text
        assert "coalesce(neu.valid_to, old.valid_to)" in write_text
        params = fake.writes[0][1]
        assert params["old_interval_id"] == old_iid
        assert params["new_interval_id"] == new_iid

    _run(run())

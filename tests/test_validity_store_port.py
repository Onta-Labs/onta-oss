"""ONTA-277 on the SHIPPED backend: valid-time CURRENT vs SUPERSEDED (E7 port).

Until this port, ``insert_facts`` dropped ``validity_triples`` /
``reopen_facts`` and ``fetch_current_object_terms`` SPARQL-queried a retired
client. A conflict receipt named a winner while both values stayed current.

Hermetic MemoryGraphStore. Invented Widget schema — no real customer data.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from infona_client.api_registry.spec import AuthorityLevel
from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_queries import attr_uri
from infona_client.graph.queries import kg_graph_uri
from infona_client.graph.store import get_graph_store
from infona_client.graph.validity import (
    STATUS_DEPRECATED,
    STATUS_SUPERSEDED,
    build_closed_interval_triples,
    build_open_interval_triples,
    fetch_current_object_terms,
    fetch_history,
)
from infona_client.graph.validity_store import parse_validity_records
from infona_client.pipeline.conflict import FactClaim
from infona_client.pipeline.mutations import write_with_conflict_resolution

TENANT, KG = "test-tenant", "kg"
GRAPH = kg_graph_uri(TENANT, KG)
TYPE = "Widget"
ENTITY = "https://graph.infona.ai/entities/Widget/e1"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
TYPE_URI = "https://graph.infona.ai/types/Widget"
HQ = attr_uri(TYPE, "headquarters")
SKU = attr_uri(TYPE, "sku")
AUSTIN, SF = "Austin", "San Francisco"
AT = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _lits(subject: str, pred: str) -> set:
    leaf = pred.rstrip("/").rsplit("/", 1)[-1]
    return {
        a.get("literal_value")
        for a in get_graph_store().snapshot_assertions()
        if a.get("subject_id") == subject
        and str(a.get("property_id") or "").endswith(leaf)
    }


async def _current(pred: str = HQ, subject: str = ENTITY) -> set[str]:
    return set(await fetch_current_object_terms(None, GRAPH, subject, pred))


async def _seed_type() -> None:
    await insert_facts(None, GRAPH, [(ENTITY, RDF_TYPE, TYPE_URI)])


# --------------------------------------------------------------------------- #
# 1. Closed interval excludes the object; instance triple stays
# --------------------------------------------------------------------------- #
def test_closed_interval_excludes_object_but_keeps_the_triple():
    async def run():
        await _seed_type()
        await insert_facts(
            None,
            GRAPH,
            [(ENTITY, HQ, SF)],
            validity_triples=build_closed_interval_triples(
                ENTITY, HQ, SF, valid_to=AT, status=STATUS_SUPERSEDED, graph_uri=GRAPH
            ),
        )
        assert SF not in await _current()
        assert SF in _lits(ENTITY, HQ)

    _run(run())


# --------------------------------------------------------------------------- #
# 2. Open interval (no valid_to) ⇒ current
# --------------------------------------------------------------------------- #
def test_open_interval_is_current():
    async def run():
        await _seed_type()
        await insert_facts(
            None,
            GRAPH,
            [(ENTITY, HQ, AUSTIN)],
            validity_triples=build_open_interval_triples(
                ENTITY, HQ, AUSTIN, valid_from=AT, graph_uri=GRAPH
            ),
        )
        assert await _current() == {AUSTIN}
        assert AUSTIN in _lits(ENTITY, HQ)

    _run(run())


# --------------------------------------------------------------------------- #
# 3. reopen_facts resurrects a previously closed value (ONTA-277)
# --------------------------------------------------------------------------- #
def test_reopen_facts_makes_a_closed_value_current_again():
    async def run():
        await _seed_type()
        await insert_facts(
            None,
            GRAPH,
            [(ENTITY, HQ, AUSTIN)],
            validity_triples=build_closed_interval_triples(
                ENTITY,
                HQ,
                AUSTIN,
                valid_to=AT,
                status=STATUS_SUPERSEDED,
                graph_uri=GRAPH,
            ),
        )
        assert AUSTIN not in await _current()
        await insert_facts(
            None,
            GRAPH,
            [],
            reopen_facts=[(ENTITY, HQ, AUSTIN)],
        )
        assert await _current() == {AUSTIN}

    _run(run())


# --------------------------------------------------------------------------- #
# 4. Functional HQ conflict: winner current, loser stored + lost_conflict
# --------------------------------------------------------------------------- #
def test_conflict_winner_is_current_loser_is_history():
    async def run():
        await _seed_type()
        await insert_facts(None, GRAPH, [(ENTITY, HQ, AUSTIN)])
        receipt = await write_with_conflict_resolution(
            None,
            GRAPH,
            subject=ENTITY,
            predicate=HQ,
            type_name=TYPE,
            value=SF,
            authority=AuthorityLevel.supplementary,
            source="directory",
            observed_at=AT,
            existing_claims=[
                FactClaim(
                    value=AUSTIN,
                    authority=AuthorityLevel.source_of_truth,
                    source="erp",
                    observed_at=AT,
                )
            ],
            run_id="e7-hq",
            refresh=False,
        )
        assert receipt.conflict is True
        assert receipt.winner == (ENTITY, HQ, AUSTIN)
        assert receipt.loser == (ENTITY, HQ, SF)
        assert await _current() == {AUSTIN}
        assert _lits(ENTITY, HQ) >= {AUSTIN, SF}
        hist = {h.obj: h for h in await fetch_history(None, GRAPH, ENTITY, HQ)}
        assert not hist[SF].is_current and hist[SF].valid_to
        assert hist[SF].status == STATUS_DEPRECATED
        assert hist[AUSTIN].is_current

    _run(run())


# --------------------------------------------------------------------------- #
# 5. Unannotated legacy fact stays current
# --------------------------------------------------------------------------- #
def test_unannotated_legacy_fact_stays_current():
    async def run():
        await _seed_type()
        await insert_facts(None, GRAPH, [(ENTITY, SKU, "W-1")])
        assert await _current(SKU) == {"W-1"}
        hist = await fetch_history(None, GRAPH, ENTITY, SKU)
        assert [h.obj for h in hist] == ["W-1"]
        assert hist[0].is_current and not hist[0].valid_to

    _run(run())


# --------------------------------------------------------------------------- #
# 6. Workspace isolation
# --------------------------------------------------------------------------- #
def test_interval_in_one_kg_does_not_close_another():
    async def run():
        other = kg_graph_uri(TENANT, "other-kg")
        await insert_facts(
            None,
            GRAPH,
            [(ENTITY, RDF_TYPE, TYPE_URI), (ENTITY, HQ, SF)],
            validity_triples=build_closed_interval_triples(
                ENTITY, HQ, SF, valid_to=AT, status=STATUS_SUPERSEDED, graph_uri=GRAPH
            ),
        )
        await insert_facts(
            None,
            other,
            [(ENTITY, RDF_TYPE, TYPE_URI), (ENTITY, HQ, SF)],
        )
        assert SF not in set(await fetch_current_object_terms(None, GRAPH, ENTITY, HQ))
        assert set(await fetch_current_object_terms(None, other, ENTITY, HQ)) == {SF}

    _run(run())


# --------------------------------------------------------------------------- #
# Parse / surface / bootstrap
# --------------------------------------------------------------------------- #
def test_parse_groups_by_interval_node_and_strips_timestamp_datatype():
    closed = build_closed_interval_triples(
        ENTITY, HQ, SF, valid_to=AT, status=STATUS_DEPRECATED, graph_uri=GRAPH
    )
    recs = parse_validity_records(closed)
    assert len(recs) == 1
    rec = recs[0]
    assert rec.subject == ENTITY and rec.predicate == HQ and rec.object_repr == SF
    assert rec.status == STATUS_DEPRECATED
    assert rec.valid_to.startswith("2026-03-01T")
    assert "^^" not in rec.valid_to
    assert rec.interval_id


def test_parse_ignores_unrelated_triples():
    assert parse_validity_records([(ENTITY, HQ, SF)]) == []
    assert parse_validity_records([]) == []


def test_memory_and_neo4j_sessions_expose_the_same_native_surface():
    from infona_client.graph.memory_store_session import MemoryGraphSession
    from infona_client.graph.neo4j_store_validity import Neo4jValidityMixin

    for name in (
        "write_validity_interval",
        "read_validity_intervals",
        "reopen_validity_interval",
        "rewrite_validity_subject",
    ):
        assert callable(getattr(MemoryGraphSession, name, None)), name
        assert callable(getattr(Neo4jValidityMixin, name, None)), name


def test_bootstrap_declares_the_validity_constraint():
    from infona_client.graph.schema_bootstrap import SCHEMA_STATEMENTS

    names = {n for n, _ in SCHEMA_STATEMENTS}
    assert "validity_interval_tenant_kg_id_unique" in names
    body = "\n".join(
        c for n, c in SCHEMA_STATEMENTS if n.startswith("validity_interval_")
    )
    assert "v.tenant_id, v.kg, v.interval_id" in body


def test_warn_unported_no_longer_names_validity():
    from infona_client.graph.kg_writer_session import _warn_unported_companions

    import inspect

    params = inspect.signature(_warn_unported_companions).parameters
    assert "validity_triples" not in params
    assert "reopen_facts" not in params


def test_store_is_reset_between_tests():
    assert isinstance(get_graph_store(), MemoryGraphStore)
    assert get_graph_store().snapshot_validity() == []

"""ONTA-279 on the SHIPPED backend: a retraction must STICK (E7 port).

The product promise: a user retracts a wrong value, and no later refresh
re-acquires it. On Neo4j BOTH halves of that promise were dead —
``insert_facts`` dropped ``suppression_triples`` on the floor
(``insert_facts_companion_payload_not_ported``) and ``is_suppressed``'s SPARQL
read raised on the retired client and degraded to "nothing is suppressed". Each
half alone is useless, so both are pinned here.

The headline test is :func:`test_retracted_value_is_not_reacquired_by_a_refresh`
— it drives the REAL enrichment refresh rail over the REAL store and asserts the
retracted value does not come back. It fails on ``main`` (the value returns).
Everything else pins one mechanism of that guarantee.

Invented Widget/Gadget schema throughout — nothing here keys off a real tenant's
types.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from infona_client.enrichment.cache import EnrichmentCache
from infona_client.enrichment.executor import EnrichmentExecutor, _attr_uri
from infona_client.enrichment.job_store import InMemoryJobStore
from infona_client.enrichment.models import ConflictPolicy, JobStatus, Verdict
from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.queries import kg_graph_uri
from infona_client.graph.store import configure_graph_store, get_graph_store
from infona_client.graph.suppression import (
    build_entity_suppression_triples,
    build_suppression_triples,
    fetch_suppressed,
    is_entity_suppressed,
    is_suppressed,
    read_suppressed,
)
from infona_client.graph.suppression_store import (
    SuppressionUnavailable,
    parse_suppression_marks,
)
from infona_client.pipeline.mutations import retract_fact

from tests._enrichment_prov_helpers import FakeWikidata, make_job, seed_enrich_entities

TENANT, KG = "test-tenant", "kg"
GRAPH = kg_graph_uri(TENANT, KG)
TYPE = "Widget"
ENTITY = "https://graph.infona.ai/entities/Widget/e1"
LABEL = "Alpha Widget"
SKU = _attr_uri(TYPE, "sku")
COLOR = _attr_uri(TYPE, "color")

# The value the user retracts and the scraper keeps re-observing.
BAD_SKU = "WX-RECALLED"
# The control attribute the SAME refresh must still acquire.
GOOD_COLOR = "cerulean"

XSD_INT = "http://www.w3.org/2001/XMLSchema#integer"


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _quiet_housekeeping(monkeypatch):
    """Silence refresh_after_write's derived-state fan-out (cache invalidate /
    embed / stats recompute) so these tests isolate the suppression mechanism.
    The real rail still CALLS refresh_after_write."""
    import infona_client.api.routes.explore as explore_mod
    import infona_client.nlp.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod.NLQueryPipeline, "invalidate_cache", lambda g: None)
    monkeypatch.setattr(pipeline_mod, "get_embedding_service", lambda: None)
    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)


def _store_only_neptune() -> AsyncMock:
    """A SPARQL client that must not be used for reads.

    ``query`` raising is the production shape after ONTA-534 (``SparqlClientRetired``)
    and makes any accidental reliance on the residual SPARQL arm visible: the
    suppression answer must come from the store.
    """
    neptune = AsyncMock()
    neptune.query.side_effect = AssertionError("suppression must not read SPARQL")
    neptune.update.return_value = None
    return neptune


def _entity_props(entity_id: str = ENTITY) -> dict:
    entity = next(
        (e for e in get_graph_store().snapshot_entities() if e["id"] == entity_id), None
    )
    return dict(entity["props"]) if entity else {}


# --------------------------------------------------------------------------- #
# THE GUARANTEE — end to end. Fails on main.
# --------------------------------------------------------------------------- #
def test_retracted_value_is_not_reacquired_by_a_refresh():
    """Retract a wrong value (hard delete), then run the REAL enrichment refresh
    whose source still reports it. The value must NOT come back.

    Load-bearing control: a DIFFERENT attribute on the SAME entity, from the same
    refresh, IS acquired — so a green assertion means "suppression held", never
    "the refresh silently did nothing".
    """

    async def run():
        await seed_enrich_entities(
            TYPE,
            [{"uri": ENTITY, "label": LABEL, "vals": f"{SKU}::{BAD_SKU}"}],
            tenant_id=TENANT,
            kg_name=KG,
        )
        neptune = _store_only_neptune()
        assert _entity_props().get("sku") == BAD_SKU, "seed did not land"

        # 1. The user retracts the wrong value and removes the triple.
        await retract_fact(
            neptune,
            GRAPH,
            subject=ENTITY,
            predicate=SKU,
            type_name=TYPE,
            value=BAD_SKU,
            reason="user says this SKU was recalled",
            tenant_id=TENANT,
            kg_name=KG,
            hard_delete=True,
        )
        assert _entity_props().get("sku") != BAD_SKU, "hard delete did not remove it"

        # 2. A refresh runs whose source STILL reports the retracted value, plus a
        #    fresh value for an unrelated attribute.
        executor = EnrichmentExecutor(
            neptune,
            InMemoryJobStore(),
            EnrichmentCache(),
            FakeWikidata(
                {
                    (LABEL, "sku"): [
                        Verdict(
                            value=BAD_SKU,
                            confidence=0.99,
                            source="scraper",
                            source_url="https://parts.example/alpha",
                            retrieved_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                        )
                    ],
                    (LABEL, "color"): [
                        Verdict(
                            value=GOOD_COLOR,
                            confidence=0.99,
                            source="scraper",
                            source_url="https://parts.example/alpha",
                            retrieved_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                        )
                    ],
                }
            ),
        )
        job = make_job(
            type_name=TYPE,
            attributes=["sku", "color"],
            policy=ConflictPolicy.overwrite,
            kg=KG,
            entity_uris=[ENTITY],
        )
        await executor._jobs.create(job)
        await executor.run(job, TENANT)
        assert (await executor._jobs.get(job.id)).status == JobStatus.applied

        props = _entity_props()
        # THE GUARANTEE.
        assert props.get("sku") != BAD_SKU, (
            "a retracted value was RE-ACQUIRED by a refresh — the ONTA-279 "
            "suppression marker did not stick"
        )
        # THE CONTROL: the refresh genuinely ran and wrote what it was allowed to.
        assert props.get("color") == GOOD_COLOR, (
            "control attribute was not written — the refresh did not run, so the "
            "suppression assertion above proves nothing"
        )
        # The marker is STICKY: surviving the refresh that tried to re-acquire.
        assert await is_suppressed(neptune, GRAPH, ENTITY, SKU, BAD_SKU)

    _run(run())


# --------------------------------------------------------------------------- #
# The WRITE half
# --------------------------------------------------------------------------- #
def test_insert_facts_persists_suppression_marks():
    """``insert_facts(suppression_triples=…)`` lands ``:Suppression`` rows scoped
    to the write's (tenant, kg) — it no longer drops the payload."""

    async def run():
        await insert_facts(
            None,
            GRAPH,
            [],
            suppression_triples=build_suppression_triples(
                ENTITY,
                SKU,
                BAD_SKU,
                suppressed_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                reason="retract",
                graph_uri=GRAPH,
            ),
        )
        rows = get_graph_store().snapshot_suppressions()
        assert len(rows) == 1
        row = rows[0]
        assert (row["tenant_id"], row["kg"]) == (TENANT, KG)
        assert row["kind"] == "fact"
        assert (row["subject"], row["predicate"], row["object_repr"]) == (
            ENTITY,
            SKU,
            BAD_SKU,
        )
        assert row["reason"] == "retract"
        # The RDF typed-literal tail is not stored on the timestamp property.
        assert row["suppressed_at"].startswith("2026-01-02T")
        assert "^^" not in row["suppressed_at"]

    _run(run())


def test_re_suppressing_the_same_value_is_idempotent():
    """The mark id is sha1(s|p|o), so retracting twice MERGEs one row."""

    async def run():
        triples = build_suppression_triples(ENTITY, SKU, BAD_SKU, graph_uri=GRAPH)
        await insert_facts(None, GRAPH, [], suppression_triples=triples)
        await insert_facts(None, GRAPH, [], suppression_triples=triples)
        assert len(get_graph_store().snapshot_suppressions()) == 1

    _run(run())


def test_warn_unported_no_longer_names_suppression():
    """Valid-time stays unported and still warns; suppression must not."""
    from infona_client.graph.kg_writer_session import _warn_unported_companions

    import inspect

    params = inspect.signature(_warn_unported_companions).parameters
    assert "suppression_triples" not in params
    assert {"validity_triples", "reopen_facts"} <= set(params)


# --------------------------------------------------------------------------- #
# The READ half
# --------------------------------------------------------------------------- #
def test_read_is_term_faithful_and_kind_faithful():
    """A typed literal never matches its plain form, and a FACT mark never answers
    the ENTITY question (or vice versa)."""

    async def run():
        typed = f"42^^{XSD_INT}"
        await insert_facts(
            None,
            GRAPH,
            [],
            suppression_triples=build_suppression_triples(
                ENTITY, SKU, typed, graph_uri=GRAPH
            ),
        )
        assert await is_suppressed(None, GRAPH, ENTITY, SKU, typed)
        assert not await is_suppressed(None, GRAPH, ENTITY, SKU, "42")
        # Same subject, different predicate — not suppressed.
        assert not await is_suppressed(None, GRAPH, ENTITY, COLOR, typed)
        # A FACT mark is not an ENTITY tombstone.
        assert not await is_entity_suppressed(None, GRAPH, ENTITY)

        await insert_facts(
            None,
            GRAPH,
            [],
            suppression_triples=build_entity_suppression_triples(
                ENTITY, graph_uri=GRAPH
            ),
        )
        assert await is_entity_suppressed(None, GRAPH, ENTITY)
        # …and an ENTITY tombstone does not suppress an arbitrary value.
        assert not await is_suppressed(None, GRAPH, ENTITY, SKU, "anything-else")

    _run(run())


def test_node_valued_object_is_matched_as_an_iri():
    """A relationship edge's target IRI round-trips as the object term."""

    async def run():
        target = "https://graph.infona.ai/entities/Gadget/g9"
        rel = "https://graph.infona.ai/onto/pairs_with"
        await insert_facts(
            None,
            GRAPH,
            [],
            suppression_triples=build_suppression_triples(
                ENTITY, rel, target, graph_uri=GRAPH
            ),
        )
        assert await is_suppressed(None, GRAPH, ENTITY, rel, target)
        assert await fetch_suppressed(None, GRAPH, ENTITY, rel) == {target}

    _run(run())


def test_marks_are_scoped_per_tenant_and_kg():
    """A mark in one workspace never withholds a value in another."""

    async def run():
        other_tenant = kg_graph_uri("other-tenant", KG)
        other_kg = kg_graph_uri(TENANT, "other-kg")
        await insert_facts(
            None,
            GRAPH,
            [],
            suppression_triples=build_suppression_triples(
                ENTITY, SKU, BAD_SKU, graph_uri=GRAPH
            ),
        )
        assert await is_suppressed(None, GRAPH, ENTITY, SKU, BAD_SKU)
        assert not await is_suppressed(None, other_tenant, ENTITY, SKU, BAD_SKU)
        assert not await is_suppressed(None, other_kg, ENTITY, SKU, BAD_SKU)
        assert await fetch_suppressed(None, other_tenant, ENTITY, SKU) == set()

    _run(run())


def test_mark_survives_a_hard_delete_of_the_fact():
    """A retraction may hard-delete the triple; the marker is NOT keyed to it.

    This is why the marker is a standalone ``:Suppression`` node rather than a
    property on the ``:Assertion`` — ``retract_fact(hard_delete=True)`` deletes
    the assertion FIRST, so an assertion-borne flag would be born dead.
    """

    async def run():
        await seed_enrich_entities(
            TYPE,
            [{"uri": ENTITY, "label": LABEL, "vals": f"{SKU}::{BAD_SKU}"}],
            tenant_id=TENANT,
            kg_name=KG,
        )
        neptune = _store_only_neptune()
        await retract_fact(
            neptune,
            GRAPH,
            subject=ENTITY,
            predicate=SKU,
            type_name=TYPE,
            value=BAD_SKU,
            tenant_id=TENANT,
            kg_name=KG,
            hard_delete=True,
        )
        # The fact is gone …
        assert _entity_props().get("sku") != BAD_SKU
        assert not [
            a
            for a in get_graph_store().snapshot_assertions()
            if a["subject_id"] == ENTITY and a.get("literal_value") == BAD_SKU
        ]
        # … and the marker is still there and still answering.
        assert await is_suppressed(neptune, GRAPH, ENTITY, SKU, BAD_SKU)

    _run(run())


# --------------------------------------------------------------------------- #
# Fail direction
# --------------------------------------------------------------------------- #
class _FailingReadStore(MemoryGraphStore):
    """A live, writable store whose suppression READ is broken.

    The realistic shape of the fail-closed case: Bolt is up enough to have
    accepted the retraction, and the read that would enforce it errors.
    """

    def session(self, scope):
        sess = super().session(scope)

        async def boom(**_kw):
            raise RuntimeError("bolt connection reset")

        sess.read_suppressions = boom  # type: ignore[method-assign]
        return sess


def test_unreadable_store_fails_closed_for_one_value():
    """A store that was ASKED and FAILED withholds the value (fail CLOSED).

    Driven through the REAL ``is_suppressed`` against a real (broken-read) store,
    not a patched reader. Answering False here is what re-acquires a retracted
    value; the condition is transient and per-call, so withholding one value is
    the cheap side of the trade.
    """

    async def run():
        from infona_client.graph import suppression_store as ss

        store = _FailingReadStore()
        configure_graph_store(store)

        # The store layer reports "asked and failed" distinctly …
        with pytest.raises(SuppressionUnavailable):
            await ss.read_suppressed_terms(GRAPH, ENTITY, SKU)

        # … and the decision predicate absorbs it into the CLOSED direction,
        # for a value that was never suppressed at all.
        assert await is_suppressed(None, GRAPH, ENTITY, SKU, "never-suppressed") is True
        assert await is_entity_suppressed(None, GRAPH, ENTITY) is True

        # The bulk filter deliberately does NOT fail closed — closing it would
        # drop a whole discovery run rather than withhold one value.
        assert await fetch_suppressed(None, GRAPH, ENTITY, SKU) == set()

    _run(run())


def test_unaskable_store_fails_open():
    """No store to consult at all → fail OPEN, because failing closed there would
    brick the whole refresh rail rather than withhold one value."""

    async def run():
        configure_graph_store(None)
        # Nothing can answer: no store, and no SPARQL client either.
        assert await read_suppressed(None, GRAPH, ENTITY, SKU) is None
        assert await is_suppressed(None, GRAPH, ENTITY, SKU, BAD_SKU) is False
        assert await is_entity_suppressed(None, GRAPH, ENTITY) is False
        assert await fetch_suppressed(None, GRAPH, ENTITY, SKU) == set()

    _run(run())


def test_residual_sparql_arm_answers_when_no_store_arm():
    """Dual-arm: with no store configured, a live SPARQL client still answers —
    the arm the pyoxigraph unit tests drive."""

    async def run():
        configure_graph_store(None)

        neptune = AsyncMock()
        neptune.query.return_value = {
            "head": {"vars": ["o"]},
            "results": {"bindings": [{"o": {"type": "literal", "value": BAD_SKU}}]},
        }
        assert await is_suppressed(neptune, GRAPH, ENTITY, SKU, BAD_SKU) is True
        assert await is_suppressed(neptune, GRAPH, ENTITY, SKU, "other") is False

    _run(run())


def test_store_arm_wins_over_sparql_arm():
    """The store is the shipped backend, so it answers first and the SPARQL client
    is never consulted."""

    async def run():
        await insert_facts(
            None,
            GRAPH,
            [],
            suppression_triples=build_suppression_triples(
                ENTITY, SKU, BAD_SKU, graph_uri=GRAPH
            ),
        )
        neptune = _store_only_neptune()  # .query raises if touched
        assert await is_suppressed(neptune, GRAPH, ENTITY, SKU, BAD_SKU) is True
        assert await is_suppressed(neptune, GRAPH, ENTITY, SKU, "other") is False
        neptune.query.assert_not_awaited()

    _run(run())


# --------------------------------------------------------------------------- #
# The triple → record parse
# --------------------------------------------------------------------------- #
def test_parse_groups_marks_by_node_and_keeps_the_kinds_apart():
    fact = build_suppression_triples(
        ENTITY, SKU, BAD_SKU, reason="r", graph_uri=GRAPH
    )
    entity = build_entity_suppression_triples(ENTITY, reason="gdpr", graph_uri=GRAPH)
    marks = parse_suppression_marks(fact + entity)
    assert len(marks) == 2
    kinds = {m.kind: m for m in marks}
    assert set(kinds) == {"fact", "entity"}
    assert kinds["fact"].object_repr == BAD_SKU
    assert kinds["fact"].predicate == SKU
    # An entity mark carries NO predicate/object — that is what keeps it from
    # ever answering a (s, p, o) question.
    assert kinds["entity"].predicate == ""
    assert kinds["entity"].object_repr == ""
    assert kinds["entity"].subject == ENTITY
    assert kinds["fact"].mark_id != kinds["entity"].mark_id


def test_parse_ignores_unrelated_triples():
    """A caller mixing payloads must not fail the write."""
    assert parse_suppression_marks([(ENTITY, SKU, BAD_SKU)]) == []
    assert parse_suppression_marks([]) == []


def test_memory_and_neo4j_sessions_expose_the_same_native_surface():
    """The hermetic store must not diverge from the shipped one."""
    from infona_client.graph.memory_store_session import MemoryGraphSession
    from infona_client.graph.neo4j_store_prov import Neo4jProvMixin

    for name in ("write_suppression", "read_suppressions"):
        assert callable(getattr(MemoryGraphSession, name, None)), name
        assert callable(getattr(Neo4jProvMixin, name, None)), name


def test_bootstrap_declares_the_suppression_constraint_and_index():
    from infona_client.graph.schema_bootstrap import SCHEMA_STATEMENTS

    names = {n for n, _ in SCHEMA_STATEMENTS}
    assert "suppression_tenant_kg_mark_unique" in names
    assert "suppression_subject_lookup" in names
    body = "\n".join(c for n, c in SCHEMA_STATEMENTS if n.startswith("suppression_"))
    # Isolation is part of the key, not an afterthought.
    assert "s.tenant_id, s.kg, s.mark_id" in body


def test_store_is_reset_between_tests():
    """Guard the fixture contract these tests lean on."""
    assert isinstance(get_graph_store(), MemoryGraphStore)
    assert get_graph_store().snapshot_suppressions() == []

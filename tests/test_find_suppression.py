"""Entity-level suppression unit tests (ONTA-345).

``graph/suppression.py`` is a sticky retraction marker. Discovery-run
acceptance (FIND path drops suppressed entities) lives in premium
``infona/web_ingest/tests``. This file pins the OSS reader/writer:
``is_entity_suppressed`` is term-faithful and kind-faithful.
"""

from __future__ import annotations

import json

import pytest

from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.queries import kg_graph_uri
from infona_client.graph.suppression import (
    build_entity_suppression_triples,
    build_suppression_triples,
    fetch_suppressed_entities,
    is_entity_suppressed,
    is_suppressed,
)

pyoxigraph = pytest.importorskip("pyoxigraph")
from pyoxigraph import QueryResultsFormat, Store  # noqa: E402


class PyoxiNeptune:
    """Minimal NeptuneClient shim over an in-process pyoxigraph Store."""

    def __init__(self) -> None:
        self.store = Store()

    async def query(self, sparql: str) -> dict:
        results = self.store.query(sparql, use_default_graph_as_union=True)
        return json.loads(results.serialize(format=QueryResultsFormat.JSON))

    async def update(self, sparql: str) -> None:
        self.store.update(sparql)


TENANT, KG = "demo-tenant", "models"
INSTANCE_GRAPH = kg_graph_uri(TENANT, KG)
TYPE = "OpenRouterModel"
SUPPRESSED_NAME = "anthropic/claude-opus-4-8"
KEPT_NAME = "openai/gpt-5"
SUPPRESSED_SUBJECT = entity_uri(TYPE, SUPPRESSED_NAME)
KEPT_SUBJECT = entity_uri(TYPE, KEPT_NAME)


@pytest.mark.asyncio
async def test_entity_mark_and_fact_mark_are_independent():
    """An ENTITY mark and a (s, p, o) FACT mark never collide."""
    neptune = PyoxiNeptune()
    subject = SUPPRESSED_SUBJECT
    predicate = "https://graph.infona.ai/onto/context_length"
    obj = "200000"

    await insert_facts(
        neptune,
        INSTANCE_GRAPH,
        [],
        suppression_triples=build_suppression_triples(
            subject, predicate, obj, graph_uri=INSTANCE_GRAPH
        ),
    )
    assert await is_suppressed(neptune, INSTANCE_GRAPH, subject, predicate, obj) is True
    assert await is_entity_suppressed(neptune, INSTANCE_GRAPH, subject) is False
    assert await fetch_suppressed_entities(neptune, INSTANCE_GRAPH) == set()

    await insert_facts(
        neptune,
        INSTANCE_GRAPH,
        [],
        suppression_triples=build_entity_suppression_triples(
            subject, graph_uri=INSTANCE_GRAPH
        ),
    )
    assert await is_entity_suppressed(neptune, INSTANCE_GRAPH, subject) is True
    assert await fetch_suppressed_entities(neptune, INSTANCE_GRAPH) == {subject}
    assert await is_entity_suppressed(neptune, INSTANCE_GRAPH, KEPT_SUBJECT) is False
    assert (
        await is_suppressed(
            neptune, INSTANCE_GRAPH, subject, "https://graph.infona.ai/onto/other", "x"
        )
        is False
    )
    assert await is_suppressed(neptune, INSTANCE_GRAPH, subject, predicate, obj) is True


@pytest.mark.asyncio
async def test_fetch_suppressed_entities_empty_and_no_graph():
    """Best-effort read: no marks / no graph → empty set (never raises)."""
    neptune = PyoxiNeptune()
    assert await fetch_suppressed_entities(neptune, INSTANCE_GRAPH) == set()
    assert await fetch_suppressed_entities(neptune, "") == set()
    assert await is_entity_suppressed(neptune, INSTANCE_GRAPH, SUPPRESSED_SUBJECT) is False

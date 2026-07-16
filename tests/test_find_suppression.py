"""FIND-path entity-level suppression guard (ONTA-345).

``graph/suppression.py`` (ONTA-279) is a reopen-PROOF STICKY retraction marker,
but it was consulted ONLY at enrichment attribute-write, per ``(subject,
predicate, object)`` fact — discovery NEVER consulted it, and there was no
ENTITY-LEVEL key. So an ERASED entity was silently re-minted on the next
discovery/refresh, violating the P1 'never re-acquire erased data' rule (the
GDPR erasure blast radius).

This test exercises the fix on two layers:

1. A discovery-run test that drives ``web_ingest_cap.execute()`` fully offline (a
   canned ``_FakeProvider``, a spied ``SchemaResolver.ingest``, a real in-process
   pyoxigraph store as Neptune, no LLM / network). It seeds an ENTITY-level
   suppression for the exact ``entity_uri`` one discovered row would mint and
   asserts that row is EXCLUDED from the A1 SourceBundle AND never reaches
   ``resolver.ingest`` — while the LOAD-BEARING CONTROL (a non-suppressed row) IS
   included and DOES reach ingest.
2. A ``graph/suppression.py`` unit test proving ``is_entity_suppressed`` is
   term-faithful and kind-faithful: an ENTITY mark and a ``(s, p, o)`` FACT mark
   for the same subject never collide or shadow one another.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from cograph_client.agent.capabilities import web_ingest_cap
from cograph_client.agent.capabilities.web_ingest_cap import WebIngestCapability
from cograph_client.agent.registry import AgentContext
from cograph_client.graph.kg_writer import insert_facts
from cograph_client.graph.ontology_queries import entity_uri
from cograph_client.graph.queries import kg_graph_uri
from cograph_client.graph.suppression import (
    build_entity_suppression_triples,
    build_suppression_triples,
    fetch_suppressed_entities,
    is_entity_suppressed,
    is_suppressed,
)
from cograph_client.pipeline.source_bundle import SourceBundle
from cograph_client.resolver.models import IngestResult
from cograph_client.resolver.schema_resolver import SchemaResolver
from cograph_client.web_sources import (
    DiscoverResult,
    register_web_source,
    reset_web_sources,
)

pyoxigraph = pytest.importorskip("pyoxigraph")
from pyoxigraph import QueryResultsFormat, Store  # noqa: E402


class PyoxiNeptune:
    """Minimal NeptuneClient shim over an in-process pyoxigraph Store — async
    query()/update() returning SPARQL-1.1 JSON, union-of-named-graphs default.
    Copied from tests/test_validity_resurrection.py."""

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
# The exact canonical subjects the resolver would mint for each row — the drop
# guard checks membership against these, so we derive them the SAME way.
SUPPRESSED_SUBJECT = entity_uri(TYPE, SUPPRESSED_NAME)
KEPT_SUBJECT = entity_uri(TYPE, KEPT_NAME)

# Confirmed spec injected via plan()'s ``parsed`` hook (no LLM), mirroring the
# test_source_bundle / test_web_ingest_cap harness.
CONFIRMED_SPEC = {
    "entity_type": TYPE,
    "key_attribute": "name",
    "query": "OpenRouter models",
    "confirmed_attributes": ["context_length"],
    "suggested_attributes": ["provider", "context_length"],
}
ROWS = [
    {"name": SUPPRESSED_NAME, "context_length": "200000"},
    {"name": KEPT_NAME, "context_length": "400000"},
]


class _FakeProvider:
    """Canned FREE web-source provider (projects rows to hint_columns, emits
    per-row provenance) — the offline discovery harness. Cost 0 so plan() takes
    the lean fast path (no plan-time sample/preview)."""

    def __init__(self, *, name: str = "web_fake", rows=None) -> None:
        self.name = name
        self.is_paid = False
        self.cost_per_call = 0.0
        self._rows = ROWS if rows is None else rows
        self.calls: list[tuple] = []

    async def discover(self, query, *, sample, max_rows, hint_columns, context, urls=None):
        self.calls.append((query, sample, max_rows))
        rows = self._rows[: (5 if sample else max_rows)]
        if hint_columns:
            rows = [{c: r.get(c, "unknown") for c in hint_columns} for r in rows]
        prov = {
            r.get("name", str(i)): f"https://src.example/page-{i}"
            for i, r in enumerate(rows)
        }
        return DiscoverResult(
            rows=rows,
            provenance=prov,
            sources=["https://openrouter.ai/models"],
            estimated_total=len(self._rows),
            is_partial=sample,
        )


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_web_sources()
    yield
    reset_web_sources()


def _ctx(neptune, sink) -> AgentContext:
    return AgentContext(
        tenant_id=TENANT,
        kg_name=KG,
        neptune=neptune,
        anthropic_key="sk-ant-test",
        openrouter_key="",
        extras={"prior_clarify_count": 0, "source_bundle_sink": sink},
    )


async def _seed_entity_suppression(neptune: PyoxiNeptune, subject: str) -> None:
    """Seed an ENTITY-level suppression via the new builder → the SHARED write
    path (insert_facts routes suppression_triples to the companion graph)."""
    await insert_facts(
        neptune,
        INSTANCE_GRAPH,
        [],
        suppression_triples=build_entity_suppression_triples(
            subject, reason="gdpr-erasure", graph_uri=INSTANCE_GRAPH
        ),
    )


async def _run_discovery(monkeypatch, neptune, provider):
    """Drive plan()+execute() for one provider; return (bundles, committed_rows).

    ``committed_rows`` are the rows that actually reached ``resolver.ingest`` —
    the spy that proves a suppressed entity never reaches the writer."""
    register_web_source(provider)

    committed: list[dict] = []

    async def fake_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None, **_kw):
        rows = json.loads(content)
        committed.extend(rows)
        return IngestResult(entities_extracted=len(rows), entities_resolved=len(rows))

    monkeypatch.setattr(SchemaResolver, "ingest", fake_ingest)

    # Isolate the run from the shared post-write housekeeping (cache-invalidate /
    # re-embed / stats recompute) — irrelevant to the FIND-path guard under test.
    async def _noop_refresh(*_a, **_k):
        return None

    monkeypatch.setattr(web_ingest_cap, "refresh_after_write", _noop_refresh)

    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap,
        "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    bundles: list[SourceBundle] = []
    ctx = _ctx(neptune, bundles)
    cap = WebIngestCapability()
    step = (await cap.plan(ctx, "find a list of OpenRouter models", parsed=CONFIRMED_SPEC))[0]
    assert step.action == "discover_ingest"
    await cap.execute(ctx, step)
    await spawned["task"]
    return bundles, committed


# --------------------------------------------------------------------------- #
# 1. discovery run: a suppressed entity is dropped BEFORE bundle + ingest
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_find_path_drops_suppressed_entity_and_keeps_others(monkeypatch):
    neptune = PyoxiNeptune()
    await _seed_entity_suppression(neptune, SUPPRESSED_SUBJECT)
    # The seed is readable via the new reader; a NON-suppressed subject is not.
    assert await is_entity_suppressed(neptune, INSTANCE_GRAPH, SUPPRESSED_SUBJECT) is True
    assert await is_entity_suppressed(neptune, INSTANCE_GRAPH, KEPT_SUBJECT) is False

    bundles, committed = await _run_discovery(monkeypatch, neptune, _FakeProvider())

    # (1) The suppressed row is EXCLUDED from the SourceBundle...
    assert len(bundles) == 1
    bundle_names = {r.data["name"] for r in bundles[0].rows}
    assert SUPPRESSED_NAME not in bundle_names
    # ...and never reaches resolver.ingest (the writer).
    committed_names = {r["name"] for r in committed}
    assert SUPPRESSED_NAME not in committed_names

    # (2) LOAD-BEARING CONTROL: the non-suppressed row IS included AND reaches ingest.
    assert KEPT_NAME in bundle_names
    assert KEPT_NAME in committed_names
    assert committed_names == {KEPT_NAME}
    assert bundle_names == {KEPT_NAME}


@pytest.mark.asyncio
async def test_find_path_without_suppression_ingests_both(monkeypatch):
    """CONTROL: with NOTHING suppressed, the SAME run ingests BOTH rows — proving
    the drop above is caused specifically by the suppression, not the harness."""
    neptune = PyoxiNeptune()  # empty suppression list
    bundles, committed = await _run_discovery(monkeypatch, neptune, _FakeProvider())

    committed_names = {r["name"] for r in committed}
    assert committed_names == {SUPPRESSED_NAME, KEPT_NAME}
    assert len(bundles) == 1
    bundle_names = {r.data["name"] for r in bundles[0].rows}
    assert bundle_names == {SUPPRESSED_NAME, KEPT_NAME}


# --------------------------------------------------------------------------- #
# 2. unit: an ENTITY mark and a (s, p, o) FACT mark never collide
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_entity_mark_and_fact_mark_are_independent():
    """``is_entity_suppressed`` is term- and KIND-faithful: suppressing a FACT
    ``(s, p, o)`` does not entity-suppress ``s``, and suppressing the ENTITY ``s``
    does not fact-suppress any ``(s, p, o)`` — the two live on distinct predicates
    and distinct mark nodes, so neither shadows the other."""
    neptune = PyoxiNeptune()
    subject = SUPPRESSED_SUBJECT
    predicate = "https://cograph.tech/onto/context_length"
    obj = "200000"

    # Suppress a FACT (s, p, o). It must NOT register as an ENTITY suppression.
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

    # Now suppress the ENTITY. It must NOT create a fact suppression, and must not
    # touch the fact mark above.
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
    # Term-faithful: a DIFFERENT subject is not entity-suppressed.
    assert await is_entity_suppressed(neptune, INSTANCE_GRAPH, KEPT_SUBJECT) is False
    # Kind-faithful: the entity mark did not mint a fact suppression for some (p, o).
    assert (
        await is_suppressed(
            neptune, INSTANCE_GRAPH, subject, "https://cograph.tech/onto/other", "x"
        )
        is False
    )
    # The original fact suppression still stands — the two are fully independent.
    assert await is_suppressed(neptune, INSTANCE_GRAPH, subject, predicate, obj) is True


@pytest.mark.asyncio
async def test_fetch_suppressed_entities_empty_and_no_graph():
    """A best-effort read: no marks → empty set; no target graph → empty set
    (never raises), so the FIND-path guard degrades to 'nothing suppressed'."""
    neptune = PyoxiNeptune()
    assert await fetch_suppressed_entities(neptune, INSTANCE_GRAPH) == set()
    assert await fetch_suppressed_entities(neptune, "") == set()
    assert await is_entity_suppressed(neptune, INSTANCE_GRAPH, SUPPRESSED_SUBJECT) is False

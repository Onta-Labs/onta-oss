"""GraphStore ingest-batch rollback (ONTA-528).

Hermetic: MemoryGraphStore only. Subjects stamped with a housekeeping
``onto/batch_id`` are removed via ``delete_facts``; other batch_ids stay.
Never SPARQL HTTP update / ``delete_batch_query``.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock

from infona_client.graph.iri import IRI_BASE
from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.queries import BATCH_PREDICATE
from infona_client.resolver.batch_rollback import rollback_ingest_batch
import infona_client.resolver.schema_ingest as schema_ingest_mod
import infona_client.resolver.schema_ingest_flush as schema_ingest_flush_mod
import infona_client.resolver.schema_resolver as schema_resolver_mod

GRAPH = f"{IRI_BASE}/graphs/t/kg/k"
OTHER_GRAPH = f"{IRI_BASE}/graphs/t/kg/other"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
WIDGET_TYPE = f"{IRI_BASE}/types/Widget"
PRICE_ATTR = f"{IRI_BASE}/types/Widget/attrs/price"


def _ids(store: MemoryGraphStore) -> set[str]:
    return {row["id"] for row in store.snapshot_entities()}


def _widget(raw_id: str, price: str, batch_id: str) -> list[tuple[str, str, str]]:
    uri = entity_uri("Widget", raw_id)
    return [
        (uri, RDF_TYPE, WIDGET_TYPE),
        (uri, PRICE_ATTR, price),
        (uri, BATCH_PREDICATE, batch_id),
    ]


def test_rollback_ingest_batch_deletes_matching_subjects_only():
    """Insert two batches; rolling back one leaves the other."""

    async def run():
        store = MemoryGraphStore()
        keep = entity_uri("Widget", "keep")
        gone = entity_uri("Widget", "gone")
        await insert_facts(
            None,
            GRAPH,
            _widget("gone", "1", "batch-a") + _widget("keep", "2", "batch-b"),
            store=store,
        )
        assert {keep, gone} <= _ids(store)

        neptune = AsyncMock()
        removed = await rollback_ingest_batch(
            GRAPH, "batch-a", store=store, neptune=neptune
        )

        assert removed > 0
        assert gone not in _ids(store)
        assert keep in _ids(store)
        assert _ids(store) == {keep}
        # Vestigial SPARQL client must not be used for the rollback write.
        neptune.update.assert_not_awaited()
        neptune.query.assert_not_awaited()
        leftover_batches = {
            (row["id"], (row.get("props") or {}).get("batch_id"))
            for row in store.snapshot_entities()
        }
        assert leftover_batches == {(keep, "batch-b")}
        assert all(
            a["subject_id"] != gone for a in store.snapshot_assertions()
        )

    asyncio.run(run())


def test_rollback_after_failed_later_step_clears_partial_writes():
    """Simulate ingest writing a batch then failing a later flush step."""

    async def run():
        store = MemoryGraphStore()
        first = entity_uri("Widget", "partial")
        other = entity_uri("Widget", "other")
        await insert_facts(
            None,
            GRAPH,
            _widget("partial", "9", "run-1") + _widget("other", "8", "run-2"),
            store=store,
        )
        try:
            raise RuntimeError("relationship flush failed")
        except RuntimeError:
            await rollback_ingest_batch(GRAPH, "run-1", store=store)
        assert first not in _ids(store)
        assert other in _ids(store)

    asyncio.run(run())


def test_rollback_ingest_batch_noop_on_unknown_or_empty():
    async def run():
        store = MemoryGraphStore()
        keep = entity_uri("Widget", "keep")
        await insert_facts(
            None, GRAPH, _widget("keep", "2", "batch-b"), store=store
        )
        assert await rollback_ingest_batch(GRAPH, "missing", store=store) == 0
        assert await rollback_ingest_batch(GRAPH, "", store=store) == 0
        assert _ids(store) == {keep}

    asyncio.run(run())


def test_rollback_does_not_touch_other_kg():
    async def run():
        store = MemoryGraphStore()
        a = entity_uri("Widget", "in-k")
        b = entity_uri("Widget", "in-other")
        await insert_facts(None, GRAPH, _widget("in-k", "1", "shared"), store=store)
        await insert_facts(
            None, OTHER_GRAPH, _widget("in-other", "1", "shared"), store=store
        )
        await rollback_ingest_batch(GRAPH, "shared", store=store)
        remaining = _ids(store)
        assert a not in remaining
        assert b in remaining

    asyncio.run(run())


def test_schema_ingest_except_blocks_call_rollback_not_skip():
    ingest_src = inspect.getsource(schema_ingest_mod)
    flush_src = inspect.getsource(schema_ingest_flush_mod)
    helper_src = inspect.getsource(schema_resolver_mod.rollback_ingest_batch)
    for src in (ingest_src, flush_src):
        assert "rollback_ingest_batch(" in src
        assert "batch_rollback_skipped" not in src
        assert "csv_batch_rollback_skipped" not in src
        assert "delete_batch_query" not in src
        assert "delete_batch not ported" not in src
        assert "_neptune.update" not in src
        assert "neptune.update" not in src
    assert "delete_facts(" in helper_src
    assert "delete_batch_query" not in helper_src
    assert "_neptune.update" not in helper_src

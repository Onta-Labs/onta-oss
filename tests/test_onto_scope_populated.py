"""Semantic top-K must not hide THIS-KG populated types in /ask planning.

Root cause (shop KG p4-82fb4e-wares): embeddings retrieved empty leftover
types (BenchIdentifier / KitIdentifier) from another ingest in the same
tenant. ``pipeline._ask_cypher`` passed those names as a hard
``ontology_from_graph_store(..., type_names=...)`` filter, which dropped
the live type (Product with ``list_price``). The model then invented
``prop_key=price`` → empty high-conf.

Mechanism under test (anti-overfit — synthetic types only):
  * resolve_planning_type_scope: semantic ranks; populated is force-include
  * zero overlap with kg_active_types → ignore semantic scope
  * ontology_from_graph_store never filters out entity_count > 0 types
  * pipeline timing / ask_process expose the three name lists

No persona CSV / Product / BenchIdentifier hardcodes in assertions.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from infona_client.graph.iri import IRI_BASE
from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_catalog import upsert_attribute, upsert_type
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.store import reset_graph_store_for_tests
from infona_client.nlp.cypher_generate import ontology_from_graph_store
from infona_client.nlp.pipeline import NLQueryPipeline
from infona_client.nlp.planning_schema import (
    DEFAULT_MAX_FORCE_POPULATED,
    resolve_planning_type_scope,
)

TENANT = "onto-scope-tenant"
KG = "onto-scope-kg"
GRAPH = f"{IRI_BASE}/graphs/{TENANT}/kg/{KG}"
TENANT_GRAPH = f"{IRI_BASE}/graphs/{TENANT}"

TYPE_WIDGET = "SynthWidget"
TYPE_GADGET = "SynthGadget"
TYPE_GHOST = "SynthGhost"  # declared empty leftover (semantic miss)

ATTR_PRICE = "list_price"
ATTR_COST = "unit_cost"
ATTR_GHOST = "ghost_code"

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"


# ---------------------------------------------------------------------------
# Pure helper
# ---------------------------------------------------------------------------


def test_resolve_scope_semantic_miss_uses_populated():
    """Semantic hits empty leftovers → ignore them; plan on live types."""
    scope = resolve_planning_type_scope(
        semantic_names=[TYPE_GHOST],
        populated_names=[TYPE_WIDGET],
    )
    assert scope.ignored_semantic is True
    assert TYPE_WIDGET in scope.force_include
    assert TYPE_WIDGET in (scope.type_names or ())
    assert TYPE_GHOST not in (scope.type_names or ())


def test_resolve_scope_overlap_keeps_populated():
    """Semantic overlap with live types still force-includes every populated."""
    scope = resolve_planning_type_scope(
        semantic_names=[TYPE_WIDGET],
        populated_names=[TYPE_WIDGET, TYPE_GADGET],
    )
    assert scope.ignored_semantic is False
    assert TYPE_WIDGET in scope.force_include
    assert TYPE_GADGET in scope.force_include
    names = scope.type_names or ()
    assert TYPE_WIDGET in names
    assert TYPE_GADGET in names


def test_resolve_scope_no_populated_falls_back_to_semantic():
    """Cold / empty KG: keep the existing semantic/catalog fallback."""
    scope = resolve_planning_type_scope(
        semantic_names=[TYPE_GHOST],
        populated_names=[],
    )
    assert scope.ignored_semantic is False
    assert scope.force_include == ()
    assert scope.type_names == (TYPE_GHOST,)


def test_resolve_scope_no_semantic_force_includes_populated():
    """Embeddings cold: no hard filter; still force-include live types."""
    scope = resolve_planning_type_scope(
        semantic_names=None,
        populated_names=[TYPE_WIDGET],
    )
    assert scope.type_names is None
    assert scope.force_include == (TYPE_WIDGET,)
    assert scope.ignored_semantic is False


def test_resolve_scope_caps_huge_populated_preferring_semantic_overlap():
    many = [f"SynthType{i:03d}" for i in range(DEFAULT_MAX_FORCE_POPULATED + 20)]
    hit = many[-1]
    scope = resolve_planning_type_scope(
        semantic_names=[hit],
        populated_names=many,
        max_populated=DEFAULT_MAX_FORCE_POPULATED,
    )
    assert len(scope.force_include) == DEFAULT_MAX_FORCE_POPULATED
    assert hit in scope.force_include


# ---------------------------------------------------------------------------
# GraphStore
# ---------------------------------------------------------------------------


async def _seed_widget_gadget_ghost(
    store: MemoryGraphStore, *, with_instances: bool = True
) -> None:
    await upsert_type(
        store=store,
        name=TYPE_WIDGET,
        description="synthetic widget",
        layer="tenant",
        tenant_id=TENANT,
    )
    await upsert_type(
        store=store,
        name=TYPE_GADGET,
        description="synthetic gadget",
        layer="tenant",
        tenant_id=TENANT,
    )
    await upsert_type(
        store=store,
        name=TYPE_GHOST,
        description="declared empty leftover type",
        layer="tenant",
        tenant_id=TENANT,
    )
    await upsert_attribute(
        store=store,
        type_name=TYPE_WIDGET,
        attr_name=ATTR_PRICE,
        datatype="float",
        layer="tenant",
        tenant_id=TENANT,
    )
    await upsert_attribute(
        store=store,
        type_name=TYPE_GADGET,
        attr_name=ATTR_COST,
        datatype="float",
        layer="tenant",
        tenant_id=TENANT,
    )
    await upsert_attribute(
        store=store,
        type_name=TYPE_GHOST,
        attr_name=ATTR_GHOST,
        datatype="string",
        layer="tenant",
        tenant_id=TENANT,
    )
    if not with_instances:
        return
    w1 = entity_uri(TYPE_WIDGET, "w1")
    g1 = entity_uri(TYPE_GADGET, "g1")
    triples = [
        (w1, RDF_TYPE, f"{IRI_BASE}/types/{TYPE_WIDGET}"),
        (w1, RDFS_LABEL, "Widget One"),
        (w1, f"{IRI_BASE}/types/{TYPE_WIDGET}/attrs/{ATTR_PRICE}", "8.5"),
        (g1, RDF_TYPE, f"{IRI_BASE}/types/{TYPE_GADGET}"),
        (g1, RDFS_LABEL, "Gadget One"),
        (g1, f"{IRI_BASE}/types/{TYPE_GADGET}/attrs/{ATTR_COST}", "12.0"),
    ]
    await insert_facts(None, GRAPH, triples, store=store)


@pytest.mark.asyncio
async def test_graphstore_semantic_miss_keeps_populated_and_list_price():
    """type_names=[SynthGhost] must still dump SynthWidget + list_price."""
    store = MemoryGraphStore()
    try:
        await _seed_widget_gadget_ghost(store)
        text, names = await ontology_from_graph_store(
            store,
            tenant_id=TENANT,
            kg=KG,
            prefer_populated=True,
            type_names=[TYPE_GHOST],
        )
        assert text, "expected planning ontology text"
        assert TYPE_WIDGET in names
        assert TYPE_WIDGET in text
        assert ATTR_PRICE in text
        # Must not be *only* the empty semantic hit.
        widget_idx = text.index(TYPE_WIDGET)
        ghost_only = TYPE_GHOST in text and TYPE_WIDGET not in text
        assert not ghost_only
        assert widget_idx >= 0
        # Empty leftover may appear after populated types, never as the only
        # schema — if present, the live type is listed first.
        if TYPE_GHOST in text:
            assert text.index(TYPE_WIDGET) < text.index(TYPE_GHOST)
    finally:
        await store.close()
        reset_graph_store_for_tests()


@pytest.mark.asyncio
async def test_graphstore_semantic_overlap_still_includes_populated():
    store = MemoryGraphStore()
    try:
        await _seed_widget_gadget_ghost(store)
        text, names = await ontology_from_graph_store(
            store,
            tenant_id=TENANT,
            kg=KG,
            prefer_populated=True,
            type_names=[TYPE_WIDGET],
        )
        assert TYPE_WIDGET in names
        assert TYPE_WIDGET in text
        assert ATTR_PRICE in text
        # Defense in depth: remaining live types stay visible.
        assert TYPE_GADGET in names
        assert ATTR_COST in text
    finally:
        await store.close()
        reset_graph_store_for_tests()


@pytest.mark.asyncio
async def test_graphstore_no_populated_keeps_semantic_fallback():
    """Empty KG: semantic/catalog scope still works (no live types to protect)."""
    store = MemoryGraphStore()
    try:
        await _seed_widget_gadget_ghost(store, with_instances=False)
        text, names = await ontology_from_graph_store(
            store,
            tenant_id=TENANT,
            kg=KG,
            prefer_populated=True,
            type_names=[TYPE_GHOST],
        )
        assert text
        assert TYPE_GHOST in names
        assert TYPE_GHOST in text
        assert ATTR_GHOST in text
        # No instances → the live-type union has nothing to add.
        assert TYPE_WIDGET not in names
    finally:
        await store.close()
        reset_graph_store_for_tests()


@pytest.mark.asyncio
async def test_graphstore_force_include_populated_when_caller_passes_it():
    """Pipeline contract: force_include=populated survives a semantic miss."""
    store = MemoryGraphStore()
    try:
        await _seed_widget_gadget_ghost(store)
        scope = resolve_planning_type_scope(
            semantic_names=[TYPE_GHOST],
            populated_names=[TYPE_WIDGET, TYPE_GADGET],
        )
        text, names = await ontology_from_graph_store(
            store,
            tenant_id=TENANT,
            kg=KG,
            prefer_populated=True,
            type_names=list(scope.type_names) if scope.type_names else None,
            force_include=list(scope.force_include),
        )
        assert TYPE_WIDGET in names
        assert ATTR_PRICE in text
        assert TYPE_GADGET in names
        assert ATTR_COST in text
    finally:
        await store.close()
        reset_graph_store_for_tests()


# ---------------------------------------------------------------------------
# Pipeline /ask
# ---------------------------------------------------------------------------


class _FakeEmbed:
    async def type_names(self, _graph_uri):
        return [TYPE_WIDGET, TYPE_GADGET, TYPE_GHOST]

    async def retrieve(self, _graph_uri, _question, top_k=8, active_types=None):
        # Reproduce the bug: retrieve returns only the empty leftover.
        return (
            f"Type: {TYPE_GHOST} [no instances]\n"
            f"  - {ATTR_GHOST}: string (literal, key={ATTR_GHOST})\n"
        )


@pytest.mark.asyncio
async def test_ask_pipeline_does_not_drop_populated_on_semantic_miss():
    """End-to-end: retrieve=SynthGhost must still prompt SynthWidget+list_price."""
    store = MemoryGraphStore()
    try:
        await _seed_widget_gadget_ghost(store)
        neptune = MagicMock()
        neptune.query = AsyncMock(side_effect=AssertionError("no sparql"))
        pipe = NLQueryPipeline(neptune, anthropic_key="", graph_store=store)

        captured: dict[str, str] = {}

        async def fake_llm(question: str, ontology: str, **kw):
            captured["ontology"] = ontology
            return {
                "cypher": (
                    "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
                    "WHERE e.primary_type IN $type_names "
                    "RETURN count(*) AS n"
                ),
                "params": {"type_names": [TYPE_WIDGET]},
                "explanation": "count live widgets",
                "functions_needed": [],
                "template": "entities_of_type_count",
            }

        pipe._try_llm_cypher = fake_llm  # type: ignore[method-assign]
        pipe._rephrase_via_openrouter = AsyncMock(return_value="")  # type: ignore[method-assign]
        pipe._fetch_ontology = AsyncMock(  # type: ignore[method-assign]
            side_effect=AssertionError("catalog path must not fall back to SPARQL")
        )

        async def fake_active(*_a, **_k):
            return {TYPE_WIDGET, TYPE_GADGET}

        with patch(
            "infona_client.nlp.pipeline.get_embedding_service",
            return_value=_FakeEmbed(),
        ):
            with patch.object(pipe, "_active_types", new=fake_active):
                result = await pipe.ask(
                    "Which records have price under 10?",
                    graph_uri=TENANT_GRAPH,
                    instance_graph=GRAPH,
                    use_cypher=True,
                )

        onto = result.ontology or captured.get("ontology") or ""
        assert TYPE_WIDGET in onto
        assert ATTR_PRICE in onto
        assert "Type: " + TYPE_GHOST in onto or TYPE_WIDGET in onto
        # Not *only* the empty semantic hit.
        assert not (
            TYPE_GHOST in onto
            and TYPE_WIDGET not in onto
            and ATTR_PRICE not in onto
        )

        timing = result.timing or {}
        assert TYPE_WIDGET in str(timing.get("ontology_type_names") or "")
        assert TYPE_GHOST in str(timing.get("semantic_type_names") or "")
        assert TYPE_WIDGET in str(timing.get("populated_type_names") or "")
        assert timing.get("ontology_semantic_ignored") == 1.0
    finally:
        await store.close()
        reset_graph_store_for_tests()

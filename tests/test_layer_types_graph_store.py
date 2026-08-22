"""ONTA-534: layer type resolve reads the catalog, not the retired SPARQL client.

``fetch_types_by_layer`` is what tells the Explorer whether a type name exists in
ANY visible layer. Every layer went through ``NeptuneClient.query``, which is
retired on the shipped Neo4j backend; the per-layer ``except`` degraded each one
to ``{}`` and the whole stack came back empty. ``_resolve_layered_type`` reads an
empty stack as "no visible layer declares this name" and returns ``None`` — so a
type that plainly exists resolved as *no such type*, with no error surfaced
anywhere.

These tests pin the catalog arm (real declared types out of a seeded
``MemoryGraphStore``, with SPARQL wired to explode), the residual SPARQL arm, and
the one decline that is load-bearing: a VERSION-PINNED global layer must degrade
to an empty layer rather than silently answering from the live catalog.

Anti-overfit: synthetic type names only.
"""

from __future__ import annotations

import pytest

from infona_client.graph.client import SparqlClientRetired
from infona_client.graph.layers import Layer, LayerStack, fetch_types_by_layer
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_catalog import upsert_type
from infona_client.graph.queries import tenant_graph_uri

pytestmark = pytest.mark.asyncio

TENANT = "layer-tenant"
TENANT_GRAPH = tenant_graph_uri(TENANT)

# Synthetic vocabulary — no warehouse / persona / benchmark nouns.
T_TENANT_ONLY = "LayerSprocket"
T_SHARED = "LayerFlange"
T_PUBLIC_ONLY = "LayerGrommet"


class RetiredSparqlNeptune:
    """Stands in for the shipped client: every SPARQL read is retired."""

    def __init__(self) -> None:
        self.calls = 0

    async def query(self, sparql: str):
        self.calls += 1
        raise SparqlClientRetired(
            "SPARQL HTTP client is retired under Neo4j GraphStore (ONTA-534)."
        )


class ScriptedNeptune:
    """Residual SPARQL arm: one ``list_types_query`` answer per call."""

    def __init__(self, per_call: list[list[str]]) -> None:
        self.per_call = list(per_call)
        self.calls = 0

    async def query(self, sparql: str):
        labels = self.per_call[self.calls] if self.calls < len(self.per_call) else []
        self.calls += 1
        return {
            "head": {"vars": ["type", "label", "comment", "parent"]},
            "results": {
                "bindings": [
                    {"label": {"type": "literal", "value": name}} for name in labels
                ]
            },
        }


async def _seed(store: MemoryGraphStore) -> None:
    """Two tenant-layer types and two public-layer types (one shadowed name)."""
    await upsert_type(
        name=T_TENANT_ONLY,
        description="tenant sprocket",
        store=store,
        layer="tenant",
        tenant_id=TENANT,
    )
    await upsert_type(
        name=T_SHARED,
        description="tenant flange",
        store=store,
        layer="tenant",
        tenant_id=TENANT,
    )
    await upsert_type(
        name=T_SHARED,
        description="public flange",
        store=store,
        layer="public",
        privileged=True,
    )
    await upsert_type(
        name=T_PUBLIC_ONLY,
        description="public grommet",
        store=store,
        layer="public",
        privileged=True,
    )


# --- Catalog arm ------------------------------------------------------------


async def test_layers_resolve_from_catalog_when_sparql_is_retired():
    """The regression: on ``main`` every layer came back ``{}`` → 'no such type'."""
    store = MemoryGraphStore()
    await _seed(store)
    neptune = RetiredSparqlNeptune()
    stack = LayerStack(TENANT_GRAPH, entitled=False)

    got = await fetch_types_by_layer(neptune, stack, store=store)

    assert got[Layer.TENANT] == {
        T_TENANT_ONLY: "tenant sprocket",
        T_SHARED: "tenant flange",
    }
    assert got[Layer.PUBLIC] == {
        T_SHARED: "public flange",
        T_PUBLIC_ONLY: "public grommet",
    }
    # A tenant-only type resolves; a public-only type resolves through the stack.
    assert stack.resolve_type(T_TENANT_ONLY, got) == (Layer.TENANT, "tenant sprocket")
    assert stack.resolve_type(T_PUBLIC_ONLY, got) == (Layer.PUBLIC, "public grommet")
    # Shadowing still works: the tenant definition wins for the shared name.
    assert stack.resolve_type(T_SHARED, got) == (Layer.TENANT, "tenant flange")
    assert stack.resolve_type("LayerNeverDeclared", got) is None


async def test_empty_tenant_layer_still_surfaces_public_types():
    """The empty-tenant + populated-Public case ``_resolve_layered_type`` exists for."""
    store = MemoryGraphStore()
    await upsert_type(
        name=T_PUBLIC_ONLY,
        description="public grommet",
        store=store,
        layer="public",
        privileged=True,
    )
    stack = LayerStack(TENANT_GRAPH, entitled=False)

    got = await fetch_types_by_layer(RetiredSparqlNeptune(), stack, store=store)

    assert got[Layer.TENANT] == {}
    assert stack.resolve_type(T_PUBLIC_ONLY, got) == (Layer.PUBLIC, "public grommet")


# --- Residual SPARQL arm ----------------------------------------------------


async def test_sparql_arm_still_answers_a_layer_the_catalog_cannot():
    """An empty catalog declines per layer; the authored SPARQL arm still runs."""
    store = MemoryGraphStore()
    neptune = ScriptedNeptune([[T_TENANT_ONLY], [T_PUBLIC_ONLY]])
    stack = LayerStack(TENANT_GRAPH, entitled=False)

    got = await fetch_types_by_layer(neptune, stack, store=store)

    assert got == {
        Layer.TENANT: {T_TENANT_ONLY: ""},
        Layer.PUBLIC: {T_PUBLIC_ONLY: ""},
    }
    assert neptune.calls == 2


# --- Preserved failure direction: a pinned layer never jumps to live --------


async def test_version_pinned_public_layer_degrades_to_empty_not_live():
    """A pin must not be answered from the LIVE catalog (pin stability, ONTA-405)."""
    store = MemoryGraphStore()
    await _seed(store)
    neptune = RetiredSparqlNeptune()
    stack = LayerStack(TENANT_GRAPH, entitled=False, public_version=3)

    got = await fetch_types_by_layer(neptune, stack, store=store)

    # Tenant is unpinned and reads the catalog; Public is pinned at a release
    # graph the catalog has no snapshot of, so it degrades to an empty layer.
    assert got[Layer.TENANT] == {
        T_TENANT_ONLY: "tenant sprocket",
        T_SHARED: "tenant flange",
    }
    assert got[Layer.PUBLIC] == {}
    assert stack.resolve_type(T_PUBLIC_ONLY, got) is None
    # The pinned layer fell through to the (retired) SPARQL arm — exactly once.
    assert neptune.calls == 1


async def test_untenanted_graph_uri_declines_the_catalog_arm():
    """A tenant graph URI that does not round-trip must not guess a workspace."""
    store = MemoryGraphStore()
    await _seed(store)
    neptune = ScriptedNeptune([[], []])
    stack = LayerStack("https://example.invalid/not-a-tenant-graph", entitled=False)

    got = await fetch_types_by_layer(neptune, stack, store=store)

    assert got[Layer.TENANT] == {}
    # Public is layer-scoped (no tenant needed), so it still reads the catalog.
    assert got[Layer.PUBLIC] == {
        T_SHARED: "public flange",
        T_PUBLIC_ONLY: "public grommet",
    }
    assert neptune.calls == 1

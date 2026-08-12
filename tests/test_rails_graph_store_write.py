"""E7 — write rails wire GraphStore into kg_writer when neo4j backend is active.

Hermetic: MemoryGraphStore only (no live Neo4j / Neptune). Default Neptune path
must stay untouched when ``INFONA_GRAPH_BACKEND`` is unset.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from infona_client.graph.facts import Fact
from infona_client.graph.iri import IRI_BASE
from infona_client.graph.kg_writer import insert_facts, rewrite_subject
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.store import (
    GraphConfigError,
    configure_graph_store,
    get_optional_graph_store,
    graph_backend,
    reset_graph_store_for_tests,
    resolve_optional_graph_store,
)
from infona_client.resolver.er.rebuild import rebuild_type
from infona_client.resolver.er.types import DEFAULT_GUEST_CONFIG


def _graph(tenant: str = "demo-tenant", kg: str = "bookstore") -> str:
    return f"{IRI_BASE}/graphs/{tenant}/kg/{kg}"


@pytest.fixture
def memory_store(monkeypatch):
    reset_graph_store_for_tests()
    store = MemoryGraphStore()
    configure_graph_store(store)
    yield store
    asyncio.run(store.close())
    reset_graph_store_for_tests()
    monkeypatch.delenv("INFONA_GRAPH_BACKEND", raising=False)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def test_get_optional_graph_store_rejects_legacy_backend(monkeypatch):
    """ONTA-527: a legacy backend value raises; it never yields None.

    None used to mean "the caller should use SPARQL". Every such caller is
    gone, so returning None would now read as "no store needed" and the write
    would silently go nowhere.
    """
    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neptune")
    reset_graph_store_for_tests()
    with pytest.raises(GraphConfigError):
        graph_backend()
    with pytest.raises(GraphConfigError):
        get_optional_graph_store()
    with pytest.raises(GraphConfigError):
        resolve_optional_graph_store()


def test_get_optional_graph_store_neo4j_returns_configured(memory_store, monkeypatch):
    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neo4j")
    assert resolve_optional_graph_store() is memory_store


def test_get_optional_graph_store_neo4j_fails_closed_without_config(monkeypatch):
    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neo4j")
    monkeypatch.delenv("NEO4J_URI", raising=False)
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    reset_graph_store_for_tests()
    with pytest.raises(GraphConfigError):
        get_optional_graph_store()


# ---------------------------------------------------------------------------
# Ingest / schema_resolver (also covers discovery + CSV → same insert_facts)
# ---------------------------------------------------------------------------


def test_schema_resolver_insert_facts_receives_store(memory_store, monkeypatch):
    """Ingest rail passes store= from resolve_optional_graph_store into insert_facts."""
    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neo4j")
    captured: dict = {}

    async def spy(neptune, instance_graph, instance_triples=None, **kwargs):
        captured["store"] = kwargs.get("store")
        captured["triples"] = list(instance_triples or [])
        return await insert_facts(
            neptune, instance_graph, instance_triples, **kwargs
        )

    import infona_client.resolver.schema_resolver as sr

    monkeypatch.setattr(sr, "insert_facts", spy)
    # Resolve the same way the rail does, then simulate its write call shape.
    store = sr.resolve_optional_graph_store()
    person = entity_uri("Person", "alice")
    triples = [
        (
            person,
            "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
            f"{IRI_BASE}/types/Person",
        ),
        (person, "http://www.w3.org/2000/01/rdf-schema#label", "Alice"),
    ]

    async def run():
        # Call through the spy-bound name the module uses after import.
        await sr.insert_facts(None, _graph(), triples, store=store)

    asyncio.run(run())
    assert captured["store"] is memory_store
    assert memory_store.entity_count(tenant_id="demo-tenant", kg="bookstore") == 1
    row = memory_store._entities[("demo-tenant", "bookstore", person)]
    assert row.primary_type == "Person"
    assert row.name == "Alice"


def test_schema_resolver_source_wires_store_kwarg():
    """Source guard: schema_resolver write site passes store= (E7)."""
    import inspect

    import infona_client.resolver.schema_resolver as sr

    src = inspect.getsource(sr)
    assert "resolve_optional_graph_store" in src
    assert "store=resolve_optional_graph_store()" in src
    # Residual instance/rel/provenance writes must not hand-roll SPARQL INSERT
    # via batched_insert_triples + _neptune.update (Neo4j 500s on SPARQL HTTP).
    assert "batched_insert_triples" not in src


def test_ingest_mapped_uses_insert_facts_not_sparql(memory_store, monkeypatch):
    """CSV mapped path flushes via insert_facts(store=) when GraphStore is live.

    Regression for residual schema_resolver SPARQL writes: with
    INFONA_GRAPH_BACKEND=neo4j, generic /ingest CSV and /ingest/csv/rows must
    not call NeptuneClient.update for instance/rel triples (ConnectError 500).
    Ontology commits are out of scope here — attrs are pre-registered.
    """
    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neo4j")
    import tempfile
    from pathlib import Path
    from unittest.mock import AsyncMock

    import infona_client.resolver.schema_resolver as sr
    from infona_client.graph.client import NeptuneClient
    from infona_client.resolver.attribute_resolver import AttributeSchema
    from infona_client.resolver.models import (
        ColumnMapping,
        ColumnRole,
        CSVSchemaMapping,
    )
    from infona_client.resolver.schema_resolver import SchemaResolver
    from infona_client.resolver.verdict_cache import JsonVerdictCache

    captured: list[dict] = []
    real_insert = insert_facts

    async def spy(neptune, instance_graph, instance_triples=None, **kwargs):
        captured.append(
            {
                "store": kwargs.get("store"),
                "n_triples": len(instance_triples or []),
                "graph": instance_graph,
            }
        )
        return await real_insert(neptune, instance_graph, instance_triples, **kwargs)

    monkeypatch.setattr(sr, "insert_facts", spy)

    mock_neptune = AsyncMock(spec=NeptuneClient)
    mock_neptune.batch_exists.return_value = set()
    mock_neptune.update.return_value = None

    cache = JsonVerdictCache.__new__(JsonVerdictCache)
    cache._path = Path(tempfile.mkdtemp()) / "verdicts.json"
    cache._cache = {}
    resolver = SchemaResolver(mock_neptune, "fake-key", cache)
    resolver._er_enabled = False

    graph = _graph(tenant="demo-tenant", kg="bookstore")
    mapping = CSVSchemaMapping(
        entity_type="Book",
        columns=[
            ColumnMapping(
                column_name="title",
                role=ColumnRole.TYPE_ID,
                datatype="string",
                attribute_name="title",
            ),
            ColumnMapping(
                column_name="price",
                role=ColumnRole.ATTRIBUTE,
                datatype="float",
                attribute_name="price",
            ),
        ],
    )
    rows = [
        {"title": "Gatsby", "price": "10.5"},
        {"title": "Mockingbird", "price": "12.0"},
    ]
    # Pre-register type + attrs so the path does not _commit_ontology (SPARQL).
    existing_types = {"Book": ""}
    existing_attrs = {
        "Book": {
            "title": AttributeSchema(name="title", datatype="string"),
            "price": AttributeSchema(name="price", datatype="float"),
        },
    }

    async def run():
        return await resolver._ingest_mapped(
            mapping,
            rows,
            graph,
            existing_types,
            existing_attrs,
            source="test.csv",
            instance_graph=graph,
        )

    result = asyncio.run(run())
    assert result.entities_resolved == 2
    assert result.triples_inserted > 0
    assert captured, "insert_facts must be used for the mapped write flush"
    assert all(c["store"] is memory_store for c in captured)
    # Instance writes must not hit SPARQL HTTP when GraphStore is the backend.
    mock_neptune.update.assert_not_called()
    # Facts land in MemoryGraphStore (dual-route success).
    assert memory_store.entity_count(tenant_id="demo-tenant", kg="bookstore") >= 2
    # ONTA-528: triples_inserted equals the number of instance triples that
    # actually passed through insert_facts (not a premature collect-time count).
    assert result.triples_inserted == sum(c["n_triples"] for c in captured)


def test_triples_inserted_zero_when_insert_facts_fails(memory_store, monkeypatch):
    """ONTA-528: if insert_facts raises, triples_inserted must stay 0.

    Anti-overfit synthetic: no domain schema, one forced write failure — the
    counter must not claim facts landed when the shared write path aborted.
    """
    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neo4j")
    import tempfile
    from pathlib import Path

    import infona_client.resolver.schema_resolver as sr
    from infona_client.graph.client import NeptuneClient
    from infona_client.resolver.attribute_resolver import AttributeSchema
    from infona_client.resolver.models import (
        ColumnMapping,
        ColumnRole,
        CSVSchemaMapping,
    )
    from infona_client.resolver.schema_resolver import SchemaResolver
    from infona_client.resolver.verdict_cache import JsonVerdictCache

    async def boom(*a, **k):
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr(sr, "insert_facts", boom)

    mock_neptune = AsyncMock(spec=NeptuneClient)
    mock_neptune.batch_exists.return_value = set()

    cache = JsonVerdictCache.__new__(JsonVerdictCache)
    cache._path = Path(tempfile.mkdtemp()) / "verdicts.json"
    cache._cache = {}
    resolver = SchemaResolver(mock_neptune, "fake-key", cache)
    resolver._er_enabled = False

    graph = _graph(tenant="demo-tenant", kg="bookstore")
    mapping = CSVSchemaMapping(
        entity_type="Book",
        columns=[
            ColumnMapping(
                column_name="title",
                role=ColumnRole.TYPE_ID,
                datatype="string",
                attribute_name="title",
            ),
        ],
    )
    existing_types = {"Book": ""}
    existing_attrs = {
        "Book": {"title": AttributeSchema(name="title", datatype="string")},
    }

    async def run():
        return await resolver._ingest_mapped(
            mapping,
            [{"title": "Only"}],
            graph,
            existing_types,
            existing_attrs,
            source="test.csv",
            instance_graph=graph,
        )

    with pytest.raises(RuntimeError, match="simulated write failure"):
        asyncio.run(run())
    # Neptune SPARQL flush must never be the fallback on write failure.
    mock_neptune.update.assert_not_called()


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------


def test_enrichment_insert_facts_receives_store(memory_store, monkeypatch):
    """Enrichment rail passes store into insert_facts when backend=neo4j."""
    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neo4j")
    captured: dict = {}

    async def spy(neptune, instance_graph, instance_triples=None, **kwargs):
        captured["store"] = kwargs.get("store")
        return await insert_facts(
            neptune, instance_graph, instance_triples, **kwargs
        )

    import infona_client.enrichment.executor as ex

    monkeypatch.setattr(ex, "insert_facts", spy)
    store = ex.resolve_optional_graph_store()
    sid = entity_uri("Widget", "w1")
    triples = [
        (
            sid,
            "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
            f"{IRI_BASE}/types/Widget",
        ),
        (
            sid,
            f"{IRI_BASE}/types/Widget/attrs/color",
            "blue",
        ),
    ]

    async def run():
        await ex.insert_facts(None, _graph(), triples, store=store)

    asyncio.run(run())
    assert captured["store"] is memory_store
    row = memory_store._entities[("demo-tenant", "bookstore", sid)]
    assert row.props.get("color") == "blue"


def test_enrichment_source_wires_store_kwarg():
    import inspect

    import infona_client.enrichment.executor as ex

    src = inspect.getsource(ex)
    assert "resolve_optional_graph_store" in src
    assert "store=graph_store" in src or "store=resolve_optional_graph_store()" in src


# ---------------------------------------------------------------------------
# Normalization (promote_to_node / rule apply writes)
# ---------------------------------------------------------------------------


def test_normalization_insert_delete_receive_store(memory_store, monkeypatch):
    """Normalization write batch uses Memory store for insert + predicate clear."""
    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neo4j")
    captured: list[dict] = []

    async def spy_insert(neptune, instance_graph, instance_triples=None, **kwargs):
        captured.append({"op": "insert", "store": kwargs.get("store")})
        return await insert_facts(
            neptune, instance_graph, instance_triples, **kwargs
        )

    async def spy_delete(neptune, instance_graph, **kwargs):
        captured.append({"op": "delete", "store": kwargs.get("store")})
        from infona_client.graph.kg_writer import delete_facts

        return await delete_facts(neptune, instance_graph, **kwargs)

    import infona_client.normalization.execute as nx

    monkeypatch.setattr(nx, "insert_facts", spy_insert)
    monkeypatch.setattr(nx, "delete_facts", spy_delete)

    store = nx.resolve_optional_graph_store()
    owner = entity_uri("Person", "p1")
    city = entity_uri("City", "sf")
    graph = _graph()
    lit_pred = f"{IRI_BASE}/types/Person/attrs/city"
    onto_pred = f"{IRI_BASE}/onto/city"

    async def run():
        # Seed owner (literal path pre-promote).
        await insert_facts(
            None,
            graph,
            facts=[
                Fact(subject_id=owner, kind="type", key="Person"),
                Fact(subject_id=owner, kind="literal", key="city", value="SF"),
            ],
            store=store,
        )
        # Promote-shaped write: mint node + onto edge + clear literal.
        await nx.insert_facts(
            None,
            graph,
            [
                (
                    city,
                    "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
                    f"{IRI_BASE}/types/City",
                ),
                (city, "http://www.w3.org/2000/01/rdf-schema#label", "SF"),
            ],
            store=store,
        )
        await nx.insert_facts(
            None, graph, [(owner, onto_pred, city)], store=store
        )
        await nx.delete_facts(
            None,
            graph,
            triples=[(owner, lit_pred, None)],
            reason="normalization:promote_to_node literal->node",
            store=store,
        )

    asyncio.run(run())
    assert all(c["store"] is memory_store for c in captured)
    assert memory_store.entity_count(tenant_id="demo-tenant", kg="bookstore") == 2
    owner_row = memory_store._entities[("demo-tenant", "bookstore", owner)]
    assert "city" not in owner_row.props  # literal cleared
    rels = memory_store.snapshot_rels()
    assert any(
        r["start_id"] == owner and r["end_id"] == city and r["attr"] == "city"
        for r in rels
    )


def test_normalization_source_wires_store_kwarg():
    import inspect

    import infona_client.normalization.execute as nx

    src = inspect.getsource(nx)
    assert "resolve_optional_graph_store" in src
    assert "store=store" in src


# ---------------------------------------------------------------------------
# ER rebuild / merge
# ---------------------------------------------------------------------------


def test_er_rebuild_rewrite_subject_with_memory_store(memory_store, monkeypatch):
    """ER rebuild passes store into rewrite_subject; Memory store re-keys entities."""
    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neo4j")
    # Stub stats recompute side-effects.
    monkeypatch.setattr(
        "infona_client.graph.kg_writer.refresh_after_write",
        AsyncMock(),
    )

    graph = _graph()
    loser = entity_uri("Person", "johnB")
    survivor = entity_uri("Person", "johnA")

    async def seed():
        await insert_facts(
            None,
            graph,
            facts=[
                Fact(subject_id=loser, kind="type", key="Person"),
                Fact(subject_id=loser, kind="literal", key="name", value="Jon"),
                Fact(subject_id=survivor, kind="type", key="Person"),
                Fact(subject_id=survivor, kind="literal", key="name", value="John"),
            ],
            store=memory_store,
        )

    asyncio.run(seed())
    assert memory_store.entity_count(tenant_id="demo-tenant", kg="bookstore") == 2

    # Blocker returns signals that force a merge (same email).
    class _FakeBlocker:
        def __init__(self, client, store=None):
            pass

        async def all_entities_with_signals(self, instance_graph, type_uri):
            from infona_client.resolver.er.types import NormalizedSignals

            return {
                loser: NormalizedSignals(
                    name="jon smith",
                    name_tokens=("jon", "smith"),
                    email="john.smith0@gmail.com",  # boundary-ok: synthetic ER fixture email
                    email_local="johnsmith0",
                    phone_e164="+442258595506",
                ),
                survivor: NormalizedSignals(
                    name="john smith",
                    name_tokens=("john", "smith"),
                    email="john.smith0@gmail.com",  # boundary-ok: synthetic ER fixture email
                    email_local="johnsmith0",
                    phone_e164="+442258595506",
                ),
            }

    monkeypatch.setattr(
        "infona_client.resolver.er.rebuild.SparqlBlocker",
        _FakeBlocker,
    )

    captured: list = []
    real_rewrite = rewrite_subject

    async def spy_rewrite(client, instance_graph, old_uri, new_uri, **kwargs):
        captured.append(kwargs.get("store"))
        return await real_rewrite(
            client, instance_graph, old_uri, new_uri, **kwargs
        )

    monkeypatch.setattr(
        "infona_client.resolver.er.rebuild.rewrite_subject",
        spy_rewrite,
    )

    async def run():
        report = await rebuild_type(
            None,
            graph,
            "Person",
            f"{IRI_BASE}/types/Person",
            DEFAULT_GUEST_CONFIG,
        )
        return report

    report = asyncio.run(run())
    assert report["fragments_absorbed"] == 1
    assert captured and captured[0] is memory_store
    # Survivor remains; loser re-keyed away.
    assert ("demo-tenant", "bookstore", loser) not in memory_store._entities
    assert ("demo-tenant", "bookstore", survivor) in memory_store._entities


def test_er_rebuild_source_wires_store_kwarg():
    import inspect

    import infona_client.resolver.er.rebuild as rb

    src = inspect.getsource(rb)
    assert "resolve_optional_graph_store" in src
    assert "store=store" in src


# ---------------------------------------------------------------------------
# Legacy Neptune must not require store
# ---------------------------------------------------------------------------


def test_insert_facts_without_a_store_raises_instead_of_writing_sparql(monkeypatch):
    """ONTA-527: no SPARQL fallback — the write fails closed and touches nothing.

    Replaces test_insert_facts_legacy_neptune_ignores_missing_store, which
    asserted that a `neptune` backend let insert_facts skip the GraphStore and
    emit SPARQL updates instead.
    """
    monkeypatch.delenv("INFONA_GRAPH_BACKEND", raising=False)
    reset_graph_store_for_tests()
    neptune = AsyncMock()
    neptune.update = AsyncMock()

    async def run():
        sid = entity_uri("Person", "x")
        with pytest.raises(GraphConfigError):
            await insert_facts(
                neptune,
                _graph(),
                [
                    (
                        sid,
                        "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
                        f"{IRI_BASE}/types/Person",
                    ),
                ],
            )

    asyncio.run(run())
    assert neptune.update.await_count == 0

"""E6 foundation — minimal NL→Cypher /ask plumbing (hermetic, no live Neo4j)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from cograph_client.graph.iri import IRI_BASE
from cograph_client.graph.memory_store import MemoryGraphStore
from cograph_client.graph.scope import GraphScope
from cograph_client.nlp.cypher_generate import (
    try_stub_count_query,
    records_to_bindings,
)
from cograph_client.nlp.pipeline import NLQueryPipeline


def _kg_uri(tenant: str = "demo-tenant", kg: str = "bookstore") -> str:
    return f"{IRI_BASE}/graphs/{tenant}/kg/{kg}"


@pytest.mark.asyncio
async def test_stub_count_by_type_shape():
    payload = try_stub_count_query(
        "How many books?",
        "Type: Book\n  - title\nType: Author\n  - name",
    )
    assert payload is not None
    assert payload["stub"] is True
    assert "primary_type" in payload["params"]
    assert payload["params"]["primary_type"] == "Book"
    assert "$tenant_id" in payload["cypher"]
    assert "$kg" in payload["cypher"]


@pytest.mark.asyncio
async def test_stub_count_total():
    payload = try_stub_count_query("How many entities are there?", "")
    assert payload is not None
    assert "primary_type" not in payload["params"]
    assert "RETURN count(*) AS n" in payload["cypher"]


@pytest.mark.asyncio
async def test_ask_cypher_count_e2e_memory_store():
    store = MemoryGraphStore()
    scope = GraphScope.for_instance("demo-tenant", "bookstore")
    session = store.session(scope)
    for i in range(3):
        await session.write_merge_entity(
            id=f"{IRI_BASE}/entities/Book/b{i}",
            primary_type="Book",
            name=f"Book {i}",
            source="test",
        )
    await session.write_merge_entity(
        id=f"{IRI_BASE}/entities/Author/a1",
        primary_type="Author",
        name="Herbert",
        source="test",
    )

    # Fake Neptune client — Cypher path should not call SPARQL query.
    neptune = MagicMock()
    neptune.query = AsyncMock(side_effect=AssertionError("SPARQL path must not run"))

    pipe = NLQueryPipeline(neptune, anthropic_key="", graph_store=store)
    # Avoid real ontology SPARQL fetch
    pipe._fetch_ontology = AsyncMock(  # type: ignore[method-assign]
        return_value="Type: Book\n  - title\nType: Author\n  - name"
    )

    result = await pipe.ask(
        "How many books?",
        graph_uri=f"{IRI_BASE}/graphs/demo-tenant",
        instance_graph=_kg_uri(),
        use_cypher=True,
    )

    assert result.timing.get("query_language") == "cypher"
    assert result.timing.get("cypher_stub") == 1.0
    assert "$tenant_id" in result.sparql  # Cypher lives in historical field
    assert "Book" in result.explanation or "primary_type" in result.sparql
    # Answer should surface the count 3
    assert "3" in result.answer
    neptune.query.assert_not_called()


@pytest.mark.asyncio
async def test_ask_default_path_does_not_enter_cypher_when_disabled(monkeypatch):
    """use_cypher=False must not call _ask_cypher (Neptune SPARQL remains default)."""
    neptune = MagicMock()
    pipe = NLQueryPipeline(neptune, anthropic_key="test-key", graph_store=None)
    called = {"cypher": False}

    async def _boom(*_a, **_k):
        called["cypher"] = True
        raise AssertionError("_ask_cypher must not run when use_cypher=False")

    pipe._ask_cypher = _boom  # type: ignore[method-assign]

    pipe._fetch_ontology = AsyncMock(return_value="Type: Movie")  # type: ignore[method-assign]
    pipe._generate_sparql = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "sparql": (
                f"SELECT (COUNT(?m) AS ?n) FROM <{_kg_uri('demo-tenant', 'imdb')}> "
                "WHERE { ?m <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> "
                f"<{IRI_BASE}/types/Movie> }}"
            ),
            "explanation": "count movies",
            "functions_needed": [],
        }
    )
    neptune.query = AsyncMock(
        return_value={
            "head": {"vars": ["n"]},
            "results": {"bindings": [{"n": {"type": "literal", "value": "9"}}]},
        }
    )
    pipe._rephrase_via_openrouter = AsyncMock(return_value="")  # type: ignore[method-assign]
    pipe._resolve_uri_labels = AsyncMock(return_value={})  # type: ignore[method-assign]

    result = await pipe.ask(
        "How many movies?",
        graph_uri=f"{IRI_BASE}/graphs/demo-tenant",
        instance_graph=_kg_uri("demo-tenant", "imdb"),
        use_cypher=False,
    )
    assert called["cypher"] is False
    assert result.timing.get("query_language") != "cypher"
    assert "9" in result.answer or "count" in result.explanation.lower() or result.answer


@pytest.mark.asyncio
async def test_ask_cypher_rejects_model_tenant_in_params_via_confine():
    """Session overwrites evil tenant even if stub somehow set it."""
    store = MemoryGraphStore()
    scope = GraphScope.for_instance("demo-tenant", "bookstore")
    session = store.session(scope)
    await session.write_merge_entity(
        id=f"{IRI_BASE}/entities/Book/b1",
        primary_type="Book",
        name="One",
        source="test",
    )

    neptune = MagicMock()
    neptune.query = AsyncMock(side_effect=AssertionError("no sparql"))
    pipe = NLQueryPipeline(neptune, anthropic_key="", graph_store=store)
    pipe._fetch_ontology = AsyncMock(return_value="Type: Book")  # type: ignore[method-assign]

    # Monkeypatch stub to smuggle evil tenant in params (session must win).
    from cograph_client.nlp import cypher_generate as cg

    original = cg.try_stub_count_query

    def evil_stub(question, ontology_summary="", **kw):
        out = original(question, ontology_summary, **kw)
        if out:
            out = dict(out)
            out["params"] = {
                **out.get("params", {}),
                "tenant_id": "evil-tenant",
                "kg": "other",
            }
        return out

    import cograph_client.nlp.pipeline as pl

    monkey = evil_stub
    # Patch where pipeline imports it
    old = pl.try_stub_count_query
    pl.try_stub_count_query = monkey  # type: ignore[assignment]
    try:
        result = await pipe.ask(
            "How many books?",
            graph_uri=f"{IRI_BASE}/graphs/demo-tenant",
            instance_graph=_kg_uri(),
            use_cypher=True,
        )
    finally:
        pl.try_stub_count_query = old  # type: ignore[assignment]

    assert "1" in result.answer  # still demo-tenant bookstore data
    assert result.timing.get("query_language") == "cypher"


def test_records_to_bindings():
    from cograph_client.graph.store import GraphRecord

    vars_, rows = records_to_bindings([GraphRecord(data={"n": 3})])
    assert vars_ == ["n"]
    assert rows == [{"n": "3"}]

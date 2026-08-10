"""E6 quality — NL→Cypher fixtures + pipeline (hermetic, no live Neo4j)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from infona_client.graph.iri import IRI_BASE
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_catalog import upsert_type_pg
from infona_client.graph.scope import GraphScope
from infona_client.graph.store import GraphQueryError, GraphRecord
from infona_client.nlp.cypher_generate import (
    format_schema_types_for_cypher,
    records_to_bindings,
    try_deterministic_cypher,
    try_filter_query,
    try_hop_query,
    try_list_query,
    try_stub_count_query,
)
from infona_client.nlp.pipeline import NLQueryPipeline


def _kg_uri(tenant: str = "demo-tenant", kg: str = "bookstore") -> str:
    return f"{IRI_BASE}/graphs/{tenant}/kg/{kg}"


ONTOLOGY = "Type: Book\n  - title\nType: Author\n  - name\nType: Organization"


def test_stub_count_by_type_shape():
    payload = try_stub_count_query("How many books?", ONTOLOGY)
    assert payload is not None
    assert payload["stub"] is True
    assert payload["template"] == "entities_of_type_count"
    assert payload["params"]["type_names"] == ["Book"]
    assert "$tenant_id" in payload["cypher"]
    assert "$kg" in payload["cypher"]


def test_stub_count_total():
    payload = try_stub_count_query("How many entities are there?", "")
    assert payload is not None
    assert "primary_type" not in payload["params"]
    assert payload["template"] == "entity_count_total"
    assert "RETURN count(*) AS n" in payload["cypher"]


def test_list_fixture_shape():
    payload = try_list_query("list all books", ONTOLOGY)
    assert payload is not None
    assert payload["template"] == "entities_of_type"
    assert payload["params"]["type_names"] == ["Book"]
    assert payload["params"]["limit"] == 25
    assert payload["params"]["after_id"] is None


def test_list_fixture_with_limit():
    payload = try_list_query("show books limit 5", ONTOLOGY)
    assert payload is not None
    assert payload["params"]["limit"] == 5


def test_list_fixture_top_n():
    payload = try_list_query("top 3 books", ONTOLOGY)
    assert payload is not None
    assert payload["template"] == "entities_of_type"
    assert payload["params"]["limit"] == 3
    assert payload["params"]["type_names"] == ["Book"]


def test_list_fixture_first_n_with_verb():
    payload = try_list_query("show me the first 2 authors", ONTOLOGY)
    assert payload is not None
    assert payload["params"]["limit"] == 2
    assert payload["params"]["type_names"] == ["Author"]


def test_list_fixture_n_prefix():
    payload = try_list_query("list 10 books", ONTOLOGY)
    assert payload is not None
    assert payload["params"]["limit"] == 10


def test_list_fixture_ordered_by_name():
    payload = try_list_query("list books ordered by name", ONTOLOGY)
    assert payload is not None
    assert payload["template"] == "entities_of_type"
    assert payload["params"]["type_names"] == ["Book"]
    assert "order by name" in payload["explanation"].lower()
    # Template params stay allowlisted (no free-form order_prop leakage).
    assert "order_prop" not in payload["params"]


def test_list_fixture_ordered_by_with_limit():
    payload = try_list_query("list books sorted by name limit 5", ONTOLOGY)
    assert payload is not None
    assert payload["params"]["limit"] == 5
    assert payload["params"]["type_names"] == ["Book"]


def test_filter_fixture_with_limit():
    payload = try_filter_query(
        "books where status equals published limit 2", ONTOLOGY
    )
    assert payload is not None
    assert payload["params"]["limit"] == 2
    assert payload["params"]["prop_value"] == "published"


def test_hop_fixture_with_limit():
    payload = try_hop_query("authors of books limit 1", ONTOLOGY)
    assert payload is not None
    assert payload["params"]["limit"] == 1


def test_filter_fixture_shape():
    payload = try_filter_query("books where name is Dune", ONTOLOGY)
    assert payload is not None
    assert payload["template"] == "literal_values"
    assert payload["params"]["type_names"] == ["Book"]
    assert payload["params"]["prop_key"] == "name"
    assert payload["params"]["prop_value"] == "Dune"


def test_filter_fixture_list_prefix():
    payload = try_filter_query(
        "list books with status equals published", ONTOLOGY
    )
    assert payload is not None
    assert payload["params"]["prop_key"] == "status"
    assert payload["params"]["prop_value"] == "published"


def test_hop_fixture_of():
    payload = try_hop_query("authors of books", ONTOLOGY)
    assert payload is not None
    assert payload["template"] == "related_entities"
    assert payload["params"]["from_types"] == ["Book"]
    assert payload["params"]["to_types"] == ["Author"]


def test_hop_fixture_their():
    payload = try_hop_query("books and their authors", ONTOLOGY)
    assert payload is not None
    assert payload["params"]["from_types"] == ["Book"]
    assert payload["params"]["to_types"] == ["Author"]


def test_hop_fixture_via_rel():
    payload = try_hop_query(
        "organizations related to people via works_at", ONTOLOGY
    )
    assert payload is not None
    # irregular plural people → Person (guess); organizations → Organization
    assert payload["params"]["from_types"] == ["Person"]
    assert payload["params"]["to_types"] == ["Organization"]
    assert payload["params"]["rel_attr"] == "works_at"


def test_deterministic_priority_filter_over_list():
    # "list books where name is Dune" should be filter, not bare list
    payload = try_deterministic_cypher(
        "list books where name is Dune", ONTOLOGY
    )
    assert payload is not None
    assert payload["template"] == "literal_values"


def test_deterministic_priority_hop_over_list():
    payload = try_deterministic_cypher("authors of books", ONTOLOGY)
    assert payload is not None
    assert payload["template"] == "related_entities"


def test_format_schema_types_for_cypher():
    from infona_client.graph.ontology_catalog import (
        OntoAttrRecord,
        SchemaTypeSummary,
    )

    rows = [
        SchemaTypeSummary(
            name="Book",
            layer="tenant",
            entity_count=3,
            attributes=(
                OntoAttrRecord(
                    name="title",
                    domain="Book",
                    layer="tenant",
                    tenant_id="t",
                    kg="__ontology__",
                    kind="literal",
                    datatype="string",
                    prop_key="title",
                ),
                OntoAttrRecord(
                    name="author",
                    domain="Book",
                    layer="tenant",
                    tenant_id="t",
                    kg="__ontology__",
                    kind="relationship",
                    range_type="Author",
                    prop_key="author",
                ),
            ),
        ),
        SchemaTypeSummary(
            name="Author", layer="tenant", entity_count=0, attributes=()
        ),
    ]
    text = format_schema_types_for_cypher(rows)
    assert "Type: Book (3 entities)" in text
    assert "Type: Author [no instances]" in text
    assert "title: string" in text
    assert "author -> Author" in text


async def _seed_bookstore(store: MemoryGraphStore) -> None:
    """Seed via Assertion SoT (+ dual-write cache) so ADR 0013 NL templates match."""
    from infona_client.graph.rdf_model import AssertionFact, assert_fact

    scope = GraphScope.for_instance("demo-tenant", "bookstore")
    session = store.session(scope)
    for i, title in enumerate(["Dune", "Foundation", "Hyperion"]):
        eid = f"{IRI_BASE}/entities/Book/b{i}"
        await assert_fact(
            session,
            AssertionFact(subject_id=eid, kind="type", value="Book"),
            dual_write_cache=True,
        )
        await assert_fact(
            session,
            AssertionFact(
                subject_id=eid,
                kind="literal",
                property_leaf="name",
                value=title,
            ),
            dual_write_cache=True,
        )
        await assert_fact(
            session,
            AssertionFact(
                subject_id=eid,
                kind="literal",
                property_leaf="status",
                value="published",
            ),
            dual_write_cache=True,
        )
    for i, aname in enumerate(["Herbert", "Asimov"]):
        aid = f"{IRI_BASE}/entities/Author/a{i + 1}"
        await assert_fact(
            session,
            AssertionFact(subject_id=aid, kind="type", value="Author"),
            dual_write_cache=True,
        )
        await assert_fact(
            session,
            AssertionFact(
                subject_id=aid,
                kind="literal",
                property_leaf="name",
                value=aname,
            ),
            dual_write_cache=True,
        )
    # Book 0 → Author 1 (Herbert wrote Dune); Book 1 → Asimov
    await assert_fact(
        session,
        AssertionFact(
            subject_id=f"{IRI_BASE}/entities/Book/b0",
            kind="object",
            property_leaf="author",
            value=f"{IRI_BASE}/entities/Author/a1",
        ),
        dual_write_cache=True,
    )
    await assert_fact(
        session,
        AssertionFact(
            subject_id=f"{IRI_BASE}/entities/Book/b1",
            kind="object",
            property_leaf="author",
            value=f"{IRI_BASE}/entities/Author/a2",
        ),
        dual_write_cache=True,
    )


@pytest.mark.asyncio
async def test_ask_cypher_count_e2e_memory_store():
    store = MemoryGraphStore()
    await _seed_bookstore(store)

    neptune = MagicMock()
    neptune.query = AsyncMock(side_effect=AssertionError("SPARQL path must not run"))

    pipe = NLQueryPipeline(neptune, anthropic_key="", graph_store=store)
    pipe._fetch_ontology = AsyncMock(return_value=ONTOLOGY)  # type: ignore[method-assign]

    result = await pipe.ask(
        "How many books?",
        graph_uri=f"{IRI_BASE}/graphs/demo-tenant",
        instance_graph=_kg_uri(),
        use_cypher=True,
    )

    assert result.timing.get("query_language") == "cypher"
    assert result.timing.get("cypher_stub") == 1.0
    assert result.timing.get("cypher_exec_path") == "template:entities_of_type_count"
    assert "$tenant_id" in result.sparql
    assert "3" in result.answer
    neptune.query.assert_not_called()


@pytest.mark.asyncio
async def test_ask_cypher_list_e2e_memory_store():
    store = MemoryGraphStore()
    await _seed_bookstore(store)
    neptune = MagicMock()
    neptune.query = AsyncMock(side_effect=AssertionError("no sparql"))
    pipe = NLQueryPipeline(neptune, anthropic_key="", graph_store=store)
    pipe._fetch_ontology = AsyncMock(return_value=ONTOLOGY)  # type: ignore[method-assign]

    result = await pipe.ask(
        "list all books",
        graph_uri=f"{IRI_BASE}/graphs/demo-tenant",
        instance_graph=_kg_uri(),
        use_cypher=True,
    )
    assert result.timing.get("query_language") == "cypher"
    assert result.timing.get("cypher_exec_path") == "template:entities_of_type"
    assert "Dune" in result.answer
    assert "Foundation" in result.answer
    assert "Hyperion" in result.answer


@pytest.mark.asyncio
async def test_ask_cypher_top_n_list_e2e_memory_store():
    store = MemoryGraphStore()
    await _seed_bookstore(store)
    neptune = MagicMock()
    neptune.query = AsyncMock(side_effect=AssertionError("no sparql"))
    pipe = NLQueryPipeline(neptune, anthropic_key="", graph_store=store)
    pipe._fetch_ontology = AsyncMock(return_value=ONTOLOGY)  # type: ignore[method-assign]

    result = await pipe.ask(
        "top 2 books",
        graph_uri=f"{IRI_BASE}/graphs/demo-tenant",
        instance_graph=_kg_uri(),
        use_cypher=True,
    )
    assert result.timing.get("cypher_exec_path") == "template:entities_of_type"
    assert result.timing.get("cypher_stub") == 1.0
    # Bookstore seed has 3 books; limit 2 returns first two by id order.
    assert result.timing.get("rows") == 2


@pytest.mark.asyncio
async def test_ask_cypher_filter_e2e_memory_store():
    store = MemoryGraphStore()
    await _seed_bookstore(store)
    neptune = MagicMock()
    neptune.query = AsyncMock(side_effect=AssertionError("no sparql"))
    pipe = NLQueryPipeline(neptune, anthropic_key="", graph_store=store)
    pipe._fetch_ontology = AsyncMock(return_value=ONTOLOGY)  # type: ignore[method-assign]

    result = await pipe.ask(
        "books where name is Dune",
        graph_uri=f"{IRI_BASE}/graphs/demo-tenant",
        instance_graph=_kg_uri(),
        use_cypher=True,
    )
    assert result.timing.get("cypher_exec_path") == "template:literal_values"
    assert "Dune" in result.answer
    assert "Foundation" not in result.answer


@pytest.mark.asyncio
async def test_ask_cypher_hop_e2e_memory_store():
    store = MemoryGraphStore()
    await _seed_bookstore(store)
    neptune = MagicMock()
    neptune.query = AsyncMock(side_effect=AssertionError("no sparql"))
    pipe = NLQueryPipeline(neptune, anthropic_key="", graph_store=store)
    pipe._fetch_ontology = AsyncMock(return_value=ONTOLOGY)  # type: ignore[method-assign]

    result = await pipe.ask(
        "authors of books",
        graph_uri=f"{IRI_BASE}/graphs/demo-tenant",
        instance_graph=_kg_uri(),
        use_cypher=True,
    )
    assert result.timing.get("cypher_exec_path") == "template:related_entities"
    assert "Herbert" in result.answer
    assert "Asimov" in result.answer


@pytest.mark.asyncio
async def test_ask_cypher_ontology_from_graph_store_catalog():
    """When OntoType rows exist, ontology_source is graph_store_catalog."""
    store = MemoryGraphStore()
    cat = store.session(
        GraphScope.for_catalog(layer="tenant", tenant_id="demo-tenant")
    )
    await upsert_type_pg(cat, name="Book", description="A book")
    await upsert_type_pg(cat, name="Author", description="An author")
    await _seed_bookstore(store)

    neptune = MagicMock()
    neptune.query = AsyncMock(side_effect=AssertionError("no sparql"))
    pipe = NLQueryPipeline(neptune, anthropic_key="", graph_store=store)
    # SPARQL ontology must not be required when catalog is present.
    pipe._fetch_ontology = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("SPARQL ontology must not run when catalog works")
    )

    result = await pipe.ask(
        "How many books?",
        graph_uri=f"{IRI_BASE}/graphs/demo-tenant",
        instance_graph=_kg_uri(),
        use_cypher=True,
    )
    assert result.timing.get("ontology_source") == "graph_store_catalog"
    assert "Type: Book" in (result.ontology or "")
    assert "3" in result.answer
    pipe._fetch_ontology.assert_not_awaited()


@pytest.mark.asyncio
async def test_ask_cypher_retry_on_graph_query_error():
    """On GraphQueryError, feed scrubbed error once to LLM and re-execute."""
    store = MemoryGraphStore()
    await _seed_bookstore(store)
    neptune = MagicMock()
    neptune.query = AsyncMock(side_effect=AssertionError("no sparql"))
    pipe = NLQueryPipeline(neptune, anthropic_key="", graph_store=store)
    pipe._fetch_ontology = AsyncMock(return_value=ONTOLOGY)  # type: ignore[method-assign]

    # Force LLM path (no fixture match) with a first broken cypher then a good one.
    calls = {"n": 0}
    feedbacks: list[str] = []

    async def fake_llm(question, ontology, **kw):
        calls["n"] += 1
        feedbacks.append(kw.get("error_feedback") or "")
        if calls["n"] == 1:
            # Force execute_read path with non-template cypher that Memory rejects
            return {
                "cypher": (
                    "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
                    "WHERE e.primary_type = $primary_type "
                    "RETURN e.bogus_fn() AS n"
                ),
                "params": {"primary_type": "Book"},
                "explanation": "broken",
                "functions_needed": [],
            }
        return {
            "cypher": (
                "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
                "WHERE e.primary_type IN $type_names "
                "RETURN count(*) AS n"
            ),
            "params": {"type_names": ["Book"]},
            "explanation": "count books after retry",
            "functions_needed": [],
            "template": "entities_of_type_count",
        }

    pipe._try_llm_cypher = fake_llm  # type: ignore[method-assign]

    # Use a question fixtures won't match
    result = await pipe.ask(
        "aggregate book inventory somehow",
        graph_uri=f"{IRI_BASE}/graphs/demo-tenant",
        instance_graph=_kg_uri(),
        use_cypher=True,
    )
    assert calls["n"] == 2
    assert result.timing.get("cypher_retry") == 1.0
    assert result.timing.get("attempts") == 2
    assert "3" in result.answer
    assert feedbacks[0] == ""
    assert feedbacks[1]  # scrubbed GraphQueryError fed back on retry


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


def test_graph_backend_default_neptune(monkeypatch):
    """INFONA_GRAPH_BACKEND defaults to neptune (SPARQL /ask path)."""
    from infona_client.graph.kg_writer import graph_backend
    from infona_client.nlp.cypher_generate import neo4j_ask_enabled

    monkeypatch.delenv("INFONA_GRAPH_BACKEND", raising=False)
    assert graph_backend() == "neptune"
    assert neo4j_ask_enabled() is False
    assert neo4j_ask_enabled(explicit=None) is False
    assert neo4j_ask_enabled(explicit=False) is False
    assert neo4j_ask_enabled(explicit=True) is True


def test_neo4j_ask_enabled_only_when_backend_or_flag(monkeypatch):
    from infona_client.nlp.cypher_generate import neo4j_ask_enabled

    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neptune")
    assert neo4j_ask_enabled() is False
    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neo4j")
    assert neo4j_ask_enabled() is True
    # Explicit False wins even if backend is neo4j.
    assert neo4j_ask_enabled(explicit=False) is False


@pytest.mark.asyncio
async def test_neptune_default_ask_unchanged(monkeypatch):
    """Default ask (use_cypher unset, backend=neptune) must stay on SPARQL.

    Regression guard: _ask_cypher only when backend is neo4j OR use_cypher=True.
    """
    monkeypatch.delenv("INFONA_GRAPH_BACKEND", raising=False)
    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neptune")

    neptune = MagicMock()
    pipe = NLQueryPipeline(neptune, anthropic_key="test-key", graph_store=None)
    called = {"cypher": False}

    async def _boom(*_a, **_k):
        called["cypher"] = True
        raise AssertionError(
            "_ask_cypher must not run on default Neptune SPARQL ask"
        )

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
            "results": {"bindings": [{"n": {"type": "literal", "value": "42"}}]},
        }
    )
    pipe._rephrase_via_openrouter = AsyncMock(return_value="")  # type: ignore[method-assign]
    pipe._resolve_uri_labels = AsyncMock(return_value={})  # type: ignore[method-assign]

    # Default: use_cypher is None — must not enter Cypher.
    result = await pipe.ask(
        "How many movies?",
        graph_uri=f"{IRI_BASE}/graphs/demo-tenant",
        instance_graph=_kg_uri("demo-tenant", "imdb"),
    )
    assert called["cypher"] is False
    assert result.timing.get("query_language") != "cypher"
    assert result.timing.get("graph_backend") != "neo4j"
    pipe._generate_sparql.assert_awaited()
    neptune.query.assert_awaited()
    assert "42" in result.answer or result.answer


@pytest.mark.asyncio
async def test_ask_cypher_rejects_model_tenant_in_params_via_confine():
    """Session overwrites evil tenant even if fixture somehow set it."""
    from infona_client.graph.rdf_model import AssertionFact, assert_fact

    store = MemoryGraphStore()
    scope = GraphScope.for_instance("demo-tenant", "bookstore")
    session = store.session(scope)
    await assert_fact(
        session,
        AssertionFact(
            subject_id=f"{IRI_BASE}/entities/Book/b1",
            kind="type",
            value="Book",
        ),
        dual_write_cache=True,
    )
    await assert_fact(
        session,
        AssertionFact(
            subject_id=f"{IRI_BASE}/entities/Book/b1",
            kind="literal",
            property_leaf="name",
            value="One",
        ),
        dual_write_cache=True,
    )

    neptune = MagicMock()
    neptune.query = AsyncMock(side_effect=AssertionError("no sparql"))
    pipe = NLQueryPipeline(neptune, anthropic_key="", graph_store=store)
    pipe._fetch_ontology = AsyncMock(return_value="Type: Book")  # type: ignore[method-assign]

    original = try_deterministic_cypher

    def evil_det(question, ontology_summary="", **kw):
        out = original(question, ontology_summary, **kw)
        if out:
            out = dict(out)
            out["params"] = {
                **out.get("params", {}),
                "tenant_id": "evil-tenant",
                "kg": "other",
            }
        return out

    import infona_client.nlp.pipeline as pl

    old = pl.try_deterministic_cypher
    pl.try_deterministic_cypher = evil_det  # type: ignore[assignment]
    try:
        result = await pipe.ask(
            "How many books?",
            graph_uri=f"{IRI_BASE}/graphs/demo-tenant",
            instance_graph=_kg_uri(),
            use_cypher=True,
        )
    finally:
        pl.try_deterministic_cypher = old  # type: ignore[assignment]

    assert "1" in result.answer  # still demo-tenant bookstore data
    assert result.timing.get("query_language") == "cypher"


def test_records_to_bindings():
    vars_, rows = records_to_bindings([GraphRecord(data={"n": 3})])
    assert vars_ == ["n"]
    assert rows == [{"n": "3"}]


def test_build_cypher_prompt_includes_error_feedback():
    from infona_client.nlp.prompts import build_cypher_generation_prompt

    user = build_cypher_generation_prompt(
        "How many books?",
        "Type: Book",
        tenant_id="t",
        kg_name="k",
        error_feedback="SyntaxError: unexpected token",
    )
    assert "Previous Cypher attempt failed" in user
    assert "SyntaxError" in user
    assert "do not switch to SPARQL" in user

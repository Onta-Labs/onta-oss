"""E5 — Explore / KG-admin read path on Memory GraphStore (no live Neo4j).

Hermetic coverage for list-by-type, entity detail, type counts, kg entity
count, dual-backend resolution, and ONTA-425 type-name path safety.
"""

from __future__ import annotations

import asyncio

import pytest

from infona_client.graph.explore_store import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    EntityDetail,
    EntityPage,
    EntitySummary,
    TypeCountRow,
    count_entities,
    get_entity_detail,
    grep_literals_pg,
    list_entities_by_type,
    resolve_explore_session,
    type_counts,
)
from infona_client.graph.iri import IRI_BASE
from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.queries import InvalidTypeName
from infona_client.graph.schema_bootstrap import TEMPLATES
from infona_client.graph.scope import GraphScope, GraphScopeError
from infona_client.graph.store import (
    GraphConfigError,
    configure_graph_store,
    graph_backend,
    reset_graph_store_for_tests,
)


@pytest.fixture
def store():
    s = MemoryGraphStore()
    yield s
    asyncio.run(s.close())
    reset_graph_store_for_tests()


def _graph(tenant: str = "demo-tenant", kg: str = "bookstore") -> str:
    return f"{IRI_BASE}/graphs/{tenant}/kg/{kg}"


async def _seed_bookstore(store: MemoryGraphStore) -> dict[str, str]:
    """Insert a small Person / Organization graph; return entity URIs."""
    alice = entity_uri("Person", "alice")
    bob = entity_uri("Person", "bob")
    carol = entity_uri("Person", "carol")
    acme = entity_uri("Organization", "acme")
    graph = _graph()
    triples = [
        (alice, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/Person"),
        (alice, "http://www.w3.org/2000/01/rdf-schema#label", "Alice"),
        (alice, f"{IRI_BASE}/types/Person/attrs/email", "a@example.com"),
        (bob, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/Person"),
        (bob, "http://www.w3.org/2000/01/rdf-schema#label", "Bob"),
        (carol, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/Person"),
        (carol, "http://www.w3.org/2000/01/rdf-schema#label", "Carol"),
        (acme, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/Organization"),
        (acme, "http://www.w3.org/2000/01/rdf-schema#label", "Acme Corp"),
        (alice, f"{IRI_BASE}/onto/works_at", acme),
        (bob, f"{IRI_BASE}/onto/works_at", acme),
    ]
    await insert_facts(None, graph, triples, store=store)
    return {"alice": alice, "bob": bob, "carol": carol, "acme": acme}


def test_explore_templates_registered():
    for name in (
        "entity_list_by_type_page",
        "entity_count_by_type",
        "entity_count_total",
        "entity_detail",
        "entity_rels",
        "entity_count_by_primary_type",
    ):
        assert name in TEMPLATES
        assert "$tenant_id" in TEMPLATES[name].cypher
        assert "$kg" in TEMPLATES[name].cypher
        assert TEMPLATES[name].writing is False


def test_graph_backend_default_neo4j(monkeypatch):
    monkeypatch.delenv("INFONA_GRAPH_BACKEND", raising=False)
    assert graph_backend() == "neo4j"


def test_resolve_explore_session_rejects_legacy_backend(monkeypatch, store):
    """ONTA-527: a legacy env value is an error, not a hand-back to SPARQL."""
    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neptune")
    reset_graph_store_for_tests()
    with pytest.raises(GraphConfigError):
        resolve_explore_session(tenant_id="demo-tenant", kg="bookstore")
    # An explicit store still wins — it never consults the env at all.
    sess = resolve_explore_session(
        store=store, tenant_id="demo-tenant", kg="bookstore"
    )
    assert sess is not None
    assert sess.scope.tenant_id == "demo-tenant"
    assert sess.scope.kg == "bookstore"


def test_resolve_explore_session_env_neo4j(monkeypatch, store):
    monkeypatch.setenv("INFONA_GRAPH_BACKEND", "neo4j")
    configure_graph_store(store)
    sess = resolve_explore_session(tenant_id="demo-tenant", kg_name="bookstore")
    assert sess is not None
    assert sess.scope.kg == "bookstore"


def test_list_entities_by_type_and_pagination(store):
    async def run():
        ids = await _seed_bookstore(store)
        page = await list_entities_by_type(
            store=store,
            tenant_id="demo-tenant",
            kg="bookstore",
            type_name="Person",
            limit=2,
        )
        assert isinstance(page, EntityPage)
        assert page.total == 3
        assert len(page.entities) == 2
        assert all(isinstance(e, EntitySummary) for e in page.entities)
        assert page.next_cursor is not None
        # Ordered by id ascending
        assert page.entities[0].id < page.entities[1].id
        names = {e.name for e in page.entities}
        assert names <= {"Alice", "Bob", "Carol"}

        page2 = await list_entities_by_type(
            store=store,
            tenant_id="demo-tenant",
            kg="bookstore",
            type_name="Person",
            limit=2,
            after_id=page.next_cursor,
        )
        assert page2.total == 3
        assert len(page2.entities) == 1
        assert page2.next_cursor is None
        # No overlap with first page
        first_ids = {e.id for e in page.entities}
        assert page2.entities[0].id not in first_ids

        orgs = await list_entities_by_type(
            store=store,
            tenant_id="demo-tenant",
            kg="bookstore",
            type_name="Organization",
        )
        assert orgs.total == 1
        assert orgs.entities[0].id == ids["acme"]
        assert orgs.entities[0].name == "Acme Corp"

    asyncio.run(run())


def test_list_entities_by_domain_label(store):
    async def run():
        await _seed_bookstore(store)
        page = await list_entities_by_type(
            store=store,
            tenant_id="demo-tenant",
            kg="bookstore",
            type_name="Person",
            match="label",
        )
        assert page.total == 3
        assert len(page.entities) == 3

    asyncio.run(run())


def test_entity_detail_props_and_rels(store):
    async def run():
        ids = await _seed_bookstore(store)
        detail = await get_entity_detail(
            store=store,
            tenant_id="demo-tenant",
            kg="bookstore",
            entity_id=ids["alice"],
        )
        assert isinstance(detail, EntityDetail)
        assert detail.id == ids["alice"]
        assert detail.primary_type == "Person"
        assert detail.name == "Alice"
        assert "Person" in detail.labels
        assert detail.properties.get("email") == "a@example.com"
        # Reserved keys must not leak into public properties
        assert "id" not in detail.properties
        assert "tenant_id" not in detail.properties
        assert "primary_type" not in detail.properties

        assert len(detail.outgoing) == 1
        out = detail.outgoing[0]
        assert out.attr == "works_at"
        assert out.other_id == ids["acme"]
        assert out.direction == "out"
        assert out.other_name == "Acme Corp"

        acme = await get_entity_detail(
            store=store,
            tenant_id="demo-tenant",
            kg="bookstore",
            entity_id=ids["acme"],
        )
        assert acme is not None
        assert len(acme.incoming) == 2
        assert {r.other_id for r in acme.incoming} == {ids["alice"], ids["bob"]}
        assert all(r.direction == "in" for r in acme.incoming)

        missing = await get_entity_detail(
            store=store,
            tenant_id="demo-tenant",
            kg="bookstore",
            entity_id=entity_uri("Person", "nope"),
        )
        assert missing is None

    asyncio.run(run())


def test_type_counts_and_kg_entity_count(store):
    async def run():
        await _seed_bookstore(store)
        counts = await type_counts(
            store=store, tenant_id="demo-tenant", kg="bookstore"
        )
        assert counts is not None
        by_name = {c.name: c.entity_count for c in counts}
        assert by_name == {"Person": 3, "Organization": 1}
        assert all(isinstance(c, TypeCountRow) for c in counts)
        # Sorted by count desc
        assert counts[0].entity_count >= counts[1].entity_count

        total = await count_entities(
            store=store, tenant_id="demo-tenant", kg_name="bookstore"
        )
        assert total == 4

        # Empty other kg
        empty = await count_entities(
            store=store, tenant_id="demo-tenant", kg="other-kg"
        )
        assert empty == 0

    asyncio.run(run())


def test_scope_isolation_across_tenants(store):
    async def run():
        await _seed_bookstore(store)
        # Different tenant sees nothing
        page = await list_entities_by_type(
            store=store,
            tenant_id="other-tenant",
            kg="bookstore",
            type_name="Person",
        )
        assert page is not None
        assert page.total == 0
        assert page.entities == ()

        counts = await type_counts(
            store=store, tenant_id="other-tenant", kg="bookstore"
        )
        assert counts == []

    asyncio.run(run())


def test_reject_unsafe_type_name_onta425(store):
    async def run():
        await _seed_bookstore(store)
        for bad in (
            "Person> DROP",
            "Foo Bar",
            "x\x00y",
            "",
            "a{b}",
        ):
            with pytest.raises(InvalidTypeName):
                await list_entities_by_type(
                    store=store,
                    tenant_id="demo-tenant",
                    kg="bookstore",
                    type_name=bad,
                )
        # Dual-backend None path still validates type name first.
        with pytest.raises(InvalidTypeName):
            await list_entities_by_type(
                tenant_id="demo-tenant",
                kg="bookstore",
                type_name="Bad>Type",
            )

    asyncio.run(run())


def test_reads_without_a_configured_store_fail_closed(monkeypatch):
    """ONTA-527: no SPARQL fallback, so a missing store raises rather than None.

    Replaces test_sparql_fallback_returns_none. Returning None used to mean
    "the caller should run SPARQL instead"; there is no such caller now, and a
    silent None would read as "this KG is empty".
    """
    monkeypatch.delenv("INFONA_GRAPH_BACKEND", raising=False)
    reset_graph_store_for_tests()

    async def run():
        with pytest.raises(GraphConfigError):
            await type_counts(tenant_id="demo-tenant", kg="bookstore")
        with pytest.raises(GraphConfigError):
            await count_entities(tenant_id="demo-tenant", kg="bookstore")
        with pytest.raises(GraphConfigError):
            await get_entity_detail(
                tenant_id="demo-tenant",
                kg="bookstore",
                entity_id=entity_uri("Person", "x"),
            )
        with pytest.raises(GraphConfigError):
            await list_entities_by_type(
                tenant_id="demo-tenant",
                kg="bookstore",
                type_name="Person",
            )

    asyncio.run(run())


def test_limit_clamp_and_invalid():
    async def run():
        store = MemoryGraphStore()
        try:
            await _seed_bookstore(store)
            page = await list_entities_by_type(
                store=store,
                tenant_id="demo-tenant",
                kg="bookstore",
                type_name="Person",
                limit=MAX_PAGE_LIMIT + 50,
            )
            assert page is not None
            # Only 3 people — clamp does not invent rows
            assert len(page.entities) == 3

            with pytest.raises(GraphScopeError, match="limit"):
                await list_entities_by_type(
                    store=store,
                    tenant_id="demo-tenant",
                    kg="bookstore",
                    type_name="Person",
                    limit=0,
                )
        finally:
            await store.close()

    asyncio.run(run())


def test_default_page_limit_constant():
    assert DEFAULT_PAGE_LIMIT == 50
    assert MAX_PAGE_LIMIT == 200


def test_list_and_count_use_instance_of_not_primary_type_alone(store):
    """ADR 0013: explore type list/count follow INSTANCE_OF→Class, not primary_type denorm."""
    from infona_client.graph.rdf_model import assert_fact, fact_to_assertion_fact
    from infona_client.graph.scope import GraphScope

    async def run():
        # Seed one Person via normal path (INSTANCE_OF written).
        await _seed_bookstore(store)
        # Add multi-type entity: Employee only via type Assertion; primary_type
        # deliberately left as something else would still match Employee via
        # INSTANCE_OF. Write type Assertion for a new entity as Employee.
        dana = entity_uri("Employee", "dana")
        session = store.session(GraphScope.for_instance("demo-tenant", "bookstore"))
        await assert_fact(
            session,
            fact_to_assertion_fact(
                subject_id=dana, kind="type", key="Employee", value="Employee"
            ),
        )
        # Corrupt denorm primary_type so primary_type-only filters would miss it.
        from infona_client.graph.pg_ops import merge_entity

        await merge_entity(session, dana, primary_type="NotARealType", name="Dana")

        page = await list_entities_by_type(
            store=store,
            tenant_id="demo-tenant",
            kg="bookstore",
            type_name="Employee",
        )
        assert page is not None
        assert page.total == 1
        assert page.entities[0].id == dana

        counts = await type_counts(
            store=store, tenant_id="demo-tenant", kg="bookstore"
        )
        by_name = {c.name: c.entity_count for c in counts}
        assert by_name.get("Employee") == 1
        assert by_name.get("Person") == 3
        # Denorm-only type must not appear as a Class count.
        assert "NotARealType" not in by_name

    asyncio.run(run())


def test_list_entities_include_subclasses(store):
    """include_subclasses expands Class SUBCLASS_OF for explore list/count."""
    from infona_client.graph.kg_writer import insert_facts as _ins
    from infona_client.graph.ontology_queries import type_uri
    from infona_client.graph.rdf_model import set_subclass_of
    from infona_client.graph.scope import GraphScope

    async def run():
        graph = _graph()
        person = entity_uri("Person", "p1")
        emp = entity_uri("Employee", "e1")
        triples = [
            (person, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/Person"),
            (person, "http://www.w3.org/2000/01/rdf-schema#label", "P1"),
            (emp, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/Employee"),
            (emp, "http://www.w3.org/2000/01/rdf-schema#label", "E1"),
        ]
        await _ins(None, graph, triples, store=store)
        session = store.session(GraphScope.for_instance("demo-tenant", "bookstore"))
        await set_subclass_of(session, type_uri("Employee"), type_uri("Person"))

        exact = await list_entities_by_type(
            store=store,
            tenant_id="demo-tenant",
            kg="bookstore",
            type_name="Person",
            include_subclasses=False,
        )
        assert exact is not None
        assert exact.total == 1
        assert exact.entities[0].id == person

        with_sub = await list_entities_by_type(
            store=store,
            tenant_id="demo-tenant",
            kg="bookstore",
            type_name="Person",
            include_subclasses=True,
        )
        assert with_sub is not None
        assert with_sub.total == 2
        ids = {e.id for e in with_sub.entities}
        assert ids == {person, emp}

    asyncio.run(run())


# ---------------------------------------------------------------------------
# grep_literals_pg — internal keys are excluded BEFORE the page is cut
# ---------------------------------------------------------------------------


class _StubGrepSession:
    """A session whose scan hands back internal rows the real templates exclude.

    Both shipped stores (the ``entity_literal_grep`` Cypher and the Memory scan)
    drop internal keys inside the scan, so through them the page filter never
    sees one. That is the belt; this stub removes it to test the braces — the
    ordering inside :func:`grep_literals_pg`, which owns the page and is the
    authority if a store's scan-level exclusion ever drifts.
    """

    def __init__(self, rows: list[dict]):
        self._rows = rows
        self.asked_for: int | None = None

    async def execute_template(self, name: str, params: dict):
        assert name == "entity_literal_grep"
        self.asked_for = params["limit"]
        return [dict(r) for r in self._rows[: params["limit"]]]


def _row(attr: str, value: str = "matrix", eid: str = "e1") -> dict:
    return {
        "entity_uri": eid,
        "label": "The Matrix",
        "type": "Movie",
        "attr": attr,
        "value": value,
    }


def test_grep_literals_pg_filters_before_cutting_the_page():
    """Filter THEN cut, never cut then filter.

    The store honours ``LIMIT`` on its side, so of the four rows here only the
    over-fetched three come back: ``blockKey``, ``name``, ``tagline``. Filtering
    first fills the caller's page of 2 with both domain rows; cutting first
    would spend a slot on ``blockKey`` and hand back a page of ONE — short, with
    nothing in the response to say why. Internal keys sort ahead of domain ones
    in both real scan orders, so that is the common case, not a corner.
    """
    session = _StubGrepSession(
        [_row("blockKey"), _row("name"), _row("tagline"), _row("title")]
    )

    hits, truncated = asyncio.run(grep_literals_pg(session, "matrix", limit=2))

    assert session.asked_for == 3, "over-fetch one row for honest truncation"
    assert [h.attr for h in hits] == ["name", "tagline"]
    # `title` matches too but was never fetched: the internal row consumed the
    # over-fetch, so `truncated` under-reports. That residue is exactly why the
    # exclusion is ALSO pushed into the scan (`entity_literal_grep` / Memory),
    # where no internal row is fetched in the first place — the end-to-end
    # property is pinned by tests/test_grep_route.py.
    assert truncated is False


def test_grep_literals_pg_keeps_the_display_name():
    """``name`` is internal as a PREDICATE (rdfs:label) and kept as a KEY.

    Finding a thing by its displayed name is grep's commonest use, so the one
    exemption in ``facts.is_internal_property_key`` is pinned here.
    """
    session = _StubGrepSession([_row("blockKey"), _row("name")])
    hits, truncated = asyncio.run(grep_literals_pg(session, "matrix", limit=50))
    assert [h.attr for h in hits] == ["name"]
    assert truncated is False

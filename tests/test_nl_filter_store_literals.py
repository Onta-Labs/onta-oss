"""NL filter fixtures + store literal normalization (core-loop price/genre)."""

from __future__ import annotations

from infona_client.graph.assertion_model import (
    canonical_literal,
    normalize_store_literal,
    strip_rdf_datatype_suffix,
)
from infona_client.nlp.cypher_generate import (
    try_numeric_filter_query,
    try_related_name_filter_query,
)


def test_strip_and_normalize_float_literal():
    lex, dt = strip_rdf_datatype_suffix(
        "15.49^^http://www.w3.org/2001/XMLSchema#float"
    )
    assert lex == "15.49"
    assert "float" in (dt or "")
    assert normalize_store_literal(
        "15.49^^http://www.w3.org/2001/XMLSchema#float"
    ) == 15.49
    assert normalize_store_literal(15.49) == 15.49
    assert normalize_store_literal("hello") == "hello"
    assert canonical_literal("12.99^^http://www.w3.org/2001/XMLSchema#float") == "12.99"


def test_normalize_int_and_bool():
    assert normalize_store_literal(
        "42^^http://www.w3.org/2001/XMLSchema#integer"
    ) == 42
    assert normalize_store_literal(
        "true^^http://www.w3.org/2001/XMLSchema#boolean"
    ) is True


def test_numeric_filter_fixture_price_under():
    onto = (
        "Type: Book (20 entities)\n"
        "  - price: float (literal, key=price)\n"
        "  - title: string (literal, key=title)\n"
    )
    got = try_numeric_filter_query(
        "Which books cost less than 15 dollars? List titles and prices.",
        onto,
        type_names=["Book"],
    )
    assert got is not None
    assert got["template"] == "literal_compare"
    assert got["params"]["prop_key"] == "price"
    assert got["params"]["op"] == "lt"
    assert got["params"]["threshold"] == 15.0
    assert "Book" in got["params"]["type_names"]


def test_numeric_filter_fixture_rating_ge():
    onto = "Type: Book\n  - rating: float (literal, key=rating)\n"
    got = try_numeric_filter_query(
        "books with rating >= 4.5",
        onto,
        type_names=["Book"],
    )
    assert got is not None
    assert got["params"]["prop_key"] == "rating"
    assert got["params"]["op"] == "ge"
    assert got["params"]["threshold"] == 4.5


def test_related_name_filter_genre():
    onto = (
        "Type: Book\n"
        "  - has_genre -> Genre (relationship, key=has_genre)\n"
        "Type: Genre\n"
        "  - display_name: string (literal, key=display_name)\n"
    )
    got = try_related_name_filter_query(
        "Which books have genre Classic Fiction?",
        onto,
        type_names=["Book", "Genre"],
    )
    assert got is not None
    assert got["template"] == "related_entity_name_filter"
    assert got["params"]["rel_attr"] == "has_genre"
    assert got["params"]["target_name"] == "Classic Fiction"


import pytest

from infona_client.graph.iri import IRI_BASE
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.rdf_model import AssertionFact, assert_fact
from infona_client.graph.scope import GraphScope
from infona_client.nlp.cypher_generate import try_filter_query


@pytest.mark.asyncio
async def test_memory_literal_compare_strips_legacy_and_native():
    store = MemoryGraphStore()
    scope = GraphScope.for_instance("demo-tenant", "bookstore")
    session = store.session(scope)

    cheap = f"{IRI_BASE}/entities/Book/cheap"
    dear = f"{IRI_BASE}/entities/Book/dear"
    for eid, title, price in (
        (cheap, "Cheap Book", "9.99^^http://www.w3.org/2001/XMLSchema#float"),
        (dear, "Expensive Book", 19.99),
    ):
        await assert_fact(
            session, AssertionFact(subject_id=eid, kind="type", value="Book")
        )
        await assert_fact(
            session,
            AssertionFact(
                subject_id=eid, kind="literal", property_leaf="title", value=title
            ),
        )
        await assert_fact(
            session,
            AssertionFact(
                subject_id=eid, kind="literal", property_leaf="price", value=price
            ),
        )

    rows = await session.execute_template(
        "literal_compare",
        {
            "type_names": ["Book"],
            "prop_key": "price",
            "op": "lt",
            "threshold": 15.0,
            "limit": 25,
        },
    )
    titles = {r.get("title") for r in rows}
    assert "Cheap Book" in titles
    assert "Expensive Book" not in titles
    # Compare path returned the coerced number (legacy ^^ seed or native).
    cheap_row = next(r for r in rows if r.get("title") == "Cheap Book")
    assert float(cheap_row.get("value")) == 9.99


@pytest.mark.asyncio
async def test_memory_related_entity_name_filter():
    store = MemoryGraphStore()
    scope = GraphScope.for_instance("demo-tenant", "bookstore")
    session = store.session(scope)
    book = f"{IRI_BASE}/entities/Book/Gatsby"
    genre = f"{IRI_BASE}/entities/Genre/Classic_Fiction"
    await assert_fact(session, AssertionFact(subject_id=book, kind="type", value="Book"))
    await assert_fact(session, AssertionFact(subject_id=genre, kind="type", value="Genre"))
    await assert_fact(
        session,
        AssertionFact(
            subject_id=book, kind="literal", property_leaf="title", value="The Great Gatsby"
        ),
    )
    await assert_fact(
        session,
        AssertionFact(
            subject_id=genre,
            kind="literal",
            property_leaf="display_name",
            value="Classic Fiction",
        ),
    )
    await assert_fact(
        session,
        AssertionFact(
            subject_id=book, kind="object", property_leaf="has_genre", value=genre
        ),
    )
    rows = await session.execute_template(
        "related_entity_name_filter",
        {
            "type_names": ["Book"],
            "rel_attr": "has_genre",
            "target_name": "Classic Fiction",
            "limit": 25,
        },
    )
    assert len(rows) == 1
    assert "Gatsby" in str(rows[0].get("title") or rows[0].get("id"))


def test_related_name_defers_is_equals_to_equality_filter():
    onto = "Type: Book\n  - author: string (literal, key=author)\n"
    assert (
        try_related_name_filter_query(
            "books with author is Herbert", onto, type_names=["Book"]
        )
        is None
    )
    got = try_filter_query(
        "books with author is Herbert", onto, type_names=["Book"]
    )
    assert got is not None
    assert got["params"]["prop_key"] == "author"
    assert got["params"]["prop_value"] == "Herbert"

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


def test_literal_values_cypher_strips_typed_suffix_for_equality():
    """Equality template must normalize legacy ``lexical^^xsd`` like compare."""
    from infona_client.graph.rdfs_helpers import LITERAL_VALUES_CYPHER

    cypher = LITERAL_VALUES_CYPHER
    assert "^^" in cypher
    assert "split(toString(a.literal_value), '^^')" in cypher
    assert "split(toString(e[$prop_key]), '^^')" in cypher
    assert "toFloat(" in cypher


def test_memory_literal_eq_helper_strips_legacy():
    """Unit-level: normalize both sides before equality (store + query)."""
    eq = MemoryGraphStore._literal_eq
    legacy = "12.99^^http://www.w3.org/2001/XMLSchema#float"
    assert eq(legacy, "12.99")
    assert eq(legacy, 12.99)
    assert eq(12.99, "12.99")
    assert eq(12.99, 12.99)
    assert eq("Herbert", "Herbert")
    assert not eq(legacy, "9.99")
    assert not eq("Herbert", "Asimov")
    assert eq(
        "Classic^^http://www.w3.org/2001/XMLSchema#string",
        "Classic",
    )


@pytest.mark.asyncio
async def test_memory_literal_values_eq_matches_legacy_typed_literals():
    """price equals 12.99 / string equals when store still has ^^ suffixes."""
    store = MemoryGraphStore()
    scope = GraphScope.for_instance("demo-tenant", "bookstore")
    session = store.session(scope)

    match_id = f"{IRI_BASE}/entities/Book/match"
    miss_id = f"{IRI_BASE}/entities/Book/miss"
    string_id = f"{IRI_BASE}/entities/Book/stringy"

    for eid, title, price in (
        (match_id, "Match Book", 12.99),
        (miss_id, "Miss Book", 9.99),
        (string_id, "String Book", 1.0),
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
    await assert_fact(
        session,
        AssertionFact(
            subject_id=string_id,
            kind="literal",
            property_leaf="genre",
            value="Classic Fiction",
        ),
    )

    # Simulate legacy SPARQL-era values still sitting in Assertion + Entity cache
    # (assert_fact normalizes on write; older graphs never went through that).
    legacy_price = "12.99^^http://www.w3.org/2001/XMLSchema#float"
    legacy_genre = "Classic Fiction^^http://www.w3.org/2001/XMLSchema#string"
    for (_t, _k, _aid), a in store._assertions.items():
        if a.subject_id == match_id and a.literal_value == 12.99:
            a.literal_value = legacy_price
        if a.subject_id == string_id and a.literal_value == "Classic Fiction":
            a.literal_value = legacy_genre
    match_ent = store._entities[("demo-tenant", "bookstore", match_id)]
    match_ent.props["price"] = legacy_price
    string_ent = store._entities[("demo-tenant", "bookstore", string_id)]
    string_ent.props["genre"] = legacy_genre

    price_rows = await session.execute_template(
        "literal_values",
        {
            "type_names": ["Book"],
            "prop_key": "price",
            "prop_value": "12.99",
            "limit": 25,
        },
    )
    price_ids = {r.get("id") for r in price_rows}
    assert match_id in price_ids
    assert miss_id not in price_ids

    # Native number query value also matches legacy store form.
    price_rows_num = await session.execute_template(
        "literal_values",
        {
            "type_names": ["Book"],
            "prop_key": "price",
            "prop_value": 12.99,
            "limit": 25,
        },
    )
    assert match_id in {r.get("id") for r in price_rows_num}

    genre_rows = await session.execute_template(
        "literal_values",
        {
            "type_names": ["Book"],
            "prop_key": "genre",
            "prop_value": "Classic Fiction",
            "limit": 25,
        },
    )
    assert {r.get("id") for r in genre_rows} == {string_id}


@pytest.mark.asyncio
async def test_memory_literal_values_eq_entity_cache_only_legacy():
    """Secondary path: Entity prop cache holds legacy ^^, no Assertion match."""
    store = MemoryGraphStore()
    scope = GraphScope.for_instance("demo-tenant", "bookstore")
    session = store.session(scope)
    eid = f"{IRI_BASE}/entities/Book/cache_only"
    await assert_fact(session, AssertionFact(subject_id=eid, kind="type", value="Book"))
    # Type assertion only — plant legacy price solely on Entity props.
    ent = store._entities[("demo-tenant", "bookstore", eid)]
    ent.props["price"] = "15.49^^http://www.w3.org/2001/XMLSchema#float"

    rows = await session.execute_template(
        "literal_values",
        {
            "type_names": ["Book"],
            "prop_key": "price",
            "prop_value": "15.49",
            "limit": 25,
        },
    )
    assert len(rows) == 1
    assert rows[0].get("id") == eid


def test_in_the_genre_and_cheaper_than_fixtures():
    onto = (
        "Type: Book (20 entities)\n"
        "  - price: float (literal, key=price)\n"
        "  - has_genre -> Genre (relationship, key=has_genre)\n"
        "Type: Genre\n"
        "  - display_name: string (literal, key=display_name)\n"
    )
    from infona_client.nlp.cypher_generate import try_deterministic_cypher

    g = try_deterministic_cypher(
        "Which books are in the genre Classic Fiction?",
        onto,
        type_names=["Book", "Genre"],
    )
    assert g is not None
    assert g["template"] == "related_entity_name_filter"
    assert g["params"]["target_name"] == "Classic Fiction"

    g2 = try_deterministic_cypher(
        "Which books cost under 15 dollars?",
        onto,
        type_names=["Book"],
    )
    assert g2 is not None
    assert g2["template"] == "literal_compare"
    assert g2["params"]["op"] == "lt"
    assert g2["params"]["threshold"] == 15.0

    g3 = try_deterministic_cypher(
        "books cheaper than 15",
        onto,
        type_names=["Book"],
    )
    assert g3 is not None
    assert g3["params"]["op"] == "lt"

    g4 = try_deterministic_cypher(
        "Which books are more expensive than 15?",
        onto,
        type_names=["Book"],
    )
    assert g4 is not None
    assert g4["params"]["op"] == "gt"
    assert g4["params"]["threshold"] == 15.0


def test_missing_template_params_helper():
    from infona_client.nlp.pipeline import _missing_template_params

    assert _missing_template_params(
        "WHERE x IN $type_names LIMIT $limit", {"limit": 10}
    ) == {"type_names"}
    assert not _missing_template_params(
        "WHERE x IN $type_names LIMIT $limit",
        {"type_names": ["Book"], "limit": 10},
    )

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

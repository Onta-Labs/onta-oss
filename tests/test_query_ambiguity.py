"""Hermetic: vague count + multiple live types → clarify (no type hardcodes)."""

from __future__ import annotations

from infona_client.nlp.query_ambiguity import (
    ambiguous_count_needs_clarify,
    format_type_count_clarification,
    question_is_vague_count,
)
from infona_client.nlp.query_build import TypePopulation

_POPS = (
    TypePopulation("SynthWidget", 8),
    TypePopulation("SynthMeasure", 8),
    TypePopulation("SynthFlag", 2),
)


def test_vague_count_nouns():
    assert question_is_vague_count("How many records are there?")
    assert question_is_vague_count("count the rows")
    assert question_is_vague_count("how many items in the graph")
    assert not question_is_vague_count("How many SynthWidget are there?")
    assert not question_is_vague_count("sum unit_cost for ready")
    assert not question_is_vague_count("how many items under 20")
    assert not question_is_vague_count("count the rows where status is ready")
    assert not question_is_vague_count("what's the total amount of data")


def test_clarify_when_vague_and_multi_type():
    assert ambiguous_count_needs_clarify("How many records?", _POPS)
    assert ambiguous_count_needs_clarify("how many items?", _POPS)


def test_no_clarify_when_type_named():
    assert not ambiguous_count_needs_clarify("How many SynthWidget?", _POPS)
    assert not ambiguous_count_needs_clarify("count synthwidgets", _POPS)


def test_no_clarify_filtered_or_numeric_asks():
    assert not ambiguous_count_needs_clarify("how many items under 20", _POPS)
    assert not ambiguous_count_needs_clarify(
        "count the rows where status is ready", _POPS
    )


def test_no_clarify_single_populated_type():
    one = (TypePopulation("SynthWidget", 8),)
    assert not ambiguous_count_needs_clarify("How many records?", one)


def test_clarification_lists_types_not_a_number():
    text = format_type_count_clarification(_POPS)
    assert "SynthWidget" in text
    assert "8" in text
    assert "What do you mean" in text
    # Must not pick a single total as the answer
    assert "18" not in text  # 8+8+2 would be a guessed total

"""Hermetic: vague count + multiple live types → clarify (no type hardcodes)."""

from __future__ import annotations

from infona_client.nlp.query_ambiguity import (
    ambiguous_anaphora_needs_clarify,
    ambiguous_count_needs_clarify,
    format_anaphora_clarification,
    format_conversation_for_prompt,
    format_type_count_clarification,
    question_is_anaphoric,
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


def test_anaphoric_followup_without_history_needs_clarify():
    assert question_is_anaphoric("what did we talk about?")
    assert question_is_anaphoric("who else was there?")
    assert question_is_anaphoric("when was that meeting?")
    assert question_is_anaphoric("what were their names and when were they met")
    assert not question_is_anaphoric("how many SynthWidget entities are there?")
    assert not question_is_anaphoric("when did I meet Ada Example?")
    assert not question_is_anaphoric("when was the last time I met Ada Example?")
    assert not ambiguous_anaphora_needs_clarify(
        "when was the last time I met Ada Example?"
    )
    # Intra-sentential "their" / "we" is bound in the same question.
    assert not question_is_anaphoric("list SynthWidget entities and their weights")
    assert not question_is_anaphoric("how many records do we have?")
    assert not ambiguous_anaphora_needs_clarify(
        "show SynthWidget entities and their weights"
    )
    assert not ambiguous_anaphora_needs_clarify("how many records do we have?")
    assert ambiguous_anaphora_needs_clarify("what did we talk about?")
    assert ambiguous_anaphora_needs_clarify("who else was there?")
    # A named person is a standalone question, not an unbound pronoun.
    assert not ambiguous_anaphora_needs_clarify(
        "what did we talk about with Ada Example?"
    )


def test_anaphoric_followup_with_history_does_not_clarify():
    prior = [
        {"role": "user", "text": "when was the last time I met Ada Example?"},
        {"role": "assistant", "text": "2026-08-12"},
    ]
    assert not ambiguous_anaphora_needs_clarify("what did we talk about?", prior)
    text = format_conversation_for_prompt(prior, question="what did we talk about?")
    assert "Ada Example" in text
    assert "what did we talk about" not in text
    assert "FOLLOW-UP" in text
    assert "required MATCH" in text
    assert format_anaphora_clarification()


def test_specified_question_does_not_inherit_prior_filters():
    prior = [
        {"role": "user", "text": "when was the last time I met Ada Example?"},
        {"role": "assistant", "text": "2026-08-12"},
    ]
    text = format_conversation_for_prompt(
        prior, question="when was the last time I met Bea Sample?"
    )
    assert "Ada Example" in text
    assert "fully specified" in text.lower()
    assert "FOLLOW-UP" not in text
    assert "do not inherit" in text.lower()


def test_conversation_prompt_ignores_empty():
    assert format_conversation_for_prompt(None) == ""
    assert format_conversation_for_prompt([]) == ""

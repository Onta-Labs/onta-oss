"""Characterization + convergence tests for the shared JSON-coercion seam
(ONTA-193 P4).

Two jobs, mirroring ``test_retrieval_substrate.py`` for the fetch layer:

1. **Freeze the two tolerant parsers' behaviour at their canonical home**
   (:mod:`cograph_client.retrieval.coerce`) — the array parser
   (:func:`parse_json_array`, discovery's row shape) and the object parser
   (:func:`parse_json_object`, enrichment's ``{"value","confidence"}`` shape).
   The two shapes deliberately diverge in tolerance (fence stripping, outermost
   slice vs. whole-text-when-it-starts-with-``{``); these tests pin each so a
   future "helpful" unification can't silently change one caller's outputs.

2. **Freeze the delegation contract** — the enrichment extractor's
   ``_try_parse_json`` (OSS) and the premium discovery ``extract_json_array``
   (parent repo, imported here only when present) now ARE the substrate parsers.
   Behavioural-parity assertions trip loudly if a rail re-forks its own helper —
   the read-path analogue of ``test_write_path_convergence.py``.

All pure/offline: no network, no LLM, no fixtures.
"""

from __future__ import annotations

import pytest

from cograph_client.enrichment.extraction import _try_parse_json
from cograph_client.retrieval import parse_json_array, parse_json_object
from cograph_client.retrieval.coerce import (
    parse_json_array as coerce_parse_json_array,
    parse_json_object as coerce_parse_json_object,
)


# --- package re-export identity ---------------------------------------------- #
def test_retrieval_package_reexports_the_coerce_functions():
    # The public substrate surface IS the coerce module's functions (identity),
    # so a re-fork that shadows one trips here.
    assert parse_json_array is coerce_parse_json_array
    assert parse_json_object is coerce_parse_json_object


# --- parse_json_array: discovery's array-of-rows shape ----------------------- #
def test_parse_json_array_plain():
    assert parse_json_array('[{"a": 1}, {"b": 2}]') == [{"a": 1}, {"b": 2}]


def test_parse_json_array_strips_code_fence():
    fenced = '```json\n[{"name": "Acme"}, {"name": "Globex"}]\n```'
    assert parse_json_array(fenced) == [{"name": "Acme"}, {"name": "Globex"}]


def test_parse_json_array_finds_outermost_brackets_in_prose():
    prose = 'Here are the rows I found:\n[{"x": 1}]\nHope that helps!'
    assert parse_json_array(prose) == [{"x": 1}]


def test_parse_json_array_filters_non_dict_members():
    mixed = '[{"a": 1}, 2, "x", [3], null, true, {"b": 2}]'
    assert parse_json_array(mixed) == [{"a": 1}, {"b": 2}]


def test_parse_json_array_malformed_returns_empty():
    assert parse_json_array("[1, 2,]") == []          # trailing comma → decode error
    assert parse_json_array("[not valid json") == []   # no closing bracket
    assert parse_json_array("no brackets here") == []  # no array at all


def test_parse_json_array_blank_and_none_return_empty():
    assert parse_json_array("") == []
    assert parse_json_array("   \n ") == []
    assert parse_json_array(None) == []  # guarded via (text or "")


# --- parse_json_object: enrichment's single-object shape --------------------- #
def test_parse_json_object_plain():
    assert parse_json_object('{"value": "Germany", "confidence": 0.77}') == {
        "value": "Germany",
        "confidence": 0.77,
    }


def test_parse_json_object_embedded_in_prose_is_sliced():
    # Does NOT start with "{" → slice the outermost {...} out of the prose.
    prose = 'The answer is {"value": "Bosch"} per the source.'
    assert parse_json_object(prose) == {"value": "Bosch"}


def test_parse_json_object_fenced_is_sliced():
    # Starts with the fence (not "{"), so the outermost {...} is sliced out.
    fenced = '```json\n{"value": "Germany"}\n```'
    assert parse_json_object(fenced) == {"value": "Germany"}


def test_parse_json_object_trailing_prose_after_leading_object_is_none():
    # Load-bearing quirk: when the stripped text ALREADY starts with "{", the
    # WHOLE text is parsed (no slice), so trailing text after a complete object
    # is a decode error → None. This is the enrichment extractor's long-standing
    # behaviour and must not change.
    assert parse_json_object('{"value": "x"} and then some trailing prose') is None


def test_parse_json_object_non_object_and_malformed_return_none():
    assert parse_json_object("[1, 2, 3]") == None       # a list, not a dict
    assert parse_json_object("just some text") is None   # no braces
    assert parse_json_object('{"value": "x"') is None    # unbalanced → decode error


def test_parse_json_object_does_not_guard_none():
    # Preserved contract: unlike parse_json_array, the object parser does NOT
    # tolerate None (it strips immediately). Callers (e.g. llm_extractor) guard
    # for a None/empty completion before calling. Locking this stops a future
    # "helpful" None guard from silently changing behaviour.
    with pytest.raises(AttributeError):
        parse_json_object(None)  # type: ignore[arg-type]


# --- delegation: enrichment extractor now IS the substrate object parser ------ #
_OBJECT_CASES = [
    '{"value": "Germany", "confidence": 0.77}',
    'The answer is {"value": "Bosch"} per the source.',
    '```json\n{"value": "Germany"}\n```',
    '{"value": "x"} and then some trailing prose',
    "[1, 2, 3]",
    "just some text",
    '{"value": "x"',
    "",
    "   ",
]


@pytest.mark.parametrize("text", _OBJECT_CASES)
def test_enrichment_try_parse_json_delegates_to_substrate(text):
    # extraction._try_parse_json is now a thin delegate to parse_json_object;
    # prove behavioural parity across the representative shapes.
    assert _try_parse_json(text) == parse_json_object(text)


# --- delegation: premium discovery extract_json_array (parent repo, optional) - #
_ARRAY_CASES = [
    '[{"a": 1}, {"b": 2}]',
    '```json\n[{"name": "Acme"}, {"name": "Globex"}]\n```',
    'Here are the rows:\n[{"x": 1}]\nDone.',
    '[{"a": 1}, 2, "x", [3], null, {"b": 2}]',
    "[1, 2,]",
    "[not valid json",
    "no brackets here",
    "",
    None,
]


def test_web_sources_extract_json_array_is_the_substrate_parser():
    # The premium discovery helper lives in the parent `cograph` repo, which is
    # absent in a standalone OSS checkout — skip cleanly there. When present (the
    # monorepo), assert it delegates: same output as parse_json_array on every
    # representative input, so a re-fork of the discovery-side helper trips here.
    _common = pytest.importorskip("cograph.web_sources._common")
    for text in _ARRAY_CASES:
        assert _common.extract_json_array(text) == parse_json_array(text), text

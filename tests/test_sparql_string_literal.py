"""The ONE hardened SPARQL string-literal escaper (ONTA-416).

``graph/queries.sparql_string_literal`` replaced FOUR partial copies that had
drifted apart: ``queries._escape_literal`` and ``enrichment.executor._esc_lit``
covered ``\\n`` but not ``\\r``/``\\t``; ``explore._esc`` covered NEITHER; only
``ontology_queries._esc`` was fully hardened (ONTA-250). A needle/value carrying
an interior CR therefore produced an UNTERMINATED literal and a store-side parse
error surfacing as an opaque 500.

These tests lock (a) the escaping itself and (b) that every caller still routes
through the single definition, so the coverage cannot diverge again.
"""

from __future__ import annotations

import pytest

from cograph_client.api.routes import explore as explore_route
from cograph_client.enrichment import executor as enrichment_executor
from cograph_client.graph import ontology_queries
from cograph_client.graph.queries import _escape_literal, sparql_string_literal


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("plain", "plain"),
        ('say "hi"', 'say \\"hi\\"'),
        ("back\\slash", "back\\\\slash"),
        ("line\nbreak", "line\\nbreak"),
        ("carriage\rreturn", "carriage\\rreturn"),
        ("tab\tsep", "tab\\tsep"),
        ("", ""),
    ],
)
def test_escapes_every_lexically_fragile_char(raw, expected):
    assert sparql_string_literal(raw) == expected


def test_backslash_is_escaped_FIRST_so_escapes_are_not_double_escaped():
    """``\\` + `n`` (two chars) must not become a real newline escape.

    Order is the whole reason none of the old copies was ever an injection hole:
    escaping the backslash first means an attacker-supplied ``\\"`` cannot be
    rebuilt into a literal-closing quote.
    """
    assert sparql_string_literal("a\\nb") == "a\\\\nb"
    assert sparql_string_literal('a\\"b') == 'a\\\\\\"b'


@pytest.mark.parametrize("raw", ["\n", "\r", "\t", 'q"q', "b\\b", "mixed\r\n\tx"])
def test_result_carries_no_raw_char_that_would_break_the_literal(raw):
    out = sparql_string_literal(raw)
    assert "\n" not in out and "\r" not in out and "\t" not in out
    # Every remaining quote is escaped (drop escaped backslashes first).
    assert '"' not in out.replace("\\\\", "").replace('\\"', "")


@pytest.mark.parametrize(
    "fn",
    [
        _escape_literal,
        ontology_queries._esc,
        explore_route._esc,
        enrichment_executor._esc_lit,
    ],
)
def test_every_caller_delegates_to_the_one_definition(fn):
    """Drift guard: a re-forked local copy would fail this on the control chars."""
    probe = 'a"b\\c\nd\re\tf'
    assert fn(probe) == sparql_string_literal(probe)


def test_explore_keyset_cursor_escaping_is_unchanged_for_ordinary_uris():
    """The shared fix must not disturb Explorer keyset pagination.

    ``explore._esc`` also escapes the ``cursor`` (an entity URI). Real cursors
    contain none of the newly-escaped characters, so their emitted form is
    byte-identical to the pre-fix behavior.
    """
    cursor = "https://graph.onta.sh/entities/Movie/m_1-2"
    assert explore_route._esc(cursor) == cursor

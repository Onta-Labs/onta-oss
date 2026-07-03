"""Unit tests for the API-registry dotted-path evaluator (ONTA-194).

Pure, dependency-free logic on the field-mapping hot path — table-driven.
"""

from __future__ import annotations

import pytest

from cograph_client.api_registry.jsonpath import (
    extract_path,
    extract_records,
    is_valid_path,
    map_record,
)

_DOC = {
    "a": {"b": {"c": 7}},
    "list": [{"x": 1}, {"x": 2}, {"x": 3}],
    "nested": [[10, 11], [20, 21]],
    "scalar": "hi",
    "flag": True,
    "num": 3.5,
    "tags": ["p", "q"],
    "objs": [{"k": "v"}],
    "blank": "   ",
}


@pytest.mark.parametrize(
    "path,expected",
    [
        ("", _DOC),                       # empty path == whole value
        ("a.b.c", 7),
        ("list[0].x", 1),
        ("list[-1].x", 3),                # negative index
        ("nested[1][0]", 20),             # chained list indexing
        ("scalar", "hi"),
        ("missing", None),                # missing key
        ("a.b.missing", None),
        ("scalar.b", None),               # key step on a scalar
        ("a[0]", None),                   # index step on a dict
        ("list[9].x", None),              # index out of range
        ("list[0].y", None),
    ],
)
def test_extract_path(path, expected):
    assert extract_path(_DOC, path) == expected


@pytest.mark.parametrize("bad", [".a", "a.", "a..b", "a[", "a[]", "a[b]", "[x]"])
def test_malformed_paths_yield_none_not_raise(bad):
    assert extract_path(_DOC, bad) is None
    assert is_valid_path(bad) is False


@pytest.mark.parametrize("good", ["", "a", "a.b.c", "list[0].x", "list[-1]", "nested[0][1]"])
def test_valid_paths(good):
    assert is_valid_path(good) is True


def test_map_record_coercions_and_drops():
    mappings = {
        "seven": "a.b.c",       # int -> "7"
        "flag": "flag",         # bool -> "true"
        "num": "num",           # float -> "3.5"
        "tags": "tags",         # list of scalars -> "p, q"
        "objs": "objs",         # list of dicts -> dropped
        "blank": "blank",       # whitespace-only -> dropped
        "missing": "nope.here",  # missing -> dropped
    }
    out = map_record(_DOC, mappings)
    assert out == {"seven": "7", "flag": "true", "num": "3.5", "tags": "p, q"}
    for dropped in ("objs", "blank", "missing"):
        assert dropped not in out


def test_extract_records_variants():
    # bare list at empty path
    assert extract_records([{"a": 1}, {"a": 2}], "") == [{"a": 1}, {"a": 2}]
    # nested array at a dotted path
    assert extract_records({"r": {"items": [{"a": 1}]}}, "r.items") == [{"a": 1}]
    # a single object at the path is wrapped into a one-element list
    assert extract_records({"product": {"code": "x"}}, "product") == [{"code": "x"}]
    # non-dict elements filtered out
    assert extract_records({"r": [{"a": 1}, 5, "s", None]}, "r") == [{"a": 1}]
    # missing path -> empty
    assert extract_records({"r": []}, "nope") == []
    # scalar at path -> empty (not a record array)
    assert extract_records({"r": 5}, "r") == []

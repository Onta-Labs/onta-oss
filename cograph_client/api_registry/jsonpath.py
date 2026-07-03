"""Minimal, dependency-free dotted-path extraction for the API registry.

The declarative call spec addresses two things inside a JSON response:

* ``result_path`` — where the record array lives (e.g. ``"results"`` for NPPES,
  ``"studies"`` for ClinicalTrials.gov, ``""`` for a bare top-level array).
* per-field source paths in ``field_mappings`` — e.g.
  ``"basic.organization_name"`` or ``"addresses[0].telephone_number"``.

There is no JSONPath dependency in the tree and no existing helper (the web
sources heuristically *guess* the record array; here the spec says exactly where
it is), so this is a tiny, well-scoped path evaluator:

    segment := KEY | KEY '[' INT ']' | '[' INT ']'
    path    := segment ('.' segment)*

Supported:
  * object key traversal              ``a.b.c``
  * list indexing (incl. negatives)   ``taxonomies[0].desc`` / ``items[-1]``
  * chained list indexing             ``a[0][1]``
  * empty path == the value itself

Anything missing / type-mismatched yields ``None`` (never raises) — the executor
treats a missing mapped field as absent, mirroring the ground-truth resolvers.
"""

from __future__ import annotations

import re
from typing import Any, Optional

# A segment is an optional key followed by zero or more [idx] subscripts.
_SEGMENT_RE = re.compile(r"^([^.\[\]]*)((?:\[-?\d+\])*)$")
_INDEX_RE = re.compile(r"\[(-?\d+)\]")


def _parse_path(path: str) -> Optional[list[Any]]:
    """Parse a dotted path into a flat list of steps (str keys / int indices).

    Returns ``None`` if the path is syntactically malformed so callers can treat
    it as "no value" rather than crashing.
    """
    path = (path or "").strip()
    if not path:
        return []
    steps: list[Any] = []
    for raw_seg in path.split("."):
        m = _SEGMENT_RE.match(raw_seg)
        if m is None:
            return None
        key, subscripts = m.group(1), m.group(2)
        if key:
            steps.append(key)
        elif not subscripts:
            # An empty segment (e.g. a trailing/leading dot) is malformed.
            return None
        for idx in _INDEX_RE.findall(subscripts):
            steps.append(int(idx))
    return steps


def is_valid_path(path: str) -> bool:
    """True if ``path`` is a syntactically valid dotted path (empty == valid).

    Used by catalog validation so a malformed ``result_path`` / field-mapping
    source path fails CI instead of silently yielding zero rows at runtime.
    """
    return _parse_path(path) is not None


def extract_path(obj: Any, path: str) -> Any:
    """Return the value at ``path`` within ``obj``, or ``None`` if absent."""
    steps = _parse_path(path)
    if steps is None:
        return None
    cur = obj
    for step in steps:
        if isinstance(step, str):
            if isinstance(cur, dict) and step in cur:
                cur = cur[step]
            else:
                return None
        else:  # int index
            if isinstance(cur, (list, tuple)):
                try:
                    cur = cur[step]
                except IndexError:
                    return None
            else:
                return None
    return cur


def extract_records(payload: Any, result_path: str) -> list[dict[str, Any]]:
    """Resolve the record array at ``result_path`` and keep only dict records.

    An empty ``result_path`` means the payload itself is the array. A single
    object (not a list) at the path is wrapped into a one-element list so
    single-record endpoints still work.
    """
    node = payload if not result_path else extract_path(payload, result_path)
    if node is None:
        return []
    if isinstance(node, dict):
        return [node]
    if isinstance(node, (list, tuple)):
        return [r for r in node if isinstance(r, dict)]
    return []


def _coerce_scalar(value: Any) -> Optional[str]:
    """Flatten a mapped value to a string cell, or ``None`` to drop it."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        s = value.strip()
        return s or None
    # Lists/dicts: join scalars, else drop (keep cells flat + string-typed).
    if isinstance(value, (list, tuple)):
        parts = [p for p in (_coerce_scalar(v) for v in value) if p]
        return ", ".join(parts) or None
    return None


def map_record(record: dict[str, Any], field_mappings: dict[str, str]) -> dict[str, str]:
    """Project one source record onto the declared output columns.

    Only columns whose source path resolves to a non-empty scalar are emitted —
    no ``"unknown"`` filler and no phantom columns, matching how enrichment omits
    a companion triple when a verdict has no value.
    """
    out: dict[str, str] = {}
    for column, source_path in field_mappings.items():
        cell = _coerce_scalar(extract_path(record, source_path))
        if cell is not None:
            out[column] = cell
    return out


__all__ = ["extract_path", "extract_records", "map_record", "is_valid_path"]

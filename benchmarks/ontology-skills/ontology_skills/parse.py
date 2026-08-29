"""Parse model text into a GraphDelta. Fail closed: no second-pass repair."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .graph_delta import GraphDelta


@dataclass(frozen=True, slots=True)
class ParseResult:
    ok: bool
    predicted: GraphDelta
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "error": self.error,
            "predicted": self.predicted.to_dict(),
        }


def parse_graph_delta(text: str) -> ParseResult:
    """Extract one JSON object and load it as GraphDelta.

    Parse failure returns ``ok=False`` and an empty predicted delta. Callers
    must score that empty delta (success false vs non-empty gold) and must not
    send the text to another model.
    """
    try:
        blob = _extract_json_object(text)
    except ValueError as exc:
        return _fail(str(exc))
    if blob is None:
        return _fail("no JSON object in model text")
    try:
        raw = json.loads(blob)
    except json.JSONDecodeError as exc:
        return _fail(f"invalid JSON: {exc.msg}")
    if not isinstance(raw, dict):
        return _fail("JSON root is not an object")
    try:
        predicted = GraphDelta.from_dict(raw)
    except (TypeError, KeyError, ValueError) as exc:
        return _fail(f"not a GraphDelta: {exc}")
    return ParseResult(ok=True, predicted=predicted, error=None)


def _fail(message: str) -> ParseResult:
    return ParseResult(ok=False, predicted=GraphDelta(), error=message)


def _extract_json_object(text: str) -> str | None:
    stripped = _strip_fence(text).strip()
    start = stripped.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for idx in range(start, len(stripped)):
        ch = stripped[idx]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return stripped[start : idx + 1]
    raise ValueError("unbalanced JSON braces")


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)

"""Minimal YAML dump/load for JSON-compatible Blueprint documents.

The protocol is YAML-at-rest (ADR 0014). We emit a conservative subset
(block mappings/lists, quoted strings when needed) so inspect is `cat`
and we do not take a PyYAML dependency. The loader only accepts what
this dumper produces plus a few human-edited equivalents (plain scalars,
nested maps/lists). Tags, anchors, and merge keys are rejected.
"""

from __future__ import annotations

from typing import Any


class YamlError(ValueError):
    """Not a Blueprint YAML document we can classify."""


def dump_yaml(data: Any) -> str:
    """Serialize a JSON-compatible value as UTF-8 YAML."""
    lines: list[str] = []
    _dump(data, lines, indent=0)
    text = "\n".join(lines)
    return text + ("\n" if text and not text.endswith("\n") else "")


def load_yaml(text: str) -> Any:
    """Parse YAML emitted by :func:`dump_yaml` (and close human edits)."""
    if not isinstance(text, str):
        raise YamlError("YAML text must be a string")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    value, end = _parse_block(lines, 0, 0)
    while end < len(lines) and not lines[end].strip():
        end += 1
    if end < len(lines) and lines[end].strip():
        raise YamlError(f"trailing content at line {end + 1}")
    return value


def _dump(data: Any, lines: list[str], indent: int) -> None:
    pad = "  " * indent
    if data is None:
        lines.append(f"{pad}null")
        return
    if isinstance(data, bool):
        lines.append(f"{pad}{'true' if data else 'false'}")
        return
    if isinstance(data, int) and not isinstance(data, bool):
        lines.append(f"{pad}{data}")
        return
    if isinstance(data, float):
        lines.append(f"{pad}{data}")
        return
    if isinstance(data, str):
        lines.append(f"{pad}{_quote(data)}")
        return
    if isinstance(data, list):
        if not data:
            lines.append(f"{pad}[]")
            return
        for item in data:
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}-")
                _dump(item, lines, indent + 1)
            else:
                item_lines: list[str] = []
                _dump(item, item_lines, 0)
                lines.append(f"{pad}- {item_lines[0].lstrip()}")
                for extra in item_lines[1:]:
                    lines.append(f"{pad}  {extra}")
        return
    if isinstance(data, dict):
        if not data:
            lines.append(f"{pad}{{}}")
            return
        for key, value in data.items():
            if not isinstance(key, str):
                raise YamlError(f"mapping key must be a string, got {type(key).__name__}")
            key_txt = _quote(key) if _needs_quote(key) else key
            if isinstance(value, (dict, list)) and value:
                lines.append(f"{pad}{key_txt}:")
                _dump(value, lines, indent + 1)
            else:
                val_lines: list[str] = []
                _dump(value, val_lines, 0)
                lines.append(f"{pad}{key_txt}: {val_lines[0].lstrip()}")
        return
    raise YamlError(f"cannot emit {type(data).__name__} as YAML")


def _needs_quote(value: str) -> bool:
    if value == "" or value.strip() != value:
        return True
    if value in {"true", "false", "null", "True", "False", "None", "~"}:
        return True
    if value[0] in "-?:{}[],&*!|>'\"%@`":
        return True
    if any(ch in value for ch in [": ", " #", "\n", "\t", "'", '"']):
        return True
    if value.replace(".", "", 1).replace("-", "", 1).isdigit():
        return True
    return False


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _parse_block(lines: list[str], idx: int, min_indent: int) -> tuple[Any, int]:
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx >= len(lines):
        return None, idx
    raw = lines[idx]
    if raw.strip().startswith("#"):
        return _parse_block(lines, idx + 1, min_indent)
    indent = _indent_of(raw)
    if indent < min_indent:
        raise YamlError(f"unexpected dedent at line {idx + 1}")
    stripped = raw.strip()
    if stripped.startswith("-"):
        return _parse_list(lines, idx, indent)
    if stripped in {"{}", "[]"} or ":" in stripped or stripped.endswith(":"):
        # A bare scalar that happens to contain ":" is quoted by the dumper.
        if stripped.endswith(":") or (": " in stripped and not stripped.startswith('"')):
            return _parse_map(lines, idx, indent)
    return _parse_scalar(stripped), idx + 1


def _parse_list(lines: list[str], idx: int, indent: int) -> tuple[list[Any], int]:
    items: list[Any] = []
    while idx < len(lines):
        raw = lines[idx]
        if not raw.strip() or raw.strip().startswith("#"):
            idx += 1
            continue
        if _indent_of(raw) != indent or not raw.strip().startswith("-"):
            break
        rest = raw.strip()[1:].strip()
        if rest == "":
            value, idx = _parse_block(lines, idx + 1, indent + 1)
            items.append(value)
            continue
        if rest.endswith(":") or (": " in rest and not rest.startswith('"')):
            # inline map start on the same line as `-`
            fake = " " * (indent + 2) + rest
            sub_lines = [fake, *lines[idx + 1 :]]
            value, rel = _parse_map(sub_lines, 0, indent + 2)
            items.append(value)
            idx = idx + rel
            continue
        items.append(_parse_scalar(rest))
        idx += 1
    return items, idx


def _parse_map(lines: list[str], idx: int, indent: int) -> tuple[dict[str, Any], int]:
    mapping: dict[str, Any] = {}
    while idx < len(lines):
        raw = lines[idx]
        if not raw.strip() or raw.strip().startswith("#"):
            idx += 1
            continue
        if _indent_of(raw) < indent:
            break
        if _indent_of(raw) != indent:
            raise YamlError(f"bad indent at line {idx + 1}")
        stripped = raw.strip()
        if stripped.startswith("-"):
            break
        if ":" not in stripped:
            raise YamlError(f"expected mapping at line {idx + 1}")
        key_txt, _, rest = stripped.partition(":")
        key = _parse_scalar(key_txt.strip())
        if not isinstance(key, str):
            key = str(key)
        rest = rest.strip()
        if rest == "":
            # nested value on following lines, or empty
            nxt = idx + 1
            while nxt < len(lines) and not lines[nxt].strip():
                nxt += 1
            if nxt >= len(lines) or _indent_of(lines[nxt]) <= indent:
                mapping[key] = None
                idx += 1
                continue
            value, idx = _parse_block(lines, nxt, indent + 1)
            mapping[key] = value
            continue
        mapping[key] = _parse_scalar(rest)
        idx += 1
    return mapping, idx


def _parse_scalar(token: str) -> Any:
    if token in {"null", "~", ""}:
        return None
    if token == "true":
        return True
    if token == "false":
        return False
    if token in {"[]"}:
        return []
    if token in {"{}"}:
        return {}
    if len(token) >= 2 and token[0] == token[-1] == '"':
        body = token[1:-1]
        return (
            body.replace("\\n", "\n")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )
    if len(token) >= 2 and token[0] == token[-1] == "'":
        return token[1:-1]
    if token.lstrip("-").isdigit():
        return int(token)
    try:
        if any(ch in token for ch in ".eE"):
            return float(token)
    except ValueError:
        pass
    return token


__all__ = ["YamlError", "dump_yaml", "load_yaml"]

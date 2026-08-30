"""Extract with exactly one already-bound skill. No second skill in scope."""

from __future__ import annotations

import re
from typing import Any, Protocol, runtime_checkable

from vrdu_binder.protocol import ProtocolError, require_one_skill
from vrdu_binder.skills import Skill, assert_extract_keys_subset


def entity_item(name: str, text: str) -> list[Any]:
    """Stock VRDU extraction item (JSON lists; eval toolkit accepts this)."""
    return [name, [text, [0, 0.0, 0.0, 0.0, 0.0], [[0, max(0, len(text))]]]]


@runtime_checkable
class Extractor(Protocol):
    def extract(self, prompt: str, skill: Skill) -> list[Any]:
        """Return VRDU entity items using only ``skill``."""


def extract_one(extractor: Extractor, prompt: str, skill: Skill) -> list[Any]:
    require_one_skill(skill)
    items = extractor.extract(prompt, skill)
    if not isinstance(items, list):
        raise ProtocolError("extractor must return a list of entity items")
    emitted: list[str] = []
    for item in items:
        if not isinstance(item, (list, tuple)) or not item:
            raise ProtocolError("malformed extraction item")
        emitted.append(str(item[0]))
    assert_extract_keys_subset(skill, emitted)
    return items


class KeywordExtractor:
    """Dry-run extractor: ``<key> <value>`` pairs for keys on the one skill."""

    _pair = re.compile(r"(?P<key>[A-Za-z][A-Za-z0-9_]*)\s+(?P<val>\S+)")

    def extract(self, prompt: str, skill: Skill) -> list[Any]:
        require_one_skill(skill)
        allowed = set(skill.keys)
        found: dict[str, str] = {}
        for match in self._pair.finditer(prompt):
            key = match.group("key")
            if key in allowed and key not in found:
                found[key] = match.group("val")
        return [entity_item(k, v) for k, v in found.items()]

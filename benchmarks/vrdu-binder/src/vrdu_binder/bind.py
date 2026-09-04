"""Bind exactly one type, then look up that type's one skill."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, runtime_checkable

from vrdu_binder.protocol import ProtocolError
from vrdu_binder.skills import Skill


@runtime_checkable
class Binder(Protocol):
    def bind(self, prompt: str, catalog: "TypeCatalog") -> str:
        """Return exactly one type_id from ``catalog``."""


@dataclass(frozen=True)
class TypeCatalog:
    """Schema-key sets only. No corpus or type nicknames in the payload."""

    keys_by_type: Mapping[str, tuple[str, ...]]

    def type_ids(self) -> tuple[str, ...]:
        return tuple(self.keys_by_type.keys())

    def describe_for_model(self) -> list[dict[str, object]]:
        """Catalog the bind model may see. Keys only, no display names."""
        return [
            {"id": type_id, "keys": list(keys)}
            for type_id, keys in self.keys_by_type.items()
        ]


def bind_one(binder: Binder, prompt: str, catalog: TypeCatalog) -> str:
    type_id = binder.bind(prompt, catalog)
    allowed = set(catalog.type_ids())
    if type_id not in allowed:
        raise ProtocolError(f"binder returned {type_id!r}, not in {sorted(allowed)}")
    return type_id


def skill_for_bind(type_id: str, skills: Mapping[str, Skill]) -> Skill:
    if type_id not in skills:
        raise ProtocolError(f"no skill registered for bound type {type_id!r}")
    skill = skills[type_id]
    if skill.type_id != type_id:
        raise ProtocolError("skill type_id does not match the bind")
    return skill


class KeywordBinder:
    """Dry-run binder: score OCR overlap with each type's key names."""

    def bind(self, prompt: str, catalog: TypeCatalog) -> str:
        text = prompt.lower()
        best_id: str | None = None
        best_score = -1
        for type_id, keys in catalog.keys_by_type.items():
            score = sum(1 for key in keys if key.lower() in text)
            if score > best_score:
                best_score = score
                best_id = type_id
        if best_id is None:
            raise ProtocolError("empty type catalog")
        return best_id


class ForcedBinder:
    """Test double: always bind a given type."""

    def __init__(self, type_id: str) -> None:
        self.type_id = type_id

    def bind(self, prompt: str, catalog: TypeCatalog) -> str:
        if self.type_id not in catalog.type_ids():
            raise ProtocolError(f"forced type {self.type_id!r} not in catalog")
        return self.type_id

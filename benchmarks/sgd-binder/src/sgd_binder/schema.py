"""SGD schemas → opaque type ids. Slot keys only in the catalog."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from sgd_binder.protocol import ProtocolError


@dataclass(frozen=True)
class ServiceSchema:
    service_name: str
    type_id: str
    slots: tuple[str, ...]
    slot_descriptions: Mapping[str, str]
    seen_in_train: bool


@dataclass(frozen=True)
class TypeCatalog:
    by_type: Mapping[str, ServiceSchema]
    by_service: Mapping[str, ServiceSchema]

    def type_ids(self) -> tuple[str, ...]:
        return tuple(self.by_type)

    def describe_for_model(self) -> list[dict[str, Any]]:
        rows = []
        for type_id, sch in self.by_type.items():
            rows.append({"id": type_id, "keys": list(sch.slots)})
        return rows


_DOMAIN_RE = re.compile(r"\b[A-Za-z]+_\d+\b")


def domain_token(service_name: str) -> str:
    return service_name.split("_", 1)[0]


def load_schema_list(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text())
    if not isinstance(raw, list):
        raise ProtocolError(f"schema file is not a list: {path}")
    return raw


def build_catalog(
    *,
    train_schemas: Sequence[Mapping[str, Any]],
    test_schemas: Sequence[Mapping[str, Any]],
) -> TypeCatalog:
    """Union of train+test schemas. Opaque ids from sorted service names."""
    train_names = {str(s["service_name"]) for s in train_schemas}
    merged: dict[str, Mapping[str, Any]] = {}
    for block in (train_schemas, test_schemas):
        for sch in block:
            merged[str(sch["service_name"])] = sch
    by_type: dict[str, ServiceSchema] = {}
    by_service: dict[str, ServiceSchema] = {}
    for i, name in enumerate(sorted(merged)):
        sch = merged[name]
        slots = tuple(str(sl["name"]) for sl in sch.get("slots") or [])
        desc = {
            str(sl["name"]): str(sl.get("description") or "")
            for sl in sch.get("slots") or []
        }
        type_id = f"type_{i}"
        row = ServiceSchema(
            service_name=name,
            type_id=type_id,
            slots=slots,
            slot_descriptions=desc,
            seen_in_train=name in train_names,
        )
        by_type[type_id] = row
        by_service[name] = row
    return TypeCatalog(by_type=by_type, by_service=by_service)


def leak_needles(catalog: TypeCatalog) -> tuple[str, ...]:
    """service_name only. Slot descriptions may use common nouns (hotel)."""
    return tuple(s.service_name for s in catalog.by_service.values())


def redact_needles(catalog: TypeCatalog) -> tuple[str, ...]:
    names = list(leak_needles(catalog))
    domains = sorted({domain_token(n) for n in names})
    return tuple(names + domains)


def redact_utterance(text: str, needles: Sequence[str]) -> str:
    out = text
    for n in sorted(needles, key=len, reverse=True):
        out = re.sub(rf"\b{re.escape(n)}\b", "[service]", out, flags=re.I)
    out = _DOMAIN_RE.sub("[service]", out)
    return out


def assert_no_leaks(blob: str, needles: Sequence[str]) -> None:
    lower = blob.lower()
    for n in needles:
        if n.lower() in lower:
            raise ProtocolError(f"prompt leaked {n!r}")

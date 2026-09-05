"""One skill per service. Schema JSON only. No test-dialogue gold."""

from __future__ import annotations

from dataclasses import dataclass

from sgd_binder.protocol import ProtocolError
from sgd_binder.schema import ServiceSchema, TypeCatalog, assert_no_leaks


@dataclass(frozen=True)
class Skill:
    type_id: str
    keys: tuple[str, ...]
    body: str


def write_skill(sch: ServiceSchema, needles: tuple[str, ...]) -> Skill:
    lines = ["Extract these keys as a JSON object. Omit missing keys."]
    for key in sch.slots:
        desc = (sch.slot_descriptions.get(key) or key).strip()
        lines.append(f"- {key}: {desc}")
    body = "\n".join(lines)
    assert_no_leaks(body, needles)
    if sch.service_name.lower() in body.lower():
        raise ProtocolError("skill leaked service_name")
    return Skill(type_id=sch.type_id, keys=sch.slots, body=body)


def write_skills(catalog: TypeCatalog, needles: tuple[str, ...]) -> dict[str, Skill]:
    return {tid: write_skill(sch, needles) for tid, sch in catalog.by_type.items()}

"""Train-only Infona LoRA JSONL. No test-dialogue gold. Dev unused."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from sgd_binder.instances import Instance
from sgd_binder.llm import bind_system
from sgd_binder.protocol import ProtocolError
from sgd_binder.schema import TypeCatalog, leak_needles
from sgd_binder.skills import Skill


def _row(system: str, user: str, assistant: str) -> dict:
    return {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
            {"role": "assistant", "content": assistant},
        ]
    }


def select_instances(
    instances: list[Instance], *, max_per_service: int
) -> list[Instance]:
    if max_per_service <= 0:
        return list(instances)
    buckets: dict[str, list[Instance]] = defaultdict(list)
    for inst in instances:
        if len(buckets[inst.service_name]) < max_per_service:
            buckets[inst.service_name].append(inst)
    out: list[Instance] = []
    for name in sorted(buckets):
        out.extend(buckets[name])
    return out


def write_infona_together_jsonl(
    instances: list[Instance],
    catalog: TypeCatalog,
    skills: dict[str, Skill],
    out_path: Path,
    *,
    max_per_service: int = 0,
) -> Path:
    needles = leak_needles(catalog)
    chosen = select_instances(instances, max_per_service=max_per_service)
    if not chosen:
        raise ProtocolError("no train instances for LoRA")
    system_bind = bind_system(catalog)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_path.open("w") as fh:
        for inst in chosen:
            if not inst.seen_in_train:
                raise ProtocolError(f"train LoRA saw unseen service {inst.service_name}")
            skill = skills[inst.type_id]
            bind_row = _row(system_bind, inst.prompt, inst.type_id)
            extract_row = _row(skill.body, inst.prompt, json.dumps(dict(inst.gold_slots)))
            for row in (bind_row, extract_row):
                blob = json.dumps(row)
                for nle in needles:
                    if nle.lower() in blob.lower():
                        raise ProtocolError(f"LoRA row leaked {nle!r}")
                fh.write(blob + "\n")
                n += 1
    sidecar = out_path.with_suffix(out_path.suffix + ".meta.json")
    sidecar.write_text(
        json.dumps(
            {
                "recipe": "infona",
                "n_rows": n,
                "n_instances": len(chosen),
                "max_per_service": max_per_service,
                "fixture": False,
            },
            indent=2,
        )
        + "\n"
    )
    return out_path

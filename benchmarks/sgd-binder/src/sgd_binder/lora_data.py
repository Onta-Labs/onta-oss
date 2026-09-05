"""Train-only LoRA JSONL. No test-dialogue gold. Dev unused."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from sgd_binder.instances import Instance
from sgd_binder.llm import BARE_EXTRACT_SYSTEM, bare_bind_system, bind_system
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
            n += _write_checked(fh, (bind_row, extract_row), needles)
    _write_meta(
        out_path,
        recipe="infona",
        n_rows=n,
        n_instances=len(chosen),
        max_per_service=max_per_service,
    )
    return out_path


def write_vanilla_together_jsonl(
    instances: list[Instance],
    catalog: TypeCatalog,
    out_path: Path,
    *,
    max_per_service: int = 0,
) -> Path:
    """Bare bind + generic extract. No catalog keys. Matches 0.8b_vanilla_ft inference."""
    needles = leak_needles(catalog)
    chosen = select_instances(instances, max_per_service=max_per_service)
    if not chosen:
        raise ProtocolError("no train instances for LoRA")
    system_bind = bare_bind_system(catalog)
    if "keys:" in system_bind.lower():
        raise ProtocolError("vanilla LoRA bind leaked catalog keys")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_path.open("w") as fh:
        for inst in chosen:
            if not inst.seen_in_train:
                raise ProtocolError(f"train LoRA saw unseen service {inst.service_name}")
            bind_row = _row(system_bind, inst.prompt, inst.type_id)
            extract_row = _row(
                BARE_EXTRACT_SYSTEM, inst.prompt, json.dumps(dict(inst.gold_slots))
            )
            n += _write_checked(fh, (bind_row, extract_row), needles)
    _write_meta(
        out_path,
        recipe="vanilla",
        n_rows=n,
        n_instances=len(chosen),
        max_per_service=max_per_service,
    )
    return out_path


def _write_checked(fh, rows: tuple[dict, ...], needles: tuple[str, ...]) -> int:
    n = 0
    for row in rows:
        blob = json.dumps(row)
        for nle in needles:
            if nle.lower() in blob.lower():
                raise ProtocolError(f"LoRA row leaked {nle!r}")
        fh.write(blob + "\n")
        n += 1
    return n


def _write_meta(
    out_path: Path,
    *,
    recipe: str,
    n_rows: int,
    n_instances: int,
    max_per_service: int,
) -> None:
    sidecar = out_path.with_suffix(out_path.suffix + ".meta.json")
    sidecar.write_text(
        json.dumps(
            {
                "recipe": recipe,
                "n_rows": n_rows,
                "n_instances": n_instances,
                "max_per_service": max_per_service,
                "fixture": False,
            },
            indent=2,
        )
        + "\n"
    )

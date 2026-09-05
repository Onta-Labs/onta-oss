"""Dialogue JSON → one instance per (dialogue, service) last filled USER frame."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from sgd_binder.protocol import ProtocolError
from sgd_binder.schema import TypeCatalog, redact_utterance


@dataclass(frozen=True)
class Instance:
    instance_id: str
    service_name: str
    type_id: str
    seen_in_train: bool
    prompt: str
    gold_slots: Mapping[str, str]


def load_dialogues(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text())
    if not isinstance(raw, list):
        raise ProtocolError(f"dialogues file is not a list: {path}")
    return raw


def instances_from_dialogues(
    dialogues: Sequence[Mapping[str, Any]],
    catalog: TypeCatalog,
    *,
    needles: Sequence[str],
) -> list[Instance]:
    out: list[Instance] = []
    for dlg in dialogues:
        did = str(dlg.get("dialogue_id") or "")
        if not did:
            raise ProtocolError("dialogue missing dialogue_id")
        last: dict[str, tuple[str, dict[str, str]]] = {}
        history: list[str] = []
        for turn in dlg.get("turns") or []:
            speaker = str(turn.get("speaker") or "")
            utt = redact_utterance(str(turn.get("utterance") or ""), needles)
            if utt:
                history.append(f"{speaker}: {utt}")
            if speaker != "USER":
                continue
            for frame in turn.get("frames") or []:
                service = str(frame.get("service") or "")
                state = frame.get("state") or {}
                if not isinstance(state, dict):
                    continue
                intent = str(state.get("active_intent") or "NONE")
                if intent == "NONE":
                    continue
                slots = _first_values(state.get("slot_values") or {})
                if not slots:
                    continue
                last[service] = ("\n".join(history), slots)
        for service, (prompt, slots) in last.items():
            sch = catalog.by_service.get(service)
            if sch is None:
                raise ProtocolError(f"dialogue uses unknown service {service!r}")
            out.append(
                Instance(
                    instance_id=f"{did}::{service}",
                    service_name=service,
                    type_id=sch.type_id,
                    seen_in_train=sch.seen_in_train,
                    prompt=prompt,
                    gold_slots=slots,
                )
            )
    return out


def _first_values(slot_values: Mapping[str, Any]) -> dict[str, str]:
    gold: dict[str, str] = {}
    for key, vals in slot_values.items():
        if isinstance(vals, list) and vals:
            gold[str(key)] = str(vals[0])
        elif isinstance(vals, str) and vals:
            gold[str(key)] = vals
    return gold

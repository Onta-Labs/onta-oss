"""Train-only LoRA JSONL. Valid and test never enter the file."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from vrdu_binder.bare import BARE_BIND_SYSTEM, BARE_EXTRACT_SYSTEM
from vrdu_binder.bind import TypeCatalog
from vrdu_binder.llm import bind_system_prompt, extract_system_prompt
from vrdu_binder.ocr import bind_prompt
from vrdu_binder.protocol import ProtocolError
from vrdu_binder.skills import Skill, annotation_values
from vrdu_binder.splits import RunSplit

RECIPES = ("vanilla", "infona")


def write_lora_jsonl(
    *,
    recipe: str,
    split_by_type: Mapping[str, RunSplit],
    docs_by_type: Mapping[str, Sequence[Mapping[str, Any]]],
    skills: Mapping[str, Skill],
    catalog: TypeCatalog,
    seed: int,
    out_path: Path | str,
) -> Path:
    if recipe not in RECIPES:
        raise ProtocolError(f"lora recipe must be one of {RECIPES}, got {recipe!r}")
    rows: list[dict[str, Any]] = []
    for type_id, split in split_by_type.items():
        train_set = set(split.train)
        train_docs = [
            d for d in docs_by_type[type_id] if str(d.get("filename") or "") in train_set
        ]
        if not train_docs:
            raise ProtocolError(f"lora writer has no train docs for {type_id}")
        for doc in train_docs:
            name = str(doc.get("filename") or "")
            if not name:
                raise ProtocolError("lora writer needs filename for the train allowlist")
            split.assert_train_only([name], what="lora writer")
            ocr = bind_prompt(doc)
            skill = skills[type_id]
            gold = annotation_values(doc.get("annotations"), set(skill.keys))
            bind_sys = BARE_BIND_SYSTEM if recipe == "vanilla" else bind_system_prompt(catalog)
            ext_sys = BARE_EXTRACT_SYSTEM if recipe == "vanilla" else extract_system_prompt(skill)
            if recipe == "vanilla":
                if skill.body.strip() and skill.body.strip() in ext_sys:
                    raise ProtocolError("vanilla LoRA must not include a skill body")
                if any(f"- {k}" in bind_sys for k in catalog.keys_by_type.get(type_id, ())):
                    raise ProtocolError("vanilla LoRA bind must not list catalog keys")
            else:
                if skill.body.strip() not in ext_sys:
                    raise ProtocolError("infona LoRA extract rows must include the skill body")
            rows.append(_chat(bind_sys, ocr, type_id, name=name, task="bind"))
            if gold:
                rows.append(_chat(ext_sys, ocr, json.dumps(gold, ensure_ascii=False), name=name, task="extract"))
    dest = Path(out_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    manifest = dest.with_suffix(".manifest.json")
    train_names = sorted({r["filename"] for r in rows})
    _assert_no_heldout_names(train_names, split_by_type)
    manifest.write_text(
        json.dumps(
            {
                "recipe": recipe,
                "seed": seed,
                "n_rows": len(rows),
                "train_filenames": train_names,
                "valid_used": False,
                "test_used": False,
                "early_stopping": "none",
                "model_selection": "none",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return dest


def _chat(system: str, user: str, assistant: str, *, name: str, task: str) -> dict[str, Any]:
    return {
        "filename": name,
        "task": task,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
            {"role": "assistant", "content": assistant},
        ],
    }


def _assert_no_heldout_names(names: Sequence[str], split_by_type: Mapping[str, RunSplit]) -> None:
    held = set()
    for split in split_by_type.values():
        held.update(split.test)
    overlap = [n for n in names if n in held]
    if overlap:
        raise ProtocolError(f"lora jsonl leaked test filenames {overlap!r}")


def assert_lora_jsonl_train_only(path: Path | str, split_by_type: Mapping[str, RunSplit]) -> None:
    """CI check: every row filename is in some train list, never test."""
    train = set()
    test = set()
    for split in split_by_type.values():
        train.update(split.train)
        test.update(split.test)
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        name = row.get("filename")
        if name not in train:
            raise ProtocolError(f"lora row filename {name!r} is not train")
        if name in test:
            raise ProtocolError(f"lora row filename {name!r} is on the test list")
        blob = json.dumps(row.get("messages"))
        if name and name in blob:
            raise ProtocolError(f"lora messages leaked filename {name!r}")

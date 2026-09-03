"""LoRA rows come from train only. Vanilla vs Infona recipes stay distinct."""

from __future__ import annotations

import json
from pathlib import Path

from vrdu_binder.bind import TypeCatalog
from vrdu_binder.cli import main
from vrdu_binder.fixtures import FIXTURE_KEYS, build_memory_fixtures
from vrdu_binder.lora_data import assert_lora_jsonl_train_only, write_lora_jsonl
from vrdu_binder.protocol import ProtocolError
from vrdu_binder.skills import write_skills_for_seed


def _ctx():
    mem = build_memory_fixtures()
    skills = write_skills_for_seed(
        split_by_type=mem["splits"],
        docs_by_type=mem["docs_by_type"],
        seed=0,
        keys_by_type=FIXTURE_KEYS,
    )
    catalog = TypeCatalog(keys_by_type=FIXTURE_KEYS)
    return mem, skills, catalog


def test_vanilla_rows_omit_skill_and_catalog(tmp_path):
    mem, skills, catalog = _ctx()
    path = write_lora_jsonl(
        recipe="vanilla",
        split_by_type=mem["splits"],
        docs_by_type=mem["docs_by_type"],
        skills=skills,
        catalog=catalog,
        seed=0,
        out_path=tmp_path / "vanilla.jsonl",
    )
    systems = _system_texts(path)
    assert all(skills["type_0"].body.strip() not in sys for sys in systems)
    assert all(skills["type_1"].body.strip() not in sys for sys in systems)
    assert all("Pick exactly one type id" not in sys for sys in systems)
    blob = path.read_text(encoding="utf-8")
    assert "W-100" in blob
    assert "LEAK_VALID_A" not in blob
    assert "W-200" not in blob
    assert "valid_a_1.pdf" not in blob
    assert "test_a_1.pdf" not in blob
    assert_lora_jsonl_train_only(path, mem["splits"])


def test_infona_rows_include_one_skill(tmp_path):
    mem, skills, catalog = _ctx()
    path = write_lora_jsonl(
        recipe="infona",
        split_by_type=mem["splits"],
        docs_by_type=mem["docs_by_type"],
        skills=skills,
        catalog=catalog,
        seed=0,
        out_path=tmp_path / "infona.jsonl",
    )
    systems = _system_texts(path)
    extract_systems = _system_texts(path, task="extract")
    assert any(skills["type_0"].body.strip() in sys for sys in extract_systems)
    assert any(skills["type_1"].body.strip() in sys for sys in extract_systems)
    for sys in extract_systems:
        has0 = skills["type_0"].body.strip() in sys
        has1 = skills["type_1"].body.strip() in sys
        assert has0 ^ has1
    assert any("Pick exactly one type id" in sys for sys in systems)
    assert "LEAK_VALID_B" not in path.read_text(encoding="utf-8")
    assert_lora_jsonl_train_only(path, mem["splits"])


def test_empty_train_pile_refuses():
    mem, skills, catalog = _ctx()
    only_valid = {
        "type_0": [d for d in mem["docs_by_type"]["type_0"] if "valid" in d["filename"]],
        "type_1": [d for d in mem["docs_by_type"]["type_1"] if "valid" in d["filename"]],
    }
    try:
        write_lora_jsonl(
            recipe="vanilla",
            split_by_type=mem["splits"],
            docs_by_type=only_valid,
            skills=skills,
            catalog=catalog,
            seed=0,
            out_path="/tmp/should-not-write.jsonl",
        )
    except ProtocolError as exc:
        assert "no train docs" in str(exc)
    else:
        raise AssertionError("valid-only pile must refuse")


def test_cli_write_lora_fixtures(tmp_path):
    dest = tmp_path / "v.jsonl"
    assert main(["write-lora-data", "--recipe", "vanilla", "--fixtures", "--out", str(dest)]) == 0
    meta = json.loads(dest.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    assert meta["valid_used"] is False
    assert meta["test_used"] is False
    assert meta["early_stopping"] == "none"
    assert Path(dest).is_file()


def test_train_lora_check_script(tmp_path):
    dest = tmp_path / "v.jsonl"
    main(["write-lora-data", "--recipe", "vanilla", "--fixtures", "--out", str(dest)])
    import importlib.util

    script = Path(__file__).resolve().parents[1] / "scripts" / "train_lora.py"
    spec = importlib.util.spec_from_file_location("train_lora_check", script)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main(["check", "--jsonl", str(dest)]) == 0


def _system_texts(path: Path, task: str | None = None) -> list[str]:
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if task is not None and row.get("task") != task:
            continue
        out.append(row["messages"][0]["content"])
    return out

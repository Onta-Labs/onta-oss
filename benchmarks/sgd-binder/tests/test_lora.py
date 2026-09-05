"""LoRA JSONL is train-only and leak-honest. Vanilla has no catalog keys."""

from pathlib import Path
import json

from sgd_binder.cli import main
from sgd_binder.fixtures import fixture_catalog


def _assert_train_only(path: Path) -> list[dict]:
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 4  # two seen instances × bind+extract
    blob = "\n".join(lines).lower()
    for n in fixture_catalog().by_service:
        assert n.lower() not in blob
    assert "sprocket_1" not in blob
    rows = [json.loads(ln) for ln in lines]
    assistants = [r["messages"][2]["content"] for r in rows]
    assert not any("S-1" in a for a in assistants)
    assert [m["role"] for m in rows[0]["messages"]] == ["system", "user", "assistant"]
    return rows


def test_fixture_lora_omits_unseen_and_service_names(tmp_path: Path) -> None:
    out = tmp_path / "infona.jsonl"
    assert main(["write-lora-data", "--recipe", "infona", "--fixtures", "--out", str(out)]) == 0
    rows = _assert_train_only(out)
    assert "keys:" in rows[0]["messages"][0]["content"].lower()


def test_fixture_vanilla_lora_has_no_catalog_keys(tmp_path: Path) -> None:
    out = tmp_path / "vanilla.jsonl"
    assert main(["write-lora-data", "--recipe", "vanilla", "--fixtures", "--out", str(out)]) == 0
    rows = _assert_train_only(out)
    bind_sys = rows[0]["messages"][0]["content"]
    assert "keys:" not in bind_sys.lower()
    assert "Reply with exactly one id:" in bind_sys
    extract_sys = rows[1]["messages"][0]["content"]
    assert "Extract a JSON object" in extract_sys
    meta = json.loads(out.with_suffix(out.suffix + ".meta.json").read_text())
    assert meta["recipe"] == "vanilla"
    assert meta["n_rows"] == 4

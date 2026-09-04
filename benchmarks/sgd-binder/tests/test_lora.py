"""Infona LoRA JSONL is train-only and leak-honest."""

from pathlib import Path
import json

from sgd_binder.cli import main
from sgd_binder.fixtures import fixture_catalog


def test_fixture_lora_omits_unseen_and_service_names(tmp_path: Path) -> None:
    out = tmp_path / "infona.jsonl"
    assert main(["write-lora-data", "--recipe", "infona", "--fixtures", "--out", str(out)]) == 0
    lines = out.read_text().strip().splitlines()
    assert len(lines) == 4  # two seen instances × bind+extract
    blob = "\n".join(lines).lower()
    for n in fixture_catalog().by_service:
        assert n.lower() not in blob
    assert "sprocket_1" not in blob
    assistants = [json.loads(ln)["messages"][2]["content"] for ln in lines]
    assert not any("S-1" in a for a in assistants)
    row = json.loads(lines[0])
    assert [m["role"] for m in row["messages"]] == ["system", "user", "assistant"]

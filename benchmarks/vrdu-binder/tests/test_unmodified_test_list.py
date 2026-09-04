"""Freeze 4: published test membership is copied, never rewritten."""

from __future__ import annotations

import json

import pytest

from vrdu_binder.constants import (
    ADBUY_SPLIT_NAME,
    REGISTRATION_SPLIT_NAME,
    SEEDS,
    split_filename,
    split_url,
)
from vrdu_binder.dump import build_results, write_predictions
from vrdu_binder.extract import entity_item
from vrdu_binder.fixtures import build_memory_fixtures
from vrdu_binder.protocol import ProtocolError
from vrdu_binder.splits import load_run_split, run_split_from_raw


def test_published_split_filenames_match_the_lock():
    for seed in SEEDS:
        reg = split_filename("registration", seed)
        ad = split_filename("adbuy", seed)
        assert reg == REGISTRATION_SPLIT_NAME.format(seed=seed) + ".json"
        assert ad == ADBUY_SPLIT_NAME.format(seed=seed) + ".json"
        assert "lv2" in reg
        assert "lv2" not in ad
        assert "mixed_template" in reg and "mixed_template" in ad
        assert split_url("registration", seed).endswith(
            f"registration-form/few_shot-splits/{reg}"
        )
        assert split_url("adbuy", seed).endswith(f"ad-buy-form/few_shot-splits/{ad}")


def test_dump_keys_equal_test_list_in_order():
    mem = build_memory_fixtures()
    split = mem["splits"]["type_0"]
    results = build_results(
        split, {"test_a_1.pdf": [entity_item("widget_id", "W-200")]}
    )
    assert tuple(results) == split.test
    assert "valid_a_1.pdf" not in results
    assert "train_a_1.pdf" not in results


def test_rewritten_test_list_is_rejected(tmp_path):
    raw = {"train": ["t.pdf"], "valid": ["v.pdf"], "test": ["a.pdf", "b.pdf"]}
    split = run_split_from_raw(raw)
    with pytest.raises(ProtocolError, match="rewritten"):
        split.assert_unmodified_test(["a.pdf"])


def test_written_json_keeps_every_test_name(tmp_path):
    mem = build_memory_fixtures()
    split = mem["splits"]["type_0"]
    path = write_predictions(
        out_dir=tmp_path,
        split_name="SYNTH-mixed_template-train_1-test_2-valid_1-SD_0",
        split=split,
        filled={"test_a_1.pdf": [entity_item("widget_id", "W-200")]},
        misbound={"test_a_misbind.pdf"},
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert list(payload["results"]) == list(split.test)


def test_load_run_split_drops_valid(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(
        json.dumps({"train": ["tr.pdf"], "valid": ["va.pdf"], "test": ["te.pdf"]}),
        encoding="utf-8",
    )
    split = load_run_split(path)
    assert split.train == ("tr.pdf",)
    assert split.test == ("te.pdf",)
    assert not hasattr(split, "valid") or getattr(split, "valid", None) is None

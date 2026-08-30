"""Dry-run / fixture mode: no VRDU download, no LLM."""

from __future__ import annotations

import json
import pytest

from vrdu_binder.cli import main
from vrdu_binder.constants import (
    ADBUY_KEYS,
    EXCLUDED_STL_FORMNET_REGISTRATION_200,
    FOOTNOTE_FORMNET_ADBUY_MTL_200,
    FOOTNOTE_FORMNET_REGISTRATION_MTL_200,
    FOOTNOTE_LAYOUTLMV2_ADBUY_MTL_200,
    REGISTRATION_KEYS,
    SPEC_VERSION,
)
from vrdu_binder.fixtures import build_memory_fixtures


def test_spec_version_is_v11():
    assert SPEC_VERSION == "v11"


def test_official_keys_match_meta_json():
    assert REGISTRATION_KEYS == (
        "file_date",
        "foreign_principle_name",
        "registrant_name",
        "registration_num",
        "signer_name",
        "signer_title",
    )
    assert "channel" in ADBUY_KEYS
    assert "sub_amount" in ADBUY_KEYS
    assert "tv_address" in ADBUY_KEYS


def test_stl_formnet_number_is_excluded_constant():
    assert EXCLUDED_STL_FORMNET_REGISTRATION_200 == 92.12
    assert FOOTNOTE_FORMNET_REGISTRATION_MTL_200 == 90.51
    assert FOOTNOTE_LAYOUTLMV2_ADBUY_MTL_200 == 46.54
    assert FOOTNOTE_FORMNET_ADBUY_MTL_200 == 43.23


def test_cli_dry_run(tmp_path, capsys):
    assert main(["dry-run", "--out", str(tmp_path)]) == 0
    captured = capsys.readouterr().out
    headline = json.loads(captured[captured.index("{") :])
    assert headline["spec"] == "v11"
    assert headline["n_types"] == 2
    assert headline["n_bind_docs"] == 3
    assert headline["bind_at_type_accuracy"] == pytest.approx(2 / 3)
    assert "f1_wrong" not in headline
    dumps = list(tmp_path.rglob("*-test_predictions.json"))
    assert len(dumps) == 2
    for path in dumps:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["meta"]["bind"] == "predicted"


def test_memory_fixtures_are_disjoint():
    mem = build_memory_fixtures()
    a = set(mem["keys"]["type_0"])
    b = set(mem["keys"]["type_1"])
    assert a.isdisjoint(b)

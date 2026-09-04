"""Dry four-arm. Fixture stub is not an SGD score."""

from __future__ import annotations

from pathlib import Path

from sgd_binder.cli import main


def test_experiment_dry(tmp_path: Path) -> None:
    out = tmp_path / "dry"
    assert main(["experiment-dry", "--out", str(out)]) == 0
    for arm in ("27b_bare", "0.8b_bare", "0.8b_vanilla_ft", "0.8b_ft_infona"):
        pred = out / arm / "predictions.json"
        assert pred.is_file()

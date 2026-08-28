"""Shipped Blueprint seed packages. Protocol artifacts, not a registry."""

from pathlib import Path

SEEDS_DIR = Path(__file__).resolve().parent
CLINICAL_TRIALS = SEEDS_DIR / "clinical-trials"

__all__ = ["CLINICAL_TRIALS", "SEEDS_DIR"]

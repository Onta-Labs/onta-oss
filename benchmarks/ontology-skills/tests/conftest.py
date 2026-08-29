"""Pytest path so `ontology_skills` imports without installing the package."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LIVE_ENV_KEYS = (
    "INFONA_BENCH_BASE_URL",
    "INFONA_BENCH_MODEL",
    "INFONA_BENCH_API_KEY",
    "INFONA_BENCH_QUANTIZATION",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "MODEL",
)


@pytest.fixture
def clear_live_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop live-backend env so CI cannot accidentally POST."""
    for key in LIVE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

"""SGD URLs and locked models. No scores."""

from __future__ import annotations

from typing import Final

SPEC_VERSION: Final = "sgd-v1"
MODEL_27B: Final = "Qwen/Qwen3.5-27B"
MODEL_08B: Final = "Qwen/Qwen3.5-0.8B"
TOGETHER_BASE_URL: Final = "https://api.together.xyz/v1"
TOGETHER_USER_AGENT: Final = "sgd-binder/0.0.0"
SGD_REPO: Final = (
    "https://github.com/google-research-datasets/dstc8-schema-guided-dialogue"
)
SGD_RAW: Final = (
    "https://raw.githubusercontent.com/google-research-datasets/"
    "dstc8-schema-guided-dialogue/master"
)
SPLITS: Final = ("train", "dev", "test")
ARM_IDS: Final = ("27b_bare", "0.8b_bare", "0.8b_vanilla_ft", "0.8b_ft_infona")

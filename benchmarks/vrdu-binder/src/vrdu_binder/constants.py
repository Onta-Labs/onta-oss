"""Published VRDU paths, official meta.json keys, and leak denylist.

Split filenames are the Wang et al. mixed_template |D|=200 lists. Ad-buy has
no ``lv2`` token. Official keys are from each corpus ``meta.json``, not paper
Table 2 aliases.
"""

from __future__ import annotations

import re
from typing import Final

SPEC_VERSION: Final = "v11"

# Slide models. Say 27B (same family). Do not write "20B" or "32B".
# Together deprecated Qwen3-32B; the locked large bare model is Qwen3.5-27B.
MODEL_27B: Final = "Qwen/Qwen3.5-27B"
MODEL_08B: Final = "Qwen/Qwen3.5-0.8B"
MODEL_27B_URL: Final = "https://huggingface.co/Qwen/Qwen3.5-27B"
MODEL_08B_URL: Final = "https://huggingface.co/Qwen/Qwen3.5-0.8B"
TOGETHER_BASE_URL: Final = "https://api.together.xyz/v1"

VRDU_RAW_BASE: Final = (
    "https://raw.githubusercontent.com/google-research-datasets/vrdu/main"
)
VRDU_DATASET_REPO: Final = "https://github.com/google-research-datasets/vrdu"
VRDU_EVAL_REPO: Final = (
    "https://github.com/google-research/google-research/tree/master/vrdu"
)
VRDU_PAPER: Final = "https://arxiv.org/abs/2211.15421"
VRDU_EVAL_ISSUE_1882: Final = (
    "https://github.com/google-research/google-research/issues/1882"
)

TYPE_0: Final = "type_0"
TYPE_1: Final = "type_1"

CORPUS_REGISTRATION: Final = "registration"
CORPUS_ADBUY: Final = "adbuy"

# Internal harness ids. Never written into skill bodies or bind document prompts.
TYPE_FOR_CORPUS: Final[dict[str, str]] = {
    CORPUS_REGISTRATION: TYPE_0,
    CORPUS_ADBUY: TYPE_1,
}
CORPUS_FOR_TYPE: Final[dict[str, str]] = {
    TYPE_0: CORPUS_REGISTRATION,
    TYPE_1: CORPUS_ADBUY,
}

CORPUS_DIR: Final[dict[str, str]] = {
    CORPUS_REGISTRATION: "registration-form",
    CORPUS_ADBUY: "ad-buy-form",
}

# Official meta.json keys (Registration / FARA).
REGISTRATION_KEYS: Final[tuple[str, ...]] = (
    "file_date",
    "foreign_principle_name",
    "registrant_name",
    "registration_num",
    "signer_name",
    "signer_title",
)

# Official meta.json keys (Ad-buy / DeepForm), including line_item keys.
ADBUY_UNREPEATED_KEYS: Final[tuple[str, ...]] = (
    "advertiser",
    "agency",
    "contract_num",
    "flight_from",
    "flight_to",
    "gross_amount",
    "product",
    "property",
    "tv_address",
)
ADBUY_LINE_ITEM_KEYS: Final[tuple[str, ...]] = (
    "channel",
    "program_desc",
    "program_start_date",
    "program_end_date",
    "sub_amount",
)
ADBUY_KEYS: Final[tuple[str, ...]] = ADBUY_UNREPEATED_KEYS + ADBUY_LINE_ITEM_KEYS

KEYS_FOR_TYPE: Final[dict[str, tuple[str, ...]]] = {
    TYPE_0: REGISTRATION_KEYS,
    TYPE_1: ADBUY_KEYS,
}

SEEDS: Final[tuple[int, ...]] = (0, 1, 2)

# Exact published mixed_template |D|=200 filenames.
REGISTRATION_SPLIT_NAME: Final = (
    "FARA-lv2-mixed_template-train_200-test_300-valid_100-SD_{seed}"
)
ADBUY_SPLIT_NAME: Final = (
    "DeepForm-mixed_template-train_200-test_300-valid_100-SD_{seed}"
)

SPLIT_NAME_FOR_CORPUS: Final[dict[str, str]] = {
    CORPUS_REGISTRATION: REGISTRATION_SPLIT_NAME,
    CORPUS_ADBUY: ADBUY_SPLIT_NAME,
}

# Table 4 Mixed |D|=200 footnotes only. STL FormNet Registration 92.12 is Task 1.
FOOTNOTE_FORMNET_REGISTRATION_MTL_200: Final = 90.51
FOOTNOTE_LAYOUTLMV2_ADBUY_MTL_200: Final = 46.54
FOOTNOTE_FORMNET_ADBUY_MTL_200: Final = 43.23
EXCLUDED_STL_FORMNET_REGISTRATION_200: Final = 92.12

# Corpus / path / dataset nicknames that must not enter bind prompts or skills.
# Do not use a bare "registration" substring: official key registration_num
# contains it.
LEAK_LITERALS: Final[tuple[str, ...]] = (
    "registration-form/",
    "ad-buy-form/",
    "sk_reg",
    "sk_adbuy",
    "mixed_template",
    "filename",
    "file_path",
    "annotations",
)

LEAK_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\bFARA\b", re.IGNORECASE),
    re.compile(r"\bDeepForm\b", re.IGNORECASE),
    re.compile(r"\blv[123]\b", re.IGNORECASE),
    re.compile(r"Short-Form", re.IGNORECASE),
    re.compile(r"Dissemination Report", re.IGNORECASE),
)


def split_filename(corpus: str, seed: int) -> str:
    if corpus not in SPLIT_NAME_FOR_CORPUS:
        raise ValueError(f"unknown corpus {corpus!r}")
    if seed not in SEEDS:
        raise ValueError(f"seed must be one of {SEEDS}, got {seed}")
    return SPLIT_NAME_FOR_CORPUS[corpus].format(seed=seed) + ".json"


def split_url(corpus: str, seed: int) -> str:
    return (
        f"{VRDU_RAW_BASE}/{CORPUS_DIR[corpus]}/few_shot-splits/"
        f"{split_filename(corpus, seed)}"
    )


def dataset_jsonl_url(corpus: str) -> str:
    return f"{VRDU_RAW_BASE}/{CORPUS_DIR[corpus]}/main/dataset.jsonl.gz"


def meta_json_url(corpus: str) -> str:
    return f"{VRDU_RAW_BASE}/{CORPUS_DIR[corpus]}/main/meta.json"

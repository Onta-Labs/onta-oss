"""Refuse KeywordBinder dumps on published VRDU split names."""

from __future__ import annotations

import re

from vrdu_binder.protocol import ProtocolError

_PUBLISHED_SPLIT = re.compile(
    r"^(FARA-lv2-mixed_template|DeepForm-mixed_template)"
    r"-train_\d+-test_\d+-valid_\d+-SD_\d+"
    r"(-test_predictions)?$"
)

KEYWORD_ADAPTER_NAMES = frozenset({"KeywordBinder", "KeywordExtractor"})


def is_published_split_name(name: str) -> bool:
    stem = name.rsplit("/", 1)[-1]
    if stem.endswith(".json"):
        stem = stem[:-5]
    if stem.endswith("-test_predictions"):
        stem = stem[: -len("-test_predictions")]
    return _PUBLISHED_SPLIT.match(stem) is not None


def is_keyword_freeze_adapter(obj: object) -> bool:
    return type(obj).__name__ in KEYWORD_ADAPTER_NAMES


def assert_may_dump(
    *,
    split_name: str,
    binder: object,
    extractor: object | None = None,
) -> None:
    """Keyword adapters may write fixture dumps only, never published splits."""
    if not is_published_split_name(split_name):
        return
    offenders = [type(binder).__name__]
    if extractor is not None:
        offenders.append(type(extractor).__name__)
    if any(name in KEYWORD_ADAPTER_NAMES for name in offenders):
        raise ProtocolError(
            "KeywordBinder/KeywordExtractor cannot write a published-split "
            f"dump ({split_name}). That adapter is a freeze/dry tool, not a "
            "score. Use `dry-run` for fixtures, or --binder llm with "
            "INFONA_BINDER_API_KEY."
        )

"""Published fetch URLs stay pinned to the locked split names."""

from __future__ import annotations

from vrdu_binder.constants import VRDU_RAW_BASE, dataset_jsonl_url, meta_json_url, split_url
from vrdu_binder.fetch import CORPORA


def test_split_urls_are_raw_github_on_the_published_paths():
    assert split_url("registration", 0) == (
        f"{VRDU_RAW_BASE}/registration-form/few_shot-splits/"
        "FARA-lv2-mixed_template-train_200-test_300-valid_100-SD_0.json"
    )
    assert split_url("adbuy", 2) == (
        f"{VRDU_RAW_BASE}/ad-buy-form/few_shot-splits/"
        "DeepForm-mixed_template-train_200-test_300-valid_100-SD_2.json"
    )


def test_ocr_and_meta_urls():
    for corpus, folder in (
        ("registration", "registration-form"),
        ("adbuy", "ad-buy-form"),
    ):
        assert dataset_jsonl_url(corpus).endswith(f"{folder}/main/dataset.jsonl.gz")
        assert meta_json_url(corpus).endswith(f"{folder}/main/meta.json")
    assert set(CORPORA) == {"registration", "adbuy"}

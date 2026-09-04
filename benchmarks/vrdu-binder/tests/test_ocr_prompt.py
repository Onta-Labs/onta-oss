"""Freeze 1: bind prompt is OCR tokens only."""

from __future__ import annotations

from vrdu_binder.ocr import bind_prompt, ocr_tokens_only


def _doc(**ocr_extra):
    return {
        "filename": "LEAK_FILENAME.pdf",
        "file_path": "registration-form/main/pdfs/LEAK_FILENAME.pdf",
        "annotations": [
            ["file_date", [["LEAK_ANNOTATION", [0, 0.0, 0.0, 0.1, 0.1], [[0, 4]]]]]
        ],
        "ocr": {
            "text": "only these tokens",
            "pages": [{"tokens": [{"text": "only"}, {"text": "these"}, {"text": "tokens"}]}],
            **ocr_extra,
        },
    }


def test_prompt_is_ocr_text():
    assert bind_prompt(_doc()) == "only these tokens"


def test_prompt_falls_back_to_page_tokens():
    doc = _doc()
    doc["ocr"]["text"] = "   "
    assert ocr_tokens_only(doc) == "only these tokens"


def test_filename_path_and_annotations_stay_out():
    prompt = bind_prompt(_doc())
    assert "LEAK_FILENAME" not in prompt
    assert "registration-form/" not in prompt
    assert "LEAK_ANNOTATION" not in prompt
    assert "file_path" not in prompt
    assert "annotations" not in prompt


def test_fara_and_deepform_names_are_not_injected():
    prompt = bind_prompt(_doc())
    assert "FARA" not in prompt
    assert "DeepForm" not in prompt
    assert "mixed_template" not in prompt


def test_dataset_names_in_ocr_are_stripped():
    doc = _doc()
    doc["ocr"]["text"] = "hello FARA and DeepForm tokens"
    prompt = bind_prompt(doc)
    assert "FARA" not in prompt
    assert "DeepForm" not in prompt
    assert "hello" in prompt
    assert "tokens" in prompt

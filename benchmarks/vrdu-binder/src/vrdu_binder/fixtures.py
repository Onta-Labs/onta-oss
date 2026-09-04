"""Tiny two-type dry-run corpus. Disjoint keys. No VRDU download, no LLM."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vrdu_binder.constants import TYPE_0, TYPE_1
from vrdu_binder.documents import index_by_filename
from vrdu_binder.splits import RunSplit, run_split_from_raw

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "dry_run"

# Synthetic schema. Not official VRDU keys. Disjoint on purpose.
TYPE_A_KEYS = ("widget_id", "widget_name")
TYPE_B_KEYS = ("invoice_id", "invoice_total")
FIXTURE_KEYS = {TYPE_0: TYPE_A_KEYS, TYPE_1: TYPE_B_KEYS}


def fixture_root() -> Path:
    return FIXTURE_ROOT


def _ann(name: str, value: str) -> list[Any]:
    return [name, [[value, [0, 0.0, 0.0, 0.1, 0.1], [[0, len(value)]]]]]


def _doc(
    filename: str,
    *,
    corpus_prefix: str,
    text: str,
    fields: list[tuple[str, str]],
) -> dict[str, Any]:
    return {
        "filename": filename,
        "file_path": f"{corpus_prefix}/main/pdfs/{filename}",
        "ocr": {"text": text, "pages": [{"tokens": [{"text": t} for t in text.split()]}]},
        "annotations": [_ann(k, v) for k, v in fields],
    }


def build_memory_fixtures() -> dict[str, Any]:
    """In-memory constructed mix used by tests and ``dry-run``."""
    type_a_docs = [
        _doc(
            "train_a_1.pdf",
            corpus_prefix="registration-form",
            text="widget_id W-100 widget_name sprocket",
            fields=[("widget_id", "W-100"), ("widget_name", "sprocket")],
        ),
        _doc(
            "valid_a_1.pdf",
            corpus_prefix="registration-form",
            text="widget_id W-VALID widget_name LEAK_VALID_A",
            fields=[("widget_id", "W-VALID"), ("widget_name", "LEAK_VALID_A")],
        ),
        _doc(
            "test_a_1.pdf",
            corpus_prefix="registration-form",
            text="widget_id W-200 widget_name gasket",
            fields=[("widget_id", "W-200"), ("widget_name", "gasket")],
        ),
        _doc(
            "test_a_misbind.pdf",
            corpus_prefix="registration-form",
            text="invoice_id INV-999 invoice_total 12.00",
            fields=[("widget_id", "W-MISS"), ("widget_name", "hidden")],
        ),
    ]
    type_b_docs = [
        _doc(
            "train_b_1.pdf",
            corpus_prefix="ad-buy-form",
            text="invoice_id INV-10 invoice_total 99.00",
            fields=[("invoice_id", "INV-10"), ("invoice_total", "99.00")],
        ),
        _doc(
            "valid_b_1.pdf",
            corpus_prefix="ad-buy-form",
            text="invoice_id INV-VALID invoice_total LEAK_VALID_B",
            fields=[("invoice_id", "INV-VALID"), ("invoice_total", "LEAK_VALID_B")],
        ),
        _doc(
            "test_b_1.pdf",
            corpus_prefix="ad-buy-form",
            text="invoice_id INV-20 invoice_total 40.00",
            fields=[("invoice_id", "INV-20"), ("invoice_total", "40.00")],
        ),
    ]
    split_a = {
        "train": ["train_a_1.pdf"],
        "valid": ["valid_a_1.pdf"],
        "test": ["test_a_1.pdf", "test_a_misbind.pdf"],
    }
    split_b = {
        "train": ["train_b_1.pdf"],
        "valid": ["valid_b_1.pdf"],
        "test": ["test_b_1.pdf"],
    }
    return {
        "docs_by_type": {
            TYPE_0: type_a_docs,
            TYPE_1: type_b_docs,
        },
        "index_by_type": {
            TYPE_0: index_by_filename(type_a_docs),
            TYPE_1: index_by_filename(type_b_docs),
        },
        "split_raw_by_type": {TYPE_0: split_a, TYPE_1: split_b},
        "splits": {
            TYPE_0: run_split_from_raw(split_a, corpus="registration", seed=0),
            TYPE_1: run_split_from_raw(split_b, corpus="adbuy", seed=0),
        },
        "keys": FIXTURE_KEYS,
    }


def write_fixture_files(root: Path | None = None) -> Path:
    dest = Path(root) if root else FIXTURE_ROOT
    mem = build_memory_fixtures()
    mapping = {
        TYPE_0: dest / "type_a",
        TYPE_1: dest / "type_b",
    }
    for type_id, folder in mapping.items():
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "documents.jsonl").write_text(
            "".join(json.dumps(d) + "\n" for d in mem["docs_by_type"][type_id]),
            encoding="utf-8",
        )
        (folder / "split_SD_0.json").write_text(
            json.dumps(mem["split_raw_by_type"][type_id], indent=2) + "\n",
            encoding="utf-8",
        )
        (folder / "meta.json").write_text(
            json.dumps({"keys": list(FIXTURE_KEYS[type_id])}, indent=2) + "\n",
            encoding="utf-8",
        )
    return dest


def load_run_split_from_fixture(type_id: str) -> RunSplit:
    return build_memory_fixtures()["splits"][type_id]

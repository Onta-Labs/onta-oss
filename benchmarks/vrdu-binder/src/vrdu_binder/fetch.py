"""Fetch published VRDU split JSON and (optionally) OCR jsonl. No PDFs."""

from __future__ import annotations

import urllib.request
from pathlib import Path

from vrdu_binder.constants import (
    CORPUS_ADBUY,
    CORPUS_DIR,
    CORPUS_REGISTRATION,
    SEEDS,
    dataset_jsonl_url,
    meta_json_url,
    split_filename,
    split_url,
)
from vrdu_binder.protocol import ProtocolError

CORPORA = (CORPUS_REGISTRATION, CORPUS_ADBUY)


def default_data_root() -> Path:
    return Path(__file__).resolve().parents[2] / "data"


def fetch_url(url: str, dest: Path, *, timeout: int = 60) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            dest.write_bytes(resp.read())
    except OSError as exc:
        raise ProtocolError(f"fetch failed for {url}: {exc}") from exc
    return dest


def fetch_splits(data_root: Path | str | None = None, *, seeds: tuple[int, ...] = SEEDS) -> list[Path]:
    root = Path(data_root) if data_root else default_data_root()
    written: list[Path] = []
    for corpus in CORPORA:
        for seed in seeds:
            name = split_filename(corpus, seed)
            dest = root / CORPUS_DIR[corpus] / "few_shot-splits" / name
            fetch_url(split_url(corpus, seed), dest)
            written.append(dest)
    return written


def fetch_meta(data_root: Path | str | None = None) -> list[Path]:
    root = Path(data_root) if data_root else default_data_root()
    written: list[Path] = []
    for corpus in CORPORA:
        dest = root / CORPUS_DIR[corpus] / "main" / "meta.json"
        fetch_url(meta_json_url(corpus), dest)
        written.append(dest)
    return written


def fetch_ocr(data_root: Path | str | None = None) -> list[Path]:
    """Download ``dataset.jsonl.gz`` (tens of MB). Never commit the result."""
    root = Path(data_root) if data_root else default_data_root()
    written: list[Path] = []
    for corpus in CORPORA:
        dest = root / CORPUS_DIR[corpus] / "main" / "dataset.jsonl.gz"
        fetch_url(dataset_jsonl_url(corpus), dest, timeout=600)
        written.append(dest)
    return written

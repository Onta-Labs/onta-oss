"""Download published SGD schemas + dialogue JSON. Do not commit data/."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from sgd_binder.constants import SGD_RAW, SPLITS
from sgd_binder.protocol import ProtocolError


def default_data_root() -> Path:
    return Path(__file__).resolve().parents[2] / "data"


def fetch_url(url: str, dest: Path, *, timeout: int = 120) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            dest.write_bytes(resp.read())
    except OSError as exc:
        raise ProtocolError(f"fetch failed for {url}: {exc}") from exc
    return dest


def fetch_split(split: str, dest: Path) -> list[Path]:
    if split not in SPLITS:
        raise ProtocolError(f"unknown split {split!r}")
    written = [fetch_url(f"{SGD_RAW}/{split}/schema.json", dest / split / "schema.json")]
    for i in range(1, 200):
        name = f"dialogues_{i:03d}.json"
        url = f"{SGD_RAW}/{split}/{name}"
        path = dest / split / name
        try:
            fetch_url(url, path)
        except ProtocolError:
            break
        written.append(path)
    if len(written) < 2:
        raise ProtocolError(f"no dialogue files for {split}")
    return written


def fetch_all(dest: Path | None = None) -> list[Path]:
    root = dest or default_data_root()
    out: list[Path] = []
    for split in SPLITS:
        out.extend(fetch_split(split, root))
    (root / "FETCHED.json").write_text(json.dumps({"splits": list(SPLITS)}) + "\n")
    return out

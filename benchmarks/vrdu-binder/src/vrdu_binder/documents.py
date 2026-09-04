"""Load dataset.jsonl / jsonl.gz. Annotations stay on disk until skill write."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from vrdu_binder.protocol import ProtocolError


def open_jsonl(path: Path | str) -> Iterator[dict[str, Any]]:
    p = Path(path)
    if not p.is_file():
        raise ProtocolError(f"missing documents file {p}")
    opener = gzip.open if p.suffix == ".gz" or p.name.endswith(".jsonl.gz") else open
    with opener(p, "rt", encoding="utf-8") as fh:  # type: ignore[arg-type]
        for line in fh:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if not isinstance(obj, dict) or "filename" not in obj:
                raise ProtocolError("document line missing filename")
            yield obj


def index_by_filename(docs: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for doc in docs:
        name = str(doc["filename"])
        if name in out:
            raise ProtocolError(f"duplicate filename {name!r}")
        out[name] = dict(doc)
    return out


def load_documents(path: Path | str) -> dict[str, dict[str, Any]]:
    return index_by_filename(open_jsonl(path))


def docs_for_filenames(
    index: Mapping[str, Mapping[str, Any]], filenames: Iterable[str]
) -> list[dict[str, Any]]:
    missing = [n for n in filenames if n not in index]
    if missing:
        raise ProtocolError(f"documents missing for {missing[:5]!r}")
    return [dict(index[n]) for n in filenames]

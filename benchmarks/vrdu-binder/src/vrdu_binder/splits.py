"""Published split loader. Valid is dropped at the door."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from vrdu_binder.constants import SEEDS, split_filename
from vrdu_binder.protocol import ProtocolError


@dataclass(frozen=True)
class RunSplit:
    """Train + unmodified test. Valid is not a field on purpose."""

    train: tuple[str, ...]
    test: tuple[str, ...]
    source_path: Path | None = None
    corpus: str | None = None
    seed: int | None = None

    def assert_train_only(self, filenames: Iterable[str], *, what: str) -> None:
        allowed = set(self.train)
        bad = [name for name in filenames if name not in allowed]
        if bad:
            raise ProtocolError(f"{what} may use train filenames only; refused {bad!r}")

    def assert_unmodified_test(self, names: Iterable[str]) -> None:
        got = tuple(names)
        if got != self.test:
            raise ProtocolError(
                "test list was dropped, rewritten, or reordered; "
                f"expected {len(self.test)} published names, got {len(got)}"
            )


def load_split_json(path: Path | str) -> dict[str, list[str]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ProtocolError("split JSON must be an object")
    for key in ("train", "valid", "test"):
        if key not in raw or not isinstance(raw[key], list):
            raise ProtocolError(f"split JSON missing list field {key!r}")
    return raw


def run_split_from_raw(
    raw: Mapping[str, Any],
    *,
    source_path: Path | None = None,
    corpus: str | None = None,
    seed: int | None = None,
) -> RunSplit:
    """Keep train/test. Discard valid so later stages cannot read it."""
    if "valid" in raw:
        # Touching raw["valid"] here would count as a use. We only check presence.
        pass
    train = tuple(str(x) for x in raw["train"])
    test = tuple(str(x) for x in raw["test"])
    if not train:
        raise ProtocolError("split train list is empty")
    if not test:
        raise ProtocolError("split test list is empty")
    return RunSplit(
        train=train, test=test, source_path=source_path, corpus=corpus, seed=seed
    )


def load_run_split(path: Path | str, *, corpus: str | None = None, seed: int | None = None) -> RunSplit:
    p = Path(path)
    return run_split_from_raw(
        load_split_json(p), source_path=p, corpus=corpus, seed=seed
    )


def published_split_path(data_root: Path | str, corpus: str, seed: int) -> Path:
    from vrdu_binder.constants import CORPUS_DIR

    if seed not in SEEDS:
        raise ProtocolError(f"seed must be one of {SEEDS}")
    root = Path(data_root)
    return root / CORPUS_DIR[corpus] / "few_shot-splits" / split_filename(corpus, seed)

"""Stock VRDU ``*-test_predictions.json`` writer. One corpus per directory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from vrdu_binder.protocol import ProtocolError
from vrdu_binder.splits import RunSplit


def empty_result() -> list[Any]:
    return []


def build_results(
    split: RunSplit,
    filled: Mapping[str, list[Any]],
    *,
    misbound: set[str] | None = None,
) -> dict[str, list[Any]]:
    """Every published test filename is present. Misbind -> ``[]``, not omitted."""
    misbound = misbound or set()
    results: dict[str, list[Any]] = {}
    for name in split.test:
        if name in misbound:
            results[name] = empty_result()
            continue
        results[name] = list(filled.get(name, empty_result()))
    split.assert_unmodified_test(results.keys())
    missing = [n for n in split.test if n not in results]
    if missing:
        raise ProtocolError(f"dump dropped test filenames {missing!r}")
    extra = [n for n in results if n not in set(split.test)]
    if extra:
        raise ProtocolError(f"dump added filenames not on the test list {extra!r}")
    for name in misbound:
        if name in results and results[name] != []:
            raise ProtocolError(f"misbind {name!r} must be an empty results list")
    return results


def predictions_payload(
    *,
    split_name: str,
    results: Mapping[str, list[Any]],
    extra_meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "spec": "v11",
        "split": split_name,
        "bind": "predicted",
    }
    if extra_meta:
        if extra_meta.get("bind") in {"oracle", "gold", "gold-routed"}:
            raise ProtocolError("oracle-type dumps cannot use the headline writer")
        meta.update(dict(extra_meta))
        meta["bind"] = "predicted"
    return {"meta": meta, "results": dict(results)}


def write_predictions(
    *,
    out_dir: Path | str,
    split_name: str,
    split: RunSplit,
    filled: Mapping[str, list[Any]],
    misbound: set[str] | None = None,
    extra_meta: Mapping[str, Any] | None = None,
) -> Path:
    """Write ``{split_name}-test_predictions.json`` for stock ``vrdu.evaluate``."""
    dest = Path(out_dir)
    dest.mkdir(parents=True, exist_ok=True)
    results = build_results(split, filled, misbound=misbound)
    payload = predictions_payload(
        split_name=split_name, results=results, extra_meta=extra_meta
    )
    path = dest / f"{split_name}-test_predictions.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def load_predictions(path: Path | str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def assert_ready_for_evaluate(out_dir: Path | str, split_names: Sequence[str]) -> None:
    dest = Path(out_dir)
    for name in split_names:
        path = dest / f"{name}-test_predictions.json"
        if not path.is_file():
            raise ProtocolError(f"missing stock dump {path}")

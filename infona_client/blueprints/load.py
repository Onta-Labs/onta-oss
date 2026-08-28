"""Load a Blueprint package from disk (ADR 0014).

Canonical package = a directory of UTF-8 plain text with ``blueprint.yaml``
at the root. ``blueprint.yml`` is an alias. ``blueprint.json`` is allowed
for machine writers; a package must not ship both. Inspect / validate /
hash / install always run on the unpacked directory. A zip/tar is an
envelope, not the format.

A single-file YAML/JSON is the degenerate small-package form. Sibling
section files (``ontology.yaml``, ``sources.yaml``, ``sample/``, …) fill
keys that the root file omitted so the sample stays independently
droppable (INF-587 / F2).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from infona_client.blueprints.schema import BlueprintManifest, Sample


class BlueprintValidationError(ValueError):
    """One or more structural problems. ``errors`` is the full list."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))

ROOT_YAML = ("blueprint.yaml", "blueprint.yml")
ROOT_JSON = "blueprint.json"

# Author-supplied code does not travel in the package (ADR 0014 F3 / INF-560 C1).
FORBIDDEN_SUFFIXES = frozenset(
    {
        ".py",
        ".pyc",
        ".pyo",
        ".js",
        ".mjs",
        ".cjs",
        ".bin",
        ".so",
        ".dll",
        ".exe",
        ".pkl",
        ".pickle",
        ".wasm",
    }
)

# Sibling file → top-level keys it may supply when the root omitted them.
SECTION_FILES: dict[str, tuple[str, ...]] = {
    "ontology.yaml": ("concepts", "relationships"),
    "sources.yaml": ("sources",),
    "acquisition.yaml": ("acquisition",),
    "tasks.yaml": ("tasks",),
    "rules.yaml": ("rules",),
    "validation.yaml": ("validation",),
    "freshness.yaml": ("freshness",),
    "skills.yaml": ("skills",),
    "examples.yaml": ("examples",),
    "evals.yaml": ("evals",),
    "sample.yaml": ("sample",),
}


def _read_mapping(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json" or text.lstrip().startswith(("{", "[")):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise BlueprintValidationError(
                [f"{path.name}: invalid JSON: {exc}"]
            ) from exc
    else:
        try:
            import yaml
        except ImportError as exc:
            raise BlueprintValidationError(
                ["YAML requires PyYAML (canonical format is blueprint.yaml)"]
            ) from exc
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise BlueprintValidationError(
            [f"{path.name} must be a YAML/JSON object at the top level"]
        )
    return data


def _reject_archives(path: Path) -> None:
    name = path.name.lower()
    if path.suffix.lower() in {".zip", ".tar"} or name.endswith(
        (".tar.gz", ".tgz", ".tar.bz2")
    ):
        raise BlueprintValidationError(
            [
                "archive is transport, not the package; unpack to a directory "
                "with blueprint.yaml (ADR 0014 F4)"
            ]
        )


def _reject_author_code(root: Path) -> None:
    hits: list[str] = []
    for child in sorted(root.rglob("*")):
        if child.is_file() and child.suffix.lower() in FORBIDDEN_SUFFIXES:
            hits.append(str(child.relative_to(root)))
    if hits:
        raise BlueprintValidationError(
            [
                f"author-supplied code is not representable: {name} "
                f"(ADR 0014 F3)"
                for name in hits
            ]
        )


def _merge_siblings(root: Path, data: dict[str, Any]) -> dict[str, Any]:
    merged = dict(data)
    for filename, keys in SECTION_FILES.items():
        sibling = root / filename
        if not sibling.is_file():
            continue
        extra = _read_mapping(sibling)
        for key in keys:
            if key not in extra:
                continue
            if key in merged and merged[key] is not None:
                raise BlueprintValidationError(
                    [
                        f"{key} is in both blueprint.yaml and {filename}; "
                        f"keep one (ADR 0014 F2)"
                    ]
                )
            merged[key] = extra[key]
        stray = [k for k in extra if k not in keys]
        if stray:
            raise BlueprintValidationError(
                [f"{filename} has unexpected key {k!r}" for k in stray]
            )
    sample_dir = root / "sample"
    if sample_dir.is_dir():
        sample_file = sample_dir / "sample.yaml"
        if not sample_file.is_file():
            sample_file = sample_dir / "sample.yml"
        if sample_file.is_file():
            if "sample" in merged and merged["sample"] is not None:
                raise BlueprintValidationError(
                    ["sample is in both blueprint.yaml and sample/; keep one"]
                )
            merged["sample"] = _read_mapping(sample_file)
    return merged


def load_package(source: Any) -> dict[str, Any]:
    """Return a merged manifest dict from a path, directory, mapping, or text."""

    if isinstance(source, (BlueprintManifest, Sample)):
        return source.model_dump(mode="json")
    if isinstance(source, Mapping):
        return dict(source)

    path: Path | None = None
    if isinstance(source, Path):
        path = source
    elif (
        isinstance(source, str)
        and not source.lstrip().startswith(("{", "["))
        and len(source) < 4096
    ):
        candidate = Path(source)
        if candidate.exists():
            path = candidate

    if path is not None:
        _reject_archives(path)
        if path.is_dir():
            yaml_roots = [path / name for name in ROOT_YAML if (path / name).is_file()]
            json_root = path / ROOT_JSON
            if yaml_roots and json_root.is_file():
                raise BlueprintValidationError(
                    [
                        "package must not ship both blueprint.yaml and "
                        "blueprint.json (ADR 0014 F1)"
                    ]
                )
            if not yaml_roots and not json_root.is_file():
                raise BlueprintValidationError(
                    ["directory package requires blueprint.yaml at the root"]
                )
            _reject_author_code(path)
            data = _read_mapping(yaml_roots[0] if yaml_roots else json_root)
            return _merge_siblings(path, data)
        if path.is_file():
            _reject_archives(path)
            return _read_mapping(path)
        raise BlueprintValidationError([f"file not found: {path}"])

    if isinstance(source, str):
        text = source.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise BlueprintValidationError([f"invalid JSON: {exc}"]) from exc
        else:
            try:
                import yaml
            except ImportError as exc:
                raise BlueprintValidationError(
                    ["YAML requires PyYAML (canonical format is blueprint.yaml)"]
                ) from exc
            data = yaml.safe_load(text)
        if not isinstance(data, dict):
            raise BlueprintValidationError(
                ["manifest must be a YAML/JSON object at the top level"]
            )
        return data
    raise BlueprintValidationError(
        [f"unsupported manifest type: {type(source).__name__}"]
    )


def load_sample_section(source: Any) -> dict[str, Any]:
    """Load just the sample — a file, a ``sample/`` directory, or a mapping.

    Independently inspectable and droppable (ADR 0014 F2 / INF-587).
    """

    if isinstance(source, Sample):
        return source.model_dump(mode="json")
    if isinstance(source, Mapping):
        data = dict(source)
        if "schema_version" in data and "sample" in data:
            inner = data["sample"]
            if not isinstance(inner, dict):
                raise BlueprintValidationError(["sample section is absent"])
            return inner
        return data

    path: Path | None = None
    if isinstance(source, Path):
        path = source
    elif isinstance(source, str) and len(source) < 4096:
        candidate = Path(source)
        if candidate.exists():
            path = candidate
    if path is not None:
        if path.is_dir():
            for name in ("sample.yaml", "sample.yml", "blueprint.yaml", "blueprint.yml"):
                candidate = path / name
                if candidate.is_file():
                    data = _read_mapping(candidate)
                    if name.startswith("blueprint"):
                        if "sample" not in data or data["sample"] is None:
                            # Directory package: look in sample/
                            nested = path / "sample"
                            if nested.is_dir():
                                return load_sample_section(nested)
                            raise BlueprintValidationError(["sample section is absent"])
                        return data["sample"]
                    return data
            raise BlueprintValidationError(
                ["sample directory requires sample.yaml"]
            )
        if path.is_file():
            data = _read_mapping(path)
            if "schema_version" in data and "sample" in data:
                inner = data["sample"]
                if not isinstance(inner, dict):
                    raise BlueprintValidationError(["sample section is absent"])
                return inner
            return data
    return load_package(source)

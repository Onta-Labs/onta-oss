"""Blueprint directory package — load / write ``blueprint.yaml``.

Canonical package = a directory of UTF-8 plain text with ``blueprint.yaml``
at the root (ADR 0014 F1–F2). ``.yml`` is an alias; ``blueprint.json`` is
the machine form. A package must not ship both YAML and JSON roots.

Sibling section files (``sources.yaml``, ``tasks.yaml``, …) are merged into
the document when present so a split tree stays one :class:`BlueprintManifest`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from infona_client.blueprint.models import (
    ALLOWED_TOP_LEVEL_KEYS,
    BlueprintManifest,
    dumps_blueprint,
    parse_blueprint,
)
from infona_client.blueprint.validate import validate_blueprint
from infona_client.blueprint.yamlutil import YamlError, dump_yaml, load_yaml

ROOT_YAML_NAMES = ("blueprint.yaml", "blueprint.yml")
ROOT_JSON_NAME = "blueprint.json"

#: Section filename → manifest key. A sibling may be a list (the section
#: value) or a mapping wrapping that key.
SECTION_FILES: dict[str, str] = {
    "sources.yaml": "sources",
    "acquisition.yaml": "acquisition",
    "tasks.yaml": "tasks",
    "rules.yaml": "rules",
    "validation.yaml": "validation",
    "freshness.yaml": "freshness",
    "skills.yaml": "skills",
    "functions.yaml": "functions",
    "evals.yaml": "evals",
    "examples.yaml": "examples",
    "sample.yaml": "sample",
    "ontology.yaml": "concepts",
}


class BlueprintPackageError(ValueError):
    """Directory is not a Blueprint package we can classify."""


def dumps_blueprint_yaml(manifest: BlueprintManifest) -> str:
    """Human default: YAML of the validated document (dates as ISO)."""
    return dump_yaml(manifest.model_dump(mode="json", exclude_none=True))


def write_blueprint_package(manifest: BlueprintManifest, dest: Path) -> Path:
    """Write ``dest/blueprint.yaml`` and return ``dest``.

    ``dest`` is created if missing. Existing files are overwritten.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    json_path = dest / ROOT_JSON_NAME
    if json_path.exists():
        raise BlueprintPackageError(
            f"{dest} already has {ROOT_JSON_NAME}; a package must not ship both"
        )
    (dest / "blueprint.yaml").write_text(dumps_blueprint_yaml(manifest), encoding="utf-8")
    return dest


def load_blueprint_package(path: Path | str) -> BlueprintManifest:
    """Load a directory or a root file into a v1 manifest.

    Raises :class:`BlueprintPackageError` when the tree cannot be classified
    (both YAML and JSON roots, unknown sibling, empty).
    """
    path = Path(path)
    if path.is_file():
        return parse_blueprint(_read_document(path))
    if not path.is_dir():
        raise BlueprintPackageError(f"{path} is not a Blueprint package")

    yaml_roots = [path / name for name in ROOT_YAML_NAMES if (path / name).exists()]
    json_root = path / ROOT_JSON_NAME
    if yaml_roots and json_root.exists():
        raise BlueprintPackageError(
            f"{path} ships both YAML and JSON roots (ADR 0014 F1)"
        )
    if len(yaml_roots) > 1:
        raise BlueprintPackageError(f"{path} has both blueprint.yaml and blueprint.yml")
    if yaml_roots:
        payload = _read_document(yaml_roots[0])
    elif json_root.exists():
        payload = _read_document(json_root)
    else:
        raise BlueprintPackageError(
            f"{path} has no blueprint.yaml / blueprint.yml / blueprint.json"
        )
    if not isinstance(payload, Mapping):
        raise BlueprintPackageError("blueprint root must be a mapping")
    merged = dict(payload)
    for filename, key in SECTION_FILES.items():
        sibling = path / filename
        if not sibling.exists():
            continue
        section = _read_document(sibling)
        merged[key] = _unwrap_section(section, key)
    extra = sorted(set(merged) - ALLOWED_TOP_LEVEL_KEYS)
    if extra:
        raise BlueprintPackageError(
            f"{path} has keys we cannot classify: {extra}"
        )
    return parse_blueprint(merged)


def _unwrap_section(section: Any, key: str) -> Any:
    if isinstance(section, Mapping) and set(section) == {key}:
        return section[key]
    if key == "concepts" and isinstance(section, Mapping):
        # ontology.yaml may carry concepts + relationships together.
        if "concepts" in section:
            return section  # caller merges? keep as-is — validate will fail
    return section


def _read_document(path: Path) -> Any:
    raw = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".json":
        return json.loads(raw)
    try:
        return load_yaml(raw)
    except YamlError as exc:
        raise BlueprintPackageError(f"{path}: {exc}") from exc


def package_files(manifest: BlueprintManifest) -> dict[str, str]:
    """Path → UTF-8 content for the HTTP/SDK envelope."""
    return {
        "blueprint.yaml": dumps_blueprint_yaml(manifest),
    }


def validate_package(path: Path | str) -> list[str]:
    """Load a directory/file and run the INF-563 validator."""
    try:
        manifest = load_blueprint_package(path)
    except (BlueprintPackageError, YamlError, json.JSONDecodeError, ValueError) as exc:
        return [str(exc)]
    return validate_blueprint(manifest)


__all__ = [
    "SECTION_FILES",
    "BlueprintPackageError",
    "dumps_blueprint_yaml",
    "load_blueprint_package",
    "package_files",
    "validate_package",
    "write_blueprint_package",
]

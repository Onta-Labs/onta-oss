"""Load an ADR 0014 Blueprint directory (or a single manifest file).

This is inspect + validate input only. Apply is ``install_blueprint``
(INF-575 / INF-577). Export (workspace → directory) is still INF-565.
"""

from __future__ import annotations

from pathlib import Path

from infona_client.blueprint.models import parse_blueprint
from infona_client.blueprint.validate import validate_blueprint

_MANIFEST_NAMES = ("blueprint.yaml", "blueprint.yml", "blueprint.json")


def find_manifest(root: str | Path) -> Path:
    """Return the manifest path for a package directory or a manifest file."""
    path = Path(root)
    if path.is_file():
        return path
    if not path.is_dir():
        raise FileNotFoundError(f"blueprint package not found: {path}")
    yaml_hit = (path / "blueprint.yaml").exists() or (path / "blueprint.yml").exists()
    json_hit = (path / "blueprint.json").exists()
    if yaml_hit and json_hit:
        raise ValueError(
            "a package must not ship both blueprint.yaml and blueprint.json "
            "(ADR 0014 F1)"
        )
    for name in _MANIFEST_NAMES:
        candidate = path / name
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"{path} is not a Blueprint package (missing blueprint.yaml)"
    )


def load_blueprint_package(root: str | Path):
    """Read and parse the v1 document. Does not install."""
    return parse_blueprint(find_manifest(root).read_text(encoding="utf-8"))


def validate_blueprint_package(root: str | Path) -> list[str]:
    """Validate a package directory or manifest file. Empty list = valid."""
    return validate_blueprint(load_blueprint_package(root))


__all__ = [
    "find_manifest",
    "load_blueprint_package",
    "validate_blueprint_package",
]

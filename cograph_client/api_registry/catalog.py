"""Catalog loader + the operator-curated layer seam.

The registry is **operator-curated, not user-managed**: entries are versioned
JSON data files in the repo, loaded at startup — there is no runtime write path
and no ``/api-sources`` CRUD (ONTA-194 scope decision).

Two layers, mirroring the ADR 0002 content split:

  * **global_public** — the OSS seed catalog shipped in ``data/`` (free/official
    APIs). Always loaded.
  * **global_enhanced** — premium entries (paid/commercial APIs we license),
    contributed by the proprietary package through
    :func:`register_api_source_layer` (same plugin shape as ``register_adapter``
    / ``register_web_source``). The OSS package never imports the premium tree.

Precedence follows ADR 0002: ``global_enhanced`` shadows ``global_public`` by
slug, so a premium entry can override a public one of the same name.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .spec import ApiSourceSpec, validate_spec

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent / "data"

# Layer precedence: lower rank is a base that higher ranks shadow by slug.
_LAYER_RANK = {"global_public": 0, "global_enhanced": 10}
_DEFAULT_RANK = 5

# Registered overlay layers (the premium package populates this at startup).
_layers: dict[str, list[ApiSourceSpec]] = {}


# --------------------------------------------------------------------------- #
# The in-memory catalog
# --------------------------------------------------------------------------- #
@dataclass
class ApiSourceCatalog:
    entries: dict[str, ApiSourceSpec] = field(default_factory=dict)

    def get(self, slug: str) -> Optional[ApiSourceSpec]:
        return self.entries.get(slug)

    def all(self) -> list[ApiSourceSpec]:
        return list(self.entries.values())

    def enabled(self) -> list[ApiSourceSpec]:
        return [e for e in self.entries.values() if e.enabled]

    def slugs(self) -> list[str]:
        return list(self.entries.keys())

    def __len__(self) -> int:
        return len(self.entries)

    def __contains__(self, slug: object) -> bool:
        return slug in self.entries


# --------------------------------------------------------------------------- #
# Loading data files
# --------------------------------------------------------------------------- #
def load_catalog_dir(directory: Path | str, *, layer: str = "global_public") -> list[ApiSourceSpec]:
    """Load every ``*.json`` entry in ``directory`` and tag it with ``layer``.

    Tolerant: a malformed file is logged and skipped rather than sinking the
    whole catalog (the CI catalog test is the strict gate that no entry ships
    malformed). Each file holds exactly one entry.
    """
    directory = Path(directory)
    out: list[ApiSourceSpec] = []
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("api_registry: could not read %s: %s", path.name, exc)
            continue
        try:
            spec = ApiSourceSpec.from_dict(raw)
            spec.layer = layer
        except Exception as exc:  # defensive: from_dict is tolerant, but be safe
            logger.warning("api_registry: could not parse %s: %s", path.name, exc)
            continue
        errors = validate_spec(spec)
        if errors:
            logger.warning(
                "api_registry: skipping invalid entry %s (%s): %s",
                path.name, spec.slug or "?", "; ".join(errors),
            )
            continue
        out.append(spec)
    return out


# --------------------------------------------------------------------------- #
# The overlay seam (premium registers here)
# --------------------------------------------------------------------------- #
def register_api_source_layer(name: str, specs: Iterable[ApiSourceSpec]) -> None:
    """Register (or replace) a catalog layer by name.

    The premium package calls this from its plugin ``register()`` to contribute
    the ``global_enhanced`` overlay — no ``cograph.*`` import crosses into OSS.
    Idempotent: re-registering the same layer name replaces it.
    """
    validated: list[ApiSourceSpec] = []
    for s in specs:
        s.layer = name
        errors = validate_spec(s)
        if errors:
            logger.warning(
                "api_registry: dropping invalid layer entry %s/%s: %s",
                name, s.slug or "?", "; ".join(errors),
            )
            continue
        validated.append(s)
    _layers[name] = validated
    reset_api_source_catalog()  # a new layer must show up on the next catalog build
    logger.info("api_registry: registered layer %s (%d entries)", name, len(validated))


def reset_api_source_layers() -> None:
    """Drop all registered overlay layers (tests)."""
    _layers.clear()
    reset_api_source_catalog()


def registered_layers() -> dict[str, list[ApiSourceSpec]]:
    return {k: list(v) for k, v in _layers.items()}


# --------------------------------------------------------------------------- #
# The factory
# --------------------------------------------------------------------------- #
def make_api_source_catalog(*, seed_dir: Optional[Path] = None) -> ApiSourceCatalog:
    """Build the merged catalog: OSS seed (global_public) + registered overlays.

    Layers are applied in ascending precedence rank so higher layers shadow lower
    ones by slug (``global_enhanced`` > ``global_public``), matching ADR 0002.
    """
    catalog = ApiSourceCatalog()

    seed = load_catalog_dir(seed_dir or _DATA_DIR, layer="global_public")
    layered: dict[str, list[ApiSourceSpec]] = {"global_public": seed}
    for name, specs in _layers.items():
        layered[name] = specs

    for name in sorted(layered, key=lambda n: _LAYER_RANK.get(n, _DEFAULT_RANK)):
        for spec in layered[name]:
            catalog.entries[spec.slug] = spec  # later layers shadow earlier by slug

    logger.info(
        "api_registry: catalog ready — %d entries across layers %s",
        len(catalog), sorted(layered),
    )
    return catalog


# --------------------------------------------------------------------------- #
# Process-wide cached catalog (built once, after startup plugin registration)
# --------------------------------------------------------------------------- #
_catalog_singleton: Optional[ApiSourceCatalog] = None


def get_api_source_catalog() -> ApiSourceCatalog:
    """Return the process-wide catalog, building it on first use.

    Built lazily so premium overlays registered at startup
    (``_load_api_registry_plugin``) are present by the time the first request
    consults the registry. Call ``reset_api_source_catalog()`` after registering
    a new layer (tests) to rebuild.
    """
    global _catalog_singleton
    if _catalog_singleton is None:
        _catalog_singleton = make_api_source_catalog()
    return _catalog_singleton


def reset_api_source_catalog() -> None:
    """Drop the cached catalog so the next access rebuilds it (tests / startup)."""
    global _catalog_singleton
    _catalog_singleton = None


__all__ = [
    "ApiSourceCatalog",
    "load_catalog_dir",
    "make_api_source_catalog",
    "get_api_source_catalog",
    "reset_api_source_catalog",
    "register_api_source_layer",
    "reset_api_source_layers",
    "registered_layers",
]

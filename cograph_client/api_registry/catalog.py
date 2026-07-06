"""Catalog loader + the operator-curated layer seam + the per-tenant layer.

The GLOBAL catalog is **operator-curated, not user-managed**: entries are
versioned JSON data files in the repo, loaded at startup — there is no runtime
write path for the global layers (ONTA-194 scope decision).

Three layers, mirroring the ADR 0002 content split (+ the ONTA-2xx tenant layer):

  * **global_public** — the OSS seed catalog shipped in ``data/`` (free/official
    APIs). Always loaded.
  * **global_enhanced** — premium entries (paid/commercial APIs we license),
    contributed by the proprietary package through
    :func:`register_api_source_layer` (same plugin shape as ``register_adapter``
    / ``register_web_source``). The OSS package never imports the premium tree.
  * **tenant_custom** — per-workspace private/internal APIs a tenant connects
    itself (ONTA-2xx). These are NOT operator-curated: they live in the durable
    store (``store.py``), are scoped strictly to one tenant, and are merged in on
    top of the global layers by :func:`get_api_source_catalog` when it is given a
    ``tenant_id``. They have the HIGHEST precedence so a tenant can shadow a
    global slug for its own workspace (never for anyone else).

Precedence: ``tenant_custom`` (20) > ``global_enhanced`` (10) > ``global_public``
(0) — later layers shadow earlier by slug. Only the two global layers are part of
the process-wide singleton; the tenant layer is merged per-request and cached
per-tenant with explicit invalidation on write.
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

# Canonical layer names. tenant_custom is the per-workspace private layer.
LAYER_GLOBAL_PUBLIC = "global_public"
LAYER_GLOBAL_ENHANCED = "global_enhanced"
LAYER_TENANT_CUSTOM = "tenant_custom"

# Layer precedence: lower rank is a base that higher ranks shadow by slug.
# tenant_custom leads so a workspace's own entry shadows a global slug for that
# workspace only (the merge is per-tenant — see get_api_source_catalog).
_LAYER_RANK = {
    LAYER_GLOBAL_PUBLIC: 0,
    LAYER_GLOBAL_ENHANCED: 10,
    LAYER_TENANT_CUSTOM: 20,
}
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

# Per-tenant custom-entry cache: tenant_id -> its tenant_custom specs (already
# materialized: layer=tenant_custom + enabled applied). A tenant NOT present here
# has an unknown/never-loaded custom layer (distinct from a tenant present with an
# empty list, which means "loaded, has none"); ``get_api_source_catalog`` returns
# just the global catalog for an unknown tenant so a sync caller never blocks on
# the store. The async ``load_tenant_custom_catalog`` populates/refreshes it, and
# every write route invalidates it — so a stale merge can never outlive a write.
_tenant_custom_cache: dict[str, list[ApiSourceSpec]] = {}


def get_api_source_catalog(tenant_id: Optional[str] = None) -> ApiSourceCatalog:
    """Return the catalog, optionally merged with a tenant's custom entries.

    Built lazily so premium overlays registered at startup
    (``_load_api_registry_plugin``) are present by the time the first request
    consults the registry. Call ``reset_api_source_catalog()`` after registering
    a new layer (tests) to rebuild.

    When ``tenant_id`` is given AND that tenant's custom layer has been loaded
    into the per-tenant cache (via :func:`load_tenant_custom_catalog`), the
    returned catalog is the GLOBAL catalog merged with that tenant's
    ``tenant_custom`` entries (highest precedence — a tenant slug shadows a global
    slug for THAT tenant only; every other tenant is unaffected). This function
    stays synchronous and never touches the durable store: an unknown/unloaded
    tenant simply gets the global catalog, so a sync caller never blocks.

    Strict isolation: a tenant only ever sees ``global_public`` + ``global_enhanced``
    + its OWN ``tenant_custom`` entries — never another tenant's.
    """
    global _catalog_singleton
    if _catalog_singleton is None:
        _catalog_singleton = make_api_source_catalog()
    if tenant_id is None:
        return _catalog_singleton
    custom = _tenant_custom_cache.get(tenant_id)
    if not custom:
        # Unknown tenant, or a tenant known to have zero custom entries: the
        # global catalog is exactly what it should see.
        return _catalog_singleton
    return _merge_tenant_custom(_catalog_singleton, custom)


def _merge_tenant_custom(
    base: ApiSourceCatalog, custom: list[ApiSourceSpec]
) -> ApiSourceCatalog:
    """Return a NEW catalog = ``base`` (global) with ``custom`` merged on top.

    tenant_custom is the highest layer, so its slugs shadow global ones. The base
    catalog and its spec objects are never mutated (the merged catalog is
    per-request/ephemeral)."""
    merged = ApiSourceCatalog(entries=dict(base.entries))
    for spec in custom:
        merged.entries[spec.slug] = spec
    return merged


async def load_tenant_custom_catalog(
    tenant_id: str, store: "object"
) -> ApiSourceCatalog:
    """Load a tenant's custom entries from ``store``, cache them, and return the
    merged catalog. This is the async entry point the routes / rails call to make
    a tenant's private sources participate.

    ``store`` is a ``TenantApiSourceStore`` (typed loosely to avoid a hard import
    cycle with ``store.py``). Only valid entries are cached — a stored spec that
    somehow fails validation is skipped (defensive; the write path validates on
    the way in) so one bad row can never sink a tenant's whole layer.
    """
    records = await store.list_for_tenant(tenant_id)  # type: ignore[attr-defined]
    specs: list[ApiSourceSpec] = []
    for rec in records:
        spec = rec.materialized_spec()
        if validate_spec(spec):
            logger.warning(
                "api_registry: skipping invalid stored tenant entry %s/%s",
                tenant_id, spec.slug or "?",
            )
            continue
        specs.append(spec)
    set_tenant_custom_specs(tenant_id, specs)
    return get_api_source_catalog(tenant_id)


def set_tenant_custom_specs(tenant_id: str, specs: list[ApiSourceSpec]) -> None:
    """Replace a tenant's cached custom-layer specs. Used by the async loader and
    by tests/injectors that want a tenant layer without a store.

    Each spec is tagged ``layer=tenant_custom`` here (the canonical marker for a
    tenant entry) so callers that pass a bare spec — not one already materialized
    by :meth:`TenantApiSource.materialized_spec` — still land in the right layer
    and are correctly reported as editable by the routes/SDK."""
    tagged: list[ApiSourceSpec] = []
    for s in specs:
        s.layer = LAYER_TENANT_CUSTOM
        tagged.append(s)
    _tenant_custom_cache[tenant_id] = tagged


def invalidate_tenant_catalog(tenant_id: str) -> None:
    """Drop a tenant's cached custom layer so the next load re-reads the store.

    Called on EVERY write (create/update/delete/enable) to that tenant's sources —
    the explicit-invalidation half of the per-tenant cache. Removing the key (vs
    setting an empty list) forces a real reload, so a tenant that just deleted its
    last entry doesn't get silently frozen as "loaded, empty" against a stale
    read.
    """
    _tenant_custom_cache.pop(tenant_id, None)


def reset_api_source_catalog() -> None:
    """Drop the cached catalog + all per-tenant custom layers so the next access
    rebuilds (tests / startup)."""
    global _catalog_singleton
    _catalog_singleton = None
    _tenant_custom_cache.clear()


__all__ = [
    "ApiSourceCatalog",
    "load_catalog_dir",
    "make_api_source_catalog",
    "get_api_source_catalog",
    "load_tenant_custom_catalog",
    "set_tenant_custom_specs",
    "invalidate_tenant_catalog",
    "reset_api_source_catalog",
    "register_api_source_layer",
    "reset_api_source_layers",
    "registered_layers",
]

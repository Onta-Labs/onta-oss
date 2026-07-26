"""The GLOBAL skill layers — curated content, registered at startup.

The two global layers are **operator-curated, not user-managed**, exactly like
the API-source catalog's global layers (``api_registry/catalog.py``):

* **Global-Public** (``Layer.PUBLIC``) — universal, domain-agnostic guidance for
  universal types. Ships in OSS as markdown files under ``skills/data/``, loaded
  once at first use.
* **Global-Enhanced** (``Layer.ENHANCED``) — the curated premium overlay. The
  proprietary package contributes it through :func:`register_skill_layer`, the
  same plugin shape as ``register_adapter`` / ``register_web_source`` /
  ``register_api_source_layer``. **The OSS package never imports the premium
  tree** — this module holds the seam, not the content.

Per-tenant skills are NOT here; they are user-authored data in the durable store
(``skills/store.py``) and are merged on top at resolution time.

Markdown file format (``skills/data/<Type>/<slug>.md``): an optional YAML-ish
front-matter block delimited by ``---`` carrying ``title`` / ``summary`` /
``enabled``, then the body. The type name comes from the parent DIRECTORY and
the slug from the FILENAME, so a file can never disagree with its own location.

Boundary: OSS. Pure ``cograph_client.*`` / stdlib — no ``from cograph.*``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional

from cograph_client.graph.layers import Layer

from .models import TypeSkill, validate_skill

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent / "data"

#: Registered global-layer content, keyed by layer. Populated by
#: :func:`register_skill_layer` (premium overlay) and by the OSS seed loader.
_layers: dict[Layer, list[TypeSkill]] = {}

#: Memoized merge of the global layers. Invalidated on registration.
_cache: Optional[dict[Layer, list[TypeSkill]]] = None


# --------------------------------------------------------------------------- #
# Front-matter parsing
# --------------------------------------------------------------------------- #
def parse_skill_markdown(text: str) -> tuple[dict[str, str], str]:
    """Split ``---`` front matter from the markdown body.

    Deliberately a tiny ``key: value`` reader rather than a YAML dependency:
    the only keys we honour are flat strings. A file with no front matter, or
    with an unterminated block, is treated as all-body — a malformed header must
    never eat the content.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    # lines[0] is the opening fence; find the closing one.
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            meta: dict[str, str] = {}
            for raw in lines[1:idx]:
                if ":" not in raw:
                    continue
                k, _, v = raw.partition(":")
                meta[k.strip().casefold()] = v.strip().strip('"').strip("'")
            return meta, "\n".join(lines[idx + 1 :]).lstrip("\n")
    return {}, text


def load_skill_file(path: Path, *, type_name: str, layer: Layer) -> Optional[TypeSkill]:
    """Build one :class:`TypeSkill` from a markdown file. ``None`` if unreadable
    or invalid (logged and skipped — one bad file must not sink the layer)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("skills: could not read %s: %s", path, exc)
        return None
    meta, body = parse_skill_markdown(text)
    skill = TypeSkill(
        slug=path.stem,
        type_name=type_name,
        body=body,
        title=meta.get("title", ""),
        summary=meta.get("summary", ""),
        layer=layer,
        enabled=meta.get("enabled", "true").casefold() not in ("false", "0", "no"),
    )
    errors = validate_skill(skill)
    if errors:
        logger.warning("skills: skipping invalid %s: %s", path, "; ".join(errors))
        return None
    return skill


def load_skill_dir(directory: Path | str, *, layer: Layer) -> list[TypeSkill]:
    """Load ``<directory>/<Type>/<slug>.md`` into skills tagged with ``layer``.

    Tolerant by design (mirrors ``api_registry.catalog.load_catalog_dir``): a
    malformed file is logged and skipped rather than raising. A missing
    directory yields ``[]`` — an OSS install that ships no seed content is a
    supported state, not an error.
    """
    directory = Path(directory)
    out: list[TypeSkill] = []
    if not directory.is_dir():
        return out
    for type_dir in sorted(p for p in directory.iterdir() if p.is_dir()):
        for path in sorted(type_dir.glob("*.md")):
            skill = load_skill_file(path, type_name=type_dir.name, layer=layer)
            if skill is not None:
                out.append(skill)
    return out


# --------------------------------------------------------------------------- #
# The registration seam
# --------------------------------------------------------------------------- #
def register_skill_layer(layer: Layer, skills: Iterable[TypeSkill]) -> None:
    """Contribute curated skills to a GLOBAL layer.

    The premium package calls this at startup for ``Layer.ENHANCED`` (via the
    existing plugin hooks) to supply the curated overlay. Repeated calls for the
    same layer APPEND, so several contributors can each register a slice.

    Raises ``ValueError`` for ``Layer.TENANT``: tenant skills are user data and
    belong in the durable store, never in a process-wide registry where they
    would leak across workspaces. This is the tenant-isolation guard for this
    module.
    """
    if layer is Layer.TENANT:
        raise ValueError(
            "register_skill_layer is for GLOBAL layers only — tenant skills are "
            "per-workspace data and belong in the TypeSkillStore"
        )
    bucket = _layers.setdefault(layer, [])
    for skill in skills:
        skill.layer = layer
        skill.tenant_id = None
        errors = validate_skill(skill)
        if errors:
            logger.warning(
                "skills: rejecting registered skill %s/%s: %s",
                skill.type_name, skill.slug, "; ".join(errors),
            )
            continue
        bucket.append(skill)
    _invalidate()


def _invalidate() -> None:
    global _cache
    _cache = None


def reset_skill_layers() -> None:
    """Test helper — drop all registered global content and the memoized merge."""
    _layers.clear()
    _invalidate()


def global_skills_by_layer() -> dict[Layer, list[TypeSkill]]:
    """All curated global skills, per layer, memoized.

    The OSS seed directory is loaded lazily on first call (so importing this
    module never touches the filesystem) and merged UNDER anything registered
    for the same layer, so a registration always wins over a shipped file.
    """
    global _cache
    if _cache is not None:
        return _cache

    merged: dict[Layer, list[TypeSkill]] = {}
    seed = load_skill_dir(_DATA_DIR, layer=Layer.PUBLIC)
    if seed:
        merged[Layer.PUBLIC] = list(seed)
    for layer, skills in _layers.items():
        merged.setdefault(layer, [])
        merged[layer].extend(skills)
    _cache = merged
    return merged


def global_skills_for_type(
    type_name: str, *, layer: Optional[Layer] = None
) -> list[TypeSkill]:
    """Curated global skills attached to ``type_name`` (case-tolerant).

    **This is the read function the operator Global Ontology assembler calls**
    to list the skills attached to a global type. It is deliberately a plain,
    importable, synchronous function with no request/tenant context: the global
    layers are shared canon, identical for every caller, so the assembler can
    call it inline while walking types without an await or an extra round trip.

    ``layer=None`` returns both global layers (Enhanced first, then Public —
    precedence order); pass a layer to scope to one.
    """
    want = (type_name or "").casefold()
    by_layer = global_skills_by_layer()
    layers = (
        [layer]
        if layer is not None
        else [Layer.ENHANCED, Layer.PUBLIC]
    )
    out: list[TypeSkill] = []
    for lyr in layers:
        for skill in by_layer.get(lyr, []):
            if skill.type_name.casefold() == want:
                out.append(skill)
    return out


__all__ = [
    "register_skill_layer",
    "reset_skill_layers",
    "global_skills_by_layer",
    "global_skills_for_type",
    "load_skill_dir",
    "load_skill_file",
    "parse_skill_markdown",
]

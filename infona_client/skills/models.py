"""Type-attached SKILLS — the model + validation.

A **skill** is type-attached PROSE: a markdown document whose consumer is an LM
agent. It teaches an agent something about one entity type ("a `Clinic` in this
workspace is a billing location, not a building — never merge two clinics that
share a street address").

A skill is NOT a :mod:`infona_client.models.function`. Functions are
type-attached COMPUTE (an async endpoint that takes the node and processes
something); skills are type-attached INSTRUCTION. They are distinct concepts
with distinct storage, distinct CRUD, and distinct consumers — a function is
invoked, a skill is *read into a prompt*. (ADR 0002 §"Global-Enhanced" lumped
them together as "strategy bundles (skills)"; that phrasing predates the
product definition and does not describe this feature.)

Scoping mirrors the ontology layers (``graph/layers.py``): a skill is attached
to a type IN A LAYER. Under the layer content matrix (ONTA-400) **Public may
not carry skills** — only Enhanced and Tenant do. The model still tags a
``layer`` so resolution (union-with-shadowing; see
:mod:`infona_client.skills.resolve`) and the reserved-empty Public seed path
stay coherent; writers refuse non-empty Public registration at the seam.

Boundary: OSS. Pure ``infona_client.*`` / stdlib — no ``from cograph.*``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from infona_client.graph.layers import Layer, layer_type_uri

#: A skill's identifier within one (scope, type). Lowercase kebab/snake so it is
#: URL-safe in ``/skills/{slug}`` and stable across renames of the title.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

#: Type names we accept. Matches the ontology's type-name shape (no slashes, no
#: whitespace) so a skill can never be attached to something that is not a type.
TYPE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")

#: Hard cap on a skill body. A skill is guidance for a prompt, not a document
#: store: anything past this is a sign the author means to attach a corpus, and
#: an oversized body would silently blow the prompt budget of every agent that
#: resolves this type. Enforced at validation time (write path), not at read.
MAX_BODY_CHARS = 20_000

MAX_TITLE_CHARS = 200
MAX_SUMMARY_CHARS = 500


@dataclass
class TypeSkill:
    """One markdown skill attached to one entity type in one ontology layer."""

    slug: str
    type_name: str
    body: str
    title: str = ""
    #: One-line gist, so a list view (and an agent deciding *whether* to read
    #: the body) does not have to pull every body.
    summary: str = ""
    layer: Layer = Layer.TENANT
    #: Owning workspace — set for ``Layer.TENANT`` skills, ``None`` for the two
    #: global layers (which are shared canon, not any one tenant's).
    tenant_id: Optional[str] = None
    enabled: bool = True
    #: Monotonic per-(scope, type, slug) revision; bumped by the store on every
    #: successful upsert. Lets a consumer cache a rendered prompt block and
    #: notice staleness without diffing bodies.
    version: int = 1
    #: Free-form author metadata (e.g. ``{"source": "curated", "owner": "..."}``).
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    # -- derived ---------------------------------------------------------- #
    @property
    def type_uri(self) -> str:
        """The layer-qualified type URI this skill is attached to.

        Delegates to :func:`infona_client.graph.layers.layer_type_uri` so a
        skill can never drift from the ontology's own URI convention — Public
        ``Person`` and Tenant ``Person`` are different types and get different
        URIs, exactly as ``layer_from_uri`` assumes.
        """
        return layer_type_uri(self.layer, self.type_name)

    @property
    def key(self) -> tuple[str, str]:
        """Shadowing key: two skills with the same ``(type, slug)`` in different
        layers are the SAME skill, and the higher layer wins."""
        return (self.type_name.casefold(), self.slug)

    # -- serialization ----------------------------------------------------- #
    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "type_name": self.type_name,
            "title": self.title,
            "summary": self.summary,
            "body": self.body,
            "layer": self.layer.value,
            "tenant_id": self.tenant_id,
            "enabled": self.enabled,
            "version": self.version,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TypeSkill":
        """Tolerant constructor (mirrors ``ApiSourceSpec.from_dict``): unknown
        keys are ignored, missing keys take defaults, and an unrecognized
        ``layer`` degrades to TENANT rather than raising — ``validate_skill``
        is the strict gate, not this."""
        layer_raw = raw.get("layer") or Layer.TENANT.value
        try:
            layer = Layer(layer_raw)
        except ValueError:
            layer = Layer.TENANT
        return cls(
            slug=str(raw.get("slug", "") or ""),
            type_name=str(raw.get("type_name", "") or ""),
            body=str(raw.get("body", "") or ""),
            title=str(raw.get("title", "") or ""),
            summary=str(raw.get("summary", "") or ""),
            layer=layer,
            tenant_id=raw.get("tenant_id"),
            enabled=bool(raw.get("enabled", True)),
            version=int(raw.get("version", 1) or 1),
            metadata=dict(raw.get("metadata") or {}),
        )


def validate_skill(skill: TypeSkill) -> list[str]:
    """Structural validation. Returns a list of human-readable errors (empty = ok).

    Same contract as ``api_registry.spec.validate_spec``: pure, no I/O, returns
    every problem at once so a caller can surface them all in one 422.
    """
    errors: list[str] = []

    if not SLUG_RE.match(skill.slug or ""):
        errors.append(
            "slug must be lowercase alphanumeric with - or _ (1-64 chars), "
            f"got {skill.slug!r}"
        )
    if not TYPE_NAME_RE.match(skill.type_name or ""):
        errors.append(
            f"type_name must be a bare ontology type name, got {skill.type_name!r}"
        )
    if not (skill.body or "").strip():
        errors.append("body must not be empty — a skill IS its markdown body")
    elif len(skill.body) > MAX_BODY_CHARS:
        errors.append(
            f"body is {len(skill.body)} chars, max {MAX_BODY_CHARS}"
        )
    if len(skill.title or "") > MAX_TITLE_CHARS:
        errors.append(f"title exceeds {MAX_TITLE_CHARS} chars")
    if len(skill.summary or "") > MAX_SUMMARY_CHARS:
        errors.append(f"summary exceeds {MAX_SUMMARY_CHARS} chars")
    if skill.layer is Layer.TENANT and not skill.tenant_id:
        errors.append("a tenant-layer skill must carry a tenant_id")
    if skill.layer is not Layer.TENANT and skill.tenant_id:
        errors.append(
            f"a {skill.layer.value}-layer skill is shared canon and must not "
            "carry a tenant_id"
        )
    return errors


__all__ = [
    "TypeSkill",
    "validate_skill",
    "SLUG_RE",
    "TYPE_NAME_RE",
    "MAX_BODY_CHARS",
    "MAX_TITLE_CHARS",
    "MAX_SUMMARY_CHARS",
]

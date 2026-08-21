"""Domain-label sanitizer + helpers (property-graph model §1.1 B1 / B2).

Neo4j labels cannot be parameterized, so dynamic type labels are applied only
after the leaf is sanitized to ``^[A-Za-z][A-Za-z0-9_]*$`` and checked against
the reserved system-label set. That allowlist is the injection boundary.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from infona_client.graph.scope import GraphScopeError

if TYPE_CHECKING:
    from infona_client.graph.store import GraphRecord, GraphSession

# Reserved labels (system; not domain type leaves) — model §1.3 B2.
RESERVED_SYSTEM_LABELS: frozenset[str] = frozenset(
    {
        "Entity",
        "OntoType",
        "OntoAttr",
        "ProvEvent",
        "AttrCitation",
        "ValueHistory",
        "Validity",
        "Suppression",
        "KgMeta",
    }
)

_UNSAFE_LABEL_CHARS = re.compile(r"[^A-Za-z0-9_]")
_SAFE_LABEL = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def sanitize_domain_label(leaf: str) -> str:
    """Map an ontology type leaf to a Neo4j-safe domain label (model B1).

    1. ``re.sub(r'[^A-Za-z0-9_]', '_', leaf)``
    2. If the result starts with a digit, prefix ``T_``
    3. Reject empty / still-unsafe tokens and reserved system labels
    """
    if not isinstance(leaf, str) or not leaf.strip():
        raise GraphScopeError(
            "Domain label leaf must be a non-empty string"
        )
    sanitized = _UNSAFE_LABEL_CHARS.sub("_", leaf.strip())
    if sanitized and sanitized[0].isdigit():
        sanitized = f"T_{sanitized}"
    if not sanitized or not _SAFE_LABEL.match(sanitized):
        raise GraphScopeError(
            f"Domain label leaf {leaf!r} sanitizes to unsafe token {sanitized!r}"
        )
    if sanitized in RESERVED_SYSTEM_LABELS:
        raise GraphScopeError(
            f"Domain label {sanitized!r} collides with a reserved system label"
        )
    return sanitized


def is_domain_label(leaf: object) -> bool:
    """Fail-soft twin of :func:`sanitize_domain_label` — *can* this be a label?

    The raise flavour above is right for a leaf the CALLER supplied and can fix
    (one schema mutation, one type summary). Use this one wherever the leaf is
    DERIVED — synthesized as a fallback, or read back from the store while
    fanning out over many — and one unusable leaf must not take the whole batch
    down. Same raise/skip split (and the same shared-not-copied reason) as
    :func:`~infona_client.graph.queries.require_valid_type_name` vs
    :func:`~infona_client.graph.queries.skip_invalid_type_name`; that pair
    covers IRI-safety, this one covers the B2 reserved-label gate.
    """
    try:
        sanitize_domain_label(leaf)  # type: ignore[arg-type]  — guarded inside
    except GraphScopeError:
        return False
    return True


def sanitize_domain_labels(leaves: Sequence[str]) -> list[str]:
    """Sanitize a sequence of leaves; preserve order, drop duplicates."""
    out: list[str] = []
    seen: set[str] = set()
    for leaf in leaves:
        token = sanitize_domain_label(leaf)
        if token not in seen:
            seen.add(token)
            out.append(token)
    if not out:
        raise GraphScopeError("At least one domain label is required")
    return out


def entity_set_labels_cypher(safe_labels: Sequence[str]) -> str:
    """Build scoped Cypher that SETs allowlisted labels on an Entity.

    ``safe_labels`` **must** already be outputs of :func:`sanitize_domain_label`
    (this function re-validates). Labels are embedded in the string because
    Neo4j does not parameterize label tokens; the sanitizer is the only gate.
    """
    validated = sanitize_domain_labels(list(safe_labels))
    # Each token is [A-Za-z][A-Za-z0-9_]* — safe to interpolate as labels.
    label_suffix = ":".join(validated)
    return (
        "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg, id: $id})\n"
        f"SET e:{label_suffix}\n"
        "RETURN e.id AS id, e.tenant_id AS tenant_id, e.kg AS kg, labels(e) AS labels"
    )


async def set_entity_type_labels(
    session: "GraphSession",
    entity_id: str,
    labels: Sequence[str],
    *,
    params: Mapping[str, Any] | None = None,
) -> list["GraphRecord"]:
    """Apply sanitized primary/domain type labels on an existing Entity.

    Uses allowlisted label tokens only. Prefer calling this after
    ``entity_merge`` when dynamic labels cannot ship inside a static template.
    """
    from infona_client.graph.store import require_entity_write_identity

    safe = sanitize_domain_labels(labels)
    require_entity_write_identity({"id": entity_id})
    # Session-native path for memory store (and future impls).
    native = getattr(session, "apply_entity_domain_labels", None)
    if callable(native):
        return await native(entity_id, safe)

    cypher = entity_set_labels_cypher(safe)
    bound: dict[str, Any] = dict(params or {})
    bound["id"] = entity_id
    return await session.execute_write(cypher, bound)


__all__ = [
    "RESERVED_SYSTEM_LABELS",
    "entity_set_labels_cypher",
    "is_domain_label",
    "sanitize_domain_label",
    "sanitize_domain_labels",
    "set_entity_type_labels",
]

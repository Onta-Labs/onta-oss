"""Layer content matrix — Wave 0 contract freeze for ONTA-396 / ONTA-400.

Single declarative table mapping each ontology layer to the content kinds it
may carry. Both the writers (ONTA-399 layer-B authoring) and the content guard
(ONTA-400) MUST read this table so they cannot disagree about what is allowed
on Public / Enhanced / Tenant.

Product rule (founder, ONTA-396):

* **A. Global / Public** — attributes and relationships only. Open.
* **B. Onta Enhanced** — extends A; adds skills, functions, and data sources. Paid.
* **C. Workspace (tenant)** — extends A (OSS) or B (paid); private; may carry
  every content kind a workspace declares.

**A-restriction enforcement (Wave 0 decision):** hard **invariant**, not an
editorial policy. Plan §11 item 5 was open; the conservative choice is
deny-by-default so ONTA-400 ships a structural guard rather than a lint. Flag
for the founder if product later wants policy-only.

Runtime helpers here (``assert_permits``, ``LayerContentError``,
``is_public_type_uri``) are the single refusal surface writers call so a
second hand-rolled check cannot disagree with the matrix.
"""


from __future__ import annotations

from cograph_client.graph.iri import IRI_BASE
from enum import Enum
from typing import Final, Literal, Mapping

from cograph_client.graph.layers import Layer, layer_from_uri, type_namespace


class ContentKind(str, Enum):
    """Kinds of content an ontology layer may carry."""

    ATTRIBUTES = "attributes"
    RELATIONSHIPS = "relationships"
    SKILLS = "skills"
    FUNCTIONS = "functions"
    SOURCES = "sources"


class LayerContentError(ValueError):
    """Raised when content of a forbidden kind would land on a layer.

    Subclass of ``ValueError`` so existing ``pytest.raises(ValueError, ...)``
    call sites keep matching while callers can pin the content-contract class.
    """


#: How the A-only-attrs-and-rels rule is enforced. Frozen as ``"invariant"``
#: (deny-by-default guard in ONTA-400). Do not silently flip to ``"policy"`` —
#: that is a product decision that changes the guard's failure mode.
LayerAContentEnforcement = Literal["invariant", "policy"]

LAYER_A_CONTENT_ENFORCEMENT: Final[LayerAContentEnforcement] = "invariant"

#: The one table every writer and the ONTA-400 guard read.
LAYER_CONTENT_MATRIX: Final[Mapping[Layer, frozenset[ContentKind]]] = {
    Layer.PUBLIC: frozenset(
        {
            ContentKind.ATTRIBUTES,
            ContentKind.RELATIONSHIPS,
        }
    ),
    Layer.ENHANCED: frozenset(
        {
            ContentKind.ATTRIBUTES,
            ContentKind.RELATIONSHIPS,
            ContentKind.SKILLS,
            ContentKind.FUNCTIONS,
            ContentKind.SOURCES,
        }
    ),
    Layer.TENANT: frozenset(
        {
            ContentKind.ATTRIBUTES,
            ContentKind.RELATIONSHIPS,
            ContentKind.SKILLS,
            ContentKind.FUNCTIONS,
            ContentKind.SOURCES,
        }
    ),
}


def permits(layer: Layer, kind: ContentKind) -> bool:
    """True iff ``layer`` may carry content of ``kind`` per the matrix."""
    return kind in LAYER_CONTENT_MATRIX[layer]


def forbidden_kinds(layer: Layer) -> frozenset[ContentKind]:
    """Content kinds the matrix forbids on ``layer``."""
    return frozenset(ContentKind) - LAYER_CONTENT_MATRIX[layer]


def assert_permits(layer: Layer, kind: ContentKind, *, what: str = "") -> None:
    """Raise :class:`LayerContentError` if ``layer`` may not carry ``kind``.

    Writers call this before attaching skills / functions / sources so the
    refusal text always names the matrix rule rather than a hand-rolled check.
    """
    if permits(layer, kind):
        return
    permitted = ", ".join(sorted(k.value for k in LAYER_CONTENT_MATRIX[layer]))
    detail = f" ({what})" if what else ""
    raise LayerContentError(
        f"{layer.value} layer may not carry {kind.value}{detail}; "
        f"permitted kinds: {permitted}. "
        f"Public is attributes + relationships only "
        f"(LAYER_A_CONTENT_ENFORCEMENT={LAYER_A_CONTENT_ENFORCEMENT!r})."
    )


def is_public_type_uri(uri_or_name: str) -> bool:
    """True iff ``uri_or_name`` is (or would resolve to) a Public-namespace type.

    Accepts a full type URI (``https://graph.onta.sh/types/public/Person``), a
    path-shaped entity_type (``public/Person`` → minted under the tenant
    namespace prefix as ``types/public/Person``), or a bare name (``Person`` →
    tenant namespace — not public). Used by function writers so a smuggled
    ``entity_type="public/Foo"`` cannot attach a function to Public.
    """
    raw = (uri_or_name or "").strip()
    if not raw:
        return False
    if raw.startswith("http://") or raw.startswith("https://"):
        return layer_from_uri(raw) is Layer.PUBLIC
    # Path-shaped: register_function_triple mints types/{entity_type}, so
    # entity_type="public/Person" becomes types/public/Person (PUBLIC).
    candidate = f"{IRI_BASE}/types/{raw}"
    if layer_from_uri(candidate) is Layer.PUBLIC:
        return True
    # Defensive: also match the namespace prefix as a substring so a future
    # mint shape that still embeds the public namespace is caught.
    public_ns = type_namespace(Layer.PUBLIC)
    return public_ns in raw or public_ns in candidate

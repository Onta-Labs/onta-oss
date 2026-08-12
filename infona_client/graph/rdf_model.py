"""RDF-semantic Assertion write API (ADR 0013) for GraphSession / kg_writer.

Identity primitives live in :mod:`infona_client.graph.assertion_model`
(``make_assertion_id``, ``property_uri``, ``type_membership_property_id``).
This module owns the **session write surface**:

* :func:`assert_fact` — structured Assertion write used by :mod:`pg_ops`
* Class / Property / hierarchy MERGE helpers
* Fact → AssertionFact mapping for dual-backend insert_facts

**SoT rule:** Assertion nodes are authoritative. Entity property cache,
typed shortcut rels, and ``INSTANCE_OF`` are dual-written derived projections.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, Mapping

from infona_client.graph.assertion_model import (
    TYPE_MEMBERSHIP_LEAF,
    AssertionFact,
    AssertionNode,
    ClassNode,
    EntityNode,
    PropertyKind,
    PropertyNode,
    canonical_literal,
    make_assertion_id,
    property_uri,
    type_membership_property_id,
)
from infona_client.graph.ontology_queries import type_uri
from infona_client.graph.scope import GraphScopeError
from infona_client.graph.store import require_entity_write_identity

if TYPE_CHECKING:
    from infona_client.graph.store import GraphRecord, GraphSession

# Public aliases (mandate names)
mint_assertion_id = make_assertion_id
TYPE_MEMBERSHIP_PROPERTY_ID = type_membership_property_id()  # frozen at import


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def class_iri(type_leaf_or_iri: str) -> str:
    """Class node id = type IRI from shared :func:`type_uri`."""
    if not type_leaf_or_iri or not str(type_leaf_or_iri).strip():
        raise GraphScopeError("class_iri requires a non-empty type leaf or IRI")
    raw = str(type_leaf_or_iri).strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return type_uri(raw)


def datatype_property_iri(leaf: str) -> str:
    return property_uri(leaf)


def object_property_iri(leaf: str) -> str:
    """Object properties share the same Property IRI namespace as datatype leaves."""
    return property_uri(leaf)


def fact_to_assertion_fact(
    *,
    subject_id: str,
    kind: Literal["literal", "rel", "type"],
    key: str,
    value: Any = None,
    source: str | None = None,
    source_url: str | None = None,
    verified_at: str | None = None,
    run_id: str | None = None,
    confidence: float | None = None,
    provenance: str | None = None,
) -> AssertionFact:
    """Map store-path :class:`~infona_client.graph.facts.Fact` → AssertionFact."""
    require_entity_write_identity({"id": subject_id})
    src = source_url or source
    if kind == "type":
        # Prefer full Class IRI in ``value`` when the Fact carried one (ETL /
        # classify_triple); fall back to type leaf in ``key``.
        type_value: Any = key
        if isinstance(value, str) and value.strip():
            type_value = value.strip()
        return AssertionFact(
            subject_id=subject_id,
            kind="type",
            value=type_value,
            property_id=type_membership_property_id(),
            source_url=src,
            verified_at=verified_at,
            run_id=run_id,
            confidence=confidence,
            provenance=provenance,
        )
    if kind == "rel":
        if not isinstance(value, str) or not value.strip():
            raise GraphScopeError(
                f"object Assertion requires target entity id string, got {value!r}"
            )
        return AssertionFact(
            subject_id=subject_id,
            kind="object",
            property_leaf=key,
            property_id=property_uri(key),
            value=value,
            source_url=src,
            verified_at=verified_at,
            run_id=run_id,
            confidence=confidence,
            provenance=provenance,
        )
    return AssertionFact(
        subject_id=subject_id,
        kind="literal",
        property_leaf=key,
        property_id=property_uri(key),
        value=value,
        source_url=src,
        verified_at=verified_at,
        run_id=run_id,
        confidence=confidence,
        provenance=provenance,
    )


async def merge_class(
    session: "GraphSession",
    class_id: str,
    *,
    name: str | None = None,
    layer: str = "tenant",
) -> list["GraphRecord"]:
    if not class_id or not str(class_id).strip():
        raise GraphScopeError("Class id must be non-empty IRI")
    leaf = name
    if not leaf:
        leaf = class_id.rstrip("/").rsplit("/", 1)[-1]
    native = getattr(session, "write_merge_class", None)
    if not callable(native):
        raise GraphScopeError(
            "GraphSession does not implement write_merge_class "
            "(MemoryGraphStore / Neo4jGraphStore)"
        )
    return await native(class_id=class_id, name=leaf, layer=layer)


async def merge_property_node(
    session: "GraphSession",
    property_id: str,
    *,
    name: str | None = None,
    kind: str = "datatype",
    layer: str = "tenant",
) -> list["GraphRecord"]:
    if not property_id or not str(property_id).strip():
        raise GraphScopeError("Property id must be non-empty IRI")
    pname = name or property_id.rstrip("/").rsplit("/", 1)[-1]
    store_kind = kind if kind in ("datatype", "object", "type") else "datatype"
    if store_kind == "type":
        store_kind = "object"  # catalog kind for rdf:type property node
    native = getattr(session, "write_merge_property", None)
    if not callable(native):
        raise GraphScopeError("GraphSession does not implement write_merge_property")
    return await native(
        property_id=property_id, name=pname, kind=store_kind, layer=layer
    )


async def set_subclass_of(
    session: "GraphSession",
    child_class_id: str,
    parent_class_id: str,
) -> None:
    await merge_class(session, child_class_id)
    await merge_class(session, parent_class_id)
    native = getattr(session, "write_subclass_of", None)
    if not callable(native):
        raise GraphScopeError("GraphSession does not implement write_subclass_of")
    await native(child_class_id, parent_class_id)


async def set_subproperty_of(
    session: "GraphSession",
    child_prop_id: str,
    parent_prop_id: str,
    *,
    child_kind: str = "datatype",
    parent_kind: str = "datatype",
) -> None:
    await merge_property_node(session, child_prop_id, kind=child_kind)
    await merge_property_node(session, parent_prop_id, kind=parent_kind)
    native = getattr(session, "write_subproperty_of", None)
    if not callable(native):
        raise GraphScopeError("GraphSession does not implement write_subproperty_of")
    await native(child_prop_id, parent_prop_id)


async def assert_fact(
    session: "GraphSession",
    fact: AssertionFact,
    *,
    dual_write_cache: bool = True,
) -> Mapping[str, Any]:
    """Write one Assertion as SoT; optionally dual-write derived Entity cache.

    Ensures subject Entity, Property, and object Entity/Class exist. MERGEs
    Assertion by stable id. When ``dual_write_cache``:

    * type → ``INSTANCE_OF`` + primary_type / domain labels
    * literal → Entity property cache
    * object → typed shortcut relationship
    """
    require_entity_write_identity({"id": fact.subject_id})
    prop_id = fact.resolved_property_id()
    prop_name = fact.property_leaf or prop_id.rstrip("/").rsplit("/", 1)[-1]

    # Ensure Property catalog node.
    prop_kind: str
    if fact.kind == "type":
        prop_kind = "type"
        prop_name = TYPE_MEMBERSHIP_LEAF
    elif fact.kind == "object":
        prop_kind = "object"
    else:
        prop_kind = "datatype"
    await merge_property_node(
        session,
        prop_id,
        name=prop_name,
        kind=prop_kind if prop_kind != "type" else "object",
    )

    from infona_client.graph import pg_ops

    await pg_ops.merge_entity(session, fact.subject_id)

    object_id: str | None = None
    object_class_id: str | None = None
    literal_value: Any = None
    object_key: str

    if fact.kind == "literal":
        # Strip SPARQL-era ``lexical^^xsd`` and coerce numerics so Neo4j stores
        # native floats/ints (NL filters: toFloat(e.price) < 15). Idempotent.
        from infona_client.graph.assertion_model import normalize_store_literal

        literal_value = normalize_store_literal(fact.value)
        object_key = canonical_literal(literal_value)
    elif fact.kind == "object":
        if not isinstance(fact.value, str) or not fact.value:
            raise GraphScopeError("object Assertion needs entity id value")
        object_id = fact.value
        object_key = object_id
        require_entity_write_identity({"id": object_id})
        await pg_ops.merge_entity(session, object_id)
    elif fact.kind == "type":
        if isinstance(fact.value, str) and fact.value.startswith("http"):
            object_class_id = fact.value
        else:
            object_class_id = class_iri(str(fact.value))
        object_key = object_class_id
        type_leaf = object_class_id.rstrip("/").rsplit("/", 1)[-1]
        await merge_class(session, object_class_id, name=type_leaf)
    else:
        raise GraphScopeError(f"unknown AssertionFact.kind {fact.kind!r}")

    aid = make_assertion_id(
        fact.subject_id,
        prop_id,
        object_key,
        source_discriminator=fact.source_url or fact.run_id,
    )

    native = getattr(session, "write_assertion", None)
    if not callable(native):
        raise GraphScopeError(
            "GraphSession does not implement write_assertion "
            "(MemoryGraphStore / Neo4jGraphStore)"
        )

    await native(
        assertion_id=aid,
        subject_id=fact.subject_id,
        property_id=prop_id,
        property_name=prop_name,
        property_kind="object" if prop_kind == "type" else prop_kind,
        object_id=object_id,
        object_class_id=object_class_id,
        literal_value=literal_value,
        literal_datatype=None,
        source_url=fact.source_url,
        verified_at=fact.verified_at,
        run_id=fact.run_id,
        confidence=fact.confidence,
        provenance=fact.provenance,
        ts=_ts(),
    )

    if dual_write_cache:
        if fact.kind == "type" and object_class_id:
            inst = getattr(session, "write_instance_of", None)
            if callable(inst):
                await inst(fact.subject_id, object_class_id)
            type_leaf = object_class_id.rstrip("/").rsplit("/", 1)[-1]
            from infona_client.graph.labels import (
                sanitize_domain_labels,
                set_entity_type_labels,
            )

            safe = sanitize_domain_labels([type_leaf])
            if safe:
                await set_entity_type_labels(session, fact.subject_id, safe)
            await pg_ops.merge_entity(
                session, fact.subject_id, primary_type=type_leaf
            )
        elif fact.kind == "literal":
            leaf = prop_name
            if leaf == "name":
                await pg_ops.merge_entity(
                    session, fact.subject_id, name=literal_value
                )
            elif leaf == "source":
                await pg_ops.merge_entity(
                    session, fact.subject_id, source=literal_value
                )
            elif leaf not in ("id", "tenant_id", "kg", "primary_type", "elementId"):
                try:
                    await pg_ops.set_literal(
                        session,
                        fact.subject_id,
                        leaf,
                        literal_value,
                        multi_union=True,
                    )
                except GraphScopeError:
                    # Reserved keys rejected by sanitize — skip cache only.
                    pass
        elif fact.kind == "object" and object_id:
            await pg_ops.merge_rel(
                session, fact.subject_id, object_id, prop_name
            )

    return {
        "assertion_id": aid,
        "subject_id": fact.subject_id,
        "property_id": prop_id,
        "kind": fact.kind,
        "object_id": object_id or object_class_id,
        "literal_value": literal_value,
        "source_url": fact.source_url,
    }


async def delete_assertions_for_subject(
    session: "GraphSession",
    subject_id: str,
    *,
    property_id: str | None = None,
    object_key: str | None = None,
) -> int:
    """Delete Assertions for a subject (ADR 0013 SoT cleanup).

    Requires ``write_delete_assertions`` on the session — soft-skip is not
    allowed on the product store path (Memory / Neo4j).
    """
    require_entity_write_identity({"id": subject_id})
    native = getattr(session, "write_delete_assertions", None)
    if not callable(native):
        raise GraphScopeError(
            "GraphSession does not implement write_delete_assertions; "
            "Assertion SoT cleanup is required on the store path (ADR 0013). "
            "Use MemoryGraphStore or Neo4jGraphStore."
        )
    return int(
        await native(
            subject_id=subject_id,
            property_id=property_id,
            object_key=object_key,
        )
    )


__all__ = [
    "TYPE_MEMBERSHIP_LEAF",
    "TYPE_MEMBERSHIP_PROPERTY_ID",
    "AssertionFact",
    "AssertionNode",
    "ClassNode",
    "EntityNode",
    "PropertyKind",
    "PropertyNode",
    "assert_fact",
    "canonical_literal",
    "class_iri",
    "datatype_property_iri",
    "delete_assertions_for_subject",
    "fact_to_assertion_fact",
    "make_assertion_id",
    "merge_class",
    "merge_property_node",
    "mint_assertion_id",
    "object_property_iri",
    "property_uri",
    "set_subclass_of",
    "set_subproperty_of",
    "type_membership_property_id",
]

"""Structured Fact IR for the Neo4j write path (property-graph model §8).

New writers should emit :class:`Fact` objects. Legacy ``(s, p, o)`` triple lists
are still accepted by :func:`cograph_client.graph.kg_writer.insert_facts` and
mapped here so callers do not all break mid-migration.

Identity / sanitizer rules (model B1–B5):
* Entity ``id`` is the ``entity_uri()`` string — never invent a second sanitizer.
* Domain labels / prop keys / rel types use the B1 character class; rel types are
  upper-snake. Original leaves stay on ``Fact.key`` / rel ``attr`` / ``primary_type``.
* Reserved Entity property keys (B2) are rejected for ontology attribute leaves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Literal, Sequence

from cograph_client.graph.iri import (
    ENTITY_URI_PREFIX,
    IRI_BASE,
    ONTO_PRED_PREFIX,
    TYPE_URI_PREFIX,
)
from cograph_client.graph.predicates import RDF_TYPE, RDFS_NS
from cograph_client.graph.scope import GraphScopeError

FactKind = Literal["literal", "rel", "type"]

# Reserved property keys on :Entity (model §1.3 B2) — not ontology attributes.
RESERVED_ENTITY_PROPERTY_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "tenant_id",
        "kg",
        "primary_type",
        "name",
        "label",
        "source",
        "created_at",
        "updated_at",
        "elementId",
    }
)

_UNSAFE = re.compile(r"[^A-Za-z0-9_]")
_SAFE_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_SAFE_PROP = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# rdf / rdfs display + type.
_RDFS_LABEL = f"{RDFS_NS}label"


@dataclass(frozen=True, slots=True)
class Fact:
    """One instance mutation unit for :func:`insert_facts` (model §8.2).

    Parameters
    ----------
    subject_id:
        Entity ``id`` — the ``entity_uri()`` string.
    kind:
        ``literal`` | ``rel`` | ``type``.
    key:
        Original attribute leaf, or type leaf for ``kind=type``.
    value:
        Literal / list / target entity id for rel; ignored for ``type`` when the
        type leaf is in ``key``.
    source:
        Optional ingest source stamp (maps to Entity ``source`` when present).
    """

    subject_id: str
    kind: FactKind
    key: str
    value: Any = None
    source: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.subject_id, str) or not self.subject_id.strip():
            raise GraphScopeError("Fact.subject_id must be a non-empty string")
        if self.kind not in ("literal", "rel", "type"):
            raise GraphScopeError(f"Fact.kind must be literal|rel|type, got {self.kind!r}")
        if not isinstance(self.key, str) or not self.key.strip():
            raise GraphScopeError("Fact.key must be a non-empty string")


def sanitize_prop_key(leaf: str) -> str:
    """Map an ontology attribute leaf to an Entity property key (model §2.5 B1).

    Rejects reserved system keys. Safe leaves pass through unchanged.
    """
    if not isinstance(leaf, str) or not leaf.strip():
        raise GraphScopeError("Property key leaf must be a non-empty string")
    raw = leaf.strip()
    if raw in RESERVED_ENTITY_PROPERTY_KEYS:
        raise GraphScopeError(
            f"Property key {raw!r} collides with a reserved Entity property"
        )
    if _SAFE_PROP.match(raw) and raw not in RESERVED_ENTITY_PROPERTY_KEYS:
        return raw
    sanitized = _UNSAFE.sub("_", raw)
    if sanitized and sanitized[0].isdigit():
        sanitized = f"T_{sanitized}"
    if not sanitized or not _SAFE_PROP.match(sanitized):
        raise GraphScopeError(
            f"Property leaf {leaf!r} sanitizes to unsafe token {sanitized!r}"
        )
    if sanitized in RESERVED_ENTITY_PROPERTY_KEYS:
        raise GraphScopeError(
            f"Property key {sanitized!r} collides with a reserved Entity property"
        )
    return sanitized


def sanitize_rel_type(leaf: str) -> str:
    """Map an ontology relationship leaf to a Neo4j rel type (model §2.2 B1).

    Sanitize then upper-case; prefix ``T_`` if the token would start with a digit.
    Always store the original leaf on the relationship as ``attr``.
    """
    if not isinstance(leaf, str) or not leaf.strip():
        raise GraphScopeError("Relationship type leaf must be a non-empty string")
    sanitized = _UNSAFE.sub("_", leaf.strip())
    if sanitized and sanitized[0].isdigit():
        sanitized = f"T_{sanitized}"
    if not sanitized or not _SAFE_TOKEN.match(sanitized):
        raise GraphScopeError(
            f"Relationship leaf {leaf!r} sanitizes to unsafe token {sanitized!r}"
        )
    return sanitized.upper()


def _leaf_from_uri(uri: str, prefix: str) -> str | None:
    if not uri.startswith(prefix):
        return None
    leaf = uri[len(prefix) :]
    return leaf if leaf else None


def _attrs_leaf(predicate: str) -> str | None:
    """Extract leaf from ``…/types/<Type>/attrs/<leaf>`` (type segment ignored — B3)."""
    marker = "/attrs/"
    if marker not in predicate:
        return None
    # Prefer TYPE_URI_PREFIX when present; also accept any …/types/…/attrs/ form.
    if "/types/" not in predicate and not predicate.startswith(TYPE_URI_PREFIX):
        # Still allow …/attrs/<leaf> only if it looks like our attr namespace.
        pass
    idx = predicate.rfind(marker)
    if idx < 0:
        return None
    leaf = predicate[idx + len(marker) :]
    if not leaf or "/" in leaf:
        # attr_meta-style multi-segment tails are not plain attrs
        return None
    return leaf


def _onto_leaf(predicate: str) -> str | None:
    leaf = _leaf_from_uri(predicate, ONTO_PRED_PREFIX)
    if leaf is not None:
        return leaf
    # Cross-host: …/onto/<leaf> (ETL keeps subject/object IRIs as-is).
    if isinstance(predicate, str) and predicate.startswith("http") and "/onto/" in predicate:
        tail = predicate.rsplit("/onto/", 1)[-1]
        if tail and "/" not in tail:
            return tail
    return None


def _type_leaf_from_object(obj: str) -> str | None:
    """``…/types/<Type>`` → Type leaf (no further path segments).

    Prefer live :data:`TYPE_URI_PREFIX`; also accept cross-host
    ``…/types/<Type>`` so Neptune ETL keeps the original Class IRI as ``value``.
    """
    leaf = _leaf_from_uri(obj, TYPE_URI_PREFIX)
    if leaf is not None and leaf and "/" not in leaf:
        return leaf
    if isinstance(obj, str) and obj.startswith("http") and "/types/" in obj:
        tail = obj.rsplit("/types/", 1)[-1]
        if tail and "/" not in tail:
            return tail
    return None


def _is_entity_ref(value: str) -> bool:
    if not isinstance(value, str):
        return False
    if value.startswith(ENTITY_URI_PREFIX):
        return True
    # Cross-base entity IRIs (legacy hosts) still look like …/entities/<T>/<id>
    return "/entities/" in value and value.startswith("http")


def classify_triple(s: str, p: str, o: str) -> Fact | None:
    """Map one RDF-era triple to a :class:`Fact`, or ``None`` if skipped.

    Skipped: empty terms, pure bookkeeping that is not Entity ``source`` /
    display ``name``, attr_meta companions (not Fact kinds in Wave 1).

    ``kind=type``: ``key`` is the type leaf; ``value`` is the **original Class
    IRI** when the object is an HTTP IRI (ADR 0013 — keep RDF IRIs as ids),
    else the leaf. Writers / ``fact_to_assertion_fact`` resolve Class nodes
    from that value.
    """
    if not s or not p:
        return None

    if p == RDF_TYPE:
        tleaf = _type_leaf_from_object(o) if o else None
        if not tleaf:
            return None
        # Preserve original Class IRI when present (do not remint under IRI_BASE).
        type_value: Any = o if (isinstance(o, str) and o.startswith("http")) else tleaf
        return Fact(subject_id=s, kind="type", key=tleaf, value=type_value)

    if p == _RDFS_LABEL:
        # Display property is ``name`` only (B2).
        return Fact(subject_id=s, kind="literal", key="name", value=o)

    attrs_leaf = _attrs_leaf(p)
    if attrs_leaf is not None:
        # name/label as ontology attrs still map to display ``name`` if leaf is label
        if attrs_leaf == "label":
            return Fact(subject_id=s, kind="literal", key="name", value=o)
        return Fact(subject_id=s, kind="literal", key=attrs_leaf, value=o)

    onto_leaf = _onto_leaf(p)
    if onto_leaf is not None:
        if onto_leaf == "source":
            return Fact(subject_id=s, kind="literal", key="source", value=o, source=o)
        # Housekeeping onto markers that are not domain attrs — keep as entity
        # props only when they are simple literals (ingested_at, batch_id).
        if onto_leaf in ("ingested_at", "batch_id", "coreSlot", "aliasOf", "lambda_refreshed_at"):
            return Fact(subject_id=s, kind="literal", key=onto_leaf, value=o)
        if o and _is_entity_ref(o):
            return Fact(subject_id=s, kind="rel", key=onto_leaf, value=o)
        # Literal-valued onto/* (rare domain) — store as entity property.
        return Fact(subject_id=s, kind="literal", key=onto_leaf, value=o)

    return None


def triples_to_facts(triples: Iterable[tuple[str, str, str]]) -> list[Fact]:
    """Convert legacy ``(s, p, o)`` triples into Facts (order-preserving, skips noise)."""
    out: list[Fact] = []
    for triple in triples:
        if not triple or len(triple) < 3:
            continue
        s, p, o = triple[0], triple[1], triple[2]
        fact = classify_triple(s, p, o if o is not None else "")
        if fact is not None:
            out.append(fact)
    return out


def group_facts_by_subject(facts: Sequence[Fact]) -> dict[str, list[Fact]]:
    """Group Facts by ``subject_id`` (insertion order of subjects preserved)."""
    grouped: dict[str, list[Fact]] = {}
    for f in facts:
        grouped.setdefault(f.subject_id, []).append(f)
    return grouped


def primary_type_from_facts(subject_facts: Sequence[Fact]) -> str | None:
    """Most-specific primary type: last ``kind=type`` fact's key, if any."""
    primary: str | None = None
    for f in subject_facts:
        if f.kind == "type":
            primary = f.key
    return primary


__all__ = [
    "RESERVED_ENTITY_PROPERTY_KEYS",
    "Fact",
    "FactKind",
    "classify_triple",
    "group_facts_by_subject",
    "primary_type_from_facts",
    "sanitize_prop_key",
    "sanitize_rel_type",
    "triples_to_facts",
]

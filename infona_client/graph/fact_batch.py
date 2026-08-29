"""Prepare a Fact list into one UNWIND-able write batch (ADR 0013).

:func:`apply_facts` used to MERGE/SET one Assertion at a time — thousands of
Bolt round-trips per CSV page. After the ontology (Class / Property catalog)
exists, instance ingest is this batch: entity shells, assertions, then the
derived Entity cache in a handful of queries.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from infona_client.graph.assertion_model import (
    TYPE_MEMBERSHIP_LEAF,
    normalize_store_literal,
)
from infona_client.graph.facts import (
    RESERVED_ENTITY_PROPERTY_KEYS,
    Fact,
    group_facts_by_subject,
    primary_type_from_facts,
    sanitize_prop_key,
    sanitize_rel_type,
)
from infona_client.graph.rdf_model import (
    class_iri,
    fact_to_assertion_fact,
    make_assertion_id,
)
from infona_client.graph.scope import GraphScopeError
from infona_client.graph.store import require_entity_write_identity


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _fact_hash(
    subject_id: str,
    attr: str | None,
    object_repr: str | None,
    source: str | None,
) -> str:
    payload = (
        f"{subject_id}|{attr or ''}|"
        f"{object_repr if object_repr is not None else ''}|"
        f"{source or ''}"
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


@dataclass
class FactBatch:
    """One :func:`apply_facts` payload, grouped for UNWIND writes."""

    n_facts: int
    entities: list[dict[str, Any]] = field(default_factory=list)
    properties: list[dict[str, str]] = field(default_factory=list)
    classes: list[dict[str, str]] = field(default_factory=list)
    assertions: list[dict[str, Any]] = field(default_factory=list)
    entity_props: dict[str, dict[str, Any]] = field(default_factory=dict)
    instance_of: list[dict[str, str]] = field(default_factory=list)
    labels: dict[str, list[str]] = field(default_factory=dict)
    rels: list[dict[str, str]] = field(default_factory=list)
    prov_events: list[dict[str, Any]] = field(default_factory=list)


def prepare_fact_batch(
    facts: list[Fact] | tuple[Fact, ...],
    *,
    provenance_enabled: bool = False,
) -> FactBatch:
    """Collapse ``facts`` into catalog + assertion + cache rows. No I/O."""
    if not facts:
        return FactBatch(n_facts=0)

    grouped = group_facts_by_subject(facts)
    target_ids: set[str] = set(grouped)
    for f in facts:
        require_entity_write_identity({"id": f.subject_id})
        if f.kind == "rel" and isinstance(f.value, str) and f.value:
            require_entity_write_identity({"id": f.value})
            target_ids.add(f.value)

    ts = _ts()
    entities: list[dict[str, Any]] = []
    for sid in target_ids:
        sub = grouped.get(sid, [])
        name = None
        source = None
        for f in sub:
            if f.kind == "literal" and f.key == "name" and f.value is not None:
                name = f.value
            if f.kind == "literal" and f.key == "source" and f.value is not None:
                source = f.value
            if f.source:
                source = f.source
            if f.source_url:
                source = f.source_url
        entities.append(
            {
                "id": sid,
                "primary_type": primary_type_from_facts(sub),
                "name": name,
                "source": source,
                "ts": ts,
            }
        )

    props_by_id: dict[str, dict[str, str]] = {}
    classes_by_id: dict[str, str] = {}
    assertions: list[dict[str, Any]] = []
    entity_props: dict[str, dict[str, Any]] = {}
    instance_of: list[dict[str, str]] = []
    labels: dict[str, list[str]] = {}
    rels: list[dict[str, str]] = []
    prov_events: list[dict[str, Any]] = []

    from infona_client.graph.labels import sanitize_domain_label

    for f in facts:
        af = fact_to_assertion_fact(
            subject_id=f.subject_id,
            kind=f.kind,
            key=f.key,
            value=f.value,
            source=f.source,
            source_url=f.source_url,
            verified_at=f.verified_at,
            run_id=f.run_id,
            confidence=f.confidence,
            provenance=f.provenance,
        )
        prop_id = af.resolved_property_id()
        if af.kind == "type":
            prop_name = TYPE_MEMBERSHIP_LEAF
            prop_kind = "object"
        elif af.kind == "object":
            prop_name = af.property_leaf or prop_id.rstrip("/").rsplit("/", 1)[-1]
            prop_kind = "object"
        else:
            prop_name = af.property_leaf or prop_id.rstrip("/").rsplit("/", 1)[-1]
            prop_kind = "datatype"
        props_by_id[prop_id] = {
            "id": prop_id,
            "name": prop_name,
            "kind": prop_kind,
        }

        object_id: str | None = None
        object_class_id: str | None = None
        literal_value: Any = None
        object_key: str

        if af.kind == "literal":
            literal_value = normalize_store_literal(af.value)
            object_key = _canonical_literal(literal_value)
        elif af.kind == "object":
            if not isinstance(af.value, str) or not af.value:
                raise GraphScopeError("object Assertion needs entity id value")
            object_id = af.value
            object_key = object_id
        elif af.kind == "type":
            if isinstance(af.value, str) and af.value.startswith("http"):
                object_class_id = af.value
            else:
                object_class_id = class_iri(str(af.value))
            object_key = object_class_id
            type_leaf = object_class_id.rstrip("/").rsplit("/", 1)[-1]
            classes_by_id[object_class_id] = type_leaf
        else:
            raise GraphScopeError(f"unknown AssertionFact.kind {af.kind!r}")

        aid = make_assertion_id(
            af.subject_id,
            prop_id,
            object_key,
            source_discriminator=af.source_url or af.run_id,
        )
        assertions.append(
            {
                "assertion_id": aid,
                "subject_id": af.subject_id,
                "property_id": prop_id,
                "object_id": object_id,
                "object_class_id": object_class_id,
                "literal_value": literal_value,
                "literal_datatype": None,
                "source_url": af.source_url,
                "verified_at": af.verified_at,
                "run_id": af.run_id,
                "confidence": af.confidence,
                "provenance": af.provenance,
                "ts": ts,
            }
        )

        if af.kind == "type" and object_class_id:
            instance_of.append(
                {"entity_id": af.subject_id, "class_id": object_class_id}
            )
            type_leaf = object_class_id.rstrip("/").rsplit("/", 1)[-1]
            try:
                token = sanitize_domain_label(type_leaf)
            except GraphScopeError:
                token = None
            if token:
                labels.setdefault(token, []).append(af.subject_id)
        elif af.kind == "literal":
            leaf = prop_name
            if leaf not in RESERVED_ENTITY_PROPERTY_KEYS:
                try:
                    key = sanitize_prop_key(leaf)
                except GraphScopeError:
                    key = None
                if key is not None:
                    slot = entity_props.setdefault(af.subject_id, {})
                    prev = slot.get(key)
                    if prev is None:
                        slot[key] = literal_value
                    elif isinstance(prev, list):
                        if literal_value not in prev:
                            prev.append(literal_value)
                    elif prev != literal_value:
                        slot[key] = [prev, literal_value]
        elif af.kind == "object" and object_id:
            rels.append(
                {
                    "start_id": af.subject_id,
                    "end_id": object_id,
                    "rel_type": sanitize_rel_type(prop_name),
                    "attr": prop_name,
                }
            )

        if provenance_enabled and f.kind in ("literal", "rel"):
            obj_repr = (
                str(f.value)
                if f.kind == "literal" and f.value is not None
                else (f.value if f.kind == "rel" else None)
            )
            if obj_repr is not None and not isinstance(obj_repr, str):
                obj_repr = str(obj_repr)
            src = f.source_url or f.source
            if obj_repr is not None:
                prov_events.append(
                    {
                        "event_type": "assert",
                        "subject_id": f.subject_id,
                        "attr": f.key,
                        "object_repr": obj_repr,
                        "old_id": None,
                        "new_id": None,
                        "reason": "",
                        "source": src,
                        "fact_hash": _fact_hash(
                            f.subject_id, f.key, obj_repr, src
                        ),
                        "ts": f.verified_at or ts,
                        "confidence": f.confidence,
                    }
                )

    # Dedupe label ids while preserving first-seen order.
    labels = {
        token: list(dict.fromkeys(ids)) for token, ids in labels.items()
    }

    return FactBatch(
        n_facts=len(facts),
        entities=entities,
        properties=list(props_by_id.values()),
        classes=[{"id": cid, "name": name} for cid, name in classes_by_id.items()],
        assertions=assertions,
        entity_props=entity_props,
        instance_of=instance_of,
        labels=labels,
        rels=rels,
        prov_events=prov_events,
    )


def _canonical_literal(value: Any) -> str:
    from infona_client.graph.assertion_model import canonical_literal

    return canonical_literal(value)

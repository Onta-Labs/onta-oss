"""Row dataclasses for the in-memory GraphStore test double.

These types are the process-local stand-ins for Entity / Rel / Prov /
Assertion / OntoType / OntoAttr nodes. Tests may import ``_EntityRow``
and ``_RelRow`` from :mod:`infona_client.graph.memory_store`.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from infona_client.graph.store import GraphRecord


@dataclass
class _EntityRow:
    tenant_id: str
    kg: str
    id: str
    primary_type: str | None = None
    name: str | None = None
    source: str | None = None
    labels: list[str] = field(default_factory=lambda: ["Entity"])
    props: dict[str, Any] = field(default_factory=dict)

    def as_record(self) -> GraphRecord:
        data = {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "kg": self.kg,
            "primary_type": self.primary_type,
            "name": self.name,
            "source": self.source,
            "labels": list(self.labels),
            "props": copy.deepcopy(self.props),
        }
        return GraphRecord(data=data)


@dataclass
class _RelRow:
    tenant_id: str
    kg: str
    start_id: str
    end_id: str
    rel_type: str
    attr: str


@dataclass
class _ProvRow:
    tenant_id: str
    kg: str
    event_type: str
    subject_id: str
    attr: str | None = None
    object_repr: str | None = None
    old_id: str | None = None
    new_id: str | None = None
    reason: str = ""
    source: str | None = None
    fact_hash: str | None = None
    ts: str | None = None
    # ONTA-536: ADR 0002 governance fields on assert events.
    confidence: float | None = None
    # ABOUT is implied: event is always about subject_id when entity exists.


@dataclass
class _ValueHistoryRow:
    """One old→new value transition (ONTA-236 / ONTA-536 property-graph port)."""

    tenant_id: str
    kg: str
    subject_id: str
    predicate: str
    old_value: str
    new_value: str
    changed_at: str


@dataclass
class _SuppressionRow:
    """One sticky suppression marker (ONTA-279 property-graph port).

    MERGE identity is ``(tenant_id, kg, mark_id)`` — ``mark_id`` is the RDF
    mark-node URI, already ``sha1``-keyed on ``(s, p, o)`` (fact) or ``(s)``
    (entity), so re-suppressing the same thing is idempotent. Deliberately
    independent of ``_AssertionRow``: the marker must outlive a hard-deleted
    fact.
    """

    tenant_id: str
    kg: str
    mark_id: str
    kind: str  # "fact" | "entity"
    statement_id: str = ""
    subject: str = ""
    predicate: str = ""
    object_repr: str = ""
    reason: str = ""
    suppressed_at: str = ""
    graph_uri: str = ""

    def as_record(self) -> GraphRecord:
        return GraphRecord(
            data={
                "mark_id": self.mark_id,
                "kind": self.kind,
                "statement_id": self.statement_id,
                "subject": self.subject,
                "predicate": self.predicate,
                "object_repr": self.object_repr,
                "reason": self.reason,
                "suppressed_at": self.suppressed_at,
                "graph_uri": self.graph_uri,
                "tenant_id": self.tenant_id,
                "kg": self.kg,
            }
        )


@dataclass
class _ValidityRow:
    """One valid-time interval (ONTA-277 property-graph port).

    MERGE identity is ``(tenant_id, kg, interval_id)`` — ``interval_id`` is the
    RDF interval-node URI, already ``sha1``-keyed on ``(s, p, o)``. Independent
    of ``_AssertionRow``: closing an interval must not delete the assertion.
    """

    tenant_id: str
    kg: str
    interval_id: str
    subject: str = ""
    predicate: str = ""
    object_repr: str = ""
    valid_from: str = ""
    valid_to: str = ""
    superseded_by: str = ""
    status: str = ""
    statement_id: str = ""
    graph_uri: str = ""

    def as_record(self) -> GraphRecord:
        return GraphRecord(
            data={
                "interval_id": self.interval_id,
                "subject": self.subject,
                "predicate": self.predicate,
                "object_repr": self.object_repr,
                "valid_from": self.valid_from,
                "valid_to": self.valid_to,
                "superseded_by": self.superseded_by,
                "status": self.status,
                "statement_id": self.statement_id,
                "graph_uri": self.graph_uri,
                "tenant_id": self.tenant_id,
                "kg": self.kg,
            }
        )


@dataclass
class _CitationRow:
    tenant_id: str
    kg: str
    entity_id: str
    attr: str
    source_url: str | None = None
    provenance: str | None = None
    verified_at: str | None = None
    value_hash: str = ""


@dataclass
class _ClassRow:
    tenant_id: str
    kg: str
    id: str
    name: str
    layer: str = "tenant"


@dataclass
class _PropertyRow:
    tenant_id: str
    kg: str
    id: str
    name: str
    kind: str = "datatype"  # datatype | object
    layer: str = "tenant"


@dataclass
class _AssertionRow:
    tenant_id: str
    kg: str
    id: str
    subject_id: str
    property_id: str
    literal_value: Any = None
    literal_datatype: str | None = None
    object_id: str | None = None  # Entity object
    object_class_id: str | None = None  # Class object (type)
    source_url: str | None = None
    verified_at: str | None = None
    run_id: str | None = None
    confidence: float | None = None
    provenance: str | None = None

    def as_record(self) -> GraphRecord:
        return GraphRecord(
            data={
                "assertion_id": self.id,
                "id": self.id,
                "tenant_id": self.tenant_id,
                "kg": self.kg,
                "subject_id": self.subject_id,
                "property_id": self.property_id,
                "literal_value": self.literal_value,
                "literal_datatype": self.literal_datatype,
                "object_id": self.object_id,
                "object_class_id": self.object_class_id,
                "source_url": self.source_url,
                "verified_at": self.verified_at,
                "run_id": self.run_id,
                "confidence": self.confidence,
                "provenance": self.provenance,
            }
        )


@dataclass
class _OntoTypeRow:
    tenant_id: str
    kg: str
    layer: str
    name: str
    description: str = ""
    description_updated_at: str | None = None
    label_token: str | None = None
    uri: str | None = None
    parent_type: str | None = None
    deprecated_at: str | None = None
    superseded_by: str | None = None

    def as_record(self) -> GraphRecord:
        return GraphRecord(
            data={
                "name": self.name,
                "layer": self.layer,
                "description": self.description,
                "description_updated_at": self.description_updated_at,
                "label_token": self.label_token,
                "uri": self.uri,
                "parent_type": self.parent_type,
                "tenant_id": self.tenant_id,
                "kg": self.kg,
                "deprecated_at": self.deprecated_at,
                "superseded_by": self.superseded_by,
            }
        )


@dataclass
class _OntoAttrRow:
    tenant_id: str
    kg: str
    layer: str
    domain: str
    name: str
    kind: str = "literal"
    datatype: str | None = None
    range_type: str | None = None
    cardinality: str = "1:1"
    description: str = ""
    description_updated_at: str | None = None
    prop_key: str | None = None
    core_slot: bool = False
    text_kind: str | None = None
    deprecated_at: str | None = None
    superseded_by: str | None = None

    def as_record(self) -> GraphRecord:
        return GraphRecord(
            data={
                "name": self.name,
                "domain": self.domain,
                "kind": self.kind,
                "datatype": self.datatype,
                "range_type": self.range_type,
                "cardinality": self.cardinality,
                "description": self.description,
                "description_updated_at": self.description_updated_at,
                "prop_key": self.prop_key,
                "layer": self.layer,
                "tenant_id": self.tenant_id,
                "kg": self.kg,
                "core_slot": self.core_slot,
                "text_kind": self.text_kind,
                "deprecated_at": self.deprecated_at,
                "superseded_by": self.superseded_by,
            }
        )


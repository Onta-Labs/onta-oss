"""ADR 0003 Pass D ontology-extension plans applied during mapping."""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from infona_client.graph.ontology_queries import _safe_id
from infona_client.resolver.csv_helpers import _cell, _snake_case

logger = structlog.stdlib.get_logger("infona.resolver.csv")
from infona_client.resolver.models import (
    ColumnMapping,
    ColumnRole,
    CSVSchemaMapping,
    ExtractedAttribute,
    ExtractedEntity,
    ExtractedRelationship,
    OntologyExtensions,
)

@dataclass
class _ConstantEdgePlan:
    """One dataset constant to materialize: ONE instance of ``target_type``
    labelled ``value``, plus a ``predicate`` edge from every source instance
    (ADR 0003 §3 — the slot is filled by the dataset's single implied party,
    not left empty)."""

    predicate: str
    target_type: str
    value: str


@dataclass
class _PromotionPlan:
    """How one dependent-entity promotion is applied per row: the source
    column's value mints an instance of ``type_name`` carrying the id string
    as ``id_attr``, linked to its owner entity via ``identifies_predicate``,
    plus any dataset-constant edges (e.g. the issuer)."""

    type_name: str
    source_column: str
    #: EntitySpec.name owning the source column in multi-entity mode;
    #: None = the single main entity (legacy single-entity mode).
    owner: str | None
    id_attr: str
    identifies_predicate: str
    constants: list[_ConstantEdgePlan]


@dataclass
class _TypeConstantPlan:
    """Dataset constants attached to a NON-promoted type already in the
    mapping: per-row edges from that type's instances to the one
    materialized constant instance."""

    owner: str | None  # EntitySpec.name (multi) / None (single main entity)
    constants: list[_ConstantEdgePlan]


def _find_source_column(mapping: CSVSchemaMapping, attr: str) -> ColumnMapping | None:
    """Locate the column a promoted attribute came from. The completion pass
    names the schema attribute (e.g. the ``predicate_or_attr``), which may
    differ from the raw header — match on attribute name first, then on the
    (normalized) column name."""
    want = _snake_case(attr)
    for col in mapping.columns:
        if col.attribute_name and _snake_case(col.attribute_name) == want:
            return col
    for col in mapping.columns:
        if _snake_case(col.column_name) == want:
            return col
    return None


def _column_is_entity_key(mapping: CSVSchemaMapping, col: ColumnMapping) -> bool:
    """True when ``col`` is the owner's primary/natural key.

    Used to refuse Pass D promotions of the entity's own key (product_id →
    ProductIdentifier). That promotion mints a second entity with the SAME
    ``safe_id`` as the owner; ``schema_resolver``'s ``entity_uri_map`` is
    keyed by ``entity.id`` alone, so the Identifier overwrites the user-facing
    type and multi-file FK stubs land on a different URI (dogfood S2/S5).
    """
    if col.role == ColumnRole.TYPE_ID:
        return True
    for spec in mapping.entities or []:
        if col.entity is not None and col.entity != spec.name:
            continue
        if spec.id_column and col.column_name == spec.id_column:
            return True
        if spec.id_from and col.column_name in spec.id_from:
            return True
    return False


def _promotion_skip_reason(
    mapping: CSVSchemaMapping, col: ColumnMapping,
) -> str | None:
    """Return a structured skip reason for a dual-world Pass D promotion, or
    None when the promotion is safe to materialize.

    Skipped cases (OSS dogfood S2/S5 dual-world):
    - The source column is already ``role: relationship`` (an FK cell).
      Promoting it into TIdentifier forks Order→Product stubs from the
      dimension-table Product nodes.
    - The source column is the entity's own type_id / EntitySpec key. The
      key already IS the entity's identity; it is not a dependent identifier
      issued by an external party (contrast SKU/MPN/tax_id — those stay
      promotable because they are non-key attributes).
    """
    if col.role == ColumnRole.RELATIONSHIP:
        return "source_column_is_relationship"
    if _column_is_entity_key(mapping, col):
        return "source_column_is_entity_key"
    return None


def _sanitize_ontology_extensions(
    mapping: CSVSchemaMapping,
    extensions: OntologyExtensions | None,
) -> OntologyExtensions | None:
    """Drop dual-world promotions (entity key / relationship FK) from
    Pass D output so they never reach held-for-review UX, ontology
    pre-registration, or apply_mapping materialization.

    True dependent-entity promotions (SKU, MPN, tax_id, …) are kept.
    Ungroundable promotions (unknown source attribute) are also dropped
    here — apply would skip them anyway; dropping early avoids empty
    Identifier types in the ontology.
    """
    if extensions is None or not extensions.types:
        return extensions
    kept: list[TypeExtension] = []
    for t in extensions.types:
        if not t.promoted_from_attribute:
            kept.append(t)
            continue
        col = _find_source_column(mapping, t.promoted_from_attribute)
        if col is None:
            logger.warning(
                "csv_extension_promotion_dropped",
                type=t.type_name,
                attribute=t.promoted_from_attribute,
                reason="source_column_missing",
            )
            continue
        reason = _promotion_skip_reason(mapping, col)
        if reason:
            logger.info(
                "csv_extension_promotion_dropped",
                type=t.type_name,
                attribute=t.promoted_from_attribute,
                column=col.column_name,
                reason=reason,
            )
            continue
        kept.append(t)
    return OntologyExtensions(types=kept)


def _build_extension_plans(
    mapping: CSVSchemaMapping,
) -> tuple[list[_PromotionPlan], list[_TypeConstantPlan]]:
    """Compile ``mapping.ontology_extensions`` into per-row application plans.

    ``held_for_review`` is deliberately NOT filtered here: the confirm gate
    is client-side (`/ingest/csv/schema` flags held items; whatever the
    client posts back to `/ingest/csv/rows` is applied as-is — COG-56 adds
    judge-panel gating). Extensions that cannot be grounded in the mapping
    (unknown source attribute / type) are skipped with a structured warning,
    never an error — they still pre-register in the ontology at ingest.

    Dual-world promotions of the entity's own key or of a relationship-role
    FK column are also skipped (see :func:`_promotion_skip_reason`) — even
    if a client posts them — so multi-file natural-key joins cannot be
    silently forked onto ``*Identifier`` types.
    """
    ext = mapping.ontology_extensions
    if ext is None or not ext.types:
        return [], []
    multi = bool(mapping.entities)
    specs_by_name = {s.name: s for s in (mapping.entities or [])}

    promotions: list[_PromotionPlan] = []
    type_constants: list[_TypeConstantPlan] = []
    for t in ext.types:
        constants = [
            _ConstantEdgePlan(
                predicate=_snake_case(s.name),
                target_type=s.target_type or _pascal_case(s.name),
                value=s.dataset_constant.value,
            )
            for s in t.core_slots
            if s.kind == "relationship" and s.dataset_constant and s.dataset_constant.value
        ]
        if t.promoted_from_attribute:
            col = _find_source_column(mapping, t.promoted_from_attribute)
            if col is None:
                logger.warning(
                    "csv_extension_source_column_missing",
                    type=t.type_name, attribute=t.promoted_from_attribute,
                )
                continue
            skip = _promotion_skip_reason(mapping, col)
            if skip:
                logger.info(
                    "csv_extension_promotion_skipped",
                    type=t.type_name,
                    attribute=t.promoted_from_attribute,
                    column=col.column_name,
                    reason=skip,
                )
                continue
            owner = col.entity if (multi and col.entity in specs_by_name) else None
            if multi and owner is None:
                logger.warning(
                    "csv_extension_unowned_source_column",
                    type=t.type_name, column=col.column_name,
                )
            owner_type = (
                specs_by_name[owner].type_name if owner else mapping.entity_type
            )
            id_attr = next(
                (_snake_case(s.name) for s in t.core_slots if s.kind == "attribute"),
                _snake_case(t.promoted_from_attribute),
            )
            identifies = next(
                (
                    _snake_case(s.name) for s in t.core_slots
                    if s.kind == "relationship"
                    and s.target_type == owner_type
                    and not s.dataset_constant
                ),
                "identifies",
            )
            promotions.append(_PromotionPlan(
                type_name=t.type_name,
                source_column=col.column_name,
                owner=owner,
                id_attr=id_attr,
                identifies_predicate=identifies,
                constants=constants,
            ))
        elif constants:
            if multi:
                owners: list[str | None] = [
                    s.name for s in (mapping.entities or [])
                    if s.type_name == t.type_name
                ]
            else:
                owners = [None] if t.type_name == mapping.entity_type else []
            if not owners:
                # A type with no instance source (e.g. a zero-instance issuer
                # type the completion invented): nothing to materialize here —
                # it still exists in the ontology via ingest pre-registration.
                continue
            for owner in owners:
                type_constants.append(_TypeConstantPlan(owner=owner, constants=constants))
    return promotions, type_constants


class _ExtensionApplier:
    """Materializes ontology extensions while ``apply_mapping`` walks the
    rows. Both mapping paths feed it one call per row with the row's resolved
    owner keys; it accumulates its own (deduplicated) entities and edges,
    merged into the result afterwards.

    Determinism mirrors the rest of ``apply_mapping``: instance ids derive
    from cell values only, edges dedup on (source, predicate, target), and a
    dataset constant becomes exactly ONE instance no matter how many rows
    reference it.
    """

    def __init__(self, mapping: CSVSchemaMapping):
        self._promotions, self._type_constants = _build_extension_plans(mapping)
        self._entities: dict[tuple[str, str], ExtractedEntity] = {}
        self._relationships: list[ExtractedRelationship] = []
        self._seen_edges: set[tuple[str, str, str]] = set()

    @property
    def active(self) -> bool:
        return bool(self._promotions or self._type_constants)

    @property
    def entities(self) -> list[ExtractedEntity]:
        return list(self._entities.values())

    @property
    def relationships(self) -> list[ExtractedRelationship]:
        return list(self._relationships)

    def process_row(self, row: dict, owner_keys: dict[str | None, str]) -> None:
        """Apply every plan to one row. ``owner_keys`` maps an owner handle
        (EntitySpec.name, or None for the single main entity) to the entity
        key minted for this row — absent when the owner was skipped, in which
        case the promoted instance is still minted but carries no
        identifies edge (nothing to point at)."""
        for plan in self._promotions:
            value = _cell(row, plan.source_column)
            if not value:
                continue
            pid = _safe_id(value)
            self._ensure_entity(
                plan.type_name, pid,
                ExtractedAttribute(name=plan.id_attr, value=value, datatype="string"),
            )
            owner_key = owner_keys.get(plan.owner)
            if owner_key:
                self._edge(pid, plan.identifies_predicate, owner_key)
            self._constant_edges(pid, plan.constants)
        for type_plan in self._type_constants:
            owner_key = owner_keys.get(type_plan.owner)
            if owner_key:
                self._constant_edges(owner_key, type_plan.constants)

    def _constant_edges(self, source_id: str, constants: list[_ConstantEdgePlan]) -> None:
        for c in constants:
            cid = _safe_id(c.value)
            self._ensure_entity(
                c.target_type, cid,
                ExtractedAttribute(name="name", value=c.value, datatype="string"),
            )
            self._edge(source_id, c.predicate, cid)

    def _ensure_entity(self, type_name: str, key: str, attr: ExtractedAttribute) -> None:
        ent = self._entities.get((type_name, key))
        if ent is None:
            self._entities[(type_name, key)] = ExtractedEntity(
                type_name=type_name, id=key, attributes=[attr],
            )
        elif not any(a.name == attr.name and a.value == attr.value for a in ent.attributes):
            ent.attributes.append(attr)

    def _edge(self, source_id: str, predicate: str, target_id: str) -> None:
        edge = (source_id, predicate, target_id)
        if edge in self._seen_edges:
            return
        self._seen_edges.add(edge)
        self._relationships.append(ExtractedRelationship(
            source_id=source_id, predicate=predicate, target_id=target_id,
        ))



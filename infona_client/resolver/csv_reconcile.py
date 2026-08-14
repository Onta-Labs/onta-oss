"""Reconcile an inferred CSV mapping against the existing ontology."""

from __future__ import annotations

import structlog

from infona_client.resolver.attribute_resolver import (
    AttributeSchema,
    _find_existing_attr,
    _normalize_attr_name,
    is_primitive_datatype,
)
from infona_client.resolver.csv_helpers import _snake_case
from infona_client.resolver.models import ColumnRole, CSVSchemaMapping

logger = structlog.stdlib.get_logger("infona.resolver.csv")

def format_existing_ontology_for_prompt(
    existing_types: dict[str, str],
    existing_attrs: dict[str, dict[str, AttributeSchema]] | None = None,
    *,
    max_props_per_type: int = 40,
) -> str:
    """Render types + their properties for CSV-inference LLM prompts.

    Historically the prompt only listed type *names*, so the model could not
    reuse ``Drug.manufacturer`` and invented ``manufactured_by`` instead.
    Literals and type-ranged relationships are distinguished by range:
    primitives as bare names, relationships as ``pred→Target``.
    """
    if not existing_types:
        return "(none)"
    existing_attrs = existing_attrs or {}
    lines: list[str] = []
    for type_name in existing_types:
        props = existing_attrs.get(type_name) or {}
        if not props:
            lines.append(f"- {type_name}")
            continue
        literals: list[str] = []
        relations: list[str] = []
        for prop_name, schema in list(props.items())[:max_props_per_type]:
            dt = schema.datatype or "string"
            if is_primitive_datatype(dt):
                literals.append(f"{prop_name}:{dt}")
            else:
                relations.append(f"{prop_name}→{dt}")
        parts: list[str] = []
        if literals:
            parts.append("attrs[" + ", ".join(literals) + "]")
        if relations:
            parts.append("rels[" + ", ".join(relations) + "]")
        extra = len(props) - max_props_per_type
        suffix = f" (+{extra} more)" if extra > 0 else ""
        lines.append(f"- {type_name}: " + "; ".join(parts) + suffix)
    return "\n".join(lines) if lines else "(none)"


def _owner_type_name(
    mapping: CSVSchemaMapping,
    col: ColumnMapping,
) -> str | None:
    """Resolve the ontology type a column is attached to.

    Multi-entity mappings carry owner handles on ``col.entity`` that map to
    ``EntitySpec.type_name``; single-entity mappings use ``mapping.entity_type``.
    """
    if mapping.entities:
        owner = col.entity
        if not owner:
            return mapping.entity_type or None
        for spec in mapping.entities:
            if spec.name == owner:
                return spec.type_name
        # Owner handle may already be the type name.
        for spec in mapping.entities:
            if spec.type_name == owner:
                return spec.type_name
        return None
    return mapping.entity_type or None


def reconcile_mapping_to_existing(
    mapping: CSVSchemaMapping,
    existing_types: dict[str, str] | None,
    existing_attrs: dict[str, dict[str, AttributeSchema]] | None,
) -> CSVSchemaMapping:
    """Deterministic post-pass: collapse invented property names onto existing schema.

    LLM inference is free to propose entity-first promotions
    (``manufacturer`` → relationship ``manufactured_by`` → Organization).
    When the chosen type *already* declares a matching property, that modeling
    choice wins — reuse the existing name and kind (literal vs relationship)
    so a one-row CSV cannot fork the ontology (Oliver DP regression: Drug kept
    string ``manufacturer`` on 21 products and added ``manufacturedby`` on the
    22nd).

    Pure / side-effect free: returns a new mapping when any column changes,
    otherwise the input mapping object.
    """
    if not existing_attrs:
        # Still re-snake attribute names so camelCase verbs from the LLM do
        # not land as opaque compacted leaves even on a greenfield graph.
        return _resnake_mapping_names(mapping)

    changed = False
    new_columns: list[ColumnMapping] = []
    for col in mapping.columns:
        type_name = _owner_type_name(mapping, col)
        type_props = existing_attrs.get(type_name or "") if type_name else None
        proposed = col.attribute_name or col.column_name
        snaked = _snake_case(proposed) if proposed else proposed

        matched = None
        if type_props:
            # Prefer the column's native name (often the original seed attr)
            # then the LLM's predicate_or_attr.
            for candidate in (col.column_name, proposed, snaked):
                if not candidate:
                    continue
                matched = _find_existing_attr(candidate, type_props)
                if matched is not None:
                    break

        if matched is None:
            if snaked and snaked != col.attribute_name:
                new_columns.append(col.model_copy(update={"attribute_name": snaked}))
                changed = True
            else:
                new_columns.append(col)
            continue

        updates: dict = {"attribute_name": matched.name}
        # Never rewrite TYPE_ID role — the key column stays the key even if
        # its name fuzzy-matches a type-ranged property.
        if col.role == ColumnRole.TYPE_ID:
            if snaked and snaked != col.attribute_name and matched.name == snaked:
                updates = {"attribute_name": matched.name}
            elif col.attribute_name != matched.name:
                # Prefer the existing property's canonical name for the key attr
                # emission, but keep role=TYPE_ID.
                updates = {"attribute_name": matched.name}
            else:
                new_columns.append(col)
                continue
        elif is_primitive_datatype(matched.datatype):
            # Existing literal wins over an invented relationship.
            if col.role == ColumnRole.RELATIONSHIP:
                updates["role"] = ColumnRole.ATTRIBUTE
                updates["target_type"] = None
                updates["datatype"] = matched.datatype or col.datatype or "string"
                logger.info(
                    "csv_reconcile_rel_to_attr",
                    column=col.column_name,
                    type=type_name,
                    proposed=proposed,
                    reused=matched.name,
                )
            else:
                updates["datatype"] = matched.datatype or col.datatype or "string"
                if col.attribute_name != matched.name:
                    logger.info(
                        "csv_reconcile_attr_name",
                        column=col.column_name,
                        type=type_name,
                        proposed=proposed,
                        reused=matched.name,
                    )
        else:
            # Existing type-ranged property wins over an invented literal.
            # Only flip ATTRIBUTE ↔ RELATIONSHIP (not TYPE_ID — handled above).
            if col.role in (ColumnRole.ATTRIBUTE, ColumnRole.RELATIONSHIP):
                updates["role"] = ColumnRole.RELATIONSHIP
                updates["target_type"] = matched.datatype
                updates["datatype"] = "string"
                if (
                    col.role != ColumnRole.RELATIONSHIP
                    or col.attribute_name != matched.name
                    or col.target_type != matched.datatype
                ):
                    logger.info(
                        "csv_reconcile_attr_to_rel",
                        column=col.column_name,
                        type=type_name,
                        proposed=proposed,
                        reused=matched.name,
                        target=matched.datatype,
                    )

        if any(getattr(col, k, None) != v for k, v in updates.items()):
            new_columns.append(col.model_copy(update=updates))
            changed = True
        else:
            new_columns.append(col)

    new_mapping = mapping
    if changed:
        new_mapping = mapping.model_copy(update={"columns": new_columns})

    # Drop Pass D promotions whose source attribute already exists as a
    # property on some known type — promoting "manufacturer" to Manufacturer
    # when Drug.manufacturer already is a literal forks the ontology the same
    # way a parallel relationship does.
    if new_mapping.ontology_extensions is not None:
        cleaned = _drop_redundant_promotions(
            new_mapping.ontology_extensions, existing_attrs,
        )
        if cleaned is not new_mapping.ontology_extensions:
            new_mapping = new_mapping.model_copy(update={"ontology_extensions": cleaned})
            changed = True

    return new_mapping if changed else mapping


def _resnake_mapping_names(mapping: CSVSchemaMapping) -> CSVSchemaMapping:
    """Re-apply :func:`_snake_case` to every column's attribute_name (greenfield)."""
    new_columns: list[ColumnMapping] = []
    changed = False
    for col in mapping.columns:
        if not col.attribute_name:
            new_columns.append(col)
            continue
        snaked = _snake_case(col.attribute_name)
        if snaked != col.attribute_name:
            new_columns.append(col.model_copy(update={"attribute_name": snaked}))
            changed = True
        else:
            new_columns.append(col)
    if not changed:
        return mapping
    return mapping.model_copy(update={"columns": new_columns})


def _drop_redundant_promotions(
    extensions: OntologyExtensions,
    existing_attrs: dict[str, dict[str, AttributeSchema]],
) -> OntologyExtensions:
    """Remove type promotions whose source attr already exists on a known type.

    Exact/compact match only (no fuzzy) so a Pass D promotion of
    ``created_by`` is not dropped just because ``created_at`` exists
    elsewhere. A hit on *any* type blocks the promotion (Oliver case:
    Drug.manufacturer blocks promoting manufacturer → Manufacturer).
    """
    if not extensions.types:
        return extensions

    def _exact_or_compact_hit(src: str) -> bool:
        want = _normalize_attr_name(src)
        want_c = want.replace("_", "")
        for props in existing_attrs.values():
            for name in props:
                n = _normalize_attr_name(name)
                if n == want or n.replace("_", "") == want_c:
                    return True
        return False

    kept: list[TypeExtension] = []
    dropped = 0
    for t in extensions.types:
        src = t.promoted_from_attribute
        if src and _exact_or_compact_hit(src):
            logger.info(
                "csv_reconcile_drop_promotion",
                type=t.type_name,
                promoted_from=src,
            )
            dropped += 1
            continue
        kept.append(t)
    if dropped == 0:
        return extensions
    return extensions.model_copy(update={"types": kept})

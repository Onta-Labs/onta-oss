"""Pure structural ontology diff + invert (ONTA-406)."""

from __future__ import annotations

from typing import Sequence

from infona_client.graph.ontology_commit import OntologyShape, load_ontology_shape
from infona_client.graph.ontology_snapshots_models import _LITERAL_DATATYPES
from infona_client.models.ontology import ChangeKind, ChangeRecord


def _is_literal_datatype(dt: str) -> bool:
    return (dt or "string") in _LITERAL_DATATYPES


def _add_slot_record(type_name: str, slot: str, datatype: str) -> ChangeRecord:
    if _is_literal_datatype(datatype):
        return ChangeRecord(
            kind=ChangeKind.ADD_ATTRIBUTE,
            type_name=type_name,
            slot_name=slot,
            new_value=datatype,
        )
    return ChangeRecord(
        kind=ChangeKind.ADD_RELATIONSHIP,
        type_name=type_name,
        slot_name=slot,
        new_value=datatype,
    )


def _remove_slot_record(type_name: str, slot: str, datatype: str) -> ChangeRecord:
    if _is_literal_datatype(datatype):
        return ChangeRecord(
            kind=ChangeKind.REMOVE_ATTRIBUTE,
            type_name=type_name,
            slot_name=slot,
            old_value=datatype,
        )
    return ChangeRecord(
        kind=ChangeKind.REMOVE_RELATIONSHIP,
        type_name=type_name,
        slot_name=slot,
        old_value=datatype,
    )


def diff_shapes(a: OntologyShape, b: OntologyShape) -> list[ChangeRecord]:
    """Structural diff ``a → b`` as a list of :class:`ChangeRecord`.

    Companions under ``attr_meta/`` are never present on either shape.
    Order is deterministic (sorted keys) so the same pair always yields the
    same list; invertibility is tested as a multiset.
    """
    records: list[ChangeRecord] = []

    types_a, types_b = set(a.types), set(b.types)
    for t in sorted(types_b - types_a):
        records.append(ChangeRecord(kind=ChangeKind.ADD_TYPE, type_name=t))
    for t in sorted(types_a - types_b):
        records.append(ChangeRecord(kind=ChangeKind.REMOVE_TYPE, type_name=t))

    # Type comments (shared types only — add/remove type covers birth/death).
    for t in sorted(types_a & types_b):
        old_c = a.types.get(t) or ""
        new_c = b.types.get(t) or ""
        if old_c != new_c:
            records.append(
                ChangeRecord(
                    kind=ChangeKind.CHANGE_COMMENT,
                    type_name=t,
                    old_value=old_c or None,
                    new_value=new_c or None,
                )
            )

    # Attributes / relationships.
    def _slots(shape: OntologyShape, t: str) -> dict[str, str]:
        return dict(shape.attrs.get(t) or {})

    all_types = types_a | types_b
    for t in sorted(all_types):
        sa, sb = _slots(a, t), _slots(b, t)
        for slot in sorted(set(sb) - set(sa)):
            records.append(_add_slot_record(t, slot, sb[slot]))
        for slot in sorted(set(sa) - set(sb)):
            records.append(_remove_slot_record(t, slot, sa[slot]))
        for slot in sorted(set(sa) & set(sb)):
            if sa[slot] != sb[slot]:
                records.append(
                    ChangeRecord(
                        kind=ChangeKind.CHANGE_RANGE,
                        type_name=t,
                        slot_name=slot,
                        old_value=sa[slot],
                        new_value=sb[slot],
                    )
                )

    # Attribute comments.
    def _attr_comments(shape: OntologyShape) -> dict[tuple[str, str], str]:
        out: dict[tuple[str, str], str] = {}
        for t, inner in (shape.attr_comments or {}).items():
            for attr, text in (inner or {}).items():
                if text:
                    out[(t, attr)] = text
        return out

    ac_a, ac_b = _attr_comments(a), _attr_comments(b)
    for key in sorted(set(ac_a) | set(ac_b)):
        old_v = ac_a.get(key)
        new_v = ac_b.get(key)
        if old_v != new_v:
            t, slot = key
            records.append(
                ChangeRecord(
                    kind=ChangeKind.CHANGE_COMMENT,
                    type_name=t,
                    slot_name=slot,
                    old_value=old_v,
                    new_value=new_v,
                )
            )

    # Subclass edges.
    for child in sorted(set(b.parent_of) - set(a.parent_of)):
        records.append(
            ChangeRecord(
                kind=ChangeKind.ADD_SUBCLASS,
                type_name=child,
                parent_type=b.parent_of[child],
            )
        )
    for child in sorted(set(a.parent_of) - set(b.parent_of)):
        records.append(
            ChangeRecord(
                kind=ChangeKind.REMOVE_SUBCLASS,
                type_name=child,
                parent_type=a.parent_of[child],
            )
        )
    for child in sorted(set(a.parent_of) & set(b.parent_of)):
        if a.parent_of[child] != b.parent_of[child]:
            records.append(
                ChangeRecord(
                    kind=ChangeKind.REMOVE_SUBCLASS,
                    type_name=child,
                    parent_type=a.parent_of[child],
                )
            )
            records.append(
                ChangeRecord(
                    kind=ChangeKind.ADD_SUBCLASS,
                    type_name=child,
                    parent_type=b.parent_of[child],
                )
            )

    # Core-slot markers.
    cs_a = set(a.core_slots)
    cs_b = set(b.core_slots)
    for t, slot in sorted(cs_b - cs_a):
        records.append(
            ChangeRecord(
                kind=ChangeKind.CHANGE_CORE_SLOT,
                type_name=t,
                slot_name=slot,
                old_value="false",
                new_value="true",
            )
        )
    for t, slot in sorted(cs_a - cs_b):
        records.append(
            ChangeRecord(
                kind=ChangeKind.CHANGE_CORE_SLOT,
                type_name=t,
                slot_name=slot,
                old_value="true",
                new_value="false",
            )
        )

    # Text-kind markers.
    tk_a, tk_b = a.text_kinds or {}, b.text_kinds or {}
    for key in sorted(set(tk_a) | set(tk_b)):
        old_v = tk_a.get(key)
        new_v = tk_b.get(key)
        if old_v != new_v:
            t, slot = key
            records.append(
                ChangeRecord(
                    kind=ChangeKind.CHANGE_TEXT_KIND,
                    type_name=t,
                    slot_name=slot,
                    old_value=old_v,
                    new_value=new_v,
                )
            )

    # Aliases (RENAME_WITH_ALIAS). Added edges set new_value only; removed
    # edges set old_value only — invert swaps the two for symmetry.
    am_a, am_b = a.alias_map or {}, b.alias_map or {}
    for old in sorted(set(am_b) - set(am_a)):
        records.append(
            ChangeRecord(
                kind=ChangeKind.RENAME_WITH_ALIAS,
                from_name=old.rsplit("/", 1)[-1],
                to_name=am_b[old].rsplit("/", 1)[-1],
                old_value=None,
                new_value=f"{old}->{am_b[old]}",
            )
        )
    for old in sorted(set(am_a) - set(am_b)):
        records.append(
            ChangeRecord(
                kind=ChangeKind.RENAME_WITH_ALIAS,
                from_name=old.rsplit("/", 1)[-1],
                to_name=am_a[old].rsplit("/", 1)[-1],
                old_value=f"{old}->{am_a[old]}",
                new_value=None,
            )
        )
    for old in sorted(set(am_a) & set(am_b)):
        if am_a[old] != am_b[old]:
            records.append(
                ChangeRecord(
                    kind=ChangeKind.RENAME_WITH_ALIAS,
                    from_name=old.rsplit("/", 1)[-1],
                    to_name=am_b[old].rsplit("/", 1)[-1],
                    old_value=f"{old}->{am_a[old]}",
                    new_value=f"{old}->{am_b[old]}",
                )
            )

    # Deprecations (ONTA-404). Newly marked types/slots → DEPRECATE; a change
    # of superseded_by on an already-deprecated subject also emits DEPRECATE.
    # Un-deprecate (marker removed) is not a ChangeKind today — fail-open as
    # absent rather than inventing a reverse op (publish gate only cares that
    # a new deprecation is DEPRECATING, not that un-deprecate is invisible).
    dt_a, dt_b = a.deprecated_types or {}, b.deprecated_types or {}
    for t in sorted(set(dt_b) - set(dt_a)):
        records.append(
            ChangeRecord(
                kind=ChangeKind.DEPRECATE,
                type_name=t,
                superseded_by=dt_b[t] or None,
                new_value=dt_b[t] or None,
            )
        )
    for t in sorted(set(dt_a) & set(dt_b)):
        if (dt_a[t] or "") != (dt_b[t] or ""):
            records.append(
                ChangeRecord(
                    kind=ChangeKind.DEPRECATE,
                    type_name=t,
                    superseded_by=dt_b[t] or None,
                    old_value=dt_a[t] or None,
                    new_value=dt_b[t] or None,
                )
            )
    ds_a, ds_b = a.deprecated_slots or {}, b.deprecated_slots or {}
    for key in sorted(set(ds_b) - set(ds_a)):
        t, slot = key
        records.append(
            ChangeRecord(
                kind=ChangeKind.DEPRECATE,
                type_name=t,
                slot_name=slot,
                superseded_by=ds_b[key] or None,
                new_value=ds_b[key] or None,
            )
        )
    for key in sorted(set(ds_a) & set(ds_b)):
        if (ds_a[key] or "") != (ds_b[key] or ""):
            t, slot = key
            records.append(
                ChangeRecord(
                    kind=ChangeKind.DEPRECATE,
                    type_name=t,
                    slot_name=slot,
                    superseded_by=ds_b[key] or None,
                    old_value=ds_a[key] or None,
                    new_value=ds_b[key] or None,
                )
            )

    return records


def invert_change(record: ChangeRecord) -> ChangeRecord:
    """Return the change that undoes ``record`` (ONTA-406 symmetry)."""
    kind = record.kind
    swap_kinds = {
        ChangeKind.ADD_TYPE: ChangeKind.REMOVE_TYPE,
        ChangeKind.REMOVE_TYPE: ChangeKind.ADD_TYPE,
        ChangeKind.ADD_ATTRIBUTE: ChangeKind.REMOVE_ATTRIBUTE,
        ChangeKind.REMOVE_ATTRIBUTE: ChangeKind.ADD_ATTRIBUTE,
        ChangeKind.ADD_RELATIONSHIP: ChangeKind.REMOVE_RELATIONSHIP,
        ChangeKind.REMOVE_RELATIONSHIP: ChangeKind.ADD_RELATIONSHIP,
        ChangeKind.ADD_SUBCLASS: ChangeKind.REMOVE_SUBCLASS,
        ChangeKind.REMOVE_SUBCLASS: ChangeKind.ADD_SUBCLASS,
    }
    if kind in swap_kinds:
        # For add/remove slot records, old_value/new_value also swap so a
        # REMOVE that carried old_value becomes an ADD with new_value.
        return record.model_copy(
            update={
                "kind": swap_kinds[kind],
                "old_value": record.new_value,
                "new_value": record.old_value,
            }
        )
    # Annotative / value changes (comment, range, core-slot, text-kind,
    # rename-with-alias add/remove encoded via old/new_value): same kind,
    # swap old/new. from_name/to_name stay put — they identify the edge,
    # not the direction of the mutation.
    return record.model_copy(
        update={
            "old_value": record.new_value,
            "new_value": record.old_value,
        }
    )


def invert_diff(records: Sequence[ChangeRecord]) -> list[ChangeRecord]:
    """Invert a whole diff (order reversed so a→b undoes in reverse apply order)."""
    return [invert_change(r) for r in reversed(records)]


def _record_key(r: ChangeRecord) -> tuple:
    """Canonical key for multiset equality (ignores list order; None-safe)."""
    d = r.model_dump()
    # Sort by field name; coerce None so heterogeneous values still order.
    return tuple(
        (k, "" if v is None else v)
        for k, v in sorted(d.items(), key=lambda kv: kv[0])
    )


def diffs_symmetric(a: OntologyShape, b: OntologyShape) -> bool:
    """True iff ``invert(diff(a,b))`` multiset-equals ``diff(b,a)``."""
    ab = diff_shapes(a, b)
    ba = diff_shapes(b, a)
    inv = invert_diff(ab)
    return sorted(_record_key(r) for r in inv) == sorted(_record_key(r) for r in ba)


async def diff_graphs(neptune, graph_a: str, graph_b: str) -> list[ChangeRecord]:
    """Load two ontology graphs and return ``diff_shapes(a, b)``."""
    a = await load_ontology_shape(neptune, graph_a)
    b = await load_ontology_shape(neptune, graph_b)
    return diff_shapes(a, b)

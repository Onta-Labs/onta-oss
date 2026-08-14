"""Explore-style coverage, grep, distinct, filter, and 1-hop reads.

Mirrors the smoke Cypher templates in :mod:`schema_bootstrap` so Explorer
and grep tests stay hermetic. Grep drops internal property keys before
the limit is counted so a page is never silently shortened.
"""

from __future__ import annotations

from typing import Any

from infona_client.graph.memory_store_rows import _EntityRow
from infona_client.graph.store import GraphRecord


class MemoryExploreMixin:
    """Coverage, grep, distinct values, prop-eq filter, 1-hop out."""

    def _entity_type_attr_coverage(
        self, tenant_id: str, kg: str, primary_type: str
    ) -> list[GraphRecord]:
        """Property-key coverage for entities of ``primary_type`` (P-A1a)."""
        matched = self._entity_ids_via_instance_of(tenant_id, kg, [primary_type])
        counts: dict[str, int] = {}
        for eid in matched:
            row = self._entities.get((tenant_id, kg, eid))
            if row is None:
                continue
            # Mirror Neo4j keys(e): system fields + props map keys with values.
            keys: set[str] = set()
            for k in ("id", "tenant_id", "kg", "primary_type", "name", "source"):
                if getattr(row, k, None) is not None:
                    keys.add(k)
            for k, v in row.props.items():
                if v is not None:
                    keys.add(str(k))
            for k in keys:
                counts[k] = counts.get(k, 0) + 1
        return [
            GraphRecord(data={"attr": attr, "n": n})
            for attr, n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))
        ]

    def _entity_type_rel_coverage(
        self, tenant_id: str, kg: str, primary_type: str
    ) -> list[GraphRecord]:
        """Outgoing relationship coverage for entities of ``primary_type`` (P-A1a)."""
        matched = self._entity_ids_via_instance_of(tenant_id, kg, [primary_type])
        entity_sets: dict[str, set[str]] = {}
        edge_totals: dict[str, int] = {}
        targets: dict[str, str | None] = {}
        for r in self._rels.values():
            if r.tenant_id != tenant_id or r.kg != kg:
                continue
            if r.start_id not in matched:
                continue
            attr = r.attr or r.rel_type
            if not attr:
                continue
            entity_sets.setdefault(attr, set()).add(r.start_id)
            edge_totals[attr] = edge_totals.get(attr, 0) + 1
            if attr not in targets:
                other = self._entities.get((tenant_id, kg, r.end_id))
                targets[attr] = other.primary_type if other else None
        out: list[GraphRecord] = []
        for attr in sorted(
            entity_sets.keys(),
            key=lambda a: (-len(entity_sets[a]), a),
        ):
            out.append(
                GraphRecord(
                    data={
                        "attr": attr,
                        "n": len(entity_sets[attr]),
                        "rel_total": edge_totals.get(attr, 0),
                        "target_type": targets.get(attr),
                    }
                )
            )
        return out

    def _entity_prop_value(self, row: _EntityRow, prop_key: str) -> Any:
        """Read a property the way Neo4j ``e[$prop_key]`` would for Entity."""
        if prop_key in ("id", "tenant_id", "kg", "primary_type", "name", "source"):
            return getattr(row, prop_key, None)
        return row.props.get(prop_key)

    def _entity_literal_grep(
        self,
        tenant_id: str,
        kg: str,
        needle: str,
        *,
        case_sensitive: bool,
        type_name: str | None,
        predicate_leaf: str | None,
        limit: int,
    ) -> list[GraphRecord]:
        """Substring scan over Entity name + props (grep dual-backend)."""
        from infona_client.graph.facts import (
            RESERVED_ENTITY_PROPERTY_KEYS,
            is_internal_property_key,
        )

        skip = set(RESERVED_ENTITY_PROPERTY_KEYS) | {
            "labels",
            "props",
            "created_at",
            "updated_at",
            "elementId",
        }
        # ``name`` is greppable (display label) even though reserved for writes.
        skip.discard("name")

        needle_cmp = needle if case_sensitive else needle.lower()
        type_filter = (type_name or "").strip() or None
        pred_filter = (predicate_leaf or "").strip() or None
        if type_filter:
            matched_ids = set(
                self._entity_ids_via_instance_of(tenant_id, kg, [type_filter])
            )
        else:
            matched_ids = None

        out: list[GraphRecord] = []
        for (t, k, eid), row in sorted(self._entities.items(), key=lambda x: x[0][2]):
            if t != tenant_id or k != kg:
                continue
            if matched_ids is not None and eid not in matched_ids:
                # Also allow primary_type denorm match when INSTANCE_OF missing.
                if row.primary_type != type_filter:
                    continue
            candidates: list[tuple[str, Any]] = []
            if row.name is not None:
                candidates.append(("name", row.name))
            for pk, pv in sorted(row.props.items()):
                if pk in skip:
                    continue
                # Internal/housekeeping keys (ER block index, ingest markers)
                # are dropped BEFORE the limit is counted, mirroring the
                # exclusion the Cypher template pushes into the scan — a page
                # must never be silently shortened by rows nobody may see.
                if is_internal_property_key(pk):
                    continue
                candidates.append((pk, pv))
            for attr, val in candidates:
                if pred_filter is not None and attr != pred_filter:
                    continue
                if val is None:
                    continue
                # Multi-value lists: any element may match (scan each).
                values: list[Any]
                if isinstance(val, (list, tuple)):
                    values = list(val)
                else:
                    values = [val]
                for item in values:
                    text = str(item)
                    hay = text if case_sensitive else text.lower()
                    if needle_cmp not in hay:
                        continue
                    out.append(
                        GraphRecord(
                            data={
                                "entity_uri": row.id,
                                "label": row.name,
                                "type": row.primary_type,
                                "attr": attr,
                                "value": text,
                            }
                        )
                    )
                    if len(out) >= limit:
                        return out
                    break  # one match per (entity, attr) is enough
        return out

    def _entity_type_prop_distinct(
        self,
        tenant_id: str,
        kg: str,
        primary_type: str,
        prop_key: str,
        limit: int,
    ) -> list[GraphRecord]:
        """Distinct stringified property values for entities of ``primary_type``."""
        matched = self._entity_ids_via_instance_of(tenant_id, kg, [primary_type])
        seen: set[str] = set()
        values: list[str] = []
        for eid in matched:
            row = self._entities.get((tenant_id, kg, eid))
            if row is None:
                continue
            raw = self._entity_prop_value(row, prop_key)
            if raw is None:
                continue
            items: list[Any]
            if isinstance(raw, (list, tuple)):
                items = list(raw)
            else:
                items = [raw]
            for item in items:
                if item is None:
                    continue
                text = str(item).strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                values.append(text)
        values.sort()
        if limit >= 0:
            values = values[:limit]
        return [GraphRecord(data={"value": v}) for v in values]

    def _entity_type_rel_target_distinct(
        self,
        tenant_id: str,
        kg: str,
        primary_type: str,
        rel_attr: str,
        limit: int,
    ) -> list[GraphRecord]:
        """Distinct related-entity names for one outgoing rel leaf."""
        matched = self._entity_ids_via_instance_of(tenant_id, kg, [primary_type])
        by_value: dict[str, str | None] = {}
        for r in self._rels.values():
            if r.tenant_id != tenant_id or r.kg != kg:
                continue
            if r.start_id not in matched:
                continue
            attr = r.attr or r.rel_type
            if attr != rel_attr and r.rel_type != rel_attr:
                continue
            other = self._entities.get((tenant_id, kg, r.end_id))
            display = None
            target_type = None
            if other is not None:
                display = other.name or other.id
                target_type = other.primary_type
            else:
                display = r.end_id
            if not display:
                continue
            key = str(display)
            if key not in by_value:
                by_value[key] = target_type
        ordered = sorted(by_value.items(), key=lambda x: x[0])
        if limit >= 0:
            ordered = ordered[:limit]
        return [
            GraphRecord(data={"value": val, "target_type": tt})
            for val, tt in ordered
        ]

    def _entity_filter_prop_eq(
        self,
        tenant_id: str,
        kg: str,
        primary_type: str,
        prop_key: str,
        prop_value: Any,
        limit: int,
    ) -> list[GraphRecord]:
        rows: list[_EntityRow] = [
            r
            for (t, k, _), r in sorted(self._entities.items(), key=lambda x: x[0][2])
            if t == tenant_id and k == kg and r.primary_type == primary_type
        ]
        out: list[GraphRecord] = []
        for r in rows:
            if self._entity_prop_value(r, prop_key) == prop_value:
                out.append(
                    GraphRecord(
                        data={
                            "id": r.id,
                            "name": r.name,
                            "primary_type": r.primary_type,
                        }
                    )
                )
            if limit >= 0 and len(out) >= limit:
                break
        return out

    def _entity_1hop_out(
        self,
        tenant_id: str,
        kg: str,
        from_type: str,
        to_type: str | None,
        rel_attr: str | None,
        limit: int,
    ) -> list[GraphRecord]:
        out: list[GraphRecord] = []
        rels = sorted(
            self._rels.values(),
            key=lambda r: (r.start_id, r.end_id, r.rel_type),
        )
        for r in rels:
            if r.tenant_id != tenant_id or r.kg != kg:
                continue
            a = self._entities.get((tenant_id, kg, r.start_id))
            b = self._entities.get((tenant_id, kg, r.end_id))
            if a is None or b is None:
                continue
            if a.primary_type != from_type:
                continue
            if to_type is not None and b.primary_type != to_type:
                continue
            if rel_attr is not None and r.attr != rel_attr and r.rel_type != rel_attr:
                continue
            out.append(
                GraphRecord(
                    data={
                        "from_id": a.id,
                        "from_name": a.name,
                        "from_type": a.primary_type,
                        "to_id": b.id,
                        "to_name": b.name,
                        "to_type": b.primary_type,
                        "rel_type": r.rel_type,
                        "attr": r.attr or r.rel_type,
                    }
                )
            )
            if limit >= 0 and len(out) >= limit:
                break
        return out

"""ADR 0013 Class / Property / Assertion + E8 structural QC scans.

Assertion is the source of truth; ``INSTANCE_OF`` is a derived cache.
QC scanners flag dual-write skew (cache without a backing type Assertion).
"""

from __future__ import annotations

from typing import Any

from infona_client.graph.memory_store_rows import _AssertionRow, _ClassRow, _PropertyRow
from infona_client.graph.store import GraphRecord


class MemoryAssertionsMixin:
    """Class / Property / Assertion natives and structural QC scans."""

    def _merge_class(
        self,
        tenant_id: str,
        kg: str,
        class_id: str,
        *,
        name: str,
        layer: str = "tenant",
    ) -> list[GraphRecord]:
        key = (tenant_id, kg, class_id)
        row = self._classes.get(key)
        if row is None:
            row = _ClassRow(
                tenant_id=tenant_id, kg=kg, id=class_id, name=name, layer=layer
            )
            self._classes[key] = row
        else:
            if name:
                row.name = name
            if layer:
                row.layer = layer
        return [
            GraphRecord(
                data={
                    "id": row.id,
                    "name": row.name,
                    "layer": row.layer,
                    "tenant_id": tenant_id,
                    "kg": kg,
                }
            )
        ]

    def _merge_property(
        self,
        tenant_id: str,
        kg: str,
        property_id: str,
        *,
        name: str,
        kind: str = "datatype",
        layer: str = "tenant",
    ) -> list[GraphRecord]:
        key = (tenant_id, kg, property_id)
        row = self._properties.get(key)
        if row is None:
            row = _PropertyRow(
                tenant_id=tenant_id,
                kg=kg,
                id=property_id,
                name=name,
                kind=kind,
                layer=layer,
            )
            self._properties[key] = row
        else:
            if name:
                row.name = name
            if kind:
                row.kind = kind
            if layer:
                row.layer = layer
        return [
            GraphRecord(
                data={
                    "id": row.id,
                    "name": row.name,
                    "kind": row.kind,
                    "layer": row.layer,
                    "tenant_id": tenant_id,
                    "kg": kg,
                }
            )
        ]

    def _set_class_subclass(
        self,
        tenant_id: str,
        kg: str,
        child_class_id: str,
        parent_class_id: str,
    ) -> None:
        self._subclass_of[(tenant_id, kg, child_class_id)] = parent_class_id

    def _clear_class_subclass(
        self, tenant_id: str, kg: str, child_class_id: str
    ) -> None:
        self._subclass_of.pop((tenant_id, kg, child_class_id), None)

    def _set_subproperty(
        self,
        tenant_id: str,
        kg: str,
        child_prop_id: str,
        parent_prop_id: str,
    ) -> None:
        self._subproperty_of[(tenant_id, kg, child_prop_id)] = parent_prop_id

    def _set_instance_of(
        self, tenant_id: str, kg: str, entity_id: str, class_id: str
    ) -> None:
        self._instance_of.setdefault((tenant_id, kg, entity_id), set()).add(class_id)

    def _clear_instance_of(
        self, tenant_id: str, kg: str, entity_id: str, class_id: str
    ) -> None:
        ios = self._instance_of.get((tenant_id, kg, entity_id))
        if ios:
            ios.discard(class_id)
            if not ios:
                self._instance_of.pop((tenant_id, kg, entity_id), None)

    def _upsert_assertion(
        self,
        tenant_id: str,
        kg: str,
        *,
        assertion_id: str,
        subject_id: str,
        property_id: str,
        object_id: str | None = None,
        object_class_id: str | None = None,
        literal_value: Any = None,
        literal_datatype: str | None = None,
        source_url: str | None = None,
        verified_at: str | None = None,
        run_id: str | None = None,
        confidence: float | None = None,
        provenance: str | None = None,
    ) -> list[GraphRecord]:
        key = (tenant_id, kg, assertion_id)
        row = _AssertionRow(
            tenant_id=tenant_id,
            kg=kg,
            id=assertion_id,
            subject_id=subject_id,
            property_id=property_id,
            literal_value=literal_value,
            literal_datatype=literal_datatype,
            object_id=object_id,
            object_class_id=object_class_id,
            source_url=source_url,
            verified_at=verified_at,
            run_id=run_id,
            confidence=confidence,
            provenance=provenance,
        )
        self._assertions[key] = row
        return [row.as_record()]

    def _delete_assertions(
        self,
        tenant_id: str,
        kg: str,
        *,
        subject_id: str,
        property_id: str | None = None,
        object_key: str | None = None,
    ) -> int:
        from infona_client.graph.assertion_model import type_membership_property_id

        type_prop = type_membership_property_id()
        drop: list[tuple[str, str, str]] = []
        # (entity_id, class_id) pairs whose type Assertion is being removed
        type_pairs: list[tuple[str, str]] = []
        for key, a in self._assertions.items():
            if a.tenant_id != tenant_id or a.kg != kg:
                continue
            if a.subject_id != subject_id:
                continue
            if property_id is not None and a.property_id != property_id:
                continue
            if object_key is not None:
                ok = (
                    a.object_id
                    or a.object_class_id
                    or (
                        str(a.literal_value)
                        if a.literal_value is not None
                        else ""
                    )
                )
                if ok != object_key:
                    continue
            drop.append(key)
            if a.object_class_id and (
                property_id is None or a.property_id == type_prop
            ):
                type_pairs.append((a.subject_id, a.object_class_id))
        for key in drop:
            del self._assertions[key]
        # Evict derived INSTANCE_OF when no remaining type Assertion backs it.
        for eid, cid in type_pairs:
            still = any(
                a.tenant_id == tenant_id
                and a.kg == kg
                and a.subject_id == eid
                and a.object_class_id == cid
                for a in self._assertions.values()
            )
            if not still:
                self._clear_instance_of(tenant_id, kg, eid, cid)
        return len(drop)

    def _subclass_closure(
        self, tenant_id: str, kg: str, class_id: str
    ) -> list[str]:
        # Build child→parent for this scope, then descendants of class_id.
        child_to_parent: dict[str, str] = {}
        for (t, k, child), parent in self._subclass_of.items():
            if t == tenant_id and k == kg:
                child_to_parent[child] = parent
        from infona_client.graph.rdfs_helpers import descendants_of

        return descendants_of(class_id, child_to_parent)

    def _subproperty_closure(
        self, tenant_id: str, kg: str, prop_id: str
    ) -> list[str]:
        child_to_parent: dict[str, str] = {}
        for (t, k, child), parent in self._subproperty_of.items():
            if t == tenant_id and k == kg:
                child_to_parent[child] = parent
        from infona_client.graph.rdfs_helpers import descendants_of

        return descendants_of(prop_id, child_to_parent)

    def _entities_of_type_ids(
        self, tenant_id: str, kg: str, class_ids: list[str]
    ) -> list[str]:
        allowed = set(class_ids)
        out: set[str] = set()
        for (t, k, eid), cids in self._instance_of.items():
            if t != tenant_id or k != kg:
                continue
            if cids & allowed:
                out.add(eid)
        # Also from type Assertions if INSTANCE_OF missed
        for (t, k, _), a in self._assertions.items():
            if t != tenant_id or k != kg:
                continue
            if a.object_class_id and a.object_class_id in allowed:
                out.add(a.subject_id)
        return sorted(out)

    def _assertions_for_subject(
        self,
        tenant_id: str,
        kg: str,
        entity_id: str,
        *,
        prop_id: str | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for (t, k, _), a in sorted(
            self._assertions.items(), key=lambda x: (x[1].property_id, x[1].id)
        ):
            if t != tenant_id or k != kg:
                continue
            if a.subject_id != entity_id:
                continue
            if prop_id is not None and a.property_id != prop_id:
                continue
            rows.append(a.as_record().to_dict())
        return rows

    def _assertion_history(
        self,
        tenant_id: str,
        kg: str,
        *,
        entity_id: str | None = None,
        prop_id: str | None = None,
        since: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """All Assertion rows in scope (optional subject/property/since)."""
        lim = max(1, min(int(limit), 10000))
        rows: list[dict[str, Any]] = []
        for (t, k, _), a in sorted(
            self._assertions.items(),
            key=lambda x: (
                x[1].verified_at or "",
                x[1].property_id,
                x[1].subject_id,
                x[1].id,
            ),
        ):
            if t != tenant_id or k != kg:
                continue
            if entity_id is not None and a.subject_id != entity_id:
                continue
            if prop_id is not None and a.property_id != prop_id:
                continue
            if since:
                va = (a.verified_at or "").strip()
                if not va or va <= since:
                    continue
            rows.append(a.as_record().to_dict())
            if len(rows) >= lim:
                break
        return rows

    # --- Structural QC scans (E8) -------------------------------------------

    def scan_entities_missing_primary_type(
        self, tenant_id: str, kg: str
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for (t, k, eid), row in self._entities.items():
            if t != tenant_id or k != kg:
                continue
            if not row.primary_type:
                out.append({"id": eid, "tenant_id": t, "kg": k})
        return out

    def scan_rels_missing_scope(
        self, tenant_id: str, kg: str
    ) -> list[dict[str, Any]]:
        """Rels in this scope whose tenant_id/kg are empty (construction bug)."""
        out: list[dict[str, Any]] = []
        for r in self._rels.values():
            # Include unscoped rows that claim this scope via empty mismatch, and
            # any row with blank scope fields that would pollute isolation.
            if not r.tenant_id or not r.kg:
                out.append(
                    {
                        "start_id": r.start_id,
                        "end_id": r.end_id,
                        "attr": r.attr,
                        "tenant_id": r.tenant_id,
                        "kg": r.kg,
                    }
                )
            elif r.tenant_id == tenant_id and r.kg == kg:
                # In-scope but somehow blank after normalize — already covered.
                pass
        return out

    def scan_orphan_rel_targets(
        self, tenant_id: str, kg: str
    ) -> list[dict[str, Any]]:
        """Relationship endpoints whose Entity node is missing in scope."""
        out: list[dict[str, Any]] = []
        for r in self._rels.values():
            if r.tenant_id != tenant_id or r.kg != kg:
                continue
            if (tenant_id, kg, r.end_id) not in self._entities:
                out.append(
                    {
                        "start_id": r.start_id,
                        "end_id": r.end_id,
                        "attr": r.attr,
                        "side": "end",
                    }
                )
            if (tenant_id, kg, r.start_id) not in self._entities:
                out.append(
                    {
                        "start_id": r.start_id,
                        "end_id": r.end_id,
                        "attr": r.attr,
                        "side": "start",
                    }
                )
        return out

    def scan_instance_of_without_type_assertion(
        self, tenant_id: str, kg: str
    ) -> list[dict[str, Any]]:
        """Derived ``INSTANCE_OF`` edges with no backing type Assertion (ADR 0013 skew).

        Assertion is SoT; ``INSTANCE_OF`` is cache only. Any cache edge without a
        type Assertion (``object_class_id`` → Class) is dual-write skew.
        """
        out: list[dict[str, Any]] = []
        for (t, k, eid), class_ids in self._instance_of.items():
            if t != tenant_id or k != kg:
                continue
            for cid in class_ids:
                backed = any(
                    a.tenant_id == tenant_id
                    and a.kg == kg
                    and a.subject_id == eid
                    and a.object_class_id == cid
                    for a in self._assertions.values()
                )
                if not backed:
                    out.append(
                        {
                            "entity_id": eid,
                            "class_id": cid,
                            "tenant_id": tenant_id,
                            "kg": kg,
                        }
                    )
        return out

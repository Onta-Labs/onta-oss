"""Related-entity, subclass-closure, detail, and incident-rel reads."""

from __future__ import annotations

import copy
from typing import Any

from infona_client.graph.store import GraphRecord


class MemoryRelsMixin:
    """Relationship traversal, subclass names, entity detail."""

    def _related_entities(
        self,
        tenant_id: str,
        kg: str,
        from_types: Any,
        to_types: Any,
        rel_attr: str | None,
        limit: int,
    ) -> list[GraphRecord]:
        # Prefer object Assertions (SoT); shortcut rels are derived dual-write.
        from_ids = self._entity_ids_via_instance_of(tenant_id, kg, from_types)
        to_ids = (
            None
            if to_types is None
            else self._entity_ids_via_instance_of(tenant_id, kg, to_types)
        )
        out: list[GraphRecord] = []
        seen: set[tuple[str, str, str]] = set()
        for (t, k, _), a in sorted(
            self._assertions.items(),
            key=lambda x: (x[1].subject_id, x[1].object_id or ""),
        ):
            if t != tenant_id or k != kg:
                continue
            if not a.object_id or a.subject_id not in from_ids:
                continue
            if to_ids is not None and a.object_id not in to_ids:
                continue
            prop_row = self._properties.get((tenant_id, kg, a.property_id))
            prop_name = prop_row.name if prop_row else a.property_id
            if rel_attr is not None and prop_name != rel_attr and a.property_id != rel_attr:
                continue
            start = self._entities.get((tenant_id, kg, a.subject_id))
            end = self._entities.get((tenant_id, kg, a.object_id))
            if start is None or end is None:
                continue
            key = (start.id, end.id, prop_name)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                GraphRecord(
                    data={
                        "from_id": start.id,
                        "from_name": start.name,
                        "from_type": start.primary_type,
                        "to_id": end.id,
                        "to_name": end.name,
                        "to_type": end.primary_type,
                        "rel_type": prop_name,
                        "attr": prop_name,
                    }
                )
            )
            if len(out) >= limit:
                return out
        # Secondary derived shortcut rels (document as cache — dual-written).
        for rel in sorted(
            self._rels.values(),
            key=lambda r: (r.start_id, r.end_id),
        ):
            if rel.tenant_id != tenant_id or rel.kg != kg:
                continue
            if rel.start_id not in from_ids:
                continue
            if to_ids is not None and rel.end_id not in to_ids:
                continue
            if rel_attr is not None and rel.attr != rel_attr and rel.rel_type != rel_attr:
                continue
            a = self._entities.get((tenant_id, kg, rel.start_id))
            b = self._entities.get((tenant_id, kg, rel.end_id))
            if a is None or b is None:
                continue
            key = (a.id, b.id, rel.attr or rel.rel_type)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                GraphRecord(
                    data={
                        "from_id": a.id,
                        "from_name": a.name,
                        "from_type": a.primary_type,
                        "to_id": b.id,
                        "to_name": b.name,
                        "to_type": b.primary_type,
                        "rel_type": rel.rel_type,
                        "attr": rel.attr or rel.rel_type,
                    }
                )
            )
            if len(out) >= limit:
                break
        return out

    def _subclass_of_closure_names(
        self,
        tenant_id: str,
        kg: str,
        type_name: str,
        layer: str | None,
    ) -> list[GraphRecord]:
        """Descendant Class names including self (prefer Class SUBCLASS_OF).

        Falls back to OntoType parent map when no Class hierarchy is present
        in this scope (dual-write lag / legacy fixtures).
        """
        from infona_client.graph.rdfs_helpers import descendants_of

        # Prefer :Class hierarchy (ADR 0013).
        child_to_parent: dict[str, str] = {}
        id_to_name: dict[str, str] = {}
        for (t, k, cid), row in self._classes.items():
            if t != tenant_id or k != kg:
                continue
            if layer is not None and row.layer != layer:
                continue
            id_to_name[cid] = row.name
        for (t, k, child_id), parent_id in self._subclass_of.items():
            if t != tenant_id or k != kg:
                continue
            cname = id_to_name.get(child_id)
            pname = id_to_name.get(parent_id)
            if cname and pname:
                child_to_parent[cname] = pname
        if child_to_parent or any(
            n == type_name for n in id_to_name.values()
        ):
            names = descendants_of(type_name, child_to_parent)
            # If type_name is known as a Class but has no descendants map entry,
            # descendants_of still returns [type_name].
            if names:
                return [GraphRecord(data={"type_name": n}) for n in names]

        # Fallback: OntoType catalog rows (legacy dual surface).
        child_to_parent = {}
        for (t, k, lyr, name), row in self._onto_types.items():
            if t != tenant_id or k != kg:
                continue
            if layer is not None and lyr != layer:
                continue
            parent = getattr(row, "parent_type", None) or getattr(row, "parent", None)
            if parent:
                child_to_parent[name] = str(parent)
        names = descendants_of(type_name, child_to_parent)
        return [GraphRecord(data={"type_name": n}) for n in names]

    def _entity_count_total(self, tenant_id: str, kg: str) -> list[GraphRecord]:
        n = sum(
            1 for (t, k, _) in self._entities if t == tenant_id and k == kg
        )
        return [GraphRecord(data={"n": n})]

    def _entity_detail(
        self, tenant_id: str, kg: str, entity_id: str
    ) -> list[GraphRecord]:
        row = self._entities.get((tenant_id, kg, entity_id))
        if row is None:
            return []
        props = {
            "id": row.id,
            "tenant_id": row.tenant_id,
            "kg": row.kg,
            "primary_type": row.primary_type,
            "name": row.name,
            "source": row.source,
            **copy.deepcopy(row.props),
        }
        return [
            GraphRecord(
                data={
                    "id": row.id,
                    "tenant_id": row.tenant_id,
                    "kg": row.kg,
                    "primary_type": row.primary_type,
                    "name": row.name,
                    "source": row.source,
                    "labels": list(row.labels),
                    "props": props,
                }
            )
        ]

    def _entity_rels(
        self, tenant_id: str, kg: str, entity_id: str
    ) -> list[GraphRecord]:
        out: list[GraphRecord] = []
        for r in self._rels.values():
            if r.tenant_id != tenant_id or r.kg != kg:
                continue
            if r.start_id == entity_id:
                other = self._entities.get((tenant_id, kg, r.end_id))
                out.append(
                    GraphRecord(
                        data={
                            "attr": r.attr,
                            "rel_type": r.rel_type,
                            "other_id": r.end_id,
                            "other_name": other.name if other else None,
                            "other_type": other.primary_type if other else None,
                            "direction": "out",
                        }
                    )
                )
            if r.end_id == entity_id:
                other = self._entities.get((tenant_id, kg, r.start_id))
                out.append(
                    GraphRecord(
                        data={
                            "attr": r.attr,
                            "rel_type": r.rel_type,
                            "other_id": r.start_id,
                            "other_name": other.name if other else None,
                            "other_type": other.primary_type if other else None,
                            "direction": "in",
                        }
                    )
                )
        out.sort(key=lambda rec: (rec.get("direction"), rec.get("attr"), rec.get("other_id")))
        return out

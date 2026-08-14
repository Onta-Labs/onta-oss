"""Paged entity listing and type-membership counts.

Membership is via ``INSTANCE_OF`` → Class (ADR 0013), never
``primary_type`` alone. Used by Explorer list-by-type and NL helpers.
"""

from __future__ import annotations

from typing import Any

from infona_client.graph.store import GraphRecord


class MemoryListingMixin:
    """Type-membership listing and counts."""

    def _entity_counts_by_primary_type(
        self, tenant_id: str, kg: str
    ) -> list[GraphRecord]:
        """Count entities per Class.name via INSTANCE_OF (ADR 0013)."""
        # entity → set of class names (from INSTANCE_OF + Class rows; fall back
        # to type Assertions' object_class leaf; never primary_type alone).
        by_class: dict[str, set[str]] = {}
        for (t, k, eid), cids in self._instance_of.items():
            if t != tenant_id or k != kg:
                continue
            for cid in cids:
                crow = self._classes.get((t, k, cid))
                cname = crow.name if crow else cid.rstrip("/").rsplit("/", 1)[-1]
                if cname:
                    by_class.setdefault(cname, set()).add(eid)
        for (t, k, _), a in self._assertions.items():
            if t != tenant_id or k != kg or not a.object_class_id:
                continue
            cid = a.object_class_id
            crow = self._classes.get((t, k, cid))
            cname = crow.name if crow else cid.rstrip("/").rsplit("/", 1)[-1]
            if cname:
                by_class.setdefault(cname, set()).add(a.subject_id)
        return [
            GraphRecord(data={"primary_type": pt, "n": len(eids)})
            for pt, eids in sorted(by_class.items())
        ]

    def _list_entities_by_type_page(
        self,
        tenant_id: str,
        kg: str,
        primary_type: str,
        after_id: str | None,
        limit: int,
    ) -> list[GraphRecord]:
        # Semantic membership via INSTANCE_OF → Class (param name historical).
        return self._list_entities_by_types_page(
            tenant_id, kg, [primary_type], after_id, limit
        )

    def _list_entities_by_label(
        self,
        tenant_id: str,
        kg: str,
        label: str,
        after_id: str | None,
        limit: int,
    ) -> list[GraphRecord]:
        rows = [
            r
            for (t, k, _), r in sorted(self._entities.items(), key=lambda x: x[0][2])
            if t == tenant_id and k == kg and label in (r.labels or [])
        ]
        if after_id is not None:
            rows = [r for r in rows if r.id > after_id]
        if limit is not None and limit >= 0:
            rows = rows[: int(limit)]
        return [
            GraphRecord(
                data={
                    "id": r.id,
                    "tenant_id": r.tenant_id,
                    "kg": r.kg,
                    "primary_type": r.primary_type,
                    "name": r.name,
                    "source": r.source,
                }
            )
            for r in rows
        ]

    def _entity_count_by_type(
        self, tenant_id: str, kg: str, primary_type: str
    ) -> list[GraphRecord]:
        return self._entity_count_by_types(tenant_id, kg, [primary_type])

    @staticmethod
    def _as_type_name_set(raw: Any) -> set[str]:
        if raw is None:
            return set()
        if isinstance(raw, str):
            return {raw} if raw else set()
        return {str(x) for x in raw if x is not None and str(x)}

    def _class_ids_matching_type_names(
        self, tenant_id: str, kg: str, type_names: Any
    ) -> set[str]:
        """Resolve NL ``$type_names`` (leaves and/or Class IRIs) to Class ids."""
        names = self._as_type_name_set(type_names)
        if not names:
            return set()
        ids: set[str] = set()
        for (t, k, cid), row in self._classes.items():
            if t != tenant_id or k != kg:
                continue
            if row.name in names or cid in names:
                ids.add(cid)
        # Accept leaf names that have not yet been MERGEd as Class rows by
        # minting the shared type_uri (assert_fact / type Assertions use this).
        from infona_client.graph.ontology_queries import type_uri

        for n in names:
            if n.startswith("http://") or n.startswith("https://"):
                ids.add(n)
            else:
                ids.add(type_uri(n))
        return ids

    def _entity_ids_via_instance_of(
        self, tenant_id: str, kg: str, type_names: Any
    ) -> set[str]:
        """Entity ids with INSTANCE_OF (or type Assertion) in ``type_names``."""
        allowed = self._class_ids_matching_type_names(tenant_id, kg, type_names)
        if not allowed:
            return set()
        out: set[str] = set()
        for (t, k, eid), cids in self._instance_of.items():
            if t == tenant_id and k == kg and cids & allowed:
                out.add(eid)
        for (t, k, _), a in self._assertions.items():
            if t != tenant_id or k != kg:
                continue
            if a.object_class_id and a.object_class_id in allowed:
                out.add(a.subject_id)
        return out

    def _entity_count_by_types(
        self, tenant_id: str, kg: str, type_names: Any
    ) -> list[GraphRecord]:
        # Semantic path: INSTANCE_OF → Class (ADR 0013), not primary_type alone.
        matched = self._entity_ids_via_instance_of(tenant_id, kg, type_names)
        return [GraphRecord(data={"n": len(matched)})]

    def _list_entities_by_types_page(
        self,
        tenant_id: str,
        kg: str,
        type_names: Any,
        after_id: str | None,
        limit: int,
    ) -> list[GraphRecord]:
        matched = self._entity_ids_via_instance_of(tenant_id, kg, type_names)
        rows = [
            r
            for (t, k, eid), r in sorted(self._entities.items(), key=lambda x: x[0][2])
            if t == tenant_id and k == kg and eid in matched
        ]
        if after_id is not None:
            rows = [r for r in rows if r.id > after_id]
        if limit is not None and limit >= 0:
            rows = rows[: int(limit)]
        return [
            GraphRecord(
                data={
                    "id": r.id,
                    "tenant_id": r.tenant_id,
                    "kg": r.kg,
                    "primary_type": r.primary_type,
                    "name": r.name,
                    "source": r.source,
                }
            )
            for r in rows
        ]

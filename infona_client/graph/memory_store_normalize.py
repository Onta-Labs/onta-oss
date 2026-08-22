"""In-memory implementations of the normalization rule-apply reads (ONTA-534).

The Memory half of :mod:`infona_client.graph.normalize_cypher`. Each method
mirrors one template's Cypher exactly — same optional ``$primary_type``
semantics (``None`` = predicate-scoped, KG-wide), same INSTANCE_OF-or-
``primary_type`` type tolerance, same unbounded result set (apply must see every
row), same column names.
"""

from __future__ import annotations

from infona_client.graph.store import GraphRecord


class MemoryNormalizeMixin:
    """``entity_literals_by_prop`` / ``entity_rels_by_attr`` / orphan scan."""

    def _type_matched_ids(
        self, tenant_id: str, kg: str, type_name: str | None
    ) -> set[str] | None:
        """Entity ids of ``type_name``, or ``None`` when no type filter applies.

        Mirrors the templates' ``e.primary_type = $primary_type OR EXISTS
        {INSTANCE_OF → Class}``: the INSTANCE_OF set plus every entity whose
        denormalized ``primary_type`` matches. Both halves are needed — a node
        minted by one rail may carry only one of them, and a type filter that
        missed it would leave its values un-normalized.
        """
        leaf = (type_name or "").strip()
        if not leaf:
            return None
        matched = set(self._entity_ids_via_instance_of(tenant_id, kg, [leaf]))
        for (t, k, eid), row in self._entities.items():
            if t == tenant_id and k == kg and row.primary_type == leaf:
                matched.add(eid)
        return matched

    def _entity_literals_by_prop(
        self,
        tenant_id: str,
        kg: str,
        prop_key: str,
        type_name: str | None,
    ) -> list[GraphRecord]:
        """Every entity carrying ``prop_key``; ``value`` may be a list."""
        if not prop_key:
            return []
        matched = self._type_matched_ids(tenant_id, kg, type_name)
        out: list[GraphRecord] = []
        for (t, k, eid), row in sorted(self._entities.items(), key=lambda x: x[0][2]):
            if t != tenant_id or k != kg:
                continue
            if matched is not None and eid not in matched:
                continue
            val = self._entity_prop_value(row, prop_key)
            if val is None:
                continue
            out.append(
                GraphRecord(
                    data={
                        "entity_uri": row.id,
                        "type": row.primary_type,
                        "value": val,
                    }
                )
            )
        return out

    def _entity_rels_by_attr(
        self,
        tenant_id: str,
        kg: str,
        rel_attr: str,
        rel_type: str | None,
    ) -> list[GraphRecord]:
        """Every ``attr``-carrying edge, with the target's name + type."""
        if not rel_attr:
            return []
        out: list[GraphRecord] = []
        for r in sorted(
            self._rels.values(), key=lambda r: (r.start_id, r.end_id)
        ):
            if r.tenant_id != tenant_id or r.kg != kg:
                continue
            if r.attr != rel_attr and (rel_type is None or r.rel_type != rel_type):
                continue
            end = self._entities.get((tenant_id, kg, r.end_id))
            out.append(
                GraphRecord(
                    data={
                        "start_id": r.start_id,
                        "end_id": r.end_id,
                        "end_name": None if end is None else end.name,
                        "end_type": None if end is None else end.primary_type,
                    }
                )
            )
        return out

    def _entity_orphans_of_type(
        self,
        tenant_id: str,
        kg: str,
        type_name: str,
        rel_attr: str,
        rel_type: str | None,
    ) -> list[GraphRecord]:
        """Entities of ``type_name`` with no inbound ``rel_attr`` edge."""
        matched = self._type_matched_ids(tenant_id, kg, type_name)
        if not matched:
            return []
        referenced: set[str] = set()
        for r in self._rels.values():
            if r.tenant_id != tenant_id or r.kg != kg:
                continue
            if r.attr != rel_attr and (rel_type is None or r.rel_type != rel_type):
                continue
            referenced.add(r.end_id)
        out: list[GraphRecord] = []
        for eid in sorted(matched):
            if eid in referenced:
                continue
            row = self._entities.get((tenant_id, kg, eid))
            if row is None:
                continue
            out.append(GraphRecord(data={"entity_uri": row.id, "name": row.name}))
        return out


__all__ = ["MemoryNormalizeMixin"]

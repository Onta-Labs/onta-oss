"""Entity / rel / provenance / citation helpers for the in-memory store.

Native writer half used by :mod:`infona_client.graph.pg_ops`. Instance
mutations still go through the shared ``insert_facts`` / ``delete_facts``
/ ``rewrite_subject`` path in production; this mixin is the hermetic
test double of those graph effects.
"""

from __future__ import annotations

import copy
from typing import Any, Sequence

from infona_client.graph.memory_store_rows import (
    _AssertionRow,
    _CitationRow,
    _EntityRow,
    _ProvRow,
    _RelRow,
    _ValueHistoryRow,
)
from infona_client.graph.store import GraphRecord


class MemoryEntitiesMixin:
    """Entity CRUD, rewrite, provenance, and citation rows."""

    def snapshot_assertions(self) -> list[dict[str, Any]]:
        return [a.as_record().to_dict() for a in self._assertions.values()]

    # --- test helpers -------------------------------------------------------

    def entity_count(self, *, tenant_id: str | None = None, kg: str | None = None) -> int:
        n = 0
        for (t, k, _id), _row in self._entities.items():
            if tenant_id is not None and t != tenant_id:
                continue
            if kg is not None and k != kg:
                continue
            n += 1
        return n

    def rel_count(self, *, tenant_id: str | None = None, kg: str | None = None) -> int:
        n = 0
        for (t, k, *_), _row in self._rels.items():
            if tenant_id is not None and t != tenant_id:
                continue
            if kg is not None and k != kg:
                continue
            n += 1
        return n

    def snapshot_entities(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(row.as_record().to_dict()) for row in self._entities.values()]

    def snapshot_rels(self) -> list[dict[str, Any]]:
        out = []
        for r in self._rels.values():
            out.append(
                {
                    "tenant_id": r.tenant_id,
                    "kg": r.kg,
                    "start_id": r.start_id,
                    "end_id": r.end_id,
                    "rel_type": r.rel_type,
                    "attr": r.attr,
                }
            )
        return out

    def snapshot_prov(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(r.__dict__) for r in self._prov]

    def snapshot_value_history(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(r.__dict__) for r in self._value_history]

    def snapshot_citations(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(r.__dict__) for r in self._citations.values()]

    def _apply_domain_labels(
        self,
        tenant_id: str,
        kg: str,
        entity_id: str,
        safe_labels: Sequence[str],
    ) -> list[GraphRecord]:
        """Accumulate domain labels (Neo4j ``SET e:Label`` is additive).

        Prior domain labels are kept; only new tokens are appended. ``Entity``
        is always present as the system label.
        """
        row = self._entities.get((tenant_id, kg, entity_id))
        if row is None:
            return []
        # Union into existing labels — never replace (matches Neo4j SET e:X).
        if "Entity" not in row.labels:
            row.labels.insert(0, "Entity")
        for lab in safe_labels:
            if lab not in row.labels:
                row.labels.append(lab)
        return [
            GraphRecord(
                data={
                    "id": row.id,
                    "tenant_id": row.tenant_id,
                    "kg": row.kg,
                    "labels": list(row.labels),
                }
            )
        ]

    def _merge_entity(
        self,
        tenant_id: str,
        kg: str,
        entity_id: str,
        *,
        primary_type: str | None = None,
        name: str | None = None,
        source: str | None = None,
    ) -> list[GraphRecord]:
        key = (tenant_id, kg, entity_id)
        existing = self._entities.get(key)
        if existing is None:
            row = _EntityRow(
                tenant_id=tenant_id,
                kg=kg,
                id=entity_id,
                primary_type=primary_type,
                name=name,
                source=source,
            )
            self._entities[key] = row
        else:
            if primary_type is not None:
                existing.primary_type = primary_type
            if name is not None:
                existing.name = name
            if source is not None:
                existing.source = source
            row = existing
        return [row.as_record()]

    def _set_literal(
        self,
        tenant_id: str,
        kg: str,
        entity_id: str,
        prop_key: str,
        value: Any,
        *,
        multi_union: bool,
    ) -> list[GraphRecord]:
        key = (tenant_id, kg, entity_id)
        row = self._entities.get(key)
        if row is None:
            row = _EntityRow(tenant_id=tenant_id, kg=kg, id=entity_id)
            self._entities[key] = row
        if prop_key in ("name", "source", "primary_type"):
            setattr(row, prop_key, value)
            return [row.as_record()]
        if multi_union and prop_key in row.props:
            existing = row.props[prop_key]
            if isinstance(value, list):
                incoming = list(value)
            else:
                incoming = [value]
            if isinstance(existing, list):
                base = list(existing)
            else:
                base = [existing]
            for v in incoming:
                if v not in base:
                    base.append(v)
            row.props[prop_key] = base if len(base) > 1 else base[0]
        else:
            row.props[prop_key] = value
        return [row.as_record()]

    def _merge_rel(
        self,
        tenant_id: str,
        kg: str,
        start_id: str,
        end_id: str,
        rel_type: str,
        attr_leaf: str,
    ) -> list[GraphRecord]:
        # Ensure endpoints exist.
        self._merge_entity(tenant_id, kg, start_id)
        self._merge_entity(tenant_id, kg, end_id)
        rkey = (tenant_id, kg, start_id, end_id, rel_type)
        self._rels[rkey] = _RelRow(
            tenant_id=tenant_id,
            kg=kg,
            start_id=start_id,
            end_id=end_id,
            rel_type=rel_type,
            attr=attr_leaf,
        )
        return [
            GraphRecord(
                data={
                    "start_id": start_id,
                    "end_id": end_id,
                    "rel_type": rel_type,
                    "attr": attr_leaf,
                    "tenant_id": tenant_id,
                    "kg": kg,
                }
            )
        ]

    def _delete_entity(self, tenant_id: str, kg: str, entity_id: str) -> int:
        removed = 0
        if self._entities.pop((tenant_id, kg, entity_id), None) is not None:
            removed += 1
        # Drop incident rels in scope.
        drop = [
            k
            for k, r in self._rels.items()
            if r.tenant_id == tenant_id
            and r.kg == kg
            and (r.start_id == entity_id or r.end_id == entity_id)
        ]
        for k in drop:
            del self._rels[k]
            removed += 1
        # Drop Assertions where subject or object is this entity.
        a_drop = [
            k
            for k, a in self._assertions.items()
            if a.tenant_id == tenant_id
            and a.kg == kg
            and (a.subject_id == entity_id or a.object_id == entity_id)
        ]
        for k in a_drop:
            del self._assertions[k]
            removed += 1
        self._instance_of.pop((tenant_id, kg, entity_id), None)
        return removed

    def _delete_literals(
        self, tenant_id: str, kg: str, entity_id: str, keys: list[str]
    ) -> int:
        row = self._entities.get((tenant_id, kg, entity_id))
        if row is None:
            return 0
        n = 0
        for key in keys:
            if key == "name" and row.name is not None:
                row.name = None
                n += 1
            elif key == "source" and row.source is not None:
                row.source = None
                n += 1
            elif key in row.props:
                del row.props[key]
                n += 1
        return n

    def _delete_rels(
        self,
        tenant_id: str,
        kg: str,
        *,
        start_id: str | None,
        end_id: str | None,
        rel_type: str | None,
        attr_leaf: str | None,
    ) -> int:
        drop = []
        for k, r in self._rels.items():
            if r.tenant_id != tenant_id or r.kg != kg:
                continue
            if start_id is not None and r.start_id != start_id:
                continue
            if end_id is not None and r.end_id != end_id:
                continue
            if rel_type is not None and r.rel_type != rel_type:
                continue
            if attr_leaf is not None and r.attr != attr_leaf:
                continue
            drop.append(k)
        for k in drop:
            del self._rels[k]
        return len(drop)

    def _rewrite_entity_id(
        self, tenant_id: str, kg: str, old_id: str, new_id: str
    ) -> None:
        old_key = (tenant_id, kg, old_id)
        new_key = (tenant_id, kg, new_id)
        row = self._entities.get(old_key)
        if row is None:
            return
        if new_key in self._entities and old_id != new_id:
            # Merge props into survivor (last-write: new keeps its own, then overlay old)
            survivor = self._entities[new_key]
            for pk, pv in row.props.items():
                if pk not in survivor.props:
                    survivor.props[pk] = pv
            if survivor.name is None and row.name is not None:
                survivor.name = row.name
            if survivor.source is None and row.source is not None:
                survivor.source = row.source
            if survivor.primary_type is None and row.primary_type is not None:
                survivor.primary_type = row.primary_type
            for lab in row.labels:
                if lab not in survivor.labels:
                    survivor.labels.append(lab)
            del self._entities[old_key]
        else:
            del self._entities[old_key]
            row.id = new_id
            self._entities[new_key] = row
        # Rebind rel endpoints.
        updates: list[tuple[tuple, _RelRow]] = []
        drop_keys = []
        for k, r in list(self._rels.items()):
            if r.tenant_id != tenant_id or r.kg != kg:
                continue
            if r.start_id != old_id and r.end_id != old_id:
                continue
            drop_keys.append(k)
            nr = _RelRow(
                tenant_id=r.tenant_id,
                kg=r.kg,
                start_id=new_id if r.start_id == old_id else r.start_id,
                end_id=new_id if r.end_id == old_id else r.end_id,
                rel_type=r.rel_type,
                attr=r.attr,
            )
            updates.append(
                (
                    (nr.tenant_id, nr.kg, nr.start_id, nr.end_id, nr.rel_type),
                    nr,
                )
            )
        for k in drop_keys:
            del self._rels[k]
        for nk, nr in updates:
            self._rels[nk] = nr
        # Rebind prov subject ids.
        for p in self._prov:
            if p.tenant_id == tenant_id and p.kg == kg and p.subject_id == old_id:
                p.subject_id = new_id
        # Rebind AttrCitation entity_id keys.
        cite_moves: list[tuple[tuple, _CitationRow]] = []
        cite_drop: list[tuple] = []
        for ck, c in list(self._citations.items()):
            if c.tenant_id == tenant_id and c.kg == kg and c.entity_id == old_id:
                cite_drop.append(ck)
                c.entity_id = new_id
                cite_moves.append(
                    (
                        (c.tenant_id, c.kg, new_id, c.attr, c.value_hash or ""),
                        c,
                    )
                )
        for ck in cite_drop:
            del self._citations[ck]
        for nk, crow in cite_moves:
            self._citations[nk] = crow
        # Rebind Assertion subject_id / object_id + denormalized keys.
        a_moves: list[tuple[tuple, _AssertionRow]] = []
        a_drop: list[tuple] = []
        for ak, a in list(self._assertions.items()):
            if a.tenant_id != tenant_id or a.kg != kg:
                continue
            changed = False
            if a.subject_id == old_id:
                a.subject_id = new_id
                changed = True
            if a.object_id == old_id:
                a.object_id = new_id
                changed = True
            if changed:
                a_drop.append(ak)
                a_moves.append(((a.tenant_id, a.kg, a.id), a))
        for ak in a_drop:
            del self._assertions[ak]
        for nk, arow in a_moves:
            self._assertions[nk] = arow
        # Rebind INSTANCE_OF cache key.
        old_io = self._instance_of.pop((tenant_id, kg, old_id), None)
        if old_io:
            self._instance_of.setdefault((tenant_id, kg, new_id), set()).update(
                old_io
            )

    def _add_prov(self, row: _ProvRow) -> None:
        self._prov.append(row)

    def _add_value_history(self, row: _ValueHistoryRow) -> None:
        self._value_history.append(row)

    def _list_value_history(
        self,
        tenant_id: str,
        kg: str,
        *,
        subject_id: str | None = None,
        predicate: str | None = None,
        since: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for r in self._value_history:
            if r.tenant_id != tenant_id or r.kg != kg:
                continue
            if subject_id is not None and r.subject_id != subject_id:
                continue
            if predicate is not None and r.predicate != predicate:
                # Also allow leaf match (attrs URI vs bare leaf).
                if not (
                    r.predicate.endswith("/" + predicate.rstrip("/").rsplit("/", 1)[-1])
                    or predicate.endswith("/" + r.predicate)
                    or r.predicate == predicate
                ):
                    pred_leaf = predicate.rstrip("/").rsplit("/", 1)[-1]
                    row_leaf = r.predicate.rstrip("/").rsplit("/", 1)[-1]
                    if pred_leaf != row_leaf:
                        continue
            if since and not ((r.changed_at or "") > since):
                continue
            out.append(
                {
                    "subject_id": r.subject_id,
                    "subject": r.subject_id,
                    "predicate": r.predicate,
                    "old_value": r.old_value,
                    "new_value": r.new_value,
                    "changed_at": r.changed_at,
                }
            )
        out.sort(key=lambda d: d.get("changed_at") or "")
        return out[: max(1, min(int(limit), 10000))]

    def _upsert_citation(self, row: _CitationRow) -> None:
        key = (row.tenant_id, row.kg, row.entity_id, row.attr, row.value_hash or "")
        existing = self._citations.get(key)
        if existing is None:
            self._citations[key] = row
            return
        # Merge non-empty fields onto existing citation.
        if row.source_url:
            existing.source_url = row.source_url
        if row.provenance:
            existing.provenance = row.provenance
        if row.verified_at:
            existing.verified_at = row.verified_at

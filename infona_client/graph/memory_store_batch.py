"""In-process FactBatch apply for :class:`MemoryGraphSession`.

Same payload as Neo4j UNWIND; natives already in the session.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infona_client.graph.fact_batch import FactBatch


class MemoryBatchMixin:
    """``write_fact_batch`` via existing Memory write_* methods."""

    async def write_fact_batch(self, batch: "FactBatch") -> int:
        from infona_client.graph.fact_batch import FactBatch as _FactBatch

        if not isinstance(batch, _FactBatch) or batch.n_facts == 0:
            return 0
        for row in batch.entities:
            await self.write_merge_entity(
                id=row["id"],
                primary_type=row.get("primary_type"),
                name=row.get("name"),
                source=row.get("source"),
                ts=row.get("ts"),
            )
        for row in batch.properties:
            await self.write_merge_property(
                property_id=row["id"], name=row["name"], kind=row["kind"]
            )
        for row in batch.classes:
            await self.write_merge_class(class_id=row["id"], name=row["name"])
        for row in batch.assertions:
            await self.write_assertion(
                assertion_id=row["assertion_id"],
                subject_id=row["subject_id"],
                property_id=row["property_id"],
                property_name="",
                object_id=row.get("object_id"),
                object_class_id=row.get("object_class_id"),
                literal_value=row.get("literal_value"),
                literal_datatype=row.get("literal_datatype"),
                source_url=row.get("source_url"),
                verified_at=row.get("verified_at"),
                run_id=row.get("run_id"),
                confidence=row.get("confidence"),
                provenance=row.get("provenance"),
                ts=row.get("ts"),
            )
        for eid, props in batch.entity_props.items():
            for key, value in props.items():
                await self.write_set_literal(eid, key, value, multi_union=True)
        for row in batch.instance_of:
            await self.write_instance_of(row["entity_id"], row["class_id"])
        for label, ids in batch.labels.items():
            for eid in ids:
                await self.apply_entity_domain_labels(eid, [label])
        for row in batch.rels:
            await self.write_merge_rel(
                row["start_id"], row["end_id"], row["rel_type"], row["attr"]
            )
        for row in batch.prov_events:
            await self.write_prov_event(
                event_type=row["event_type"],
                subject_id=row["subject_id"],
                attr=row.get("attr"),
                object_repr=row.get("object_repr"),
                old_id=row.get("old_id"),
                new_id=row.get("new_id"),
                reason=row.get("reason") or "",
                source=row.get("source"),
                fact_hash=row.get("fact_hash"),
                ts=row.get("ts"),
                confidence=row.get("confidence"),
            )
        return batch.n_facts

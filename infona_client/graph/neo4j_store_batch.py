"""UNWIND batch instance writes for :class:`Neo4jGraphSession`.

One :func:`apply_facts` call becomes a handful of Cypher statements instead of
one Bolt round-trip per Assertion / property / ProvEvent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

from infona_client.graph.labels import sanitize_domain_label
from infona_client.graph.scope import GraphScopeError

if TYPE_CHECKING:
    from infona_client.graph.fact_batch import FactBatch
    from infona_client.graph.store import GraphRecord

# Keep each UNWIND under typical server memory; 5k rows is well inside
# Neo4j's default tx heap for Assertion nodes with small literals.
_UNWIND_CHUNK = 5000


def _chunks(rows: Sequence[Any], size: int = _UNWIND_CHUNK):
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


class Neo4jBatchMixin:
    """``write_fact_batch`` — UNWIND MERGE/SET for a prepared FactBatch."""

    async def write_fact_batch(self, batch: "FactBatch") -> int:
        if batch.n_facts == 0:
            return 0
        if batch.entities:
            await self._unwind_merge_entities(batch.entities)
        if batch.properties:
            await self._unwind_merge_properties(batch.properties)
        if batch.classes:
            await self._unwind_merge_classes(batch.classes)
        for chunk in _chunks(batch.assertions):
            await self._unwind_assertions(chunk)
        if batch.entity_props:
            await self._unwind_entity_props(batch.entity_props)
        if batch.instance_of:
            await self._unwind_instance_of(batch.instance_of)
        for label, ids in batch.labels.items():
            await self._unwind_set_label(label, ids)
        if batch.rels:
            await self._unwind_rels(batch.rels)
        for chunk in _chunks(batch.prov_events):
            await self._unwind_prov_events(chunk)
        return batch.n_facts

    async def _unwind_merge_entities(self, rows: Sequence[dict[str, Any]]) -> None:
        cypher = (
            "UNWIND $rows AS row\n"
            "MERGE (e:Entity {tenant_id: $tenant_id, kg: $kg, id: row.id})\n"
            "ON CREATE SET e.primary_type = row.primary_type,\n"
            "              e.name = row.name,\n"
            "              e.source = row.source,\n"
            "              e.created_at = row.ts,\n"
            "              e.updated_at = row.ts\n"
            "ON MATCH SET e.primary_type = coalesce(row.primary_type, e.primary_type),\n"
            "             e.name = coalesce(row.name, e.name),\n"
            "             e.source = coalesce(row.source, e.source),\n"
            "             e.updated_at = row.ts\n"
            "RETURN count(*) AS n"
        )
        for chunk in _chunks(rows):
            await self.execute_write(cypher, {"rows": list(chunk)})

    async def _unwind_merge_properties(self, rows: Sequence[dict[str, str]]) -> None:
        cypher = (
            "UNWIND $rows AS row\n"
            "MERGE (p:Property {tenant_id: $tenant_id, kg: $kg, id: row.id})\n"
            "ON CREATE SET p.name = row.name, p.kind = row.kind, p.layer = 'tenant'\n"
            "ON MATCH SET p.name = coalesce(row.name, p.name),\n"
            "             p.kind = coalesce(row.kind, p.kind)\n"
            "RETURN count(*) AS n"
        )
        await self.execute_write(cypher, {"rows": list(rows)})

    async def _unwind_merge_classes(self, rows: Sequence[dict[str, str]]) -> None:
        cypher = (
            "UNWIND $rows AS row\n"
            "MERGE (c:Class {tenant_id: $tenant_id, kg: $kg, id: row.id})\n"
            "ON CREATE SET c.name = row.name, c.layer = 'tenant'\n"
            "ON MATCH SET c.name = coalesce(row.name, c.name)\n"
            "RETURN count(*) AS n"
        )
        await self.execute_write(cypher, {"rows": list(rows)})

    async def _unwind_assertions(self, rows: Sequence[dict[str, Any]]) -> None:
        # First-write ingest MERGEs by assertion id. Same-id re-ingest SETs
        # fields. Object/class edges are MERGEd; we do not DELETE prior
        # OBJECT edges here (id includes the object key, so a value change
        # is a new assertion).
        cypher = (
            "UNWIND $rows AS row\n"
            "MATCH (s:Entity {tenant_id: $tenant_id, kg: $kg, id: row.subject_id})\n"
            "MATCH (p:Property {tenant_id: $tenant_id, kg: $kg, id: row.property_id})\n"
            "MERGE (a:Assertion {tenant_id: $tenant_id, kg: $kg, id: row.assertion_id})\n"
            "SET a.subject_id = row.subject_id,\n"
            "    a.property_id = row.property_id,\n"
            "    a.object_id = row.object_id,\n"
            "    a.literal_value = row.literal_value,\n"
            "    a.literal_datatype = row.literal_datatype,\n"
            "    a.source_url = row.source_url,\n"
            "    a.verified_at = row.verified_at,\n"
            "    a.run_id = row.run_id,\n"
            "    a.confidence = row.confidence,\n"
            "    a.provenance = row.provenance,\n"
            "    a.updated_at = row.ts\n"
            "MERGE (a)-[:SUBJECT]->(s)\n"
            "MERGE (a)-[:PREDICATE]->(p)\n"
            "FOREACH (_ IN CASE WHEN row.object_id IS NULL THEN [] ELSE [1] END |\n"
            "  MERGE (o:Entity {tenant_id: $tenant_id, kg: $kg, id: row.object_id})\n"
            "  MERGE (a)-[:OBJECT]->(o)\n"
            ")\n"
            "FOREACH (_ IN CASE WHEN row.object_class_id IS NULL THEN [] ELSE [1] END |\n"
            "  MERGE (c:Class {tenant_id: $tenant_id, kg: $kg, id: row.object_class_id})\n"
            "  MERGE (a)-[:OBJECT_CLASS]->(c)\n"
            ")\n"
            "RETURN count(*) AS n"
        )
        await self.execute_write(cypher, {"rows": list(rows)})

    async def _unwind_entity_props(self, entity_props: dict[str, dict[str, Any]]) -> None:
        rows = [{"id": eid, "props": props} for eid, props in entity_props.items() if props]
        if not rows:
            return
        cypher = (
            "UNWIND $rows AS row\n"
            "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg, id: row.id})\n"
            "SET e += row.props\n"
            "RETURN count(*) AS n"
        )
        for chunk in _chunks(rows):
            await self.execute_write(cypher, {"rows": list(chunk)})

    async def _unwind_instance_of(self, rows: Sequence[dict[str, str]]) -> None:
        cypher = (
            "UNWIND $rows AS row\n"
            "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg, id: row.entity_id})\n"
            "MATCH (c:Class {tenant_id: $tenant_id, kg: $kg, id: row.class_id})\n"
            "MERGE (e)-[:INSTANCE_OF]->(c)\n"
            "RETURN count(*) AS n"
        )
        for chunk in _chunks(rows):
            await self.execute_write(cypher, {"rows": list(chunk)})

    async def _unwind_set_label(self, label: str, ids: Sequence[str]) -> None:
        token = sanitize_domain_label(label)
        cypher = (
            "UNWIND $ids AS id\n"
            "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg, id: id})\n"
            f"SET e:`{token}`\n"
            "RETURN count(*) AS n"
        )
        for chunk in _chunks(list(ids)):
            await self.execute_write(cypher, {"ids": list(chunk)})

    async def _unwind_rels(self, rows: Sequence[dict[str, str]]) -> None:
        by_type: dict[str, list[dict[str, str]]] = {}
        for row in rows:
            by_type.setdefault(row["rel_type"], []).append(row)
        for rel_type, group in by_type.items():
            if not rel_type or not rel_type.replace("_", "").isalnum():
                raise GraphScopeError(f"unsafe rel type {rel_type!r}")
            cypher = (
                "UNWIND $rows AS row\n"
                "MATCH (a:Entity {tenant_id: $tenant_id, kg: $kg, id: row.start_id})\n"
                "MATCH (b:Entity {tenant_id: $tenant_id, kg: $kg, id: row.end_id})\n"
                f"MERGE (a)-[r:`{rel_type}` {{tenant_id: $tenant_id, kg: $kg}}]->(b)\n"
                "ON CREATE SET r.attr = row.attr\n"
                "ON MATCH SET r.attr = coalesce(r.attr, row.attr)\n"
                "RETURN count(*) AS n"
            )
            for chunk in _chunks(group):
                await self.execute_write(cypher, {"rows": list(chunk)})

    async def _unwind_prov_events(self, rows: Sequence[dict[str, Any]]) -> None:
        if not rows:
            return
        cypher = (
            "UNWIND $rows AS row\n"
            "OPTIONAL MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg, id: row.subject_id})\n"
            "CREATE (p:ProvEvent {\n"
            "  tenant_id: $tenant_id, kg: $kg,\n"
            "  event_type: row.event_type, subject_id: row.subject_id,\n"
            "  attr: row.attr, object_repr: row.object_repr,\n"
            "  old_id: row.old_id, new_id: row.new_id,\n"
            "  reason: row.reason, source: row.source,\n"
            "  fact_hash: row.fact_hash, ts: row.ts,\n"
            "  confidence: row.confidence\n"
            "})\n"
            "FOREACH (_ IN CASE WHEN e IS NULL THEN [] ELSE [1] END |\n"
            "  CREATE (p)-[:ABOUT]->(e)\n"
            ")\n"
            "RETURN count(*) AS n"
        )
        await self.execute_write(cypher, {"rows": list(rows)})

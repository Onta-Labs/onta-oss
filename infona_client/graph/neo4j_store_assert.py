"""ADR 0013 Assertion-model writes + typed reads for Neo4jGraphSession.

Cypher strings are copied verbatim — do not change query semantics.
"""

from __future__ import annotations

from typing import Any, Sequence

from infona_client.graph.store import GraphRecord


class Neo4jAssertMixin:
    """Class / Property / Assertion writes and explore/history reads."""

    async def write_merge_class(
        self,
        *,
        class_id: str,
        name: str,
        layer: str = "tenant",
    ) -> list[GraphRecord]:
        cypher = (
            "MERGE (c:Class {tenant_id: $tenant_id, kg: $kg, id: $class_id})\n"
            "ON CREATE SET c.name = $name, c.layer = $layer\n"
            "ON MATCH SET c.name = coalesce($name, c.name),\n"
            "             c.layer = coalesce($layer, c.layer)\n"
            "RETURN c.id AS id, c.name AS name, c.layer AS layer"
        )
        return await self.execute_write(
            cypher, {"class_id": class_id, "name": name, "layer": layer}
        )

    async def write_merge_property(
        self,
        *,
        property_id: str,
        name: str,
        kind: str = "datatype",
        layer: str = "tenant",
    ) -> list[GraphRecord]:
        cypher = (
            "MERGE (p:Property {tenant_id: $tenant_id, kg: $kg, id: $property_id})\n"
            "ON CREATE SET p.name = $name, p.kind = $kind, p.layer = $layer\n"
            "ON MATCH SET p.name = coalesce($name, p.name),\n"
            "             p.kind = coalesce($kind, p.kind),\n"
            "             p.layer = coalesce($layer, p.layer)\n"
            "RETURN p.id AS id, p.name AS name, p.kind AS kind"
        )
        return await self.execute_write(
            cypher,
            {
                "property_id": property_id,
                "name": name,
                "kind": kind,
                "layer": layer,
            },
        )

    async def write_subclass_of(
        self, child_class_id: str, parent_class_id: str
    ) -> None:
        cypher = (
            "MATCH (child:Class {tenant_id: $tenant_id, kg: $kg, id: $child_id})\n"
            "MATCH (parent:Class {tenant_id: $tenant_id, kg: $kg, id: $parent_id})\n"
            "OPTIONAL MATCH (child)-[old:SUBCLASS_OF]->(:Class {tenant_id: $tenant_id, kg: $kg})\n"
            "DELETE old\n"
            "MERGE (child)-[:SUBCLASS_OF]->(parent)\n"
            "RETURN child.id AS id"
        )
        await self.execute_write(
            cypher, {"child_id": child_class_id, "parent_id": parent_class_id}
        )

    async def write_clear_class_subclass(self, child_class_id: str) -> None:
        """Drop Class-level SUBCLASS_OF edges for ``child_class_id``."""
        cypher = (
            "MATCH (child:Class {tenant_id: $tenant_id, kg: $kg, id: $child_id})\n"
            "OPTIONAL MATCH (child)-[old:SUBCLASS_OF]->(:Class {tenant_id: $tenant_id, kg: $kg})\n"
            "DELETE old\n"
            "RETURN child.id AS id"
        )
        await self.execute_write(cypher, {"child_id": child_class_id})

    async def write_subproperty_of(
        self, child_prop_id: str, parent_prop_id: str
    ) -> None:
        cypher = (
            "MATCH (child:Property {tenant_id: $tenant_id, kg: $kg, id: $child_id})\n"
            "MATCH (parent:Property {tenant_id: $tenant_id, kg: $kg, id: $parent_id})\n"
            "OPTIONAL MATCH (child)-[old:SUBPROPERTY_OF]->(:Property {tenant_id: $tenant_id, kg: $kg})\n"
            "DELETE old\n"
            "MERGE (child)-[:SUBPROPERTY_OF]->(parent)\n"
            "RETURN child.id AS id"
        )
        await self.execute_write(
            cypher, {"child_id": child_prop_id, "parent_id": parent_prop_id}
        )

    async def write_instance_of(self, entity_id: str, class_id: str) -> None:
        cypher = (
            "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg, id: $entity_id})\n"
            "MATCH (c:Class {tenant_id: $tenant_id, kg: $kg, id: $class_id})\n"
            "MERGE (e)-[:INSTANCE_OF]->(c)\n"
            "RETURN e.id AS id"
        )
        await self.execute_write(
            cypher, {"entity_id": entity_id, "class_id": class_id}
        )

    async def write_assertion(
        self,
        *,
        assertion_id: str,
        subject_id: str,
        property_id: str,
        property_name: str,
        property_kind: str = "datatype",
        object_id: str | None = None,
        object_class_id: str | None = None,
        literal_value: Any = None,
        literal_datatype: str | None = None,
        source_url: str | None = None,
        verified_at: str | None = None,
        run_id: str | None = None,
        confidence: float | None = None,
        provenance: str | None = None,
        ts: str | None = None,
    ) -> list[GraphRecord]:
        """MERGE Assertion + SUBJECT / PREDICATE / OBJECT|OBJECT_CLASS links."""
        # Denormalized subject_id / property_id / object_id on the node for indexes.
        cypher = (
            "MATCH (s:Entity {tenant_id: $tenant_id, kg: $kg, id: $subject_id})\n"
            "MATCH (p:Property {tenant_id: $tenant_id, kg: $kg, id: $property_id})\n"
            "MERGE (a:Assertion {tenant_id: $tenant_id, kg: $kg, id: $assertion_id})\n"
            "SET a.subject_id = $subject_id,\n"
            "    a.property_id = $property_id,\n"
            "    a.object_id = $object_id,\n"
            "    a.literal_value = $literal_value,\n"
            "    a.literal_datatype = $literal_datatype,\n"
            "    a.source_url = $source_url,\n"
            "    a.verified_at = $verified_at,\n"
            "    a.run_id = $run_id,\n"
            "    a.confidence = $confidence,\n"
            "    a.provenance = $provenance,\n"
            "    a.updated_at = $ts\n"
            "MERGE (a)-[:SUBJECT]->(s)\n"
            "MERGE (a)-[:PREDICATE]->(p)\n"
            "WITH a\n"
            "OPTIONAL MATCH (a)-[old_o:OBJECT]->()\n"
            "DELETE old_o\n"
            "WITH a\n"
            "OPTIONAL MATCH (a)-[old_c:OBJECT_CLASS]->()\n"
            "DELETE old_c\n"
            "WITH a\n"
            "FOREACH (_ IN CASE WHEN $object_id IS NULL THEN [] ELSE [1] END |\n"
            "  MERGE (o:Entity {tenant_id: $tenant_id, kg: $kg, id: $object_id})\n"
            "  MERGE (a)-[:OBJECT]->(o)\n"
            ")\n"
            "FOREACH (_ IN CASE WHEN $object_class_id IS NULL THEN [] ELSE [1] END |\n"
            "  MERGE (c:Class {tenant_id: $tenant_id, kg: $kg, id: $object_class_id})\n"
            "  MERGE (a)-[:OBJECT_CLASS]->(c)\n"
            ")\n"
            "RETURN a.id AS assertion_id, a.subject_id AS subject_id"
        )
        return await self.execute_write(
            cypher,
            {
                "assertion_id": assertion_id,
                "subject_id": subject_id,
                "property_id": property_id,
                "object_id": object_id,
                "object_class_id": object_class_id,
                "literal_value": literal_value,
                "literal_datatype": literal_datatype,
                "source_url": source_url,
                "verified_at": verified_at,
                "run_id": run_id,
                "confidence": confidence,
                "provenance": provenance,
                "ts": ts,
            },
        )

    async def write_delete_assertions(
        self,
        *,
        subject_id: str,
        property_id: str | None = None,
        object_key: str | None = None,
    ) -> int:
        # Match object Entity id, literal, or OBJECT_CLASS id (type Assertions).
        cypher = (
            "MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg, subject_id: $subject_id})\n"
            "WHERE ($property_id IS NULL OR a.property_id = $property_id)\n"
            "  AND ($object_key IS NULL OR a.object_id = $object_key\n"
            "       OR toString(a.literal_value) = $object_key\n"
            "       OR EXISTS {\n"
            "         (a)-[:OBJECT_CLASS]->(:Class {\n"
            "           tenant_id: $tenant_id, kg: $kg, id: $object_key\n"
            "         })\n"
            "       })\n"
            "DETACH DELETE a\n"
            "RETURN count(*) AS n"
        )
        rows = await self.execute_write(
            cypher,
            {
                "subject_id": subject_id,
                "property_id": property_id,
                "object_key": object_key,
            },
        )
        # Evict derived INSTANCE_OF edges with no remaining type Assertion.
        prune = (
            "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg, id: $subject_id})\n"
            "      -[io:INSTANCE_OF]->(c:Class {tenant_id: $tenant_id, kg: $kg})\n"
            "WHERE NOT EXISTS {\n"
            "  MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg, subject_id: e.id})\n"
            "        -[:OBJECT_CLASS]->(c)\n"
            "}\n"
            "DELETE io\n"
            "RETURN count(*) AS n"
        )
        await self.execute_write(prune, {"subject_id": subject_id})
        if not rows:
            return 0
        return int(rows[0].get("n") or 0)

    async def read_subclass_closure(self, class_id: str) -> list[str]:
        from infona_client.graph.rdfs_helpers import CLASS_SUBCLASS_DESCENDANTS_CYPHER

        rows = await self.execute_read(
            CLASS_SUBCLASS_DESCENDANTS_CYPHER, {"class_id": class_id}
        )
        return [str(r.get("id")) for r in rows if r.get("id")]

    async def read_subproperty_closure(self, prop_id: str) -> list[str]:
        from infona_client.graph.rdfs_helpers import SUBPROPERTY_DESCENDANTS_CYPHER

        rows = await self.execute_read(
            SUBPROPERTY_DESCENDANTS_CYPHER, {"prop_id": prop_id}
        )
        return [str(r.get("id")) for r in rows if r.get("id")]

    async def read_entities_of_type(self, class_ids: Sequence[str]) -> list[str]:
        cypher = (
            "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})"
            "-[:INSTANCE_OF]->(c:Class {tenant_id: $tenant_id, kg: $kg})\n"
            "WHERE c.id IN $class_ids\n"
            "RETURN DISTINCT e.id AS id\n"
            "ORDER BY e.id"
        )
        rows = await self.execute_read(cypher, {"class_ids": list(class_ids)})
        return [str(r.get("id")) for r in rows if r.get("id")]

    async def read_assertions_for_subject(
        self, entity_id: str, *, prop_id: str | None = None
    ) -> list[dict[str, Any]]:
        from infona_client.graph.rdfs_helpers import ASSERTIONS_FOR_SUBJECT_CYPHER

        rows = await self.execute_read(
            ASSERTIONS_FOR_SUBJECT_CYPHER,
            {"entity_id": entity_id, "prop_id": prop_id},
        )
        return [r.to_dict() for r in rows]

    async def read_assertion_history(
        self,
        *,
        entity_id: str | None = None,
        prop_id: str | None = None,
        since: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Assertion provenance rows for GET /history (optional subject filter)."""
        lim = max(1, min(int(limit), 10000))
        cypher = """
MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg})
WHERE ($entity_id IS NULL OR a.subject_id = $entity_id)
  AND ($prop_id IS NULL OR a.property_id = $prop_id)
  AND (
    $since IS NULL
    OR (a.verified_at IS NOT NULL AND a.verified_at > $since)
  )
OPTIONAL MATCH (a)-[:OBJECT]->(o:Entity)
OPTIONAL MATCH (a)-[:OBJECT_CLASS]->(oc:Class)
RETURN a.id AS assertion_id,
       a.subject_id AS subject_id,
       a.property_id AS property_id,
       a.literal_value AS literal_value,
       a.literal_datatype AS literal_datatype,
       a.source_url AS source_url,
       a.verified_at AS verified_at,
       a.run_id AS run_id,
       a.confidence AS confidence,
       a.provenance AS provenance,
       o.id AS object_id,
       oc.id AS object_class_id
ORDER BY coalesce(a.verified_at, ''), a.property_id, a.subject_id, a.id
LIMIT $limit
""".strip()
        rows = await self.execute_read(
            cypher,
            {
                "entity_id": entity_id,
                "prop_id": prop_id,
                "since": since,
                "limit": lim,
            },
        )
        return [r.to_dict() for r in rows]

    async def read_list_entities_by_label(
        self,
        label: str,
        *,
        after_id: str | None = None,
        limit: int = 50,
    ) -> list[GraphRecord]:
        """List entities carrying a sanitized domain label (E5 explore).

        Label tokens are re-validated via :func:`sanitize_domain_label` before
        interpolation — Neo4j cannot parameterize labels.
        """
        from infona_client.graph.labels import sanitize_domain_label

        safe = sanitize_domain_label(label)
        cypher = (
            f"MATCH (e:Entity:`{safe}` {{tenant_id: $tenant_id, kg: $kg}})\n"
            "WHERE $after_id IS NULL OR e.id > $after_id\n"
            "RETURN e.id AS id, e.tenant_id AS tenant_id, e.kg AS kg,\n"
            "       e.primary_type AS primary_type, e.name AS name, e.source AS source\n"
            "ORDER BY e.id\n"
            "LIMIT $limit"
        )
        return await self.execute_read(
            cypher, {"after_id": after_id, "limit": int(limit)}
        )

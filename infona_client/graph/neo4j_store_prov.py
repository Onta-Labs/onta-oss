"""Provenance, value-history, and citation writes for Neo4jGraphSession.

Cypher strings are copied verbatim — do not change query semantics.
"""

from __future__ import annotations

from typing import Any, Mapping

from infona_client.graph.store import GraphRecord


class Neo4jProvMixin:
    """ProvEvent / ValueHistory / AttrCitation + write_get_entity."""

    async def write_prov_event(
        self,
        *,
        event_type: str,
        subject_id: str,
        attr: str | None = None,
        object_repr: str | None = None,
        old_id: str | None = None,
        new_id: str | None = None,
        reason: str = "",
        source: str | None = None,
        fact_hash: str | None = None,
        ts: str | None = None,
        confidence: float | None = None,
    ) -> None:
        # OPTIONAL MATCH so tombstones never re-MERGE a deleted Entity; ABOUT
        # is created only when the subject still exists (subject_id is durable).
        cypher = (
            "OPTIONAL MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg, id: $subject_id})\n"
            "CREATE (p:ProvEvent {\n"
            "  tenant_id: $tenant_id, kg: $kg,\n"
            "  event_type: $event_type, subject_id: $subject_id,\n"
            "  attr: $attr, object_repr: $object_repr,\n"
            "  old_id: $old_id, new_id: $new_id,\n"
            "  reason: $reason, source: $source,\n"
            "  fact_hash: $fact_hash, ts: $ts,\n"
            "  confidence: $confidence\n"
            "})\n"
            "FOREACH (_ IN CASE WHEN e IS NULL THEN [] ELSE [1] END |\n"
            "  CREATE (p)-[:ABOUT]->(e)\n"
            ")\n"
            "RETURN p.subject_id AS subject_id"
        )
        await self.execute_write(
            cypher,
            {
                "subject_id": subject_id,
                "event_type": event_type,
                "attr": attr,
                "object_repr": object_repr,
                "old_id": old_id,
                "new_id": new_id,
                "reason": reason,
                "source": source,
                "fact_hash": fact_hash,
                "ts": ts,
                "confidence": confidence,
            },
        )

    async def write_value_history(
        self,
        *,
        subject_id: str,
        predicate: str,
        old_value: str,
        new_value: str,
        changed_at: str,
    ) -> None:
        """Append a ``:ValueHistory`` old→new node (ONTA-236 / ONTA-536)."""
        cypher = (
            "CREATE (v:ValueHistory {\n"
            "  tenant_id: $tenant_id, kg: $kg,\n"
            "  subject_id: $subject_id, predicate: $predicate,\n"
            "  old_value: $old_value, new_value: $new_value,\n"
            "  changed_at: $changed_at\n"
            "})\n"
            "WITH v\n"
            "OPTIONAL MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg, id: $subject_id})\n"
            "FOREACH (_ IN CASE WHEN e IS NULL THEN [] ELSE [1] END |\n"
            "  CREATE (v)-[:ABOUT]->(e)\n"
            ")\n"
            "RETURN v.subject_id AS subject_id"
        )
        await self.execute_write(
            cypher,
            {
                "subject_id": subject_id,
                "predicate": predicate,
                "old_value": old_value,
                "new_value": new_value,
                "changed_at": changed_at,
            },
        )

    async def read_value_history(
        self,
        *,
        subject_id: str | None = None,
        predicate: str | None = None,
        since: str | None = None,
        limit: int = 1000,
    ) -> list:
        """List ``:ValueHistory`` rows for this session scope, oldest → newest."""
        lim = max(1, min(int(limit), 10000))
        cypher = (
            "MATCH (v:ValueHistory {tenant_id: $tenant_id, kg: $kg})\n"
            "WHERE ($subject_id IS NULL OR v.subject_id = $subject_id)\n"
            "  AND ($predicate IS NULL OR v.predicate = $predicate\n"
            "       OR v.predicate ENDS WITH ('/' + $pred_leaf))\n"
            "  AND ($since IS NULL OR v.changed_at > $since)\n"
            "RETURN v.subject_id AS subject_id, v.subject_id AS subject,\n"
            "       v.predicate AS predicate, v.old_value AS old_value,\n"
            "       v.new_value AS new_value, v.changed_at AS changed_at\n"
            "ORDER BY v.changed_at ASC\n"
            "LIMIT $limit"
        )
        pred_leaf = ""
        if predicate:
            pred_leaf = predicate.rstrip("/").rsplit("/", 1)[-1]
        rows = await self.execute_read(
            cypher,
            {
                "subject_id": subject_id,
                "predicate": predicate,
                "pred_leaf": pred_leaf,
                "since": since,
                "limit": lim,
            },
        )
        return [r.to_dict() if hasattr(r, "to_dict") else dict(r) for r in rows]


    async def write_attr_citation(
        self,
        *,
        entity_id: str,
        attr: str,
        source_url: str | None = None,
        provenance: str | None = None,
        verified_at: str | None = None,
        value_hash: str = "",
    ) -> None:
        """MERGE ``:AttrCitation`` + ``HAS_CITATION`` (model §4.2)."""
        cypher = (
            "MERGE (e:Entity {tenant_id: $tenant_id, kg: $kg, id: $entity_id})\n"
            "MERGE (c:AttrCitation {\n"
            "  tenant_id: $tenant_id, kg: $kg,\n"
            "  entity_id: $entity_id, attr: $attr, value_hash: $value_hash\n"
            "})\n"
            "ON CREATE SET\n"
            "  c.source_url = $source_url,\n"
            "  c.provenance = $provenance,\n"
            "  c.verified_at = $verified_at\n"
            "ON MATCH SET\n"
            "  c.source_url = coalesce($source_url, c.source_url),\n"
            "  c.provenance = coalesce($provenance, c.provenance),\n"
            "  c.verified_at = coalesce($verified_at, c.verified_at)\n"
            "MERGE (e)-[:HAS_CITATION]->(c)\n"
            "RETURN c.entity_id AS entity_id, c.attr AS attr"
        )
        await self.execute_write(
            cypher,
            {
                "entity_id": entity_id,
                "attr": attr,
                "source_url": source_url,
                "provenance": provenance,
                "verified_at": verified_at,
                "value_hash": value_hash or "",
            },
        )

    async def write_get_entity(self, entity_id: str) -> Mapping[str, Any] | None:
        rows = await self.execute_template("entity_get", {"id": entity_id})
        if not rows:
            return None
        return rows[0].to_dict()

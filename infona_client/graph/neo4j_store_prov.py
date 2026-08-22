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


    async def write_suppression(
        self,
        *,
        mark_id: str,
        kind: str,
        statement_id: str = "",
        subject: str = "",
        predicate: str = "",
        object_repr: str = "",
        reason: str = "",
        suppressed_at: str = "",
        graph_uri: str = "",
    ) -> None:
        """MERGE one sticky ``:Suppression`` marker (ONTA-279 / E7 port).

        Identity is ``(tenant_id, kg, mark_id)``; ``mark_id`` is the RDF
        mark-node URI, already ``sha1``-keyed on ``(s, p, o)`` for a fact mark
        and on ``(s)`` for an entity mark, so re-retracting the same thing is
        idempotent and the two kinds can never share a node.

        Deliberately standalone: NO ``[:ABOUT]->(:Entity)`` edge (unlike
        ``write_prov_event``). A marker is written for a value the caller may
        have just hard-deleted — and an ENTITY mark is a tombstone for a subject
        that must not be re-minted — so an edge would either resurrect the node
        or make the mark unreadable exactly when it matters.
        """
        cypher = (
            "MERGE (s:Suppression {tenant_id: $tenant_id, kg: $kg, mark_id: $mark_id})\n"
            "SET s.kind = $kind,\n"
            "    s.statement_id = $statement_id,\n"
            "    s.subject = $subject,\n"
            "    s.predicate = $predicate,\n"
            "    s.object_repr = $object_repr,\n"
            "    s.reason = coalesce($reason, s.reason),\n"
            "    s.suppressed_at = coalesce($suppressed_at, s.suppressed_at),\n"
            "    s.graph_uri = coalesce($graph_uri, s.graph_uri)\n"
            "RETURN s.mark_id AS mark_id"
        )
        await self.execute_write(
            cypher,
            {
                "mark_id": mark_id,
                "kind": kind,
                "statement_id": statement_id or "",
                "subject": subject or "",
                "predicate": predicate or "",
                "object_repr": object_repr or "",
                "reason": reason or None,
                "suppressed_at": suppressed_at or None,
                "graph_uri": graph_uri or None,
            },
        )

    async def read_suppressions(
        self,
        *,
        kind: str | None = None,
        subject: str | None = None,
        predicate: str | None = None,
    ) -> list:
        """Suppression marks in THIS session's ``(tenant_id, kg)`` scope.

        The scope map is unconditional — a mark written in one workspace must
        never withhold (or expose) a value in another. ``kind`` keeps a
        ``(s, p, o)`` fact mark and an entity tombstone from ever answering each
        other's question, the same separation the RDF predicates gave.
        """
        cypher = (
            "MATCH (s:Suppression {tenant_id: $tenant_id, kg: $kg})\n"
            "WHERE ($kind IS NULL OR s.kind = $kind)\n"
            "  AND ($subject IS NULL OR s.subject = $subject)\n"
            "  AND ($predicate IS NULL OR s.predicate = $predicate)\n"
            "RETURN s.mark_id AS mark_id, s.kind AS kind,\n"
            "       s.statement_id AS statement_id, s.subject AS subject,\n"
            "       s.predicate AS predicate, s.object_repr AS object_repr,\n"
            "       s.reason AS reason, s.suppressed_at AS suppressed_at,\n"
            "       s.graph_uri AS graph_uri\n"
            "ORDER BY s.mark_id"
        )
        rows = await self.execute_read(
            cypher,
            {"kind": kind, "subject": subject, "predicate": predicate},
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

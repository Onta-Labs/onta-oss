"""Neo4j ``:ValidityInterval`` natives (ONTA-277 / E7 port).

Standalone node — NO ``[:ABOUT]->(:Entity)`` edge. MERGE identity is
``(tenant_id, kg, interval_id)``. Every Cypher string includes ``$tenant_id``
and ``$kg`` so session scope enforcement accepts it.
"""

from __future__ import annotations


class Neo4jValidityMixin:
    """Session natives for ``:ValidityInterval`` companions."""

    async def write_validity_interval(
        self,
        *,
        interval_id: str,
        subject: str = "",
        predicate: str = "",
        object_repr: str = "",
        valid_from: str = "",
        valid_to: str = "",
        superseded_by: str = "",
        status: str = "",
        statement_id: str = "",
        graph_uri: str = "",
    ) -> None:
        """MERGE one ``:ValidityInterval`` (ONTA-277 / E7 port).

        Identity is ``(tenant_id, kg, interval_id)``; ``interval_id`` is the RDF
        interval-node URI, already ``sha1``-keyed on ``(s, p, o)``, so closing a
        fact MERGEs onto the node that was opened.

        Empty optional fields pass as ``None`` so ``coalesce`` keeps an existing
        closure — matching SPARQL open-interval semantics. Reopen is a separate
        native that clears ``valid_to`` / ``superseded_by`` / ``status``.

        Deliberately standalone: NO ``[:ABOUT]->(:Entity)`` edge.
        """
        cypher = (
            "MERGE (v:ValidityInterval "
            "{tenant_id: $tenant_id, kg: $kg, interval_id: $interval_id})\n"
            "SET v.subject = $subject,\n"
            "    v.predicate = $predicate,\n"
            "    v.object_repr = $object_repr,\n"
            "    v.valid_from = coalesce($valid_from, v.valid_from),\n"
            "    v.valid_to = coalesce($valid_to, v.valid_to),\n"
            "    v.superseded_by = coalesce($superseded_by, v.superseded_by),\n"
            "    v.status = coalesce($status, v.status),\n"
            "    v.statement_id = coalesce($statement_id, v.statement_id),\n"
            "    v.graph_uri = coalesce($graph_uri, v.graph_uri)\n"
            "RETURN v.interval_id AS interval_id"
        )
        await self.execute_write(
            cypher,
            {
                "interval_id": interval_id,
                "subject": subject or "",
                "predicate": predicate or "",
                "object_repr": object_repr or "",
                "valid_from": valid_from or None,
                "valid_to": valid_to or None,
                "superseded_by": superseded_by or None,
                "status": status or None,
                "statement_id": statement_id or None,
                "graph_uri": graph_uri or None,
            },
        )

    async def read_validity_intervals(
        self,
        *,
        subject: str | None = None,
        predicate: str | None = None,
    ) -> list:
        """Validity intervals in THIS session's ``(tenant_id, kg)`` scope."""
        cypher = (
            "MATCH (v:ValidityInterval {tenant_id: $tenant_id, kg: $kg})\n"
            "WHERE ($subject IS NULL OR v.subject = $subject)\n"
            "  AND ($predicate IS NULL OR v.predicate = $predicate)\n"
            "RETURN v.interval_id AS interval_id, v.subject AS subject,\n"
            "       v.predicate AS predicate, v.object_repr AS object_repr,\n"
            "       coalesce(v.valid_from, '') AS valid_from,\n"
            "       coalesce(v.valid_to, '') AS valid_to,\n"
            "       coalesce(v.superseded_by, '') AS superseded_by,\n"
            "       coalesce(v.status, '') AS status,\n"
            "       coalesce(v.statement_id, '') AS statement_id,\n"
            "       coalesce(v.graph_uri, '') AS graph_uri\n"
            "ORDER BY v.interval_id"
        )
        rows = await self.execute_read(
            cypher,
            {"subject": subject, "predicate": predicate},
        )
        return [r.to_dict() if hasattr(r, "to_dict") else dict(r) for r in rows]

    async def reopen_validity_interval(
        self,
        *,
        subject: str,
        predicate: str,
        object_repr: str,
    ) -> None:
        """Clear the three closure fields so the value is current again (ONTA-277)."""
        cypher = (
            "MATCH (v:ValidityInterval {tenant_id: $tenant_id, kg: $kg})\n"
            "WHERE v.subject = $subject\n"
            "  AND v.predicate = $predicate\n"
            "  AND v.object_repr = $object_repr\n"
            "SET v.valid_to = null,\n"
            "    v.superseded_by = null,\n"
            "    v.status = null\n"
            "RETURN v.interval_id AS interval_id"
        )
        await self.execute_write(
            cypher,
            {
                "subject": subject,
                "predicate": predicate,
                "object_repr": object_repr,
            },
        )

"""Suppression-marker storage for the in-memory GraphStore test double.

Mirrors ``neo4j_store_prov.Neo4jProvMixin``'s ``write_suppression`` /
``read_suppressions`` semantics exactly — same MERGE identity
``(tenant_id, kg, mark_id)``, same ``kind`` discrimination, same
``(tenant_id, kg)`` isolation on every read — so the hermetic suite exercises the
shipped contract rather than a lookalike (ONTA-279 / E7).
"""

from __future__ import annotations

from typing import Any

from infona_client.graph.memory_store_rows import _SuppressionRow


class MemorySuppressionMixin:
    """``:Suppression`` node storage + scoped reads."""

    def snapshot_suppressions(self) -> list[dict[str, Any]]:
        """Every mark in the store, unscoped — a TEST helper (peer of
        ``snapshot_prov`` / ``snapshot_citations``), never a read path."""
        return [dict(row.__dict__) for row in self._suppressions.values()]

    def _upsert_suppression(self, row: _SuppressionRow) -> None:
        """MERGE on ``(tenant_id, kg, mark_id)``; later non-empty fields win.

        Idempotent by construction: the mark id is the ``sha1``-keyed RDF mark
        node URI, so re-retracting the same value updates one row instead of
        appending a duplicate.
        """
        key = (row.tenant_id, row.kg, row.mark_id)
        existing = self._suppressions.get(key)
        if existing is None:
            self._suppressions[key] = row
            return
        existing.kind = row.kind or existing.kind
        for field in (
            "statement_id",
            "subject",
            "predicate",
            "object_repr",
            "reason",
            "suppressed_at",
            "graph_uri",
        ):
            value = getattr(row, field)
            if value:
                setattr(existing, field, value)

    def _list_suppressions(
        self,
        tenant_id: str,
        kg: str,
        *,
        kind: str | None = None,
        subject: str | None = None,
        predicate: str | None = None,
    ) -> list[dict[str, Any]]:
        """Marks in THIS ``(tenant_id, kg)`` scope only.

        The scope filter is unconditional — a suppression written in one
        workspace must never withhold (or expose) a value in another.
        """
        out: list[dict[str, Any]] = []
        for row in self._suppressions.values():
            if row.tenant_id != tenant_id or row.kg != kg:
                continue
            if kind is not None and row.kind != kind:
                continue
            if subject is not None and row.subject != subject:
                continue
            if predicate is not None and row.predicate != predicate:
                continue
            out.append(row.as_record().to_dict())
        out.sort(key=lambda d: str(d.get("mark_id") or ""))
        return out

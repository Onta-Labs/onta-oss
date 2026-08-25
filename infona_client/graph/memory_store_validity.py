"""Valid-time interval storage for the in-memory GraphStore test double.

Mirrors ``neo4j_store_validity.Neo4jValidityMixin`` — same MERGE identity
``(tenant_id, kg, interval_id)``, same ``(tenant_id, kg)`` isolation on every
read — so the hermetic suite exercises the shipped contract (ONTA-277 / E7).
"""

from __future__ import annotations

from typing import Any

from infona_client.graph.memory_store_rows import _ValidityRow


class MemoryValidityMixin:
    """``:ValidityInterval`` node storage + scoped reads."""

    def snapshot_validity(self) -> list[dict[str, Any]]:
        """Every interval in the store, unscoped — a TEST helper, never a read path."""
        return [dict(row.__dict__) for row in self._validity.values()]

    def _upsert_validity(self, row: _ValidityRow) -> None:
        """MERGE on ``(tenant_id, kg, interval_id)``; later non-empty fields win.

        Empty ``valid_to`` does NOT clear an existing closure — that is the
        SPARQL open-interval semantics (append ``validFrom``, leave ``validTo``).
        :meth:`_reopen_validity` is what clears the three closure fields.
        """
        key = (row.tenant_id, row.kg, row.interval_id)
        existing = self._validity.get(key)
        if existing is None:
            self._validity[key] = row
            return
        for field in (
            "subject",
            "predicate",
            "object_repr",
            "valid_from",
            "valid_to",
            "superseded_by",
            "status",
            "statement_id",
            "graph_uri",
        ):
            value = getattr(row, field)
            if value:
                setattr(existing, field, value)

    def _list_validity(
        self,
        tenant_id: str,
        kg: str,
        *,
        subject: str | None = None,
        predicate: str | None = None,
    ) -> list[dict[str, Any]]:
        """Intervals in THIS ``(tenant_id, kg)`` scope only."""
        out: list[dict[str, Any]] = []
        for row in self._validity.values():
            if row.tenant_id != tenant_id or row.kg != kg:
                continue
            if subject is not None and row.subject != subject:
                continue
            if predicate is not None and row.predicate != predicate:
                continue
            out.append(row.as_record().to_dict())
        out.sort(key=lambda d: str(d.get("interval_id") or ""))
        return out

    def _reopen_validity(
        self,
        tenant_id: str,
        kg: str,
        *,
        subject: str,
        predicate: str,
        object_repr: str,
    ) -> int:
        """Clear ``valid_to`` / ``superseded_by`` / ``status`` on matching rows."""
        n = 0
        for row in self._validity.values():
            if row.tenant_id != tenant_id or row.kg != kg:
                continue
            if row.subject != subject or row.predicate != predicate:
                continue
            if row.object_repr != object_repr:
                continue
            row.valid_to = ""
            row.superseded_by = ""
            row.status = ""
            n += 1
        return n


class MemoryValiditySessionMixin:
    """Session natives for validity intervals — mixed into ``MemoryGraphSession``.

    Lives here (not in ``memory_store_session.py``) so that file stays under the
    550-line cap.
    """

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
        """Persist one ``:ValidityInterval`` (ONTA-277). Standalone — no Entity edge."""
        t, k = self._scope_tk()
        self._store._upsert_validity(
            _ValidityRow(
                tenant_id=t,
                kg=k,
                interval_id=interval_id,
                subject=subject,
                predicate=predicate,
                object_repr=object_repr,
                valid_from=valid_from,
                valid_to=valid_to,
                superseded_by=superseded_by,
                status=status,
                statement_id=statement_id,
                graph_uri=graph_uri,
            )
        )

    async def read_validity_intervals(
        self,
        *,
        subject: str | None = None,
        predicate: str | None = None,
    ) -> list[dict[str, Any]]:
        """Validity intervals in THIS session's ``(tenant_id, kg)`` scope."""
        t, k = self._scope_tk()
        return self._store._list_validity(
            t, k, subject=subject, predicate=predicate
        )

    async def reopen_validity_interval(
        self,
        *,
        subject: str,
        predicate: str,
        object_repr: str,
    ) -> None:
        """Clear the three closure fields so the value is current again (ONTA-277)."""
        t, k = self._scope_tk()
        self._store._reopen_validity(
            t, k, subject=subject, predicate=predicate, object_repr=object_repr
        )

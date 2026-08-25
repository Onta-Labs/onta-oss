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

    def _closed_terms_for_prop(
        self, tenant_id: str, kg: str, subject: str, prop_key: str
    ) -> set[str]:
        """Closed object term-keys for ``(subject, prop_key)`` in this scope."""
        from infona_client.graph.current_facts import closed_terms_for_prop

        return closed_terms_for_prop(
            self._list_validity(tenant_id, kg, subject=subject),
            prop_key,
        )

    def _value_is_current(
        self,
        tenant_id: str,
        kg: str,
        subject: str,
        prop_key: str,
        value: Any,
    ) -> bool:
        """True when ``value`` has no closed interval (legacy / open)."""
        from infona_client.graph.current_facts import drop_closed_value

        if value is None:
            return False
        closed = self._closed_terms_for_prop(tenant_id, kg, subject, prop_key)
        return drop_closed_value(value, closed) is not None

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

    def _rewrite_validity_subject(
        self, tenant_id: str, kg: str, old_id: str, new_id: str
    ) -> None:
        """Retarget intervals whose ``subject`` is ``old_id`` onto ``new_id``.

        MERGE identity is ``(tenant_id, kg, interval_id)`` and ``interval_id``
        is ``sha1(s|p|o)``, so a subject-only SET would fork a new node on the
        next MERGE and leave closures invisible to current-read on the winner.
        Occupied destination keys coalesce like :meth:`_upsert_validity`
        (empty ``valid_to`` does not clear an existing closure).
        """
        from infona_client.graph.validity import _interval_uri, statement_id

        if not old_id or not new_id or old_id == new_id:
            return
        pending: list[_ValidityRow] = []
        drop: list[tuple[str, str, str]] = []
        for key, row in self._validity.items():
            if row.tenant_id != tenant_id or row.kg != kg:
                continue
            if row.subject != old_id:
                continue
            drop.append(key)
            pending.append(row)
        for key in drop:
            self._validity.pop(key, None)
        for row in pending:
            row.subject = new_id
            row.interval_id = _interval_uri(new_id, row.predicate, row.object_repr)
            row.statement_id = statement_id(
                new_id, row.predicate, row.object_repr
            )
            self._upsert_validity(row)


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

    async def rewrite_validity_subject(self, old_id: str, new_id: str) -> None:
        """Retarget ``:ValidityInterval`` rows from ``old_id`` onto ``new_id``."""
        t, k = self._scope_tk()
        self._store._rewrite_validity_subject(t, k, str(old_id), str(new_id))

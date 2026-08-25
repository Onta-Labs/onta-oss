"""Property-graph port of the valid-time interval (ONTA-277 / E7).

``graph/validity.py`` owns the SEMANTICS (currency = absence of ``valid_to``,
half-open ``[valid_from, valid_to)``, statuses). This module owns the STORAGE:
the RDF companion-graph shape that module builds is parsed here into a
:class:`ValidityRecord`, written through the ONE shared write path
(``kg_writer.insert_facts(validity_triples=…)`` / ``reopen_facts=…`` →
``pg_ops``), and read back per ``(tenant_id, kg)`` session scope.

**Why this module exists.** ``write_with_conflict_resolution`` /
``supersede_fact`` / ``retract_fact`` already BUILD ``validity_triples`` +
``reopen_facts``. Until this port, ``insert_facts`` warned and DROPPED them
(``insert_facts_companion_payload_not_ported``), and ``fetch_current_object_terms``
/ ``fetch_history`` SPARQL-queried a retired client and returned ``[]``. A
conflict "winner" receipt was theater: both values stayed current.

Representation (see ADR 0013 §4 companions — ``:Suppression`` is the peer this
follows)::

    (:ValidityInterval {
        tenant_id, kg,          # isolation scope — every read/write matches both
        interval_id,            # = the RDF interval-node URI; MERGE identity
        subject, predicate,
        object_repr,            # write-convention object TERM, verbatim
        valid_from, valid_to,   # valid_to present → CLOSED (not current)
        superseded_by, status, statement_id, graph_uri
    })

**Why a standalone node and not a flag on ``:Entity`` / ``:Assertion``.**
Currency is a fact-level interval, not an entity property. Stamping validity onto
the entity would collide across predicates and leak onto Explorer / NL dumps.
There is deliberately NO ``[:ABOUT]->(:Entity)`` edge (same reason as
``:Suppression``): the instance triple STAYS when the interval closes, and a
closed fact must remain queryable even if later readers do not join through the
entity.

**Currency.** A fact ``(s, p, o)`` is CURRENT iff it has NO validity node
carrying ``valid_to``. No node at all → current (legacy unannotated). Closed →
history only; the instance triple is untouched.

Boundary: OSS. Imports only stdlib / ``infona_client.*``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

import structlog

from infona_client.graph.validity import (
    VAL_GRAPH,
    VAL_OBJECT,
    VAL_PREDICATE,
    VAL_STATEMENT,
    VAL_STATUS,
    VAL_SUBJECT,
    VAL_SUPERSEDED_BY,
    VAL_VALID_FROM,
    VAL_VALID_TO,
    ValidityInterval,
)

logger = structlog.stdlib.get_logger("infona.graph.validity")

Triple = tuple[str, str, str]


class ValidityUnavailable(RuntimeError):
    """The validity store was ASKED and could not answer.

    Distinct from a ``None`` return, which means the store could not be asked
    at all (no configured store, no per-KG scope, no native reader).
    """


@dataclass(frozen=True, slots=True)
class ValidityRecord:
    """One validity interval, store-shaped (the parse of one RDF interval node)."""

    interval_id: str
    subject: str = ""
    predicate: str = ""
    object_repr: str = ""
    valid_from: str = ""
    valid_to: str = ""
    superseded_by: str = ""
    status: str = ""
    statement_id: str = ""
    graph_uri: str = ""

    @property
    def is_current(self) -> bool:
        """Open interval (no ``valid_to``) → the fact is still current."""
        return not self.valid_to


def parse_validity_records(triples: Iterable[Triple]) -> list[ValidityRecord]:
    """Group ``build_open_interval_triples`` / ``build_closed_interval_triples``
    output into store-shaped records.

    Grouping is by the interval-node URI (the builders' ``sha1``-keyed subject),
    so closing a fact MERGEs onto the same node that was opened. Unknown
    triples are ignored rather than raising: a caller mixing payloads must not
    fail a write.
    """
    buckets: dict[str, dict[str, str]] = {}
    field_of = {
        VAL_SUBJECT: "subject",
        VAL_PREDICATE: "predicate",
        VAL_OBJECT: "object_repr",
        VAL_STATEMENT: "statement_id",
        VAL_VALID_FROM: "valid_from",
        VAL_VALID_TO: "valid_to",
        VAL_SUPERSEDED_BY: "superseded_by",
        VAL_STATUS: "status",
        VAL_GRAPH: "graph_uri",
    }
    for triple in triples or ():
        if not triple or len(triple) < 3:
            continue
        node, pred, obj = str(triple[0]), str(triple[1]), str(triple[2])
        if not node:
            continue
        field = field_of.get(pred)
        if field is None:
            continue
        buckets.setdefault(node, {})[field] = obj

    out: list[ValidityRecord] = []
    for node, fields in buckets.items():
        subject = fields.get("subject", "")
        if not subject:
            continue
        out.append(
            ValidityRecord(
                interval_id=node,
                subject=subject,
                predicate=fields.get("predicate", ""),
                object_repr=fields.get("object_repr", ""),
                valid_from=_strip_datatype(fields.get("valid_from", "")),
                valid_to=_strip_datatype(fields.get("valid_to", "")),
                superseded_by=fields.get("superseded_by", ""),
                status=fields.get("status", ""),
                statement_id=fields.get("statement_id", ""),
                graph_uri=fields.get("graph_uri", ""),
            )
        )
    out.sort(key=lambda r: r.interval_id)
    return out


def _strip_datatype(value: str) -> str:
    """``2026-07-13T00:00:00+00:00^^…#dateTime`` → the lexical form.

    Timestamps are plain string properties on ``:ValidityInterval`` (as on
    ``:Suppression.suppressed_at``). ``object_repr`` deliberately KEEPS its
    ``^^`` tail — that is the value being matched.
    """
    if "^^" in value:
        return value.rsplit("^^", 1)[0]
    return value


def _session_for(instance_graph: str, *, store=None, session=None):
    """Scoped session for a validity read/write, or ``None`` when unscopable."""
    if session is not None:
        return session
    try:
        from infona_client.graph.kg_writer_session import _resolve_graph_session

        return _resolve_graph_session(store=store, instance_graph=instance_graph)
    except Exception:  # noqa: BLE001 — unconfigured / unscopable, not a read failure
        return None


async def apply_validity_records(session, records: list[ValidityRecord]) -> int:
    """Persist validity intervals through the session's native writer.

    Called ONLY from ``kg_writer._insert_facts_store``. Returns the number of
    records written (0 when the store implements no native writer).
    """
    native = getattr(session, "write_validity_interval", None)
    if not callable(native) or not records:
        return 0
    written = 0
    for rec in records:
        await native(
            interval_id=rec.interval_id,
            subject=rec.subject,
            predicate=rec.predicate,
            object_repr=rec.object_repr,
            valid_from=rec.valid_from,
            valid_to=rec.valid_to,
            superseded_by=rec.superseded_by,
            status=rec.status,
            statement_id=rec.statement_id,
            graph_uri=rec.graph_uri,
        )
        written += 1
    return written


async def apply_reopen_facts(session, facts: list[Triple]) -> int:
    """Clear ``valid_to`` / ``superseded_by`` / ``status`` on each fact's interval.

    ONTA-277: a previously-closed value re-asserted as current must genuinely
    reopen. Called LAST in the insert payload so it wins on resurrection.
    """
    native = getattr(session, "reopen_validity_interval", None)
    if not callable(native) or not facts:
        return 0
    written = 0
    for fact in facts:
        if not fact or len(fact) < 3:
            continue
        await native(
            subject=str(fact[0]),
            predicate=str(fact[1]),
            object_repr=str(fact[2]),
        )
        written += 1
    return written


async def persist_validity_payload(
    session,
    instance_graph: str,
    validity_triples: Optional[list[Triple]],
    reopen_facts: Optional[list[Triple]],
) -> None:
    """Parse + write intervals, then reopen. Logs, never raises.

    Reopen LAST so a resurrection in the same payload wins over a closed
    interval written for the same ``(s, p, o)``.
    """
    if validity_triples:
        records = parse_validity_records(validity_triples)
        written = await apply_validity_records(session, records)
        if records and not written:
            logger.warning(
                "insert_facts_validity_store_unsupported",
                instance_graph=instance_graph,
                records=len(records),
                detail="session implements no write_validity_interval; dropped",
            )
    if reopen_facts:
        n = await apply_reopen_facts(session, list(reopen_facts))
        if not n:
            logger.warning(
                "insert_facts_validity_reopen_unsupported",
                instance_graph=instance_graph,
                facts=len(reopen_facts),
                detail="session implements no reopen_validity_interval; dropped",
            )


async def read_validity_records(
    instance_graph: str,
    subject: str,
    predicate: str,
    *,
    store=None,
    session=None,
) -> Optional[list[ValidityRecord]]:
    """Validity rows for ``(subject, predicate)`` in this KG scope.

    Tri-state:

    * ``[]`` — the store answered; no interval for this pair.
    * ``[record, …]`` — the store answered.
    * ``None`` — the store could not be ASKED.

    Raises :class:`ValidityUnavailable` when the store WAS asked and FAILED.
    """
    sess = _session_for(instance_graph, store=store, session=session)
    if sess is None:
        return None
    native = getattr(sess, "read_validity_intervals", None)
    if not callable(native):
        return None
    try:
        rows = await native(subject=subject, predicate=predicate)
    except Exception as exc:  # noqa: BLE001 — asked and FAILED, never "unknown"
        logger.warning(
            "validity_store_read_failed",
            instance_graph=instance_graph,
            subject=subject,
            predicate=predicate,
            exc_info=True,
        )
        raise ValidityUnavailable(str(exc)) from exc
    return [_record_from_row(r) for r in _as_dicts(rows)]


def _record_from_row(row: dict) -> ValidityRecord:
    return ValidityRecord(
        interval_id=str(row.get("interval_id") or ""),
        subject=str(row.get("subject") or ""),
        predicate=str(row.get("predicate") or ""),
        object_repr=str(row.get("object_repr") or ""),
        valid_from=str(row.get("valid_from") or ""),
        valid_to=str(row.get("valid_to") or ""),
        superseded_by=str(row.get("superseded_by") or ""),
        status=str(row.get("status") or ""),
        statement_id=str(row.get("statement_id") or ""),
        graph_uri=str(row.get("graph_uri") or ""),
    )


def _as_dicts(rows: Any) -> list[dict]:
    """Normalize GraphRecord / Mapping rows to plain dicts."""
    out: list[dict] = []
    for row in rows or ():
        if isinstance(row, dict):
            out.append(row)
            continue
        to_dict = getattr(row, "to_dict", None)
        if callable(to_dict):
            out.append(to_dict())
        elif hasattr(row, "keys"):
            out.append(dict(row))
    return out


def _term_key(term: str) -> str:
    """Lexical form for matching: strip a typed-literal ``^^`` tail if present."""
    if "^^" in term:
        return term.rsplit("^^", 1)[0]
    return term


def _property_matches(property_id: str, predicate: str) -> bool:
    """True if an Assertion ``property_id`` is the same fact-predicate.

    Assertions store ``property_uri(leaf)``; callers pass the original RDF
    predicate (``…/attrs/<leaf>`` or ``…/onto/<leaf>``). Match exact URI or
    shared leaf.
    """
    if not property_id or not predicate:
        return False
    if property_id == predicate:
        return True
    leaf = predicate.rstrip("/").rsplit("/", 1)[-1]
    return bool(leaf) and property_id.rstrip("/").rsplit("/", 1)[-1] == leaf


def _object_reprs_from_assertion(row: dict) -> list[str]:
    """Reconstruct write-convention object term(s) from an Assertion row."""
    obj_id = row.get("object_id")
    if obj_id:
        return [str(obj_id)]
    class_id = row.get("object_class_id")
    if class_id:
        return [str(class_id)]
    lit = row.get("literal_value")
    if lit is None:
        return []
    dt = row.get("literal_datatype")
    values = lit if isinstance(lit, list) else [lit]
    out: list[str] = []
    for v in values:
        if v is None:
            continue
        if dt and dt != "http://www.w3.org/2001/XMLSchema#string":
            out.append(f"{v}^^{dt}")
        else:
            out.append(str(v))
    return out


async def _assertion_object_terms(
    session, subject: str, predicate: str
) -> Optional[list[str]]:
    """Instance-graph object terms for ``(subject, predicate)``, or ``None``."""
    reader = getattr(session, "read_assertions_for_subject", None)
    if not callable(reader):
        return None
    rows = await reader(subject)
    terms: list[str] = []
    seen: set[str] = set()
    for row in _as_dicts(rows):
        if not _property_matches(str(row.get("property_id") or ""), predicate):
            continue
        for term in _object_reprs_from_assertion(row):
            if not term or term in seen:
                continue
            seen.add(term)
            terms.append(term)
    return terms


async def fetch_current_from_store(
    instance_graph: str,
    subject: str,
    predicate: str,
    *,
    store=None,
    session=None,
) -> Optional[list[str]]:
    """Current object terms from GraphStore, or ``None`` if unaskable.

    Current = instance assertion terms whose validity row has empty/absent
    ``valid_to`` (no row ⇒ current). Raises :class:`ValidityUnavailable` when
    the store was asked and failed.
    """
    sess = _session_for(instance_graph, store=store, session=session)
    if sess is None:
        return None
    native = getattr(sess, "read_validity_intervals", None)
    if not callable(native) or not callable(
        getattr(sess, "read_assertions_for_subject", None)
    ):
        return None
    try:
        records = await read_validity_records(
            instance_graph, subject, predicate, session=sess
        )
        terms = await _assertion_object_terms(sess, subject, predicate)
    except ValidityUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "validity_store_current_read_failed",
            instance_graph=instance_graph,
            subject=subject,
            predicate=predicate,
            exc_info=True,
        )
        raise ValidityUnavailable(str(exc)) from exc
    if records is None or terms is None:
        return None
    closed = {
        _term_key(r.object_repr)
        for r in records
        if r.valid_to and r.object_repr
    }
    return [t for t in terms if _term_key(t) not in closed]


async def fetch_history_from_store(
    instance_graph: str,
    subject: str,
    predicate: str,
    *,
    store=None,
    session=None,
) -> Optional[list[ValidityInterval]]:
    """History rows from GraphStore (assertion ⟕ validity), or ``None``."""
    sess = _session_for(instance_graph, store=store, session=session)
    if sess is None:
        return None
    native = getattr(sess, "read_validity_intervals", None)
    if not callable(native) or not callable(
        getattr(sess, "read_assertions_for_subject", None)
    ):
        return None
    try:
        records = await read_validity_records(
            instance_graph, subject, predicate, session=sess
        )
        terms = await _assertion_object_terms(sess, subject, predicate)
    except ValidityUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "validity_store_history_read_failed",
            instance_graph=instance_graph,
            subject=subject,
            predicate=predicate,
            exc_info=True,
        )
        raise ValidityUnavailable(str(exc)) from exc
    if records is None or terms is None:
        return None
    by_obj: dict[str, ValidityRecord] = {}
    for rec in records:
        if rec.object_repr:
            by_obj[_term_key(rec.object_repr)] = rec
    out: list[ValidityInterval] = []
    for term in terms:
        rec = by_obj.get(_term_key(term))
        if rec is None:
            out.append(ValidityInterval(obj=term))
            continue
        out.append(
            ValidityInterval(
                obj=term,
                valid_from=rec.valid_from,
                valid_to=rec.valid_to,
                superseded_by=rec.superseded_by,
                status=rec.status,
            )
        )
    out.sort(key=lambda h: h.obj)
    return out


__all__ = [
    "ValidityRecord",
    "ValidityUnavailable",
    "apply_reopen_facts",
    "apply_validity_records",
    "fetch_current_from_store",
    "fetch_history_from_store",
    "parse_validity_records",
    "persist_validity_payload",
    "read_validity_records",
]

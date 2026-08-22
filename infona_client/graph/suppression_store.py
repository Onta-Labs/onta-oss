"""Property-graph port of the sticky suppression marker (ONTA-279 / E7).

``graph/suppression.py`` owns the SEMANTICS (what a mark means, how a fact is
keyed, when a value may be re-acquired). This module owns the STORAGE: the RDF
companion-graph shape that module builds is parsed here into a
:class:`SuppressionMark` record, written through the ONE shared write path
(``kg_writer.insert_facts(suppression_triples=…)`` → ``pg_ops``), and read back
per ``(tenant_id, kg)`` session scope.

**Why this module exists.** Both halves of ONTA-279 were dead on the shipped
Neo4j backend: ``kg_writer_session._warn_unported_companions`` dropped
``suppression_triples`` on the floor, and ``suppression.fetch_suppressed``'s
SPARQL read raised on a retired client and degraded to "nothing is suppressed".
Net effect: a user retracted a wrong value and the next enrichment refresh
silently re-acquired it. Porting only one half fixes nothing, so both land here.

Representation (see ADR 0013 §4 companions — ``:ProvEvent`` / ``:AttrCitation`` /
``:ValueHistory`` are the peers this follows)::

    (:Suppression {
        tenant_id, kg,          # isolation scope — every read/write matches both
        mark_id,                # = the RDF mark-node URI; MERGE identity
        kind,                   # "fact" | "entity"
        statement_id,           # sha1(s|p|o) for a fact mark, sha1(s) for entity
        subject, predicate,     # predicate is "" on an entity mark
        object_repr,            # write-convention object TERM, verbatim ("" on entity)
        reason, suppressed_at, graph_uri
    })

**Why a standalone node and not a flag on ``:Assertion``.** The marker must
survive the assertion being removed: ``retract_fact(hard_delete=True)`` calls
``delete_facts`` (which deletes the Assertion) and THEN writes the mark, so a
property riding the Assertion would be born dead. Suppression is also explicitly
"an orthogonal governance signal, not a removal" — it can outlive, precede, or
never coincide with a stored fact. For the same reason there is deliberately NO
``[:ABOUT]->(:Entity)`` edge (``:ProvEvent`` has one): an edge would either
re-MERGE a just-deleted entity or make the mark unreadable once the entity is
gone, and an ENTITY-level mark is precisely a tombstone for a subject that must
not come back.

**Value equality.** ``object_repr`` stores the object EXACTLY as the writer
wrote it — the same canonical write-convention term ``graph/queries._escape_value``
consumes and ``suppression._object_term`` reconstructs (``value^^datatype`` for a
typed literal, the bare lexical form for a plain literal, the bare IRI string for
a node-valued object). Matching is string equality on that term, which is
term-for-term what the SPARQL arm compared: suppressing ``42^^xsd:integer`` does
not suppress the plain string ``42``, and a relationship edge's target IRI is
matched as an IRI. The one residual ambiguity — a plain literal whose lexical
form happens to be an ``https://`` IRI — is inherited unchanged from the RDF arm
(``_object_term`` collapses a ``uri`` binding and a plain literal to the same
Python string), so this is parity, not a new gap.

Boundary: OSS. Imports only stdlib / ``infona_client.*``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

import structlog

logger = structlog.stdlib.get_logger("infona.graph.suppression")

Triple = tuple[str, str, str]

#: ``kind`` discriminator values. A FACT mark carries subject+predicate+object;
#: an ENTITY mark carries only the erased subject. The two never collide (they
#: MERGE on different ``mark_id`` prefixes) and a read for one never matches the
#: other, exactly as the RDF predicates (``sup:subject`` vs ``sup:entity``) did.
KIND_FACT = "fact"
KIND_ENTITY = "entity"


class SuppressionUnavailable(RuntimeError):
    """The suppression store was ASKED and could not answer.

    Deliberately distinct from a ``None`` return, which means the store could not
    be asked at all (no configured store, no per-KG scope, no native reader). The
    two demand opposite fail directions and collapsing them is what made this bug
    invisible: see :func:`infona_client.graph.suppression.is_suppressed`.
    """


@dataclass(frozen=True, slots=True)
class SuppressionMark:
    """One suppression marker, store-shaped (the parse of one RDF mark node)."""

    mark_id: str
    kind: str
    statement_id: str = ""
    subject: str = ""
    predicate: str = ""
    object_repr: str = ""
    reason: str = ""
    suppressed_at: str = ""
    graph_uri: str = ""


def parse_suppression_marks(triples: Iterable[Triple]) -> list[SuppressionMark]:
    """Group ``build_suppression_triples`` / ``build_entity_suppression_triples``
    output into store-shaped records.

    The inverse of the two builders in :mod:`infona_client.graph.suppression`,
    kept here (not there) so the semantics module stays free of storage concerns
    — the same split ``graph/provenance.py`` → ``pg_ops.parse_provenance_records``
    uses. Grouping is by the mark-node URI (the builders' ``sha1``-keyed subject),
    so re-suppressing the same value produces the same ``mark_id`` and MERGEs onto
    the same node. Triples that carry no recognizable suppression predicate are
    ignored rather than raising: a caller mixing payloads must not fail a write.
    """
    from infona_client.graph.suppression import (
        SUP_ENTITY,
        SUP_GRAPH,
        SUP_OBJECT,
        SUP_PREDICATE,
        SUP_REASON,
        SUP_STATEMENT,
        SUP_SUBJECT,
        SUP_SUPPRESSED_AT,
    )

    buckets: dict[str, dict[str, str]] = {}
    for triple in triples or ():
        if not triple or len(triple) < 3:
            continue
        node, pred, obj = str(triple[0]), str(triple[1]), str(triple[2])
        if not node:
            continue
        field = {
            SUP_SUBJECT: "subject",
            SUP_ENTITY: "entity",
            SUP_PREDICATE: "predicate",
            SUP_OBJECT: "object_repr",
            SUP_STATEMENT: "statement_id",
            SUP_REASON: "reason",
            SUP_SUPPRESSED_AT: "suppressed_at",
            SUP_GRAPH: "graph_uri",
        }.get(pred)
        if field is None:
            continue
        buckets.setdefault(node, {})[field] = obj

    out: list[SuppressionMark] = []
    for node, fields in buckets.items():
        entity = fields.pop("entity", "")
        subject = entity or fields.pop("subject", "")
        if not subject:
            continue
        # An entity mark is the subject-only tombstone; anything carrying a
        # predicate is a (s, p, o) fact mark. Keyed off the builder that made it,
        # never guessed from the node URI.
        kind = KIND_ENTITY if entity else KIND_FACT
        suppressed_at = fields.get("suppressed_at", "")
        out.append(
            SuppressionMark(
                mark_id=node,
                kind=kind,
                statement_id=fields.get("statement_id", ""),
                subject=subject,
                predicate=fields.get("predicate", ""),
                object_repr=fields.get("object_repr", ""),
                reason=fields.get("reason", ""),
                suppressed_at=_strip_datatype(suppressed_at),
                graph_uri=fields.get("graph_uri", ""),
            )
        )
    out.sort(key=lambda m: m.mark_id)
    return out


def _strip_datatype(value: str) -> str:
    """``2026-07-14T00:00:00+00:00^^…#dateTime`` → the lexical form.

    The RDF builder stamps ``suppressed_at`` with the typed-literal convention
    because it wrote into a triple store. In the property graph a timestamp is a
    plain string property (as on ``:ProvEvent.ts`` / ``:ValueHistory.changed_at``),
    so the datatype tail is dropped here rather than stored and re-stripped by
    every reader. Only the display/audit field is affected — ``object_repr``
    deliberately KEEPS its ``^^`` tail, because that is the value being matched.
    """
    if "^^" in value:
        return value.rsplit("^^", 1)[0]
    return value


def _session_for(instance_graph: str, *, store=None, session=None):
    """Scoped session for a suppression read/write, or ``None`` when unscopable.

    ``None`` means "there is no per-KG suppression scope here" (a bare tenant /
    companion / malformed graph URI, or no configured store) — deliberately
    distinguished by callers from "the store was asked and failed", because those
    two demand opposite fail directions (see :func:`read_suppressed_terms`).
    """
    if session is not None:
        return session
    try:
        from infona_client.graph.kg_writer_session import _resolve_graph_session

        return _resolve_graph_session(store=store, instance_graph=instance_graph)
    except Exception:  # noqa: BLE001 — unconfigured / unscopable, not a read failure
        return None


async def apply_suppression_marks(
    session, marks: list[SuppressionMark]
) -> int:
    """Persist suppression marks through the session's native writer.

    Called ONLY from ``kg_writer._insert_facts_store`` — suppression rides the
    one shared insert primitive (``insert_facts(suppression_triples=…)``) exactly
    as provenance and citation companions do, so this is not a second write path.
    Returns the number of marks written (0 when the store implements no native
    writer, which keeps older/partial stores degrading rather than crashing).
    """
    native = getattr(session, "write_suppression", None)
    if not callable(native) or not marks:
        return 0
    written = 0
    for mark in marks:
        await native(
            mark_id=mark.mark_id,
            kind=mark.kind,
            statement_id=mark.statement_id,
            subject=mark.subject,
            predicate=mark.predicate,
            object_repr=mark.object_repr,
            reason=mark.reason,
            suppressed_at=mark.suppressed_at,
            graph_uri=mark.graph_uri,
        )
        written += 1
    return written


async def read_suppressed_terms(
    instance_graph: str,
    subject: str,
    predicate: str,
    *,
    store=None,
    session=None,
) -> Optional[set[str]]:
    """Object TERMS suppressed for ``(subject, predicate)`` in this KG scope.

    Tri-state on purpose:

    * ``set()`` — the store answered; nothing is suppressed for this pair.
    * ``{term, …}`` — the store answered; these terms are suppressed.
    * ``None`` — the store could not be ASKED: no configured store, no per-KG
      scope in ``instance_graph``, or a store implementing no suppression reader.
      That is a STATIC property of the deployment — identical for every value on
      every run.

    Raises :class:`SuppressionUnavailable` when the store WAS asked and the read
    FAILED — a transient, per-call condition. Neither is ever expressed as
    "nothing is suppressed": that conflation is what let a retracted value come
    back.
    """
    sess = _session_for(instance_graph, store=store, session=session)
    if sess is None:
        return None
    native = getattr(sess, "read_suppressions", None)
    if not callable(native):
        return None
    try:
        rows = await native(kind=KIND_FACT, subject=subject, predicate=predicate)
    except Exception as exc:  # noqa: BLE001 — asked and FAILED, never "unknown"
        logger.warning(
            "suppression_store_read_failed",
            instance_graph=instance_graph,
            subject=subject,
            predicate=predicate,
            exc_info=True,
        )
        raise SuppressionUnavailable(str(exc)) from exc
    return {str(r.get("object_repr", "")) for r in _as_dicts(rows)}


async def read_suppressed_entity_uris(
    instance_graph: str,
    *,
    store=None,
    session=None,
) -> Optional[set[str]]:
    """Every ENTITY-level suppressed (erased) subject in this KG scope.

    Same contract as :func:`read_suppressed_terms`: ``None`` when the store could
    not be ASKED, :class:`SuppressionUnavailable` when it was asked and FAILED.
    """
    sess = _session_for(instance_graph, store=store, session=session)
    if sess is None:
        return None
    native = getattr(sess, "read_suppressions", None)
    if not callable(native):
        return None
    try:
        rows = await native(kind=KIND_ENTITY)
    except Exception as exc:  # noqa: BLE001 — asked and FAILED, never "unknown"
        logger.warning(
            "suppression_store_entity_read_failed",
            instance_graph=instance_graph,
            exc_info=True,
        )
        raise SuppressionUnavailable(str(exc)) from exc
    return {str(r.get("subject", "")) for r in _as_dicts(rows) if r.get("subject")}


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


__all__ = [
    "KIND_ENTITY",
    "KIND_FACT",
    "SuppressionMark",
    "SuppressionUnavailable",
    "apply_suppression_marks",
    "parse_suppression_marks",
    "read_suppressed_entity_uris",
    "read_suppressed_terms",
]

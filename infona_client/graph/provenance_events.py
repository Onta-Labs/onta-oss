"""Tombstone / rewrite / supersession / retraction / conflict-loss events.

Governance/undo substrate events (ADR 0007, ONTA-276, ONTA-277). Assertions
record a fact ARRIVING; these record a fact LEAVING or losing currency.

Look up patched names on :mod:`infona_client.graph.provenance` via ``_host()``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from infona_client.graph.provenance_uris import (
    EVENT_CONFLICT_LOSS,
    EVENT_RETRACT,
    EVENT_REWRITE,
    EVENT_SUPERSEDE,
    EVENT_TOMBSTONE,
    PROV_AFFECTED_TYPE,
    PROV_AUTHORITY,
    PROV_CONFIDENCE,
    PROV_EVENT,
    PROV_GRAPH,
    PROV_OBJECT,
    PROV_PREDICATE,
    PROV_REASON,
    PROV_REWRITTEN_TO,
    PROV_SOURCE,
    PROV_SUBJECT,
    PROV_SUPERSEDED_BY,
    PROV_TIMESTAMP,
    PROV_VALID_TO,
    _XSD,
    _event_uri,
)


def _host():
    """Call-time lookup of the public provenance module (monkeypatch surface)."""
    from infona_client.graph import provenance as _mod

    return _mod


def _event_common(
    node: str,
    event: str,
    subject: str,
    reason: str,
    ts: str,
    graph_uri: str,
    touched_types,
) -> list[tuple[str, str, str]]:
    triples = [
        (node, PROV_EVENT, event),
        (node, PROV_SUBJECT, subject),
    ]
    if reason:
        triples.append((node, PROV_REASON, reason))
    if ts:
        triples.append((node, PROV_TIMESTAMP, f"{ts}^^{_XSD}#dateTime"))
    if graph_uri:
        triples.append((node, PROV_GRAPH, graph_uri))
    for t in touched_types or ():
        if t:
            triples.append((node, PROV_AFFECTED_TYPE, t))
    return triples


def build_tombstone_triples(
    *,
    subjects=(),
    triples=(),
    graph_uri: str = "",
    reason: str = "",
    timestamp: datetime | str = "",
    touched_types=(),
) -> list[tuple[str, str, str]]:
    """Build the statement-metadata triples for a removal (``delete_facts``).

    One ``tombstone`` event node per removed **subject** (whole-subject delete)
    and per removed **triple** (concrete or predicate-scoped). Each records the
    subject (and predicate/object where applicable), the reason, a timestamp, the
    data graph, and any affected types — the mirror of
    :func:`build_provenance_triples`'s assertion node so an undo can see exactly
    what left the graph. ``o is None`` in a ``triples`` entry means a
    predicate-scoped removal (all objects of that ``(subject, predicate)``), so no
    ``prov:object`` is recorded. Returned triples target the companion provenance
    graph; the caller inserts them there.
    """
    ts = timestamp.isoformat() if isinstance(timestamp, datetime) else timestamp
    out: list[tuple[str, str, str]] = []
    for s in subjects or ():
        if not s:
            continue
        node = _event_uri(EVENT_TOMBSTONE, s, "", ts)
        out.extend(_event_common(node, EVENT_TOMBSTONE, s, reason, ts, graph_uri, touched_types))
    for triple in triples or ():
        s, p, o = triple
        if not s:
            continue
        node = _event_uri(EVENT_TOMBSTONE, s, f"{p}|{'' if o is None else o}", ts)
        node_triples = _event_common(node, EVENT_TOMBSTONE, s, reason, ts, graph_uri, touched_types)
        if p:
            node_triples.append((node, PROV_PREDICATE, p))
        if o is not None:
            node_triples.append((node, PROV_OBJECT, o))
        out.extend(node_triples)
    return out


def build_rewrite_triples(
    old_uri: str,
    new_uri: str,
    *,
    graph_uri: str = "",
    reason: str = "",
    timestamp: datetime | str = "",
    touched_types=(),
) -> list[tuple[str, str, str]]:
    """Build the statement-metadata triples for a subject rename (``rewrite_subject``).

    One ``rewrite`` event node mapping ``old_uri → new_uri`` (``prov:rewrittenTo``)
    so governance/undo can follow an ER merge, and derived indexes have a record
    of the re-key. Returned triples target the companion provenance graph.
    """
    ts = timestamp.isoformat() if isinstance(timestamp, datetime) else timestamp
    node = _event_uri(EVENT_REWRITE, old_uri, new_uri, ts)
    out = _event_common(node, EVENT_REWRITE, old_uri, reason, ts, graph_uri, touched_types)
    out.append((node, PROV_REWRITTEN_TO, new_uri))
    return out


def build_supersession_triples(
    subject: str,
    predicate: str,
    old_obj: str,
    new_obj: str,
    *,
    graph_uri: str = "",
    reason: str = "",
    timestamp: datetime | str = "",
    touched_types=(),
) -> list[tuple[str, str, str]]:
    """Build the governance event for a SUPERSESSION (ONTA-277).

    Records that ``(subject, predicate, old_obj)`` lost currency because
    ``(subject, predicate, new_obj)`` arrived — a companion to the always-on
    valid-time interval (``graph/validity.py``), giving governance/undo the "who
    replaced what, and why" record without re-deriving it from two interval nodes.
    The superseded fact is NOT deleted (supersession closes an interval); this
    event simply witnesses the closure. ``prov:supersededBy`` carries the
    replacement fact's ``statement_id``. Returned triples target the companion
    provenance graph; gated by ``INFONA_PROVENANCE_ENABLED`` at the call site.
    """
    ts = timestamp.isoformat() if isinstance(timestamp, datetime) else timestamp
    node = _event_uri(EVENT_SUPERSEDE, subject, f"{predicate}|{old_obj}|{new_obj}", ts)
    out = _event_common(node, EVENT_SUPERSEDE, subject, reason, ts, graph_uri, touched_types)
    if predicate:
        out.append((node, PROV_PREDICATE, predicate))
    if old_obj is not None:
        out.append((node, PROV_OBJECT, old_obj))
    out.append((node, PROV_SUPERSEDED_BY, _host().statement_id(subject, predicate, new_obj)))
    if ts:
        out.append((node, PROV_VALID_TO, f"{ts}^^{_XSD}#dateTime"))
    return out


def build_conflict_loss_triples(
    subject: str,
    predicate: str,
    loser_obj: str,
    winner_obj: str,
    *,
    graph_uri: str = "",
    reason: str = "",
    loser_source: str = "",
    loser_confidence: Optional[float] = None,
    loser_authority: str = "",
    timestamp: datetime | str = "",
    touched_types=(),
) -> list[tuple[str, str, str]]:
    """Build the governance event for a functional-attribute CONFLICT LOSS (ONTA-276).

    Records that ``(subject, predicate, loser_obj)`` lost a write-time conflict to
    ``(subject, predicate, winner_obj)`` — the winner was the higher-ranked fact
    under the conflict policy (authority + confidence + recency). A companion to
    the always-on valid-time closure (``graph/validity.py`` with
    ``STATUS_DEPRECATED``): the loser is NOT deleted, its interval is closed and it
    stays queryable, and this event witnesses "why it lost, and to what".
    ``prov:supersededBy`` carries the WINNER fact's ``statement_id``; ``prov:reason``
    the deciding axis; the loser's ``source`` / ``confidence`` / ``authority`` are
    recorded too so the losing claim's provenance is self-contained. Returned
    triples target the companion provenance graph; gated by
    ``INFONA_PROVENANCE_ENABLED`` at the call site.
    """
    ts = timestamp.isoformat() if isinstance(timestamp, datetime) else timestamp
    node = _event_uri(EVENT_CONFLICT_LOSS, subject, f"{predicate}|{loser_obj}|{winner_obj}", ts)
    out = _event_common(node, EVENT_CONFLICT_LOSS, subject, reason, ts, graph_uri, touched_types)
    if predicate:
        out.append((node, PROV_PREDICATE, predicate))
    if loser_obj is not None:
        out.append((node, PROV_OBJECT, loser_obj))
    out.append((node, PROV_SUPERSEDED_BY, _host().statement_id(subject, predicate, winner_obj)))
    if loser_source:
        out.append((node, PROV_SOURCE, loser_source))
    if loser_confidence is not None:
        out.append((node, PROV_CONFIDENCE, f"{loser_confidence}^^{_XSD}#float"))
    if loser_authority:
        out.append((node, PROV_AUTHORITY, loser_authority))
    if ts:
        out.append((node, PROV_VALID_TO, f"{ts}^^{_XSD}#dateTime"))
    return out


def build_retraction_triples(
    subject: str,
    predicate: str,
    obj: str,
    *,
    graph_uri: str = "",
    reason: str = "",
    timestamp: datetime | str = "",
    touched_types=(),
) -> list[tuple[str, str, str]]:
    """Build the governance event for a RETRACTION (ONTA-277).

    Records that ``(subject, predicate, obj)`` was explicitly asserted
    no-longer-true (distinct from supersession, which is driven by a replacement).
    The default retraction path closes the fact's valid-time interval rather than
    deleting it (history stays queryable), so this event witnesses the removal of
    currency; when a caller genuinely hard-deletes the triple, the removal also
    goes through ``delete_facts`` (which writes its own tombstone). Returned
    triples target the companion provenance graph; gated by
    ``INFONA_PROVENANCE_ENABLED`` at the call site.
    """
    ts = timestamp.isoformat() if isinstance(timestamp, datetime) else timestamp
    node = _event_uri(EVENT_RETRACT, subject, f"{predicate}|{'' if obj is None else obj}", ts)
    out = _event_common(node, EVENT_RETRACT, subject, reason, ts, graph_uri, touched_types)
    if predicate:
        out.append((node, PROV_PREDICATE, predicate))
    if obj is not None:
        out.append((node, PROV_OBJECT, obj))
    if ts:
        out.append((node, PROV_VALID_TO, f"{ts}^^{_XSD}#dateTime"))
    return out

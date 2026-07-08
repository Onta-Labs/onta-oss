"""Temporal value-history substrate (ONTA-236).

**What this answers:** "which attribute values changed since <date>, old → new,
with a change date." Today an attribute UPDATE overwrites in place
(``delete_facts`` of the old value + ``insert_facts`` of the new — see
``graph/kg_writer.py``), so the prior value is gone and the change is
unanswerable. This module records a **dated value-history entry** per change so
the transition is queryable as ``old → new`` with a timestamp.

**GENERAL — not price/model-specific.** A history entry is emitted for ANY
attribute of ANY type when its value actually CHANGES; the mechanism has no
knowledge of the domain. Discovery/enrichment invent whatever types they like;
this versions them all the same way.

Encoding decision (the same reasoning as ``graph/provenance.py``: Neptune has NO
RDF-star, so a triple cannot be annotated in place): a dedicated **companion
value-history named graph** per data graph (``<data-graph>/history``) holding one
version node per (subject, predicate, change) event. Chosen over a per-value
snapshot table or valid-time-stamped instance triples because it (a) composes
with the existing single-data-graph layout — the current instance triples stay
exactly where they are and always hold the CURRENT value, (b) mirrors the
provenance companion-graph pattern already in the codebase (one reader, one
writer, one clear undo story), and (c) makes "what changed since <date>" a plain
SELECT over one graph with a dated FILTER, rather than reconstructing history by
diffing snapshots.

For a value change on ``(subject, predicate)`` from ``old`` to ``new`` at
``changed_at`` the history graph holds::

    <https://cograph.tech/history/ver/{sha1(s|p|old|new|ts)}>
        hist:subject    <s> ;
        hist:predicate  <p> ;
        hist:oldValue   old ;                       # literal or URI, as written
        hist:newValue   new ;                       # literal or URI, as written
        hist:changedAt  "2026-07-08T…"^^xsd:dateTime .

``changedAt`` is a TYPED ``xsd:dateTime`` (like ``prov:timestamp`` and the
``<attr>_verified_at`` freshness stamp) so a "changed since <cutoff>" query can
FILTER it with a typed comparison and never silently drop a row on a type
mismatch (the ONTA-247 lesson).

**On the shared write path.** These triples are NOT written by a bespoke writer:
``kg_writer.delete_facts`` composes ``build_value_change_triples`` here and routes
the result through the same ``batched_insert_triples`` seam every other write
uses, into the history companion graph. This module only BUILDS triples and
QUERIES them (it constructs no raw instance-graph writes), exactly like
``graph/provenance.py`` — so it stays outside the write-path convergence guard's
concern the same way provenance does.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from cograph_client.graph.parser import parse_sparql_results
from cograph_client.graph.queries import _escape_value

HIST_NS = "https://cograph.tech/history/"

HIST_SUBJECT = f"{HIST_NS}subject"
HIST_PREDICATE = f"{HIST_NS}predicate"
HIST_OLD_VALUE = f"{HIST_NS}oldValue"
HIST_NEW_VALUE = f"{HIST_NS}newValue"
HIST_CHANGED_AT = f"{HIST_NS}changedAt"

_XSD = "http://www.w3.org/2001/XMLSchema"


def lexical_value(value: str) -> str:
    """The comparable / display lexical form of a stored object value.

    A value change must be detected and recorded on the SAME axis the user reads.
    Instance objects are written in this codebase's conventions —
    ``"92"^^xsd:integer`` (typed literal), ``<https://…/entities/…>`` (URI), or a
    plain string — but the SPARQL reader (``parse_sparql_results``) returns only
    the LEXICAL form (``92``, ``https://…``). So both sides of a comparison are
    normalized here to that lexical form:

    - ``value^^type`` → ``value`` (drop the datatype wrapper),
    - ``<uri>`` → ``uri`` (drop the angle brackets),
    - anything else → unchanged.

    Storing the lexical form (not the typed wrapper) also keeps the history record
    readable/queryable without the caller needing the datatype, and makes
    ``old == new`` a true no-op check regardless of how each side happened to be
    serialized.
    """
    if not isinstance(value, str):
        return value
    if "^^" in value:
        return value.rsplit("^^", 1)[0]
    if value.startswith("<") and value.endswith(">"):
        return value[1:-1]
    return value


def history_graph_uri(graph_uri: str) -> str:
    """Companion value-history graph for a data graph.

    Distinct from ``provenance_graph_uri``'s ``/provenance`` suffix so the two
    companion stores never collide, and — like the provenance suffix —
    ``parse_kg_graph_uri`` returns ``None`` for it (its kg segment carries a
    trailing ``/history``), so a history graph is never mistaken for an instance
    graph by the derived-index hooks.
    """
    return f"{graph_uri}/history"


def _version_uri(subject: str, predicate: str, old: str, new: str, ts: str) -> str:
    """Deterministic version-node URI: one per (fact, old→new, timestamp) event.

    Keyed over the raw strings as written to Neptune (typed-literal convention
    included) so re-running the identical change at the identical timestamp is
    idempotent (RDF set semantics), while two changes of the same fact at
    different times are distinct nodes.
    """
    vid = hashlib.sha1(
        f"{subject}|{predicate}|{old}|{new}|{ts}".encode("utf-8")
    ).hexdigest()
    return f"{HIST_NS}ver/{vid}"


def build_value_change_triples(
    subject: str,
    predicate: str,
    old_value: str,
    new_value: str,
    *,
    changed_at: datetime | str,
) -> list[tuple[str, str, str]]:
    """Build the version-node triples for ONE value change (``old → new``).

    Returned triples target the companion history graph
    (``history_graph_uri`` of the data graph) — the caller inserts them there via
    the shared batched-insert seam; the instance fact itself (already updated to
    the new value) is untouched.

    Args:
        old_value / new_value: exactly as written to Neptune (typed-literal
            convention included) so writer and reader agree on the values and the
            node id.
        changed_at: aware datetime or ISO-8601 string; stored as a TYPED
            ``xsd:dateTime`` so a "changed since <cutoff>" FILTER matches it.

    Returns an empty list when the (lexical) old and new values are equal (a no-op
    "change" is not versioned — see the first-insert / no-op contract) or when the
    subject / predicate is missing. Both values are stored in their LEXICAL form
    (``lexical_value``) so a change is detected on the axis the user reads and the
    record is queryable without the datatype wrapper.
    """
    if not subject or not predicate:
        return []
    old_lex = lexical_value(old_value)
    new_lex = lexical_value(new_value)
    if old_lex == new_lex:
        return []
    ts = changed_at.isoformat() if isinstance(changed_at, datetime) else str(changed_at)
    node = _version_uri(subject, predicate, old_lex, new_lex, ts)
    triples = [
        (node, HIST_SUBJECT, subject),
        (node, HIST_PREDICATE, predicate),
        (node, HIST_OLD_VALUE, old_lex),
        (node, HIST_NEW_VALUE, new_lex),
    ]
    if ts:
        triples.append((node, HIST_CHANGED_AT, f"{ts}^^{_XSD}#dateTime"))
    return triples


def value_history_query(
    graph_uri: str,
    *,
    subject: str | None = None,
    predicate: str | None = None,
    since: str | None = None,
    limit: int = 1000,
) -> str:
    """SELECT over the companion history graph, oldest → newest.

    Optionally narrowed to one ``subject`` and/or ``predicate``, and to changes
    ``since`` a cutoff (an ISO-8601 date/dateTime string, compared as a typed
    ``xsd:dateTime`` — the whole reason ``changedAt`` is typed). Ordered by
    ``changedAt`` ascending so a caller reads the transitions in the order they
    happened.
    """
    subj = _escape_value(subject) if subject else "?s"
    pred = _escape_value(predicate) if predicate else "?p"
    since_filter = ""
    if since:
        # Typed comparison — an untyped bound would be type-incompatible with the
        # typed changedAt literal and match nothing (ONTA-247). STRICTLY AFTER the
        # cutoff (`>`), so "changed since <last run at T>" excludes the boundary
        # itself and returns only genuinely newer transitions.
        since_filter = f'  FILTER(?changedAt > "{since}"^^<{_XSD}#dateTime>)\n'
    return (
        f"SELECT ?s ?p ?oldValue ?newValue ?changedAt "
        f"FROM <{history_graph_uri(graph_uri)}>\n"
        f"WHERE {{\n"
        f"  ?node <{HIST_SUBJECT}> {subj} ;\n"
        f"        <{HIST_PREDICATE}> {pred} ;\n"
        f"        <{HIST_OLD_VALUE}> ?oldValue ;\n"
        f"        <{HIST_NEW_VALUE}> ?newValue ;\n"
        f"        <{HIST_CHANGED_AT}> ?changedAt .\n"
        f"{since_filter}"
        f"}}\nORDER BY ?changedAt\nLIMIT {limit}"
    )


@dataclass
class ValueChange:
    """One versioned value transition read back from the history graph."""

    subject: str
    predicate: str
    old_value: str
    new_value: str
    changed_at: str


async def fetch_value_history(
    neptune,
    graph_uri: str,
    *,
    subject: str | None = None,
    predicate: str | None = None,
    since: str | None = None,
    limit: int = 1000,
) -> list[ValueChange]:
    """Read parsed value-history transitions (oldest → newest).

    ``graph_uri`` is the DATA graph; the companion history graph is derived. See
    :func:`value_history_query` for the ``subject`` / ``predicate`` / ``since``
    narrowing. Returns an empty list on any read failure so a history read never
    breaks a caller.
    """
    try:
        raw = await neptune.query(
            value_history_query(
                graph_uri,
                subject=subject,
                predicate=predicate,
                since=since,
                limit=limit,
            )
        )
    except Exception:  # noqa: BLE001 — a history read is informational, never load-bearing
        return []
    _, bindings = parse_sparql_results(raw)
    out: list[ValueChange] = []
    for row in bindings:
        out.append(
            ValueChange(
                subject=row.get("s", ""),
                predicate=row.get("p", ""),
                old_value=row.get("oldValue", ""),
                new_value=row.get("newValue", ""),
                changed_at=row.get("changedAt", ""),
            )
        )
    return out

"""Suppression list — a STICKY, reopen-PROOF retraction marker (ONTA-279).

**What this answers:** "was this exact ``(subject, predicate, object)`` fact
explicitly RETRACTED / suppressed, so a later refresh must NOT re-acquire it?"

The valid-time interval substrate (``graph/validity.py``) already lets a fact stop
being *current* — but a validity closure is deliberately REVERSIBLE: re-asserting
the same value clears its closure (``insert_facts(reopen_facts=…)`` →
``validity.reopen_interval_update``) so an oscillating value resurrects (ONTA-277).
That reversibility is exactly WRONG for a retraction: a value a user (or an
explicit retraction) deemed no-longer-true must NOT silently come back the next
time a scraper re-observes it. Suppression is the complementary, *irreversible*
signal — a value on the suppression list stays off no matter how many times a
refresh re-scrapes it, until an explicit un-suppress (or a ``user_assertion``
re-asserting that exact value) clears it.

**Why a SEPARATE companion graph (not another validity predicate).** A validity
node is keyed by ``sha1(s|p|o)`` and ``reopen_facts`` clears its closure
predicates on the SAME node. If suppression rode the validity node, the reopen
that resurrects an oscillating value would clear the suppression too — defeating
the whole point. Suppression therefore lives in its OWN companion named graph
(``<data-graph>/suppression``) that ``reopen_facts`` never touches, so a
suppression marker survives every interval reopen. This mirrors the
provenance / validity / history companion-graph pattern exactly:

- one node per ``(subject, predicate, object)`` fact, keyed by ``sha1(s|p|o)``;
- ``parse_kg_graph_uri`` returns ``None`` for the ``/suppression`` suffix (its kg
  segment carries a trailing ``/suppression``), so a suppression graph is never
  mistaken for an instance graph by the derived-index hooks;
- the whole namespace is classified internal by
  ``graph/predicates.py::is_internal_predicate`` (defense-in-depth), so a
  suppression predicate could never surface as a domain attribute.

For a suppressed fact ``(s, p, o)`` the suppression graph holds::

    <https://cograph.tech/suppression/mark/{sha1(s|p|o)}>
        sup:subject      <s> ;
        sup:predicate    <p> ;
        sup:object       o ;                       # literal or URI, as written
        sup:statement    "{sha1(s|p|o)}" ;
        sup:suppressedAt "2026-07-14T…"^^xsd:dateTime ;   # optional
        sup:reason       "user retraction" ;       # optional
        sup:graph        <data graph the fact lives in> .

**On the shared write path.** These triples are NOT written by a bespoke writer:
``kg_writer.insert_facts(suppression_triples=…)`` routes them through the same
``batched_insert_triples`` seam every other write uses, into the suppression
companion graph (exactly as it routes ``validity_triples`` to the validity
graph). This module only BUILDS triples and QUERIES them (it constructs no raw
instance-graph writes) and COMPOSES the ``graph/queries.py`` DELETE builder for an
un-suppress rather than hand-rolling SPARQL — exactly like ``graph/validity.py`` —
so it stays outside the write-path convergence guard's concern the same way that
module does.

Boundary: OSS. Imports only stdlib / ``cograph_client.*``.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from cograph_client.graph.queries import _escape_value, delete_node_predicates_query

# The suppression namespace. A whole-namespace exclusion is added to
# graph/predicates.py::is_internal_predicate for defense-in-depth (these
# predicates live in a separate companion graph and never appear on the instance
# graph, but classifying the namespace internal makes it structurally impossible
# for a suppression predicate to be surfaced as a domain attribute if one leaked).
SUPPRESSION_NS = "https://cograph.tech/suppression/"

SUP_SUBJECT = f"{SUPPRESSION_NS}subject"
SUP_PREDICATE = f"{SUPPRESSION_NS}predicate"
SUP_OBJECT = f"{SUPPRESSION_NS}object"
SUP_STATEMENT = f"{SUPPRESSION_NS}statement"
SUP_SUPPRESSED_AT = f"{SUPPRESSION_NS}suppressedAt"
SUP_REASON = f"{SUPPRESSION_NS}reason"
SUP_GRAPH = f"{SUPPRESSION_NS}graph"

_XSD = "http://www.w3.org/2001/XMLSchema"

# The predicates an un-suppress must clear off a mark node to fully remove it.
_MARK_PREDICATES = (
    SUP_SUBJECT,
    SUP_PREDICATE,
    SUP_OBJECT,
    SUP_STATEMENT,
    SUP_SUPPRESSED_AT,
    SUP_REASON,
    SUP_GRAPH,
)


def suppression_graph_uri(graph_uri: str) -> str:
    """Companion suppression graph for a data graph.

    Distinct from ``provenance_graph_uri``'s ``/provenance``,
    ``validity_graph_uri``'s ``/validity`` and ``history_graph_uri``'s
    ``/history`` suffixes so the companion stores never collide, and — like those
    — ``parse_kg_graph_uri`` returns ``None`` for it (its kg segment carries a
    trailing ``/suppression``), so a suppression graph is never mistaken for an
    instance graph by the derived-index hooks.
    """
    return f"{graph_uri}/suppression"


def statement_id(subject: str, predicate: str, obj: str) -> str:
    """Deterministic fact id: sha1 over the raw s|p|o strings as written.

    Identical keying to ``validity.statement_id`` / ``provenance.statement_id`` so
    a fact's suppression mark groups on the same id as its validity/provenance
    nodes.
    """
    return hashlib.sha1(f"{subject}|{predicate}|{obj}".encode("utf-8")).hexdigest()


def _mark_uri(subject: str, predicate: str, obj: str) -> str:
    """Suppression-mark node URI: one per ``(subject, predicate, object)`` fact,
    keyed by ``sha1(s|p|o)`` so re-suppressing the same value collides idempotently
    on the same node."""
    return f"{SUPPRESSION_NS}mark/{statement_id(subject, predicate, obj)}"


def _as_iso(ts: datetime | str) -> str:
    return ts.isoformat() if isinstance(ts, datetime) else str(ts)


def build_suppression_triples(
    subject: str,
    predicate: str,
    obj: str,
    *,
    suppressed_at: datetime | str = "",
    reason: str = "",
    graph_uri: str = "",
) -> list[tuple[str, str, str]]:
    """Build the suppression-mark triples for one retracted/suppressed fact.

    Returned triples target the companion suppression graph
    (``suppression_graph_uri`` of the data graph); the caller writes them via
    ``kg_writer.insert_facts(suppression_triples=…)``. The instance triple itself
    is untouched — suppression is a governance marker, not a removal (removal, when
    wanted, is the caller's separate ``delete_facts`` hard-delete). ``obj`` is
    stored EXACTLY as written to Neptune (typed-literal convention included) so a
    later ``is_suppressed`` check on the identical term matches. Returns an empty
    list when subject/predicate is missing.
    """
    if not subject or not predicate:
        return []
    node = _mark_uri(subject, predicate, obj)
    triples = [
        (node, SUP_SUBJECT, subject),
        (node, SUP_PREDICATE, predicate),
        (node, SUP_OBJECT, obj),
        (node, SUP_STATEMENT, statement_id(subject, predicate, obj)),
    ]
    if graph_uri:
        triples.append((node, SUP_GRAPH, graph_uri))
    if suppressed_at:
        triples.append((node, SUP_SUPPRESSED_AT, f"{_as_iso(suppressed_at)}^^{_XSD}#dateTime"))
    if reason:
        triples.append((node, SUP_REASON, reason))
    return triples


def clear_suppression_update(
    instance_graph: str, subject: str, predicate: str, obj: str
) -> str:
    """Build the update that CLEARS a value's suppression mark (un-suppress).

    The ONLY way a suppression is lifted: an explicit un-suppress, or a
    ``user_assertion`` re-asserting that exact value. Composes the
    ``graph/queries.py`` DELETE builder against the companion suppression graph —
    this module never hand-rolls raw SPARQL — so it stays free of write markers the
    way ``validity.reopen_interval_update`` does. Returns ``""`` when
    subject/predicate is missing (nothing to clear).
    """
    if not subject or not predicate:
        return ""
    node = _mark_uri(subject, predicate, obj)
    sup_graph = suppression_graph_uri(instance_graph)
    return delete_node_predicates_query(sup_graph, node, list(_MARK_PREDICATES))


def suppressed_objects_query(instance_graph: str, subject: str, predicate: str) -> str:
    """SELECT every SUPPRESSED object of ``(subject, predicate)``.

    Reads the companion suppression graph for all ``sup:object`` marked under this
    ``(subject, predicate)``. Used by :func:`fetch_suppressed` / :func:`is_suppressed`
    to decide whether a refresh may (re-)acquire a value.
    """
    sup_graph = suppression_graph_uri(instance_graph)
    s, p = _escape_value(subject), _escape_value(predicate)
    return (
        f"SELECT ?o WHERE {{\n"
        f"  GRAPH <{sup_graph}> {{\n"
        f"    ?node <{SUP_SUBJECT}> {s} ;\n"
        f"          <{SUP_PREDICATE}> {p} ;\n"
        f"          <{SUP_OBJECT}> ?o .\n"
        f"  }}\n"
        f"}}"
    )


def _object_term(binding: dict) -> str:
    """Reconstruct the write-convention object string from a raw SPARQL JSON binding.

    The inverse of ``graph/queries._escape_value`` on the read side (mirrors
    ``validity._object_term``): preserve the EXACT term so a suppressed typed
    literal round-trips and matches term-for-term (the ONTA-247 typed-literal
    lesson). ``uri`` → the URI string; a typed literal → ``value^^datatype``; a
    plain / ``xsd:string`` literal → the bare value.
    """
    kind = binding.get("type")
    value = binding.get("value", "")
    if kind == "uri":
        return value
    dt = binding.get("datatype")
    if dt and dt != f"{_XSD}#string":
        return f"{value}^^{dt}"
    return value


async def fetch_suppressed(
    neptune, instance_graph: str, subject: str, predicate: str
) -> set[str]:
    """The write-convention object terms currently SUPPRESSED for ``(subject, predicate)``.

    Reads the raw SPARQL JSON (not ``parse_sparql_results``, which drops datatype)
    so each term is reconstructed exactly and can be compared term-identically to a
    value a refresh is about to write. Best-effort: returns an empty set on any read
    failure (a suppression read must never fail the caller — worst case a value that
    should have stayed suppressed gets re-considered, which the conflict policy then
    arbitrates, rather than crashing the run).
    """
    try:
        raw = await neptune.query(suppressed_objects_query(instance_graph, subject, predicate))
    except Exception:  # noqa: BLE001 — a suppression read is best-effort
        return set()
    bindings = raw.get("results", {}).get("bindings", [])
    out: set[str] = set()
    for row in bindings:
        o = row.get("o")
        if o is not None:
            out.add(_object_term(o))
    return out


async def is_suppressed(
    neptune, instance_graph: str, subject: str, predicate: str, obj: str
) -> bool:
    """True iff ``(subject, predicate, obj)`` is on the suppression list.

    Term-faithful: ``obj`` must match the suppressed term exactly (typed-literal
    convention included), so suppressing ``"42"^^xsd:integer`` does not
    accidentally suppress the plain string ``"42"`` and vice-versa. Best-effort via
    :func:`fetch_suppressed`.
    """
    return obj in await fetch_suppressed(neptune, instance_graph, subject, predicate)


__all__ = [
    "SUPPRESSION_NS",
    "SUP_SUBJECT",
    "SUP_PREDICATE",
    "SUP_OBJECT",
    "SUP_STATEMENT",
    "SUP_SUPPRESSED_AT",
    "SUP_REASON",
    "SUP_GRAPH",
    "suppression_graph_uri",
    "statement_id",
    "build_suppression_triples",
    "clear_suppression_update",
    "suppressed_objects_query",
    "fetch_suppressed",
    "is_suppressed",
]

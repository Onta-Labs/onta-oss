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
refresh re-scrapes it. The marker is cleared ONLY by an EXPLICIT un-suppress
(:func:`clear_suppression_update`), which is provided for that purpose but is not
yet invoked by any caller (no un-suppress endpoint is wired yet — the builder is
kept for that future path). A ``user_assertion`` that RE-ASSERTS the exact value
is a SEPARATE mechanism, not an implicit un-suppress: it makes the value current
again through ``supersede_fact``'s reopen regardless of the marker, and a later
refresh that re-scrapes a now-current value is skipped harmlessly (it is already
the current fact). So a human re-assertion takes effect without NEEDING the mark
cleared, and this module does not clear it on their behalf.

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

# ENTITY-LEVEL (subject-only) suppression predicate (ONTA-345). Deliberately
# DISTINCT from ``SUP_SUBJECT``: a fact mark carries the whole ``(s, p, o)`` on
# ``sup:subject`` / ``sup:predicate`` / ``sup:object``, whereas an ENTITY mark
# carries ONLY the erased entity's URI on ``sup:entity`` — no predicate/object.
# The two never collide (different predicate + a different mark-node prefix), so
# an ENTITY suppression is never mistaken for a ``(s, p, o)`` FACT suppression and
# vice-versa. This is the FIND-path re-acquisition guard: an erased entity whose
# canonical subject is on this list must NOT be re-minted by discovery/refresh.
SUP_ENTITY = f"{SUPPRESSION_NS}entity"

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


def entity_statement_id(subject: str) -> str:
    """Deterministic ENTITY-mark id: sha1 over the raw subject URI only.

    Distinct from :func:`statement_id` (which keys a ``(s, p, o)`` FACT mark on
    ``sha1(s|p|o)``): an entity mark keys on ``sha1(s)`` alone, so re-suppressing
    the same entity collides idempotently on the same node."""
    return hashlib.sha1(subject.encode("utf-8")).hexdigest()


def _entity_mark_uri(subject: str) -> str:
    """Entity-suppression-mark node URI: one per ERASED entity, keyed by
    ``sha1(subject)`` under the ``entity/`` prefix.

    The ``entity/`` prefix (vs :func:`_mark_uri`'s ``mark/``) plus the distinct
    ``sha1(subject)`` key (vs ``sha1(s|p|o)``) make it structurally impossible for
    an entity mark and a ``(s, p, o)`` fact mark to share a node — so the two
    suppression kinds never collide."""
    return f"{SUPPRESSION_NS}entity/{entity_statement_id(subject)}"


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


def build_entity_suppression_triples(
    subject: str,
    *,
    reason: str = "",
    suppressed_at: datetime | str = "",
    graph_uri: str = "",
) -> list[tuple[str, str, str]]:
    """Build the ENTITY-level (subject-only) suppression-mark triples for one
    ERASED entity (ONTA-345).

    This is the entity-level counterpart to :func:`build_suppression_triples`: it
    marks a whole entity URI as suppressed (a GDPR erasure / tombstone), NOT a
    single ``(s, p, o)`` fact. The mark lands on the ``sup:entity`` predicate on a
    ``…/entity/{sha1(subject)}`` node — deliberately distinct from a fact mark's
    ``sup:subject`` on a ``…/mark/{sha1(s|p|o)}`` node — so an entity suppression
    and a fact suppression can never collide or be mistaken for one another.
    Returned triples target the companion suppression graph
    (``suppression_graph_uri`` of the data graph); the caller writes them via
    ``kg_writer.insert_facts(suppression_triples=…)``, the same batched seam every
    other write uses — this module never hand-rolls a raw instance-graph write.
    The FIND path (``web_ingest_cap``) then consults this list and DROPS any
    discovered row whose would-be canonical subject is marked here, so an erased
    entity is never silently re-acquired. Returns an empty list when ``subject`` is
    missing.
    """
    if not subject:
        return []
    node = _entity_mark_uri(subject)
    triples = [
        (node, SUP_ENTITY, subject),
        (node, SUP_STATEMENT, entity_statement_id(subject)),
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

    This is the ONE mechanism that lifts a suppression: an EXPLICIT un-suppress
    built here. It is provided for that purpose but is NOT yet invoked by any caller
    (no un-suppress endpoint is wired yet — the builder is retained for that future
    path). A ``user_assertion`` re-asserting the exact value does NOT route through
    here and is not an implicit un-suppress: it makes the value current again via
    ``supersede_fact``'s reopen regardless of the marker, and a subsequent refresh
    that re-scrapes a now-current value is skipped harmlessly — so a re-assertion
    takes effect without the mark being cleared, and this builder is not called on
    its behalf. Composes the ``graph/queries.py`` DELETE builder against the
    companion suppression graph — this module never hand-rolls raw SPARQL — so it
    stays free of write markers the way ``validity.reopen_interval_update`` does.
    Returns ``""`` when subject/predicate is missing (nothing to clear).
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


def suppressed_entities_query(instance_graph: str) -> str:
    """SELECT every ENTITY-level suppressed subject in the companion graph.

    Reads ONLY the ``sup:entity`` marks (the subject-only erasure tombstones), so
    a ``(s, p, o)`` FACT mark — which carries ``sup:subject`` / ``sup:predicate`` /
    ``sup:object``, never ``sup:entity`` — is structurally excluded. Used by
    :func:`fetch_suppressed_entities` / :func:`is_entity_suppressed` to decide, in
    ONE batched read per run, whether a discovered row may be (re-)acquired. Mirrors
    :func:`suppressed_objects_query` in shape (an inline SELECT over the companion
    suppression graph) — a READ, not a write, so it stays outside the write-path
    convergence guard exactly as the ``(s, p, o)`` reader does.
    """
    sup_graph = suppression_graph_uri(instance_graph)
    return (
        f"SELECT ?s WHERE {{\n"
        f"  GRAPH <{sup_graph}> {{\n"
        f"    ?node <{SUP_ENTITY}> ?s .\n"
        f"  }}\n"
        f"}}"
    )


async def fetch_suppressed_entities(neptune, instance_graph: str) -> set[str]:
    """The set of ENTITY subjects currently SUPPRESSED (erased/tombstoned) in a graph.

    ONE query per run (the FIND-path guard checks set-membership per row, so a
    discovery of N rows costs a single read, not N reads). Reconstructs each
    subject term via :func:`_object_term` (a suppressed entity URI round-trips to
    its exact URI string, comparable term-identically to a discovered row's
    would-be ``entity_uri``). Best-effort: returns an empty set on any read failure
    or when there is no target graph — a suppression read must never fail the
    caller (worst case an erased entity is re-considered rather than the run
    crashing).
    """
    if not instance_graph:
        return set()
    try:
        raw = await neptune.query(suppressed_entities_query(instance_graph))
    except Exception:  # noqa: BLE001 — a suppression read is best-effort
        return set()
    bindings = raw.get("results", {}).get("bindings", [])
    out: set[str] = set()
    for row in bindings:
        s = row.get("s")
        if s is not None:
            out.add(_object_term(s))
    return out


async def is_entity_suppressed(neptune, instance_graph: str, subject: str) -> bool:
    """True iff the ENTITY ``subject`` is on the entity-level suppression list.

    Term-faithful and kind-faithful: matches only ``sup:entity`` marks, so a
    ``(s, p, o)`` FACT suppression of the same subject does NOT make this return
    True (and an entity suppression does not make :func:`is_suppressed` return
    True) — the two suppression kinds are independent. Best-effort via
    :func:`fetch_suppressed_entities`.
    """
    return subject in await fetch_suppressed_entities(neptune, instance_graph)


__all__ = [
    "SUPPRESSION_NS",
    "SUP_SUBJECT",
    "SUP_PREDICATE",
    "SUP_OBJECT",
    "SUP_STATEMENT",
    "SUP_SUPPRESSED_AT",
    "SUP_REASON",
    "SUP_GRAPH",
    "SUP_ENTITY",
    "suppression_graph_uri",
    "statement_id",
    "entity_statement_id",
    "build_suppression_triples",
    "build_entity_suppression_triples",
    "clear_suppression_update",
    "suppressed_objects_query",
    "suppressed_entities_query",
    "fetch_suppressed",
    "fetch_suppressed_entities",
    "is_suppressed",
    "is_entity_suppressed",
]

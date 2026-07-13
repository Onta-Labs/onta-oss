"""Valid-time (validity-interval) substrate for facts (ONTA-277).

**What this answers:** "is this fact CURRENT, or was it superseded / retracted?"
The graph is append-only today: a refresh that brings a new CEO leaves the old
``hasCEO`` edge in place, so a "current facts" reader (P7 answer layer) can cite a
stale value. This module models a **valid-time interval** per fact so a superseded
fact can be marked *closed* — no longer current — WITHOUT being deleted or
re-pointed, keeping history queryable and lineage intact.

**Supersession closes an interval; it does NOT delete or re-point the edge.** The
superseded instance triple stays exactly where it is in the instance graph; a
companion validity node records that its interval closed (``valid_to`` +
``superseded_by``). A "current facts" query excludes facts carrying a closed
interval; a full/history query still returns them, with the closed interval
attached — proving the fact was retired, not removed.

Encoding decision (the same reasoning as ``graph/provenance.py`` /
``graph/history.py``: Neptune has NO RDF-star, so a triple cannot be annotated in
place): a dedicated **companion validity named graph** per data graph
(``<data-graph>/validity``) holding one interval node per ``(subject, predicate,
object)`` fact. Chosen over stamping validity predicates onto the entity itself
because (a) it composes with the existing single-data-graph layout — the instance
triples stay exactly where they are and always hold the fact verbatim; (b) it
mirrors the provenance / history companion-graph pattern already in the codebase
(one reader, one writer, one clear undo story); (c) the interval nodes never reach
any instance-graph read surface (Explorer panels, NL ``ask`` dumps, the A6 Graph
Delta), so validity is *structurally invisible* to user surfaces yet plainly
queryable — the strongest form of the ``attr_meta`` / ``is_internal_predicate``
invisibility the provenance companions get. ``parse_kg_graph_uri`` returns ``None``
for the ``/validity`` suffix (its kg segment carries a trailing ``/validity``), so
the derived-index hooks never mistake it for an instance graph.

The interval is **half-open** ``[valid_from, valid_to)`` — the same convention the
spatio-temporal index uses (``spatiotemporal/protocol.py``): ``valid_to`` ABSENT
means "still current / open-ended". A fact is CURRENT iff its ``(s, p, o)`` has no
validity node carrying a ``valid_to``.

For a fact ``(s, p, o)`` the validity graph holds, when it is closed::

    <https://cograph.tech/validity/int/{sha1(s|p|o)}>
        val:subject      <s> ;
        val:predicate    <p> ;
        val:object       o ;                       # literal or URI, as written
        val:statement    "{sha1(s|p|o)}" ;
        val:validFrom    "2026-01-01T…"^^xsd:dateTime ;   # optional
        val:validTo      "2026-07-13T…"^^xsd:dateTime ;   # CLOSED marker
        val:supersededBy "{sha1(s|p|newObj)}" ;    # supersession only
        val:status       "superseded" ;            # "superseded" | "retracted"
        val:graph        <data graph the fact lives in> .

An OPEN interval (a newly-current fact) carries ``val:validFrom`` and NO
``val:validTo`` — recorded for history richness; its absence of a ``valid_to`` is
what keeps it current.

**On the shared write path.** These triples are NOT written by a bespoke writer:
``kg_writer.insert_facts(validity_triples=…)`` routes them through the same
``batched_insert_triples`` seam every other write uses, into the validity
companion graph (exactly as it routes ``provenance_triples`` to the provenance
graph). This module only BUILDS triples and QUERIES them (it constructs no raw
instance-graph writes), exactly like ``graph/provenance.py`` and
``graph/history.py`` — so it stays outside the write-path convergence guard's
concern the same way those do.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from cograph_client.graph.parser import parse_sparql_results
from cograph_client.graph.queries import _escape_value, delete_node_predicates_query

# The validity namespace. A whole-namespace exclusion is added to
# graph/predicates.py::is_internal_predicate for defense-in-depth (these
# predicates live in a separate companion graph and never appear on the instance
# graph, but classifying the namespace internal makes it structurally impossible
# for a validity predicate to be surfaced as a domain attribute if one ever did).
VALIDITY_NS = "https://cograph.tech/validity/"

VAL_SUBJECT = f"{VALIDITY_NS}subject"
VAL_PREDICATE = f"{VALIDITY_NS}predicate"
VAL_OBJECT = f"{VALIDITY_NS}object"
VAL_STATEMENT = f"{VALIDITY_NS}statement"
VAL_VALID_FROM = f"{VALIDITY_NS}validFrom"
VAL_VALID_TO = f"{VALIDITY_NS}validTo"
VAL_SUPERSEDED_BY = f"{VALIDITY_NS}supersededBy"
VAL_STATUS = f"{VALIDITY_NS}status"
VAL_GRAPH = f"{VALIDITY_NS}graph"

# Closed-interval statuses. All carry a ``valid_to`` (so all drop out of the
# "current" set); they differ only in WHY the fact stopped being current.
STATUS_SUPERSEDED = "superseded"  # a replacement fact closed it (recency wins)
STATUS_RETRACTED = "retracted"  # explicitly asserted no-longer-true
# A functional-attribute CONFLICT closed it: an independently-verified
# contradicting fact won under the write-time conflict policy (authority +
# confidence + recency), so this value is DEPRECATED — not current, but kept
# queryable in history WITH its provenance and a ``superseded_by`` pointer at the
# winning fact (never silently dropped). ONTA-276.
STATUS_DEPRECATED = "lost_conflict"

_XSD = "http://www.w3.org/2001/XMLSchema"


def validity_graph_uri(graph_uri: str) -> str:
    """Companion validity graph for a data graph.

    Distinct from ``provenance_graph_uri``'s ``/provenance`` and
    ``history_graph_uri``'s ``/history`` suffixes so the three companion stores
    never collide, and — like those — ``parse_kg_graph_uri`` returns ``None`` for
    it (its kg segment carries a trailing ``/validity``), so a validity graph is
    never mistaken for an instance graph by the derived-index hooks.
    """
    return f"{graph_uri}/validity"


def statement_id(subject: str, predicate: str, obj: str) -> str:
    """Deterministic fact id: sha1 over the raw s|p|o strings as written.

    Identical keying to ``provenance.statement_id`` so a fact's validity node and
    its provenance node group on the same id.
    """
    return hashlib.sha1(f"{subject}|{predicate}|{obj}".encode("utf-8")).hexdigest()


def _interval_uri(subject: str, predicate: str, obj: str) -> str:
    """Validity-node URI: one per ``(subject, predicate, object)`` fact.

    Keyed by ``sha1(s|p|o)`` so the OPEN node written when a fact becomes current
    and the CLOSED marker added when it is later superseded/retracted attach to the
    SAME node (closing a fact = adding ``valid_to`` to its existing interval node).
    """
    return f"{VALIDITY_NS}int/{statement_id(subject, predicate, obj)}"


def _as_iso(ts: datetime | str) -> str:
    return ts.isoformat() if isinstance(ts, datetime) else str(ts)


def _common(
    node: str, subject: str, predicate: str, obj: str, graph_uri: str
) -> list[tuple[str, str, str]]:
    triples = [
        (node, VAL_SUBJECT, subject),
        (node, VAL_PREDICATE, predicate),
        (node, VAL_OBJECT, obj),
        (node, VAL_STATEMENT, statement_id(subject, predicate, obj)),
    ]
    if graph_uri:
        triples.append((node, VAL_GRAPH, graph_uri))
    return triples


def build_open_interval_triples(
    subject: str,
    predicate: str,
    obj: str,
    *,
    valid_from: datetime | str = "",
    graph_uri: str = "",
) -> list[tuple[str, str, str]]:
    """Build the validity node for a fact that is now CURRENT (open interval).

    Carries ``val:validFrom`` (when it became valid) and NO ``val:validTo`` — its
    open interval is what keeps it in the "current facts" set. Recorded for history
    richness (so a current fact answers "valid since when?"); the fact would be
    current even without this node, since currency is defined by the ABSENCE of a
    ``valid_to``. Returns an empty list when subject/predicate is missing.
    """
    if not subject or not predicate:
        return []
    node = _interval_uri(subject, predicate, obj)
    triples = _common(node, subject, predicate, obj, graph_uri)
    if valid_from:
        triples.append((node, VAL_VALID_FROM, f"{_as_iso(valid_from)}^^{_XSD}#dateTime"))
    return triples


def build_closed_interval_triples(
    subject: str,
    predicate: str,
    obj: str,
    *,
    valid_to: datetime | str,
    valid_from: datetime | str = "",
    superseded_by: str = "",
    status: str = STATUS_SUPERSEDED,
    graph_uri: str = "",
) -> list[tuple[str, str, str]]:
    """Build the validity node marking a fact CLOSED (no longer current).

    The load-bearing supersession/retraction record: ``val:validTo`` closes the
    half-open interval ``[valid_from, valid_to)`` so a "current facts" query
    excludes ``(s, p, o)`` while a history query still returns it (the instance
    triple is UNTOUCHED — supersession closes an interval, it never deletes or
    re-points the edge). ``superseded_by`` is the ``statement_id`` of the
    replacement fact (supersession only; empty for a bare retraction).
    ``status`` distinguishes ``superseded`` from ``retracted``.

    Returned triples target the companion validity graph; the caller writes them
    via ``kg_writer.insert_facts(validity_triples=…)``. Returns an empty list when
    subject/predicate is missing.
    """
    if not subject or not predicate:
        return []
    node = _interval_uri(subject, predicate, obj)
    triples = _common(node, subject, predicate, obj, graph_uri)
    if valid_from:
        triples.append((node, VAL_VALID_FROM, f"{_as_iso(valid_from)}^^{_XSD}#dateTime"))
    triples.append((node, VAL_VALID_TO, f"{_as_iso(valid_to)}^^{_XSD}#dateTime"))
    if superseded_by:
        triples.append((node, VAL_SUPERSEDED_BY, superseded_by))
    if status:
        triples.append((node, VAL_STATUS, status))
    return triples


# Closure predicates an OPEN write must clear off a value's interval node when a
# previously-closed value is re-asserted as current. These are exactly the three
# markers ``build_closed_interval_triples`` adds; leaving any of them (esp.
# ``val:validTo``) makes ``current_objects_query`` treat the re-asserted value as
# still closed — the "value resurrection" bug (ONTA-277).
_CLOSURE_PREDICATES = (VAL_VALID_TO, VAL_SUPERSEDED_BY, VAL_STATUS)


def reopen_interval_update(
    instance_graph: str, subject: str, predicate: str, obj: str
) -> str:
    """Build the update that RE-OPENS a value's validity interval by CLEARING any
    prior closure on its node (ONTA-277 value-resurrection fix).

    A validity node is keyed by ``sha1(s|p|o)`` (:func:`_interval_uri`), so CLOSING
    a fact adds ``val:validTo`` (+ ``supersededBy`` / ``status``) to that node and
    RE-ASSERTING the SAME value later only appends ``val:validFrom`` — the stale
    ``val:validTo`` survives, and ``current_objects_query`` (currency == absence of
    ``val:validTo``) then silently excludes the resurrected value. This update
    DELETEs the three closure predicates off THAT value's node (only), so opening
    the interval genuinely makes the value current again. Other values' interval
    nodes are untouched (they key on a different object).

    Targets the companion VALIDITY graph (``validity_graph_uri(instance_graph)``),
    the same routing ``insert_facts`` uses for ``validity_triples``. Composes the
    ``graph/queries.py`` DELETE builder rather than constructing raw SPARQL here, so
    this module stays free of hand-rolled write markers (as its module docstring
    promises) while the executor lives in ``kg_writer.insert_facts(reopen_facts=…)``.
    Returns ``""`` when subject/predicate is missing (nothing to reopen).
    """
    if not subject or not predicate:
        return ""
    node = _interval_uri(subject, predicate, obj)
    val_graph = validity_graph_uri(instance_graph)
    return delete_node_predicates_query(val_graph, node, list(_CLOSURE_PREDICATES))


def current_objects_query(instance_graph: str, subject: str, predicate: str) -> str:
    """SELECT the CURRENT objects of ``(subject, predicate)`` — those with no closed
    validity interval.

    The "current facts" projection: every ``(s, p, ?o)`` in the instance graph
    whose ``(s, p, o)`` has NO validity node carrying a ``val:validTo`` (i.e. an
    open / absent interval). A superseded or retracted value drops out here while
    remaining in the instance graph (see :func:`history_objects_query`). This is
    the read P7 uses to avoid citing a stale fact.
    """
    val_graph = validity_graph_uri(instance_graph)
    s, p = _escape_value(subject), _escape_value(predicate)
    return (
        f"SELECT ?o WHERE {{\n"
        f"  GRAPH <{instance_graph}> {{ {s} {p} ?o }}\n"
        f"  FILTER NOT EXISTS {{\n"
        f"    GRAPH <{val_graph}> {{\n"
        f"      ?node <{VAL_SUBJECT}> {s} ;\n"
        f"            <{VAL_PREDICATE}> {p} ;\n"
        f"            <{VAL_OBJECT}> ?o ;\n"
        f"            <{VAL_VALID_TO}> ?end .\n"
        f"    }}\n"
        f"  }}\n"
        f"}}"
    )


def history_objects_query(instance_graph: str, subject: str, predicate: str) -> str:
    """SELECT ALL objects of ``(subject, predicate)`` with any validity interval.

    The full-history projection: every ``(s, p, ?o)`` in the instance graph, LEFT
    JOINed to its validity interval (``validFrom`` / ``validTo`` / ``supersededBy``
    / ``status``) when one exists. A superseded value appears here WITH a
    ``validTo`` (proving it was closed, not deleted); a current value appears with
    no ``validTo``. Ordered by object for a stable read.
    """
    val_graph = validity_graph_uri(instance_graph)
    s, p = _escape_value(subject), _escape_value(predicate)
    return (
        f"SELECT ?o ?validFrom ?validTo ?supersededBy ?status WHERE {{\n"
        f"  GRAPH <{instance_graph}> {{ {s} {p} ?o }}\n"
        f"  OPTIONAL {{\n"
        f"    GRAPH <{val_graph}> {{\n"
        f"      ?node <{VAL_SUBJECT}> {s} ;\n"
        f"            <{VAL_PREDICATE}> {p} ;\n"
        f"            <{VAL_OBJECT}> ?o .\n"
        f"      OPTIONAL {{ ?node <{VAL_VALID_FROM}> ?validFrom }}\n"
        f"      OPTIONAL {{ ?node <{VAL_VALID_TO}> ?validTo }}\n"
        f"      OPTIONAL {{ ?node <{VAL_SUPERSEDED_BY}> ?supersededBy }}\n"
        f"      OPTIONAL {{ ?node <{VAL_STATUS}> ?status }}\n"
        f"    }}\n"
        f"  }}\n"
        f"}} ORDER BY ?o"
    )


def _object_term(binding: dict) -> str:
    """Reconstruct the write-convention object string from a raw SPARQL JSON binding.

    The inverse of ``graph/queries._escape_value`` on the read side: preserve the
    EXACT term so a value read from the instance graph can be closed with an
    identical ``val:object`` (a superseded typed literal must match term-for-term
    or the "current" FILTER would never exclude it — the ONTA-247 typed-literal
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


async def fetch_current_object_terms(
    neptune, instance_graph: str, subject: str, predicate: str
) -> list[str]:
    """The write-convention object terms currently valid for ``(subject, predicate)``.

    Used by the supersede op to discover which existing value(s) a newer fact
    closes when the caller does not name the old value explicitly. Reads the raw
    SPARQL JSON (not ``parse_sparql_results``, which drops datatype) so each term
    is reconstructed exactly and can be closed with a term-identical
    ``val:object``. Best-effort: returns ``[]`` on any read failure.
    """
    try:
        raw = await neptune.query(current_objects_query(instance_graph, subject, predicate))
    except Exception:  # noqa: BLE001 — discovery read is best-effort
        return []
    bindings = raw.get("results", {}).get("bindings", [])
    terms: list[str] = []
    for row in bindings:
        o = row.get("o")
        if o is not None:
            terms.append(_object_term(o))
    return terms


@dataclass
class ValidityInterval:
    """One fact's validity interval read back from the validity graph."""

    obj: str
    valid_from: str = ""
    valid_to: str = ""
    superseded_by: str = ""
    status: str = ""

    @property
    def is_current(self) -> bool:
        """Open interval (no ``valid_to``) → the fact is still current."""
        return not self.valid_to


async def fetch_history(
    neptune, instance_graph: str, subject: str, predicate: str
) -> list[ValidityInterval]:
    """Read every value of ``(subject, predicate)`` with its validity interval.

    A convenience over :func:`history_objects_query` returning parsed
    :class:`ValidityInterval` rows (current values have an empty ``valid_to`` /
    ``is_current`` True; superseded/retracted values carry a ``valid_to`` +
    ``status``). Returns an empty list on any read failure so a history read never
    breaks a caller.
    """
    try:
        raw = await neptune.query(history_objects_query(instance_graph, subject, predicate))
    except Exception:  # noqa: BLE001 — a history read is informational, never load-bearing
        return []
    _, rows = parse_sparql_results(raw)
    out: list[ValidityInterval] = []
    for row in rows:
        out.append(
            ValidityInterval(
                obj=row.get("o", ""),
                valid_from=row.get("validFrom", ""),
                valid_to=row.get("validTo", ""),
                superseded_by=row.get("supersededBy", ""),
                status=row.get("status", ""),
            )
        )
    return out


__all__ = [
    "VALIDITY_NS",
    "VAL_SUBJECT",
    "VAL_PREDICATE",
    "VAL_OBJECT",
    "VAL_STATEMENT",
    "VAL_VALID_FROM",
    "VAL_VALID_TO",
    "VAL_SUPERSEDED_BY",
    "VAL_STATUS",
    "VAL_GRAPH",
    "STATUS_SUPERSEDED",
    "STATUS_RETRACTED",
    "STATUS_DEPRECATED",
    "ValidityInterval",
    "validity_graph_uri",
    "statement_id",
    "build_open_interval_triples",
    "build_closed_interval_triples",
    "reopen_interval_update",
    "current_objects_query",
    "history_objects_query",
    "fetch_current_object_terms",
    "fetch_history",
]

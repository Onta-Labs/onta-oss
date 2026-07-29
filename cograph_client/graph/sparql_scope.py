"""Tenant confinement for CALLER-SUPPLIED SPARQL (ONTA-412).

``POST /graphs/{tenant}/query`` hands a client's SPARQL string straight to the
store. Authorizing the tenant in the ROUTE PATH is not enough, because nothing
about the path constrains which graphs the query text reads.

Why a bare, graph-less query is the primary leak (not just an explicit ``FROM``):
Amazon Neptune defines the default graph as the UNION OF ALL NAMED GRAPHS. Its
docs are explicit: "If you submit a SPARQL query without explicitly specifying a
graph via the GRAPH keyword or constructs such as FROM NAMED, Neptune always
considers all triples in your DB instance." So on the deployed backend
``SELECT * WHERE { ?s ?p ?o }`` already returns EVERY tenant's triples, with no
``FROM`` clause anywhere. Any rule shaped as "reject clauses that name a foreign
graph" is therefore not merely fragile, it is ineffective: the worst query names
no graph at all.

The confinement rule here is inverted, so it fails CLOSED:

A. The query MUST carry at least one dataset clause, every ``FROM`` /
   ``FROM NAMED`` must name a full IRI, and every one of those IRIs must belong
   to the calling tenant. Once a ``FROM`` is present the store's own dataset
   rules do the enforcing: per SPARQL 1.1 Query (and per Neptune's own
   documentation of it) the default graph becomes the union of exactly the
   ``FROM`` graphs, and the named-graph set becomes exactly the ``FROM NAMED``
   graphs (empty when none are given, so ``GRAPH ?g`` can bind nothing outside
   the dataset). Enforcement lands in the STORE, driven by a dataset we
   validated, rather than in a SPARQL parser we would have to keep correct.

B. Independently of A, no IRI anywhere in the RAW query text may sit under
   ``https://cograph.tech/graphs/`` unless it belongs to the calling tenant.
   This is the belt for A's suspenders: it is a scan of the untouched text, so a
   clause my tokenizer failed to recognize cannot smuggle a foreign graph IRI
   past it. It also covers inline ``GRAPH <victim>`` / ``WITH`` / ``USING``.

C. ``SERVICE`` is rejected. Federation is an outbound channel (send this
   tenant's rows to an attacker-chosen endpoint), which is a different hole than
   the one above but reachable through the same passthrough.

Both text scans fail in the safe direction by construction:

* Clause DETECTION (rule A) runs on a copy with comments and string literals
  blanked, because a false POSITIVE there is the dangerous direction: the word
  "from" inside a literal must not be mistaken for a real dataset clause and let
  an unscoped query through. Missing a real ``FROM`` only rejects a valid query.
* The foreign-IRI scan (rule B) runs on the RAW text, because there a false
  NEGATIVE is the dangerous direction. Masking first would let a stray quote
  swallow a trailing ``FROM <victim>``. A victim IRI quoted inside a literal is
  rejected instead, which is a harmless false positive.

Relative IRIs and prefixed names get no special handling and need none: rule A
demands a full ``<...>`` IRI whose text starts with the tenant's own graph
prefix, so ``BASE <...victim> ... FROM <>``, ``FROM <../victim>`` and
``PREFIX g: <...> FROM g:victim`` are all rejected outright.

NOT handled here: SPARQL Update. ``DROP ALL``, ``CLEAR DEFAULT`` and a bare
graph-less removal (a ``DELETE`` whose ``WHERE`` matches ``?s ?p ?o``) name no
graph at all yet act on everything, so no text rule can confine an arbitrary
update. ``/graphs/{tenant}/update`` is gated on operator auth instead (see
``api/routes/query.py``).
"""

from __future__ import annotations

import re

GRAPH_NAMESPACE = "https://cograph.tech/graphs/"


class TenantScopeError(ValueError):
    """Caller-supplied SPARQL that is not provably confined to one tenant.

    Carries the HTTP status the route should surface: 400 when the query is
    merely unscoped (the caller can fix it by adding a dataset clause), 403 when
    it reaches at a graph the caller does not own.
    """

    def __init__(self, detail: str, status_code: int = 400):
        self.detail = detail
        self.status_code = status_code
        super().__init__(detail)


def tenant_owns_graph(graph_uri: str, tenant_id: str) -> bool:
    """True when ``graph_uri`` is the tenant's ontology graph or a child of it.

    Children cover every companion the platform mints under the tenant base:
    ``/kg/<name>``, its ``/provenance`` and history graphs, and anything added
    later. The "/" in the child test is load-bearing: without it tenant ``acme``
    would own tenant ``acme-corp``'s graphs.
    """
    base = GRAPH_NAMESPACE + tenant_id
    return graph_uri == base or graph_uri.startswith(base + "/")


# Regions of a SPARQL string that a keyword scan must not read literally. IRIREF
# is FIRST so that the "#" inside e.g. <http://www.w3.org/1999/02/22-rdf-syntax-ns#type>
# is never mistaken for the start of a comment. Leftmost-match scanning then
# handles interleaving correctly on its own: at a "#" the IRIREF branch cannot
# match, and a "<" inside an already-matched literal is never visited.
_MASKABLE_RE = re.compile(
    r"<[^<>\"{}|^`\\\s]*>"
    r'|"""(?:[^"\\]|\\.|"(?!""))*"""'
    r"|'''(?:[^'\\]|\\.|'(?!''))*'''"
    r'|"(?:[^"\\\n]|\\.)*"'
    r"|'(?:[^'\\\n]|\\.)*'"
    r"|#[^\n]*"
)

# "FROM" as a standalone keyword. \b alone is not enough: it would also fire on
# the "from" in ?from / $from / ex:from, and "FROM:" could open a prefixed name.
_FROM_KEYWORD_RE = re.compile(r"(?<![?$:\w])FROM(?![\w:])", re.IGNORECASE)
_FROM_IRI_RE = re.compile(
    # \s* rather than \s+ because SPARQL needs no whitespace before "<", so
    # "FROM<g>" is legal and must not be rejected as a malformed clause.
    r"(?<![?$:\w])FROM(?![\w:])\s*(?:NAMED(?![\w:])\s*)?<([^>]*)>",
    re.IGNORECASE,
)
_SERVICE_KEYWORD_RE = re.compile(r"(?<![?$:\w])SERVICE(?![\w:])", re.IGNORECASE)

# Any occurrence of our graph namespace in the raw text, however it is spelled.
# The terminator class only ever SHORTENS a match, which can never turn a
# foreign IRI into an owned one (ownership is a prefix test against the tenant's
# own base), so truncation is safe.
_GRAPH_IRI_RE = re.compile(
    re.escape(GRAPH_NAMESPACE) + r"[^\s<>\"'`{}|^\\()\[\],;]*"
)


def _mask(sparql: str) -> tuple[str, str]:
    """Blank comments and string literals, returning two aligned copies.

    Both results have the same length and character offsets as the input, so a
    keyword position found in one indexes into the other. The first copy KEEPS
    IRIs (needed to read the IRI that follows a ``FROM``); the second blanks
    them too (needed so a keyword-shaped path segment inside an IRI, e.g.
    ``<http://example.com/from>``, is not counted as a dataset clause).
    """
    keep_iris = list(sparql)
    blank_iris = list(sparql)
    for match in _MASKABLE_RE.finditer(sparql):
        start, end = match.span()
        is_iri = match.group(0).startswith("<")
        for i in range(start, end):
            blank_iris[i] = " "
            if not is_iri:
                keep_iris[i] = " "
    return "".join(keep_iris), "".join(blank_iris)


def enforce_query_scope(sparql: str, tenant_id: str) -> None:
    """Raise :class:`TenantScopeError` unless ``sparql`` is confined to ``tenant_id``.

    Returns ``None`` on success. See the module docstring for the rules and for
    why each scan runs on the copy of the text that it does.
    """
    keep_iris, blank_iris = _mask(sparql)

    if _SERVICE_KEYWORD_RE.search(blank_iris):
        raise TenantScopeError(
            "SPARQL federation (SERVICE) is not allowed on this endpoint.", 400
        )

    keyword_offsets = {m.start() for m in _FROM_KEYWORD_RE.finditer(blank_iris)}
    if not keyword_offsets:
        raise TenantScopeError(
            "Query must name the graphs it reads. Add a dataset clause, e.g. "
            f"FROM <{GRAPH_NAMESPACE}{tenant_id}> for the ontology graph or "
            f"FROM <{GRAPH_NAMESPACE}{tenant_id}/kg/YOUR_KG> for a knowledge "
            "graph. Without one the store reads every graph it holds.",
            400,
        )

    # Read each clause's IRI out of the IRI-preserving copy, but only at offsets
    # where the IRI-blanked copy also saw a real FROM keyword.
    dataset_iris = {
        m.start(): m.group(1)
        for m in _FROM_IRI_RE.finditer(keep_iris)
        if m.start() in keyword_offsets
    }
    if set(dataset_iris) != keyword_offsets:
        raise TenantScopeError(
            "Every FROM / FROM NAMED clause must name a full IRI in angle "
            "brackets, e.g. FROM <"
            + GRAPH_NAMESPACE
            + tenant_id
            + ">. Prefixed names and relative IRIs are not accepted here.",
            400,
        )
    for graph_uri in dataset_iris.values():
        if not tenant_owns_graph(graph_uri, tenant_id):
            raise TenantScopeError(
                f"Graph <{graph_uri}> does not belong to workspace "
                f"'{tenant_id}'.",
                403,
            )

    _reject_foreign_graph_iris(sparql, tenant_id)


def _reject_foreign_graph_iris(sparql: str, tenant_id: str) -> None:
    """Rule B: no foreign graph IRI anywhere in the RAW text, in any position."""
    for match in _GRAPH_IRI_RE.finditer(sparql):
        graph_uri = match.group(0)
        if not tenant_owns_graph(graph_uri, tenant_id):
            raise TenantScopeError(
                f"Graph <{graph_uri}> does not belong to workspace "
                f"'{tenant_id}'.",
                403,
            )

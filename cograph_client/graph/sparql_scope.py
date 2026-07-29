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

A. The query MUST carry at least one dataset clause, and every graph named by a
   ``FROM`` / ``FROM NAMED`` must belong to the calling tenant. Once a ``FROM``
   is present the store's own dataset rules do the enforcing: per SPARQL 1.1
   Query (and per Neptune's own documentation of it) the default graph becomes
   the union of exactly the ``FROM`` graphs, and the named-graph set becomes
   exactly the ``FROM NAMED`` graphs (empty when none are given, so ``GRAPH ?g``
   can bind nothing outside the dataset). Enforcement lands in the STORE, driven
   by a dataset we validated.

B. Independently of A, no IRI anywhere in the RAW query text may sit under
   ``https://cograph.tech/graphs/`` unless it belongs to the calling tenant.
   This is the belt for A's suspenders and also covers inline ``GRAPH <victim>``.

C. ``SERVICE`` is rejected. Federation is an outbound channel (send this
   tenant's rows to an attacker-chosen endpoint), which is a different hole than
   the one above but reachable through the same passthrough.

Rule A is decided by a REAL SPARQL PARSER, not by scanning for the ``FROM``
keyword. That is not fastidiousness; a keyword scan is exploitable. SPARQL's
``PN_LOCAL`` and ``BLANK_NODE_LABEL`` productions both allow ``-``, ``.`` and a
long tail of ``PN_CHARS`` codepoints INSIDE a name, so ``_:b-FROM`` is a single
token to the store while a scanner reads a standalone ``FROM`` keyword followed
by whatever IRI comes next. That lets a query look scoped to us while the store
sees no dataset clause at all and falls back to the union of every graph, which
is exactly the bug this module exists to prevent:

    SELECT ?s ?p ?o WHERE { ?s ?p ?o OPTIONAL { _:b-FROM <...own graph...> ?z } }

Tightening the scanner's lookbehind fixes the instance, not the class:
``ex:p·FROM`` (U+00B7, a legal ``PN_CHARS``) walks straight through the tightened
version. Chasing the ``PN_CHARS`` tables by hand is a losing game, so the parser
decides token boundaries and a parse failure is a safe 400.

Rule B deliberately keeps its RAW-TEXT scan rather than reading the parse tree.
The two rules then fail independently: a divergence between our parser and the
store's cannot also blind the check that no foreign graph IRI is present. A
victim IRI quoted inside a string literal is rejected too, which is a harmless
false positive.

Relative IRIs get no special handling and need none. The parser does NOT resolve
a dataset IRI against the prologue's ``BASE`` (that happens later, at algebra
translation), so ``BASE <...victim> ... FROM <>`` surfaces here as the empty IRI,
fails the ownership test and is rejected before the store ever resolves it.

NOT handled here: SPARQL Update. ``DROP ALL``, ``CLEAR DEFAULT`` and a bare
graph-less removal (a ``DELETE`` whose ``WHERE`` matches ``?s ?p ?o``) name no
graph at all yet act on everything, so no rule of this shape can confine an
arbitrary update. ``/graphs/{tenant}/update`` is gated on operator auth instead
(see ``api/routes/query.py``).
"""

from __future__ import annotations

import re

GRAPH_NAMESPACE = "https://cograph.tech/graphs/"


class TenantScopeError(ValueError):
    """Caller-supplied SPARQL that is not provably confined to one tenant.

    Carries the HTTP status the route should surface: 400 when the query is
    unscoped or unparseable (the caller can fix it), 403 when it reaches at a
    graph the caller does not own, 503 when the parser itself is unavailable.
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

    A prefix test alone is not sufficient, because an owned PREFIX does not mean
    an owned TARGET. RFC 3986 section 5.2.2 applies ``remove_dot_segments`` when
    resolving a reference even if it carries a scheme, and Jena's resolver does
    exactly that, so ``<...graphs/acme/../victim>`` starts inside our namespace
    and lands outside it. Dot segments, backslashes and percent-encoding (which
    could spell either) are refused outright rather than normalised: no graph
    this platform mints contains any of them, so refusing costs nothing, while
    guessing a normalisation the store might not share would cost a lot.
    """
    base = GRAPH_NAMESPACE + tenant_id
    if graph_uri == base:
        return True
    if not graph_uri.startswith(base + "/"):
        return False
    remainder = graph_uri[len(base) + 1 :]
    if "\\" in remainder or "%" in remainder:
        return False
    return all(segment not in ("", ".", "..") for segment in remainder.split("/"))


# Any occurrence of our graph namespace in the raw text, however it is spelled.
# The terminator class only ever SHORTENS a match, which can never turn a
# foreign IRI into an owned one (ownership is a prefix test against the tenant's
# own base, and dot segments are refused), so truncation is safe.
_GRAPH_IRI_RE = re.compile(
    re.escape(GRAPH_NAMESPACE) + r"[^\s<>\"'`{}|^\\()\[\],;]*"
)


def _parse(sparql: str):
    """Parse ``sparql`` as a SPARQL 1.1 QUERY, or raise :class:`TenantScopeError`.

    The rdflib import is deliberately LAZY, and its absence is a hard 503 rather
    than an ImportError at module scope. ``api/app.py`` imports this module's
    route at startup, so an eager import would turn a missing dependency into a
    backend that will not boot. Failing closed here costs one route; failing at
    import would cost the platform, and skipping the check would cost the tenant
    boundary.
    """
    try:
        from rdflib.plugins.sparql.parser import parseQuery
    except ImportError as exc:  # pragma: no cover - dependency-missing path
        raise TenantScopeError(
            "Raw SPARQL is unavailable: the query parser this endpoint needs to "
            "confine a query to your workspace is not installed.",
            503,
        ) from exc
    try:
        return parseQuery(sparql)
    except Exception as exc:
        # Includes pyparsing's ParseException and any recursion error on a
        # pathological input. Unparseable means unverifiable, which means no.
        raise TenantScopeError("Query could not be parsed as SPARQL 1.1.", 400) from exc


def _walk(node, seen_names: set[str]) -> None:
    """Collect every production name appearing in a parse tree."""
    from rdflib.plugins.sparql.parserutils import CompValue

    if isinstance(node, CompValue):
        seen_names.add(node.name)
        for key in list(node.keys()):
            _walk(node[key], seen_names)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _walk(item, seen_names)


def dataset_graphs(query_part) -> list[str]:
    """Every graph IRI named by a FROM / FROM NAMED clause, as plain strings.

    rdflib's ``CompValue`` has a trap worth spelling out: ``.get(key)`` on an
    ABSENT key returns the key's own NAME as a string, so ``get("datasetClause")``
    is truthy on a query that has no dataset clause at all, and each clause
    reports the string ``"named"`` for whichever alternative it is not.
    Membership tests plus an explicit ``URIRef`` check are the only safe reads.
    """
    from rdflib.term import URIRef

    if "datasetClause" not in query_part:
        return []
    graphs: list[str] = []
    for clause in query_part["datasetClause"]:
        for key in ("default", "named"):
            if key not in clause:
                continue
            value = clause[key]
            if isinstance(value, URIRef):
                graphs.append(str(value))
    return graphs


def enforce_query_scope(sparql: str, tenant_id: str) -> None:
    """Raise :class:`TenantScopeError` unless ``sparql`` is confined to ``tenant_id``.

    Returns ``None`` on success. See the module docstring for the rules, and for
    why rule A is decided by a parser rather than a keyword scan.
    """
    parsed = _parse(sparql)

    names: set[str] = set()
    for part in parsed:
        _walk(part, names)
    if "ServiceGraphPattern" in names:
        raise TenantScopeError(
            "SPARQL federation (SERVICE) is not allowed on this endpoint.", 400
        )

    graphs = dataset_graphs(parsed[1])
    if not graphs:
        raise TenantScopeError(
            "Query must name the graphs it reads. Add a dataset clause, e.g. "
            f"FROM <{GRAPH_NAMESPACE}{tenant_id}> for the ontology graph or "
            f"FROM <{GRAPH_NAMESPACE}{tenant_id}/kg/YOUR_KG> for a knowledge "
            "graph. Without one the store reads every graph it holds.",
            400,
        )
    for graph_uri in graphs:
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

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
   ``https://graph.onta.sh/graphs/`` unless it belongs to the calling tenant.
   This is the belt for A's suspenders. It CATCHES the plainly-spelled inline
   ``GRAPH <victim>``, but do not lean on it there: it is a raw-text scan, so a
   ``\\u``-escaped namespace or an IRI whose match its terminator class truncates
   can read as owned. What actually confines an inline ``GRAPH`` is rule A —
   with a ``FROM`` and no ``FROM NAMED`` the store's named-graph set is empty,
   so the pattern binds nothing. Rule B's real job is defence in depth against a
   divergence between our parser and the store's, not primary enforcement.

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

ONTA-424 — the same leak on LLM-GENERATED SPARQL
------------------------------------------------

``/ask`` and ``/agent`` do not take SPARQL from the caller; they GENERATE it and
hand it to the same store. ``nlp/prompts.py`` teaches the model to write
``FROM <data graph>``, but a prompt is not an enforcement mechanism: a model that
omits or fumbles the dataset clause produces exactly the graph-less query
described above, and the answer comes back looking entirely normal.

:func:`confine_generated_query` is the enforcement for that path. It reuses the
same parser-based extractor and the same ownership test, and differs from
:func:`enforce_query_scope` only in what it does with each verdict:

* A **missing** dataset clause is REPAIRED, not rejected. On the raw route a
  graph-less query is the caller's own mistake and 400 is the honest answer. On
  ``/ask`` the person asking the question never wrote SPARQL and has nothing to
  fix, so failing them for the generator's slip is a self-inflicted outage. The
  repair injects the dataset the ROUTE already chose (the graphs it was going to
  read anyway), so it cannot reach anything the request was not already scoped
  to, and the repaired text is re-parsed and re-checked before it runs.
* A **foreign** graph is never repaired, under any circumstances. Repair only
  ever ADDS the request's own target graphs; it never removes, rewrites or
  "corrects" a clause the generator produced. So there is no input for which
  repair turns a cross-workspace read into an accepted query: the foreign check
  runs first and raises :class:`CrossTenantQueryError`, which callers deliberately
  do NOT feed back to the model as retry advice.
* A query naming a DIFFERENT graph of the SAME workspace is left exactly as it
  is. Nothing crosses the boundary this module defends, and "repairing" it would
  union a second KG into an answer the route scoped to one, which is a semantics
  change wearing a security guard's clothes. Choosing the right KG within a
  workspace is a real problem and a separate one.

The scope for a generated query is wider than one tenant by design: the platform's
Global ontology layers (``…/graphs/global/public``, ``…/graphs/global/enhanced``
and their ``/vN`` release snapshots) are shared, read-only schema that the
subclass-closure walk must see. Those enter through an explicit ``allowed_graphs``
allowlist supplied by the route from the tenant's own visible layer stack, and
entries are structurally filtered (:func:`is_global_layer_graph`) so an allowlist
can never smuggle in another workspace's data graph.
"""

from __future__ import annotations

from cograph_client.graph.iri import GRAPH_URI_PREFIX, IRI_BASE


import re
from collections.abc import Iterable, Sequence

import structlog

logger = structlog.stdlib.get_logger("cograph.graph.sparql_scope")

GRAPH_NAMESPACE = GRAPH_URI_PREFIX

#: Shared, read-only Global ontology layers (ADR 0002 section 1). Not owned by
#: any tenant, and the only graphs outside a tenant's own namespace that a
#: generated query may name.
GLOBAL_LAYER_NAMESPACE = GRAPH_NAMESPACE + "global/"


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


class CrossTenantQueryError(TenantScopeError):
    """A query that names a graph outside the caller's scope.

    A SUBCLASS of :class:`TenantScopeError`, so every existing handler keeps
    working unchanged. It exists so the generated-SPARQL path can tell the one
    outcome that is a SECURITY EVENT apart from the ordinary "this query is not
    scoped yet" case: the unscoped case is repaired or retried, this one is
    logged and propagated, and is never described back to the model as feedback
    it could iterate against.
    """

    def __init__(self, detail: str, status_code: int = 403):
        super().__init__(detail, status_code)


def is_global_layer_graph(graph_uri: str) -> bool:
    """True for a shared Global ontology layer graph (live or ``/vN`` snapshot).

    Deliberately as strict as :func:`tenant_owns_graph`: dot segments,
    backslashes and percent-encoding are refused rather than normalised, so
    ``…/graphs/global/../victim-tenant`` does not read as a Global layer.
    """
    if not graph_uri.startswith(GLOBAL_LAYER_NAMESPACE):
        return False
    remainder = graph_uri[len(GLOBAL_LAYER_NAMESPACE) :]
    if "\\" in remainder or "%" in remainder:
        return False
    return all(segment not in ("", ".", "..") for segment in remainder.split("/"))


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
        # Includes pyparsingf's ParseException and any recursion error on a
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

    Raises :class:`TenantScopeError` on any clause this cannot resolve to a
    concrete IRI. DROPPING such a clause instead would be a silent fail-open,
    and it is a real one: ``SourceSelector`` accepts a PrefixedName, which
    ``parseQuery`` leaves UNEXPANDED as a ``CompValue``. A filter that kept only
    the ``URIRef`` values would report this query as scoped to its first clause
    while the store reads the second:

        PREFIX g: <https://graph.onta.sh/gr>
        SELECT * FROM <...own graph...> FROM g:aphs\\/victim WHERE { ?s ?p ?o }

    ``PN_LOCAL_ESC`` allows a backslash-escaped ``/`` inside a local name, so the
    prefix can be split anywhere and the literal text
    ``https://graph.onta.sh/graphs/`` never appears, which keeps rule B blind too.
    Rejecting prefixed names outright is the honest fix: resolving one means
    trusting a PREFIX declaration to expand exactly the way the store will, and
    the escape rules are precisely where that assumption breaks.

    rdflib's ``CompValue`` has a second trap: ``.get(key)`` on an ABSENT key
    returns the key's own NAME as a string, so ``get("datasetClause")`` is truthy
    on a query with no dataset clause at all, and each clause reports the string
    ``"named"`` for whichever alternative it is not. Membership tests plus an
    explicit ``URIRef`` check are the only safe reads.
    """
    from rdflib.term import URIRef

    if "datasetClause" not in query_part:
        return []
    graphs: list[str] = []
    for clause in query_part["datasetClause"]:
        values = [clause[key] for key in ("default", "named") if key in clause]
        if not values:
            raise TenantScopeError(
                "A FROM / FROM NAMED clause names no graph this endpoint can "
                "resolve.",
                400,
            )
        for value in values:
            if not isinstance(value, URIRef):
                raise TenantScopeError(
                    "Every FROM / FROM NAMED clause must name a full IRI in "
                    f"angle brackets, e.g. FROM <{GRAPH_NAMESPACE}WORKSPACE>. "
                    "Prefixed names are not accepted here.",
                    400,
                )
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
            raise CrossTenantQueryError(
                f"Graph <{graph_uri}> does not belong to workspace "
                f"'{tenant_id}'."
            )

    _reject_foreign_graph_iris(sparql, tenant_id)


def _first_out_of_scope_iri(
    sparql: str, tenant_id: str | None, allowed: Iterable[str] = ()
) -> str | None:
    """Rule B scan: the first graph IRI in the RAW text that is out of scope.

    ``allowed`` names graphs that are in scope despite not belonging to
    ``tenant_id`` (the Global ontology layers on the generated-query path). It
    defaults to empty, so the raw passthrough route is unchanged. One scanner
    for both paths, so a fix to either cannot drift.
    """
    allowed_set = frozenset(allowed)
    for match in _GRAPH_IRI_RE.finditer(sparql):
        graph_uri = match.group(0)
        if graph_uri in allowed_set:
            continue
        if tenant_id and tenant_owns_graph(graph_uri, tenant_id):
            continue
        return graph_uri
    return None


def _reject_foreign_graph_iris(sparql: str, tenant_id: str) -> None:
    """Rule B: no foreign graph IRI anywhere in the RAW text, in any position."""
    graph_uri = _first_out_of_scope_iri(sparql, tenant_id)
    if graph_uri is not None:
        raise CrossTenantQueryError(
            f"Graph <{graph_uri}> does not belong to workspace '{tenant_id}'."
        )


# ---------------------------------------------------------------------------
# Generated SPARQL (ONTA-424)
# ---------------------------------------------------------------------------

#: Where a dataset clause may legally be inserted, most likely first. SPARQL 1.1
#: puts ``DatasetClause*`` immediately before the ``WhereClause``, whose ``WHERE``
#: keyword is itself optional, so a query written ``SELECT * { ... }`` needs the
#: brace anchor. Candidates are only PROPOSALS: each repair is re-parsed and the
#: resulting dataset re-checked, so a candidate that lands inside a string
#: literal, inside a ``CONSTRUCT`` template, or anywhere else wrong is discarded
#: rather than shipped.
_WHERE_ANCHOR_RE = re.compile(r"\bWHERE\b", re.IGNORECASE)
_BRACE_ANCHOR_RE = re.compile(r"\{")

#: How many insertion points a repair will try before giving up.
_MAX_REPAIR_CANDIDATES = 12


def tenant_of_graph(graph_uri: str) -> str | None:
    """The workspace ``graph_uri`` belongs to, or ``None`` when it names none.

    ``None`` is returned for a Global layer graph, for a graph outside the
    platform namespace (a self-hosted store with its own naming), and for
    anything :func:`tenant_owns_graph` would refuse to round-trip. Callers must
    treat ``None`` as "no tenant boundary is encoded here", NOT as "no
    confinement needed": :func:`confine_generated_query` still requires the
    query to name its target graphs in that case.
    """
    if not graph_uri.startswith(GRAPH_NAMESPACE) or is_global_layer_graph(graph_uri):
        return None
    candidate = graph_uri[len(GRAPH_NAMESPACE) :].split("/", 1)[0]
    if not candidate or not tenant_owns_graph(graph_uri, candidate):
        return None
    return candidate


def _in_scope(graph_uri: str, tenant_id: str | None, allowed: frozenset[str]) -> bool:
    """True when a generated query may READ ``graph_uri``."""
    if graph_uri in allowed:
        return True
    return bool(tenant_id) and tenant_owns_graph(graph_uri, tenant_id)


def _is_data_scope(
    graph_uri: str, tenant_id: str | None, defaults: frozenset[str]
) -> bool:
    """True when naming ``graph_uri`` already confines the query to a workspace.

    Strictly narrower than :func:`_in_scope`: the shared Global ontology LAYERS
    are readable but hold no workspace data, so a query naming only those is
    still unconfined and gets repaired. With no tenant to reason about (a
    self-hosted store outside the platform namespace) only the request's own
    target graphs count, which is tighter still.
    """
    if tenant_id:
        return tenant_owns_graph(graph_uri, tenant_id)
    return graph_uri in defaults


def _vetted_allowlist(
    tenant_id: str | None,
    default_graphs: Sequence[str],
    allowed_graphs: Iterable[str],
) -> frozenset[str]:
    """The extra graphs a generated query may name, structurally vetted.

    An allowlist entry is honoured only when it is the tenant's own graph or a
    Global ontology layer. Anything else is DROPPED (and logged) rather than
    trusted: the allowlist exists to admit shared read-only schema, and a caller
    that could widen it arbitrarily would be a second way to reach another
    workspace, which is the hole this module exists to close.

    ``default_graphs`` are always admitted. They are the graphs the ROUTE
    resolved from the authenticated tenant and was going to read regardless, so
    admitting them adds no reach; they are re-checked against ``tenant_id`` by
    the caller below.
    """
    vetted = set(default_graphs)
    for graph_uri in allowed_graphs:
        if not graph_uri:
            continue
        if is_global_layer_graph(graph_uri) or (
            tenant_id and tenant_owns_graph(graph_uri, tenant_id)
        ):
            vetted.add(graph_uri)
        else:
            logger.warning(
                "generated_query_allowlist_entry_refused",
                graph=graph_uri,
                tenant=tenant_id,
            )
    return frozenset(vetted)


def _inject_dataset_clause(
    sparql: str, graphs: Sequence[str], existing: Sequence[str]
) -> str:
    """Return ``sparql`` with ``FROM <g>`` added for each of ``graphs``.

    Verified, not assumed. Every candidate insertion point is re-parsed and the
    resulting dataset compared against ``set(existing) | set(graphs)``: the
    repair is accepted only when the parser agrees the injected clauses took
    effect AND nothing else appeared or vanished. That equality is what makes
    "repair cannot widen scope" a checked property rather than a claim about the
    regex: an insertion that silently landed inside a literal yields a dataset
    still equal to ``existing`` and is rejected, and there is no candidate that
    could ADD a graph other than the ones passed in.

    Raises :class:`TenantScopeError` (400) when no candidate verifies, which
    leaves the query unscoped and therefore unrunnable.

    Candidates are capped: each one costs a full re-parse, and the grammatically
    correct position is the first or second in every real query. Without a cap a
    long generated query full of braces would turn one repair into a quadratic
    parse loop.
    """
    clause = " ".join(f"FROM <{g}>" for g in graphs)
    want = set(existing) | set(graphs)
    positions: list[int] = [m.start() for m in _WHERE_ANCHOR_RE.finditer(sparql)]
    positions += [m.start() for m in _BRACE_ANCHOR_RE.finditer(sparql)]
    for position in positions[:_MAX_REPAIR_CANDIDATES]:
        candidate = f"{sparql[:position]}{clause} {sparql[position:]}"
        try:
            got = dataset_graphs(_parse(candidate)[1])
        except TenantScopeError:
            continue
        if set(got) == want and len(got) == len(existing) + len(graphs):
            return candidate
    raise TenantScopeError(
        "The generated query names no graph to read and could not be scoped to "
        "one. Rewrite it with an explicit dataset clause, e.g. "
        f"FROM <{graphs[0]}>.",
        400,
    )


def confine_generated_query(
    sparql: str,
    *,
    default_graphs: Sequence[str],
    tenant_id: str | None = None,
    allowed_graphs: Iterable[str] = (),
) -> str:
    """Confine LLM-GENERATED SPARQL to the graphs this request already reads.

    Returns the query to execute, which is ``sparql`` itself when it was already
    scoped, or a repaired copy carrying an injected dataset clause. Raises
    :class:`CrossTenantQueryError` (403) when the query reaches outside the
    request's scope, and :class:`TenantScopeError` (400) when it cannot be parsed
    or cannot be scoped.

    ``default_graphs`` is what the route resolved for this request (the data
    graph). ``allowed_graphs`` is the tenant's visible Global layer stack, which
    a generated query may READ but is never repaired TO.

    The order of the checks is the security property: the foreign-graph test runs
    BEFORE any repair, so no input reaches the repair path with a cross-workspace
    clause still in it.
    """
    defaults = [g for g in default_graphs if g]
    # Materialise the allowlist before anything reads it. It is typed as an
    # Iterable, and the repair path re-enters this function with it: a caller
    # passing a generator would have it exhausted by the first pass and see an
    # EMPTY allowlist on the second, which would 403 a legitimate layer-aware
    # query. Fail-closed, but wrong, and only on the repair path, which is
    # exactly the kind of bug that hides.
    allowed_graphs = tuple(allowed_graphs)
    if not defaults:
        raise TenantScopeError(
            "Refusing to run a generated query with no target graph.", 400
        )
    if tenant_id:
        for graph_uri in defaults:
            if not tenant_owns_graph(graph_uri, tenant_id):
                # Caller bug, not model behaviour: the route handed us a target
                # outside the tenant it authorized. Fail rather than repair a
                # query INTO it.
                raise CrossTenantQueryError(
                    f"Refusing to scope a generated query to <{graph_uri}>, which "
                    f"does not belong to workspace '{tenant_id}'."
                )
    allowed = _vetted_allowlist(tenant_id, defaults, allowed_graphs)
    allowed_defaults = frozenset(defaults)

    parsed = _parse(sparql)
    names: set[str] = set()
    for part in parsed:
        _walk(part, names)
    if "ServiceGraphPattern" in names:
        raise TenantScopeError(
            "SPARQL federation (SERVICE) is not allowed in a generated query.", 400
        )

    # Rule A, foreign half. Unresolvable clauses raise 400 from dataset_graphs
    # rather than being dropped, so an unreadable clause can never be mistaken
    # for "no clause" and repaired around.
    graphs = dataset_graphs(parsed[1])
    for graph_uri in graphs:
        if not _in_scope(graph_uri, tenant_id, allowed):
            logger.error(
                "generated_query_cross_tenant_graph",
                security_event="cross_tenant_sparql",
                graph=graph_uri,
                tenant=tenant_id,
                target_graphs=defaults,
            )
            raise CrossTenantQueryError(
                f"The generated query names graph <{graph_uri}>, which is outside "
                "this workspace."
            )

    # Rule B on the RAW text, before repair for the same reason.
    _reject_foreign_graph_iris_or_log(sparql, tenant_id, allowed, defaults)

    # Rule A, scoping half. A query that names no graph holding this WORKSPACE's
    # data is not confined at all: with no dataset clause Neptune reads the union
    # of every named graph on the instance, and one naming only the shared Global
    # layers reads schema instead of data. Both are repaired to the dataset the
    # route already chose.
    #
    # Also repair when the model named the *workspace base graph* (or other
    # non-target tenant graphs) but omitted the route's target KG graph. Named
    # KGs store instance data under ``…/kg/<name>``; ``FROM <…/graphs/tenant>``
    # alone returns empty / wrong rows for kg-scoped /ask (Eval-MH freeze flaky
    # fails: missing kg FROM). Injecting ``defaults`` restores the route's intent
    # without removing model-named in-scope graphs.
    #
    # A query naming a DIFFERENT *kg-specific* graph of the SAME workspace is
    # deliberately left alone. It is not a confinement failure (nothing crosses
    # the workspace boundary), and "repairing" it would silently union a second
    # KG into an answer the route scoped to one. Picking the right KG within a
    # workspace is a separate concern.
    if any(g in allowed_defaults for g in graphs):
        return sparql
    other_kgs = [g for g in graphs if "/kg/" in g and g not in allowed_defaults]
    if other_kgs:
        return sparql
    repaired = _inject_dataset_clause(sparql, defaults, graphs)
    logger.warning(
        "generated_query_scope_repaired",
        tenant=tenant_id,
        injected=defaults,
        named_by_model=graphs,
    )
    # Belt and suspenders: re-run the confinement on the repaired text. This is
    # the assertion that repair is a fixed point, so a future change to the
    # injection cannot quietly produce a query that would not pass the checks
    # above. Recursion terminates because the repaired dataset contains defaults.
    return confine_generated_query(
        repaired,
        default_graphs=defaults,
        tenant_id=tenant_id,
        allowed_graphs=allowed_graphs,
    )


def _reject_foreign_graph_iris_or_log(
    sparql: str,
    tenant_id: str | None,
    allowed: frozenset[str],
    defaults: Sequence[str],
) -> None:
    """Rule B for generated queries: log the security event, then raise."""
    graph_uri = _first_out_of_scope_iri(sparql, tenant_id, allowed)
    if graph_uri is None:
        return
    logger.error(
        "generated_query_cross_tenant_iri",
        security_event="cross_tenant_sparql",
        graph=graph_uri,
        tenant=tenant_id,
        target_graphs=list(defaults),
    )
    raise CrossTenantQueryError(
        f"The generated query mentions graph <{graph_uri}>, which is outside "
        "this workspace."
    )

"""Index-free literal grep over ONE knowledge graph (ONTA-416).

``POST /graphs/{tenant}/grep`` is a plain substring match against the LITERAL
objects of the triples in a single KG graph, answered by a live SPARQL scan.
It exists because the canonical ``/search`` route cannot answer the question it
is repeatedly asked to answer: "is this string anywhere in my graph?".

Why this is a SEPARATE route and not a mode flag on ``/search``
--------------------------------------------------------------
``/search`` is hybrid in RANKING, but both of its legs read the SAME derived
table (``entity_semantic_chunk``), so NEITHER leg is index-free — a value that
has not been indexed yet, or an attribute that was never marked as free text, is
invisible to it, and the route never touches the triple store at all. Its
contract (the locked ONTA-176 one, see ``routes/search.py``) is the exact
opposite of grep's on every axis that matters:

============================  ==============================  ====================
axis                          ``/search``                     ``/grep``
============================  ==============================  ====================
data source                   derived chunk index             live triple store
scope                         all KGs (``kg_name`` optional)  ONE KG (required)
result order                  ranked (RRF-fused legs)         scan order, unranked
``top_k`` ceiling             50 (== ``_CANDIDATES_PER_LEG``) 200 (result cap only)
unknown KG                    empty 200 (index is authority)  empty 200 + honest scan
unit of a hit                 an entity + chunk snippet       ONE matching triple
============================  ==============================  ====================

Folding grep into ``/search`` would therefore mean a single route whose response
shape, ceiling, ordering and freshness semantics all silently flip on a flag —
precisely the drift the interface-convergence rule exists to prevent. So grep
gets its own canonical route, and EVERY interface (MCP ``grep`` tool, SDK
``grep()``, CLI, webapp) rides THIS one route; none may hand-roll a SPARQL scan
of its own.

The two queries
---------------
1. **The scan** — ``SELECT ?s ?p ?o FROM <kg-graph>`` with
   ``isLiteral(?o) && CONTAINS(LCASE(STR(?o)), "needle")``, ``LIMIT limit + 1``.
   The ``+1`` is what makes ``truncated`` HONEST: we ask for one row more than we
   will ever return, so "there is more" is observed rather than inferred from a
   full page.
2. **The decoration** — labels + types for the matched subjects only, in a second
   ``VALUES``-scoped ``SELECT``. Kept out of the scan on purpose: adding two
   OPTIONALs to a full literal scan multiplies the work of the expensive query by
   the cost of the cheap one.

Cost posture (this route CAN be expensive)
------------------------------------------
A ``CONTAINS`` over literals has no supporting index on Neptune (its full-text
search needs an OpenSearch integration this repo does not configure), and Fuseki
scans too. ``LIMIT`` bounds the RESULT, never the SCAN. So the route ships with
guardrails rather than pretending it is cheap:

* ``kg_name`` is REQUIRED and validated against the KG-name charset, so the scan
  is always bounded to one graph and the graph URI is built ONLY by
  ``kg_graph_uri(tenant.tenant_id, kg_name)`` — never from caller-supplied text;
* EVERY caller value that reaches the query is gated before interpolation: the
  needle through ``sparql_string_literal``, ``kg_name`` and ``type`` through
  their charset regexes, a full-URI ``predicate`` through the IRIREF-forbidden
  character check. No input can alter the graph pattern's structure;
* the needle must carry >= 2 non-whitespace characters (a 1-char grep matches
  most of the graph and is never what the caller meant);
* ``limit`` is clamped to [1, 200];
* a dedicated SHORT SPARQL timeout (``COGRAPH_GREP_TIMEOUT_S``, default 15s)
  instead of the client-wide 120s, so a pathological scan fails fast;
* ``@limiter.limit("60/minute")`` per API key;
* ``COGRAPH_GREP_ENABLED`` is an OPT-OUT kill switch (default ON) for an
  operator who wants the surface gone entirely on a large deployment.

Internal predicates
-------------------
Matches are filtered through ``graph/predicates.is_internal_predicate`` so
provenance companions (``attr_meta/…``), ER signals (``er/…``), normalization
bookkeeping and ingest markers never flood the output. The namespace exclusions
are ALSO pushed into the scan query (derived from the same constants, not a
second copy of the list) so internal triples cannot consume the ``LIMIT`` and
silently shrink a page. ``rdfs:label`` is the one deliberate exemption: finding
an entity by its displayed name is the single most common reason to grep.
"""


from __future__ import annotations

from cograph_client.graph.iri import IRI_BASE
import os
import re
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from cograph_client.api.deps import get_neptune_client
from cograph_client.api.rate_limit import limiter
from cograph_client.auth.api_keys import TenantContext, get_tenant
from cograph_client.graph.client import NeptuneClient
from cograph_client.graph.layers import Layer, layer_type_uri, type_name_from_uri
from cograph_client.graph.parser import parse_sparql_results
from cograph_client.graph.predicates import (
    INTERNAL_NAMESPACE_PREFIXES,
    RDF_TYPE,
    RDFS_NS,
    is_internal_predicate,
)
from cograph_client.graph.queries import kg_graph_uri, sparql_string_literal

logger = structlog.stdlib.get_logger("cograph.api.grep")

router = APIRouter(prefix="/graphs/{tenant}")

#: Same charset ``KGCreate`` enforces. A name outside it can never name a real KG,
#: so rejecting it is both a validation nicety and the structural reason no caller
#: input can ever reach the graph URI in a form that could widen the scan.
_KG_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

#: Charset for the ``type`` filter. Same rule as ``kg_name``, and for the same
#: structural reason: ``type`` is interpolated into a type IRI
#: (``layer_type_uri`` → ``f"{namespace}{type_name}"``) and wrapped in ``<…>``.
#: Left unvalidated, a ``>`` would close the IRI early and let a caller append
#: graph patterns, and an ordinary SPACE would emit an illegal IRIREF — a hard
#: parse error surfacing as an opaque 500, exactly the malformed-SPARQL class the
#: escaper promotion exists to eliminate. A type name outside this charset can
#: never have a well-formed IRI, so rejecting it never rejects a reachable type.
_TYPE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

LIMIT_MAX = 200
LIMIT_DEFAULT = 50
#: Minimum non-whitespace characters in the needle. One character matches a large
#: fraction of any real graph, so it is a caller bug, not a query.
MIN_NEEDLE_CHARS = 2
#: Centered window around the match, in characters. Grep output is read by an LLM
#: over MCP; an uncapped literal (a scraped page body, a JSON blob) would blow the
#: context window on a single row.
SNIPPET_CHARS = 200
#: The raw ``value`` is echoed for exactness but is likewise capped.
VALUE_CHARS = 500
LABEL_PRED = f"{RDFS_NS}label"

_DEFAULT_TIMEOUT_S = 15.0


def grep_enabled() -> bool:
    """OPT-OUT kill switch (default **on**).

    Inverted relative to ``COGRAPH_SEMANTIC_INDEX_ENABLED`` (opt-IN) on purpose:
    the semantic index costs embedding spend and storage merely by being on,
    whereas grep costs nothing until someone calls it and is the debugging
    surface users asked for. An operator who does not want an unindexed scan
    reachable at all sets ``COGRAPH_GREP_ENABLED=false`` and the route 503s with
    a message naming the gate.
    """
    raw = os.environ.get("COGRAPH_GREP_ENABLED", "").strip().lower()
    if not raw:
        return True
    return raw in ("1", "true", "yes", "on")


def grep_timeout_s() -> float:
    """Dedicated SPARQL read timeout for the scan (``COGRAPH_GREP_TIMEOUT_S``).

    Much shorter than the client-wide 120s: grep is an interactive debugging aid,
    so a scan that has not answered in seconds is more useful as a fast failure
    than as a two-minute stall holding a connection.
    """
    raw = os.environ.get("COGRAPH_GREP_TIMEOUT_S", "").strip()
    if not raw:
        return _DEFAULT_TIMEOUT_S
    try:
        val = float(raw)
    except ValueError:
        return _DEFAULT_TIMEOUT_S
    return val if val > 0 else _DEFAULT_TIMEOUT_S


class GrepRequest(BaseModel):
    """Body of ``POST /graphs/{tenant}/grep``."""

    q: str = Field(
        ...,
        description=(
            "Substring to look for in literal values. Must contain at least "
            f"{MIN_NEEDLE_CHARS} non-whitespace characters. Plain substring "
            "matching, NOT a regex or a glob."
        ),
    )
    kg_name: str = Field(
        ...,
        description=(
            "REQUIRED context graph to scan. Unlike /search this is not "
            "optional: the scan is index-free, so bounding it to one graph is "
            "the primary cost control."
        ),
    )
    type: Optional[str] = Field(
        None,
        description=(
            "Only match triples whose subject is an instance of this type "
            "(bare type name, e.g. 'Person'). Matched across every ontology "
            "layer namespace, so a Public/Enhanced-typed instance is included. "
            "Must match ^[a-zA-Z0-9_-]+$ — the charset a well-formed type IRI "
            "can carry."
        ),
    )
    predicate: Optional[str] = Field(
        None,
        description=(
            "Only match this predicate. Accepts a full URI, or a bare leaf name "
            "(e.g. 'title') matched against the tail of the predicate URI."
        ),
    )
    case_sensitive: bool = Field(
        False,
        description="Match case-sensitively. Default false (LCASE both sides).",
    )
    limit: int = Field(
        LIMIT_DEFAULT,
        description=(
            f"Maximum matches to return. Clamped server-side to [1, {LIMIT_MAX}]; "
            "the effective value is echoed in the response."
        ),
    )


class GrepMatch(BaseModel):
    """ONE matching triple, decorated with its subject's label and type.

    The unit is a TRIPLE, not an entity (contrast ``/search``): the same entity
    appears once per matching attribute, because "which field did this match in?"
    is the whole point of a grep.
    """

    entity_uri: str
    #: ``rdfs:label`` of the subject; empty when it has none.
    label: str = ""
    #: Bare display type name of the subject; empty when untyped.
    type: str = ""
    #: Full predicate URI of the matching triple.
    predicate: str
    #: Leaf name of ``predicate`` (the human-readable attribute name).
    attr: str
    #: The literal value, truncated to ``VALUE_CHARS`` with a trailing "…".
    value: str
    #: A ``SNIPPET_CHARS`` window CENTERED on the match, elided on both sides.
    snippet: str


class GrepResponse(BaseModel):
    matches: list[GrepMatch] = Field(default_factory=list)
    #: ``len(matches)`` — explicit so thin clients render "N matches" directly.
    count: int = 0
    #: The EFFECTIVE limit after clamping to [1, LIMIT_MAX].
    limit: int = LIMIT_DEFAULT
    #: True when the scan hit the limit and more matches exist. Observed via the
    #: ``LIMIT limit + 1`` over-fetch, never inferred from a full page.
    truncated: bool = False


def _snippet(value: str, needle: str, *, case_sensitive: bool) -> str:
    """A ``SNIPPET_CHARS`` window centered on the first occurrence of ``needle``.

    Falls back to a head window when the needle is not found in the raw value
    (possible only if the store's ``CONTAINS`` and Python disagree on casing for
    some exotic codepoint) — never raises, never returns the whole blob.
    """
    hay = value if case_sensitive else value.lower()
    ndl = needle if case_sensitive else needle.lower()
    if len(value) <= SNIPPET_CHARS:
        return value
    ix = hay.find(ndl)
    if ix < 0:
        return value[:SNIPPET_CHARS] + "…"
    pad = max(0, (SNIPPET_CHARS - len(ndl)) // 2)
    start = max(0, ix - pad)
    end = min(len(value), start + SNIPPET_CHARS)
    out = value[start:end]
    if start > 0:
        out = "…" + out
    if end < len(value):
        out = out + "…"
    return out


def _leaf(uri: str) -> str:
    """Human-readable tail of a predicate URI (after the last ``/`` or ``#``)."""
    tail = uri.rsplit("#", 1)[-1]
    return tail.rsplit("/", 1)[-1]


def _predicate_clause(predicate: str) -> str:
    """SPARQL fragment restricting ``?p`` to the caller's predicate.

    A full URI binds ``?p`` EXACTLY (a ``VALUES`` clause, so the store can use it
    to drive the scan); a bare leaf name degrades to a ``STRENDS`` filter on
    ``"/<leaf>"``, which is the form a user actually types ("title", not the full
    ``https://graph.onta.sh/onto/title``).
    """
    p = predicate.strip()
    if p.startswith("http://") or p.startswith("https://"):
        # Reuse the IRI validation from the shared escaper's sibling by rejecting
        # anything that could close the <...> wrapper early.
        if re.search(r'[<>"{}|\^`\\\x00-\x20]', p):
            raise HTTPException(
                status_code=400, detail=f"invalid predicate IRI: {predicate!r}"
            )
        return f"  VALUES ?p {{ <{p}> }}\n"
    esc = sparql_string_literal("/" + p)
    return f'  FILTER(STRENDS(STR(?p), "{esc}"))\n'


def _type_clause(type_name: str) -> str:
    """SPARQL fragment restricting ``?s`` to instances of ``type_name``.

    Enumerates the type URI in EVERY layer namespace (tenant / Enhanced /
    Public) via ``VALUES``, because instance data is typed with whatever URI the
    writer minted and layer entitlement gates ONTOLOGY visibility, not instance
    triples. A ``VALUES`` list keeps this an index-driven join rather than a
    string filter over every type URI in the graph.
    """
    uris = {layer_type_uri(layer, type_name) for layer in Layer}
    values = " ".join(f"<{u}>" for u in sorted(uris))
    return f"  VALUES ?t {{ {values} }}\n  ?s <{RDF_TYPE}> ?t .\n"


def _internal_predicate_filter() -> str:
    """Push the internal-namespace exclusions INTO the scan.

    Reuses ``predicates.INTERNAL_NAMESPACE_PREFIXES`` rather than restating the
    namespace strings here. Post-filtering alone would be correct but not honest:
    internal triples would consume the ``LIMIT`` and shrink the page invisibly.
    (That tuple is hand-maintained — a namespace added to the classifier but not
    to it merely costs LIMIT honesty for that namespace, never correctness.)

    Note this is a PREfilter only — ``is_internal_predicate`` still runs on every
    row and remains the authority (it also knows the curated per-marker
    exclusions, which are cheaper to apply in Python than as a SPARQL NOT IN).
    """
    clauses = [
        f'!STRSTARTS(STR(?p), "{sparql_string_literal(ns)}")'
        for ns in INTERNAL_NAMESPACE_PREFIXES
    ]
    return f"  FILTER({' && '.join(clauses)})\n"


def _scan_query(
    graph_uri: str,
    needle: str,
    *,
    case_sensitive: bool,
    type_name: Optional[str],
    predicate: Optional[str],
    limit: int,
) -> str:
    if case_sensitive:
        match = f'CONTAINS(STR(?o), "{sparql_string_literal(needle)}")'
    else:
        esc = sparql_string_literal(needle.lower())
        match = f'CONTAINS(LCASE(STR(?o)), "{esc}")'
    body = ""
    if type_name:
        body += _type_clause(type_name)
    body += "  ?s ?p ?o .\n"
    if predicate:
        body += _predicate_clause(predicate)
    body += _internal_predicate_filter()
    body += f"  FILTER(isLiteral(?o) && {match})\n"
    # DISTINCT only under a type filter: the rdf:type join can bind ?t more than
    # once for an entity typed in two layer namespaces under the same name, which
    # would emit the SAME triple twice and burn the caller's limit on a duplicate.
    # Without that join a triple is unique, so DISTINCT would be pure overhead on
    # the expensive path.
    select = "SELECT DISTINCT ?s ?p ?o" if type_name else "SELECT ?s ?p ?o"
    # LIMIT limit + 1: the extra row is never returned, it only tells us
    # truthfully whether more matches exist.
    return f"{select} FROM <{graph_uri}> WHERE {{\n{body}}} LIMIT {limit + 1}"


def _decorate_query(graph_uri: str, subjects: list[str]) -> str:
    """Labels + primary types for the matched subjects only.

    ``VALUES``-scoped so this is a handful of point lookups, not a second scan.
    Both bindings are OPTIONAL: an unlabeled or untyped subject must still return
    its match rather than vanish from the page.
    """
    values = " ".join(f"<{u}>" for u in subjects)
    return (
        f"SELECT ?s ?label ?type FROM <{graph_uri}> WHERE {{\n"
        f"  VALUES ?s {{ {values} }}\n"
        f"  OPTIONAL {{ ?s <{LABEL_PRED}> ?label }}\n"
        f"  OPTIONAL {{ ?s <{RDF_TYPE}> ?type }}\n"
        f"}}"
    )


def _truncate(value: str, cap: int) -> str:
    return value if len(value) <= cap else value[:cap] + "…"


@router.post("/grep", response_model=GrepResponse)
@limiter.limit("60/minute")
async def grep_graph(
    request: Request,
    body: GrepRequest,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
) -> GrepResponse:
    """Literal substring scan over one KG's triples. See the module docstring.

    Auth is the same ``get_tenant`` dependency as every ``/graphs/{tenant}``
    route, and the scanned graph URI is built ONLY from the RESOLVED tenant id
    plus a charset-validated KG name — a caller can never widen the scan beyond
    the graph its key authorizes. (Explicitly NOT built on
    ``POST /graphs/{tenant}/query``, which executes caller SPARQL verbatim with
    no graph scoping — ONTA-412; a grep layered on that route would inherit its
    cross-tenant read hazard.)

    ``request`` is required positionally by slowapi's ``@limiter.limit``, which
    reads the API key off it for the per-key bucket.
    """
    if not grep_enabled():
        raise HTTPException(
            status_code=503,
            detail=(
                "literal grep is disabled on this deployment "
                "(COGRAPH_GREP_ENABLED)"
            ),
        )

    needle = body.q.strip()
    # Count NON-WHITESPACE chars: "  a  " is a 1-character needle wearing a
    # disguise, and would match a large fraction of any real graph.
    if len("".join(needle.split())) < MIN_NEEDLE_CHARS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"q must contain at least {MIN_NEEDLE_CHARS} non-whitespace "
                "characters (a shorter needle matches most of the graph)"
            ),
        )

    kg_name = body.kg_name.strip()
    if not _KG_NAME_RE.match(kg_name):
        raise HTTPException(
            status_code=400,
            detail=(
                "kg_name must match ^[a-zA-Z0-9_-]+$ (the KG naming charset)"
            ),
        )

    type_name = body.type.strip() if body.type else None
    if type_name and not _TYPE_NAME_RE.match(type_name):
        # Validated for the same structural reason as kg_name: `type` is
        # interpolated into a type IRI and wrapped in <…>, so a `>` would close
        # the IRI early and a space would emit an illegal IRIREF (an opaque 500).
        raise HTTPException(
            status_code=400,
            detail="type must match ^[a-zA-Z0-9_-]+$ (the type-name charset)",
        )

    limit = max(1, min(body.limit, LIMIT_MAX))
    graph_uri = kg_graph_uri(tenant.tenant_id, kg_name)

    sparql = _scan_query(
        graph_uri,
        needle,
        case_sensitive=body.case_sensitive,
        type_name=type_name,
        predicate=body.predicate.strip() if body.predicate else None,
        limit=limit,
    )
    raw = await client.query(sparql, timeout=grep_timeout_s())
    _, rows = parse_sparql_results(raw)

    # `truncated` from the OVER-FETCH, before any post-filtering: it answers
    # "did the scan stop early?", which is a property of the scan, not of the
    # rows that survived the internal-predicate filter.
    truncated = len(rows) > limit
    rows = rows[:limit]

    kept: list[dict] = []
    for r in rows:
        p_uri = r.get("p", "")
        # Authoritative internal-predicate filter (the SPARQL side is only a
        # prefilter). rdfs:label is the deliberate exemption — matching an
        # entity by its displayed name is the commonest reason to grep, and the
        # shared classifier calls label a system predicate.
        if p_uri != LABEL_PRED and is_internal_predicate(p_uri):
            continue
        kept.append(r)

    subjects = list(dict.fromkeys(r.get("s", "") for r in kept if r.get("s")))
    labels: dict[str, str] = {}
    types: dict[str, str] = {}
    if subjects:
        try:
            _, drows = parse_sparql_results(
                await client.query(
                    _decorate_query(graph_uri, subjects),
                    timeout=grep_timeout_s(),
                )
            )
        except Exception as exc:  # noqa: BLE001 — decoration is cosmetic
            # A failed decoration must degrade to bare URIs, never lose the
            # matches the (expensive) scan already paid for.
            logger.warning("grep_decorate_failed", error=str(exc)[:500])
            drows = []
        for d in drows:
            s = d.get("s", "")
            if not s:
                continue
            if d.get("label") and s not in labels:
                labels[s] = d["label"]
            t_uri = d.get("type", "")
            if t_uri:
                name = type_name_from_uri(t_uri)
                # Primary type = lexicographically smallest, the SAME tie-break
                # the Explorer's _PRIMARY_TYPE_GUARD uses, so one entity gets one
                # stable display type across surfaces.
                if name and (s not in types or name < types[s]):
                    types[s] = name

    matches = [
        GrepMatch(
            entity_uri=r.get("s", ""),
            label=labels.get(r.get("s", ""), ""),
            type=types.get(r.get("s", ""), ""),
            predicate=r.get("p", ""),
            attr=_leaf(r.get("p", "")),
            value=_truncate(r.get("o", ""), VALUE_CHARS),
            snippet=_snippet(
                r.get("o", ""), needle, case_sensitive=body.case_sensitive
            ),
        )
        for r in kept
    ]

    logger.info(
        "grep_scan",
        tenant=tenant.tenant_id,
        kg=kg_name,
        matches=len(matches),
        truncated=truncated,
    )
    return GrepResponse(
        matches=matches,
        count=len(matches),
        limit=limit,
        truncated=truncated,
    )

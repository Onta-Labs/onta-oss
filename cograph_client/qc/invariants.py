"""Deterministic KG invariant checks — the un-gameable backbone of the QC eval loop.

RDFUnit-style: each invariant is a SPARQL graph-pattern query that RETURNS violations.
No LLM, no golden output — structural properties any faithfully-produced KG must hold,
so they generalize across domains instead of overfitting to fixtures. Cheap enough to
run per-PR as a hard gate and per-domain in the nightly loop.

ONTA-208: the instance-edge predicate convention + node completeness — the exact bug
class fixed in onta-oss #123/#125/#126/#127:
  * a node-valued relationship edge written on the ``attrs/<leaf>`` DECLARATION
    predicate instead of ``onto/<leaf>`` (the instance-edge predicate the NL planner
    queries) — invisible to natural-language queries (#123/#127);
  * a relationship edge pointing at a raw LITERAL instead of a node — a dangling value.
    NOTE this is NOT "any literal on ``onto/<leaf>``": ``onto/`` is a MIXED namespace —
    the resolver stamps system-predicate literals (``onto/ingested_at``, ``onto/source``;
    also ``onto/lambda_refreshed_at`` etc. — see ``SYSTEM_PREDICATES`` in
    ``api/routes/explore.py``) on every entity, so a blanket check false-positives on
    every clean ingest. This check is DECLARATION-AWARE: it flags a literal only where
    the property is DECLARED a relationship (``attrs/<leaf> rdfs:range <types/…>``), which
    needs the ontology graph joined in (pass ``onto_graph_uri``);
  * a promoted target node left bare (no ``rdf:type`` / ``rdfs:label``) — unlistable (#125).

Designed to run over ANY produced graph: the decomposition harness, the nightly loop,
or a one-off backfill audit. See docs/specs/continuous_kg_qc_eval_spec.md §4a + Appendix A.
Follow-ups (later slices of ONTA-208): SHACL per-type cardinality/datatype shapes
(pySHACL); metamorphic checks (order/paraphrase/superset/batch invariance) via Hypothesis.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

# --- URI scheme (mirrors graph/ontology_queries) --------------------------------
ENTITY_PREFIX = "https://cograph.tech/entities/"
ONTO_PREFIX = "https://cograph.tech/onto/"
TYPES_PREFIX = "https://cograph.tech/types/"
ATTRS_INFIX = "/attrs/"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
RDFS_RANGE = "http://www.w3.org/2000/01/rdf-schema#range"
RDFS_SUBCLASSOF = "http://www.w3.org/2000/01/rdf-schema#subClassOf"


@dataclass(frozen=True)
class Violation:
    """One invariant breach found in a produced graph."""

    invariant: str
    severity: str  # "error" | "warn"
    detail: str
    binding: dict = field(default_factory=dict)


def _scoped(graph_uri: Optional[str], body: str) -> str:
    """Wrap a triple pattern in ``GRAPH <uri> { }`` when a graph is given; else leave
    it against the default graph (a union-of-named-graphs store then sees everything).
    Used identically for every pattern in a query so each is scoped to its own graph
    (instance vs. ontology) or, when ``None``, the union."""
    return f"GRAPH <{graph_uri}> {{ {body} }}" if graph_uri else body


def _val(binding: dict, key: str) -> str:
    cell = binding.get(key)
    return cell.get("value", "") if isinstance(cell, dict) else ""


@dataclass(frozen=True)
class Invariant:
    name: str
    severity: str
    description: str
    # (instance graph_uri, ontology onto_graph_uri) -> SPARQL SELECT returning violations.
    sparql_fn: Callable[[Optional[str], Optional[str]], str]
    detail_fn: Callable[[dict], str]
    # True if the query joins the ontology (declarations); skipped when no onto graph.
    needs_onto: bool = False

    def sparql(self, graph_uri: Optional[str], onto_graph_uri: Optional[str] = None) -> str:
        return self.sparql_fn(graph_uri, onto_graph_uri)

    def to_violation(self, binding: dict) -> Violation:
        return Violation(self.name, self.severity, self.detail_fn(binding), binding)


INVARIANTS: list[Invariant] = [
    Invariant(
        name="node_edge_on_attrs_predicate",
        severity="error",
        description=(
            "A node-valued relationship edge is written on the attrs/<leaf> DECLARATION "
            "predicate instead of onto/<leaf>. The NL planner queries relationships only "
            "on onto/<leaf> (no attrs/ fallback), so the edge is invisible to NL "
            "(onta-oss #123/#127)."
        ),
        sparql_fn=lambda g, og: (
            "SELECT ?s ?p ?o WHERE { "
            + _scoped(g, "?s ?p ?o .")
            + f' FILTER(isIRI(?o) && STRSTARTS(STR(?o), "{ENTITY_PREFIX}"))'
            + f' FILTER(STRSTARTS(STR(?p), "{TYPES_PREFIX}") && CONTAINS(STR(?p), "{ATTRS_INFIX}"))'
            + " }"
        ),
        detail_fn=lambda b: (
            f"{_val(b, 's')} --[{_val(b, 'p')}]--> {_val(b, 'o')} "
            "(node-valued edge on attrs/<leaf> — NL-invisible; belongs on onto/<leaf>)"
        ),
    ),
    Invariant(
        name="relationship_edge_points_at_literal",
        severity="error",
        description=(
            "A property DECLARED a relationship (attrs/<leaf> rdfs:range <types/…>) has an "
            "instance edge (onto/<leaf>) pointing at a LITERAL instead of an entity node — "
            "a dangling raw value in a node-valued slot. TYPE-SCOPED + declaration-aware: "
            "binds the SUBJECT's own rdf:type and checks THAT type's declaration "
            "(types/<Type>/attrs/<leaf>), so (a) system predicates (onto/ingested_at, "
            "onto/source, …) are never flagged, and (b) a leaf that is a relationship on one "
            "type but a literal attribute on another does not cross-contaminate. Needs the "
            "ontology graph (onto_graph_uri)."
        ),
        sparql_fn=lambda g, og: (
            # DISTINCT: a property with >1 declared range would otherwise emit one identical
            # (?s ?p ?o) row per range — dedupe to one violation per bad edge.
            "SELECT DISTINCT ?s ?p ?o WHERE { "
            + _scoped(g, f"?s ?p ?o . ?s <{RDF_TYPE}> ?stype .")
            + f' FILTER(STRSTARTS(STR(?p), "{ONTO_PREFIX}") && isLiteral(?o)) '
            # the declaration URI for THIS subject's type: types/<Type>/attrs/<leaf>.
            + f' BIND(IRI(CONCAT(STR(?stype), "{ATTRS_INFIX}", STRAFTER(STR(?p), "{ONTO_PREFIX}"))) AS ?decl) '
            + _scoped(og, f"?decl <{RDFS_RANGE}> ?range .")
            + f' FILTER(STRSTARTS(STR(?range), "{TYPES_PREFIX}")) '
            + "}"
        ),
        detail_fn=lambda b: (
            f'{_val(b, "s")} --[{_val(b, "p")}]--> "{_val(b, "o")}" '
            "(a declared relationship's instance edge points at a literal, not a node)"
        ),
        needs_onto=True,
    ),
    Invariant(
        name="edge_target_type_mismatch",
        severity="error",
        description=(
            "A relationship edge (onto/<leaf>) points at an entity node whose rdf:type "
            "satisfies NONE of the declared ranges (attrs/<leaf> rdfs:range <types/R>) — the "
            "target is a real node but the WRONG kind of thing (e.g. works_at -> a City). The "
            "next check after relationship_edge_points_at_literal: that one asks 'is the target "
            "a node at all?', this asks 'is it the RIGHT node?'. TYPE-SCOPED on the SUBJECT's "
            "own declaration; subclass-aware (rdfs:subClassOf+ plus the exact range as the "
            "reflexive case, so it never depends on zero-length-path-in-graph semantics); "
            "multi-type-tolerant (OK if ANY of the target's types is at/under a range); "
            "multi-RANGE-tolerant (ranges are DISJUNCTIVE alternatives — matching ANY declared "
            "range is valid, so it fires only when the target satisfies none); and it SKIPS "
            "untyped targets — that is bare_entity_node_missing_type's job. Needs the ontology "
            "graph (onto_graph_uri)."
        ),
        # DISJUNCTIVE over ranges: don't project ?range and test per-range (that flags a
        # valid target against a range it happens not to match when a property has >1 range).
        # Instead fire only when NO declared types/ range is satisfied by ANY of the target's
        # types — the whole range-membership test lives inside the negative.
        sparql_fn=lambda g, og: (
            "SELECT DISTINCT ?s ?p ?o WHERE { "
            + _scoped(g, f"?s ?p ?o . ?s <{RDF_TYPE}> ?stype .")
            + f' FILTER(STRSTARTS(STR(?p), "{ONTO_PREFIX}") && isIRI(?o) '
            + f' && STRSTARTS(STR(?o), "{ENTITY_PREFIX}")) '
            + f' BIND(IRI(CONCAT(STR(?stype), "{ATTRS_INFIX}", STRAFTER(STR(?p), "{ONTO_PREFIX}"))) AS ?decl) '
            # the property must be DECLARED a relationship: at least one types/ range.
            + " FILTER EXISTS { " + _scoped(og, f"?decl <{RDFS_RANGE}> ?anyr .")
            + f' FILTER(STRSTARTS(STR(?anyr), "{TYPES_PREFIX}")) ' + " } "
            # target must be typed at all — an untyped target is the bare-node invariant's job.
            + " FILTER EXISTS { " + _scoped(g, f"?o <{RDF_TYPE}> ?anytype .") + " } "
            # ...and NO declared types/ range is satisfied — as the range itself (reflexive) or
            # a proper subclass — by any of the target's types.
            + " FILTER NOT EXISTS { "
            + _scoped(og, f"?decl <{RDFS_RANGE}> ?range .")
            + f' FILTER(STRSTARTS(STR(?range), "{TYPES_PREFIX}")) '
            + "{ " + _scoped(g, f"?o <{RDF_TYPE}> ?range .") + " } UNION "
            + "{ " + _scoped(g, f"?o <{RDF_TYPE}> ?ot .")
            + _scoped(og, f"?ot <{RDFS_SUBCLASSOF}>+ ?range .") + " } "
            + " } "
            + "}"
        ),
        detail_fn=lambda b: (
            f'{_val(b, "s")} --[{_val(b, "p")}]--> {_val(b, "o")} '
            "(relationship target's type is not the declared range nor a subclass of it)"
        ),
        needs_onto=True,
    ),
    # INV3/INV4 assume an entity's rdf:type + rdfs:label live in the SAME graph as its
    # edges (true today — the resolver co-writes them to the instance graph). If typing
    # ever moves to a separate named graph, run these against the union (graph_uri=None)
    # or they would false-positive on a "missing" type/label that lives elsewhere.
    Invariant(
        name="bare_entity_node_missing_type",
        severity="error",
        description=(
            "An entity node referenced by an edge has no rdf:type — a bare/untyped node, "
            "invisible to 'list all <Type>' (onta-oss #125 bare-node class)."
        ),
        sparql_fn=lambda g, og: (
            "SELECT DISTINCT ?node WHERE { "
            + _scoped(g, "?s ?p ?node .")
            + f' FILTER(isIRI(?node) && STRSTARTS(STR(?node), "{ENTITY_PREFIX}"))'
            + " FILTER NOT EXISTS { "
            + _scoped(g, f"?node <{RDF_TYPE}> ?t .")
            + " } }"
        ),
        detail_fn=lambda b: f"{_val(b, 'node')} (entity node referenced by an edge but has no rdf:type)",
    ),
    Invariant(
        name="bare_entity_node_missing_label",
        severity="warn",
        description="A typed entity node has no rdfs:label — unlabelled, harder to surface.",
        sparql_fn=lambda g, og: (
            "SELECT DISTINCT ?node WHERE { "
            + _scoped(g, f"?node <{RDF_TYPE}> ?t .")
            + f' FILTER(isIRI(?node) && STRSTARTS(STR(?node), "{ENTITY_PREFIX}"))'
            + " FILTER NOT EXISTS { "
            + _scoped(g, f"?node <{RDFS_LABEL}> ?l .")
            + " } }"
        ),
        detail_fn=lambda b: f"{_val(b, 'node')} (typed entity node has no rdfs:label)",
    ),
]

_SEVERITY_RANK = {"error": 0, "warn": 1}


async def check_invariants(
    neptune,
    graph_uri: Optional[str] = None,
    *,
    onto_graph_uri: Optional[str] = None,
    include: Optional[set[str]] = None,
) -> list[Violation]:
    """Run the deterministic invariants over ``graph_uri`` (the instance graph, or the
    whole store / default graph if ``None``) and return every violation, most-severe first.

    ``neptune`` is any client exposing ``async query(sparql) -> dict`` returning SPARQL-1.1
    JSON (the production NeptuneClient, the harness store, and the pyoxigraph test shim all
    satisfy this). ``onto_graph_uri`` is the graph holding the ontology DECLARATIONS
    (``attrs/<leaf> rdfs:range …``); invariants that need it (``needs_onto=True``, e.g.
    ``relationship_edge_points_at_literal``) are SKIPPED when it is ``None`` — pass it (or,
    on a union-of-named-graphs store, the tenant graph) to get their coverage. ``include``
    optionally restricts to a subset of invariant names.
    """
    selected = [inv for inv in INVARIANTS if include is None or inv.name in include]
    violations: list[Violation] = []
    for inv in selected:
        if inv.needs_onto and onto_graph_uri is None:
            continue
        result = await neptune.query(inv.sparql(graph_uri, onto_graph_uri))
        for binding in result.get("results", {}).get("bindings", []):
            violations.append(inv.to_violation(binding))
    violations.sort(key=lambda v: _SEVERITY_RANK.get(v.severity, 9))
    return violations

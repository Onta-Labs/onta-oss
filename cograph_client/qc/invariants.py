"""Deterministic KG invariant checks — the un-gameable backbone of the QC eval loop.

RDFUnit-style: each invariant is a SPARQL graph-pattern query that RETURNS violations.
No LLM, no golden output — structural properties any faithfully-produced KG must hold,
so they generalize across domains instead of overfitting to fixtures. Cheap enough to
run per-PR as a hard gate and per-domain in the nightly loop.

First slice (ONTA-208): the instance-edge predicate convention + node completeness +
referential integrity — the exact bug class fixed in onta-oss #123/#126/#127:
  * a node-valued relationship edge written on the ``attrs/<leaf>`` DECLARATION
    predicate instead of ``onto/<leaf>`` (the instance-edge predicate the NL planner
    queries) — the edge is invisible to natural-language queries;
  * a promoted target node left bare (no ``rdf:type`` / ``rdfs:label``) — unlistable;
  * a relationship edge pointing at a raw literal instead of a node — a dangling value.

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
    Used identically for the main pattern and any ``FILTER NOT EXISTS`` sub-pattern so
    the two are scoped to the same graph."""
    return f"GRAPH <{graph_uri}> {{ {body} }}" if graph_uri else body


def _val(binding: dict, key: str) -> str:
    cell = binding.get(key)
    return cell.get("value", "") if isinstance(cell, dict) else ""


@dataclass(frozen=True)
class Invariant:
    name: str
    severity: str
    description: str
    sparql_fn: Callable[[Optional[str]], str]
    detail_fn: Callable[[dict], str]

    def sparql(self, graph_uri: Optional[str]) -> str:
        return self.sparql_fn(graph_uri)

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
        sparql_fn=lambda g: (
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
            "A relationship instance edge (onto/<leaf>) points at a LITERAL instead of "
            "an entity node — a dangling raw value in a node-valued slot."
        ),
        sparql_fn=lambda g: (
            "SELECT ?s ?p ?o WHERE { "
            + _scoped(g, "?s ?p ?o .")
            + f' FILTER(STRSTARTS(STR(?p), "{ONTO_PREFIX}") && isLiteral(?o))'
            + " }"
        ),
        detail_fn=lambda b: (
            f'{_val(b, "s")} --[{_val(b, "p")}]--> "{_val(b, "o")}" '
            "(relationship edge points at a literal, not a node)"
        ),
    ),
    Invariant(
        name="bare_entity_node_missing_type",
        severity="error",
        description=(
            "An entity node referenced by an edge has no rdf:type — a bare/untyped node, "
            "invisible to 'list all <Type>' (onta-oss #125 bare-node class)."
        ),
        sparql_fn=lambda g: (
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
        sparql_fn=lambda g: (
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
    include: Optional[set[str]] = None,
) -> list[Violation]:
    """Run the deterministic invariants over ``graph_uri`` (or the whole store /
    default graph if ``None``) and return every violation, most-severe first.

    ``neptune`` is any client exposing ``async query(sparql) -> dict`` that returns
    SPARQL-1.1 JSON results (the production NeptuneClient, the harness store, and the
    pyoxigraph test shim all satisfy this). ``include`` optionally restricts to a
    subset of invariant names.
    """
    selected = [inv for inv in INVARIANTS if include is None or inv.name in include]
    violations: list[Violation] = []
    for inv in selected:
        result = await neptune.query(inv.sparql(graph_uri))
        for binding in result.get("results", {}).get("bindings", []):
            violations.append(inv.to_violation(binding))
    violations.sort(key=lambda v: _SEVERITY_RANK.get(v.severity, 9))
    return violations

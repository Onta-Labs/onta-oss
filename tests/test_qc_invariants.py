"""Tests for the deterministic KG invariant library (`cograph_client.qc.invariants`).

Two layers, mirroring the repo's split for SPARQL-backed code:
- **CI-safe** structural + runner tests (no SPARQL engine) — the catalogue shape, the
  graph-scoping, the query patterns, and the runner's parse/sort/include/needs-onto
  plumbing.
- **pyoxigraph** semantic tests (importorskip via the `n` fixture; run locally, skipped
  in CI) — seed real triples and prove each invariant catches its bug class AND that a
  correct graph produces ZERO violations (no false positives — the property that
  matters most for a per-PR gate). The declaration-aware relationship check is proven
  to catch a real relationship-edge-on-literal while NOT flagging system-predicate
  literals (onto/ingested_at) that legitimately share the onto/ namespace.
"""
from __future__ import annotations

import json

import pytest

from cograph_client.qc import INVARIANTS, Violation, check_invariants
from cograph_client.qc.invariants import (
    RDF_TYPE,
    RDFS_LABEL,
    RDFS_RANGE,
    _val,
)

ENT = "https://cograph.tech/entities/"
ONTO = "https://cograph.tech/onto/"
TYPES = "https://cograph.tech/types/"
G = "https://omnix.dev/graphs/qc-test"        # instance graph
ONTO_G = "https://omnix.dev/graphs/qc-onto"   # ontology (declaration) graph


# --------------------------------------------------------------------------- #
# CI-safe: catalogue + query construction + runner plumbing (no SPARQL engine)
# --------------------------------------------------------------------------- #
class _FixedNeptune:
    """Returns the same canned bindings for every query — exercises the runner's
    parse/sort/include/needs-onto logic without needing a real triplestore."""

    def __init__(self, bindings: list[dict]):
        self._bindings = bindings

    async def query(self, sparql: str) -> dict:
        return {"results": {"bindings": list(self._bindings)}}


def test_invariant_catalogue_shape():
    assert [i.name for i in INVARIANTS] == [
        "node_edge_on_attrs_predicate",
        "relationship_edge_points_at_literal",
        "bare_entity_node_missing_type",
        "bare_entity_node_missing_label",
    ]
    assert [i.severity for i in INVARIANTS] == ["error", "error", "error", "warn"]
    # only the declaration-aware relationship check needs the ontology graph.
    assert [i.needs_onto for i in INVARIANTS] == [False, True, False, False]
    assert all(i.description for i in INVARIANTS)


def test_sparql_graph_scoping():
    inv = INVARIANTS[0]
    assert "GRAPH <" not in inv.sparql(None)  # default graph / union
    assert "GRAPH <https://g/1>" in inv.sparql("https://g/1")


def test_sparql_encodes_the_right_patterns():
    by = {i.name: i.sparql(G, ONTO_G) for i in INVARIANTS}
    q1 = by["node_edge_on_attrs_predicate"]
    assert ENT in q1 and "/attrs/" in q1 and TYPES in q1 and "isIRI(?o)" in q1
    q2 = by["relationship_edge_points_at_literal"]
    assert ONTO in q2 and "isLiteral(?o)" in q2 and RDFS_RANGE in q2 and RDF_TYPE in q2
    assert "STRAFTER" in q2 and "CONCAT" in q2 and f"GRAPH <{ONTO_G}>" in q2
    q3 = by["bare_entity_node_missing_type"]
    assert "FILTER NOT EXISTS" in q3 and RDF_TYPE in q3 and ENT in q3
    q4 = by["bare_entity_node_missing_label"]
    assert "FILTER NOT EXISTS" in q4 and RDFS_LABEL in q4


def test_val_parsing():
    assert _val({"x": {"value": "v"}}, "x") == "v"
    assert _val({}, "x") == ""
    assert _val({"x": "not-a-cell"}, "x") == ""


@pytest.mark.asyncio
async def test_runner_collects_one_violation_per_invariant_sorted_errors_first():
    binding = {k: {"value": k.upper()} for k in ("s", "p", "o", "node")}
    # pass onto_graph_uri so the needs_onto invariant runs too.
    vs = await check_invariants(_FixedNeptune([binding]), onto_graph_uri="x")
    assert len(vs) == len(INVARIANTS)
    assert all(isinstance(v, Violation) for v in vs)
    assert [v.severity for v in vs] == ["error", "error", "error", "warn"]
    assert vs[0].detail


@pytest.mark.asyncio
async def test_runner_skips_needs_onto_when_no_onto_graph():
    binding = {"s": {"value": "S"}, "p": {"value": "P"}, "o": {"value": "O"}}
    without = {v.invariant for v in await check_invariants(_FixedNeptune([binding]))}
    assert "relationship_edge_points_at_literal" not in without
    with_onto = {
        v.invariant
        for v in await check_invariants(_FixedNeptune([binding]), onto_graph_uri="x")
    }
    assert "relationship_edge_points_at_literal" in with_onto


@pytest.mark.asyncio
async def test_runner_include_filter_restricts_to_named_invariants():
    vs = await check_invariants(
        _FixedNeptune([{"o": {"value": "x"}}]),
        include={"node_edge_on_attrs_predicate"},
    )
    assert [v.invariant for v in vs] == ["node_edge_on_attrs_predicate"]


@pytest.mark.asyncio
async def test_runner_no_bindings_no_violations():
    assert await check_invariants(_FixedNeptune([])) == []


# --------------------------------------------------------------------------- #
# pyoxigraph: real SPARQL semantics (local only; skipped where pyoxigraph absent)
# --------------------------------------------------------------------------- #
class PyoxiNeptune:
    """Minimal NeptuneClient shim over an in-process pyoxigraph Store (lazy import so
    the module still loads for the CI-safe tests when pyoxigraph is absent)."""

    def __init__(self) -> None:
        from pyoxigraph import Store

        self.store = Store()

    async def query(self, sparql: str) -> dict:
        from pyoxigraph import QueryResultsFormat

        results = self.store.query(sparql, use_default_graph_as_union=True)
        return json.loads(results.serialize(format=QueryResultsFormat.JSON))

    async def update(self, sparql: str) -> None:
        self.store.update(sparql)


@pytest.fixture
def n():
    pytest.importorskip("pyoxigraph")
    return PyoxiNeptune()


async def _insert(n: PyoxiNeptune, triples: str, graph: str = G) -> None:
    await n.update(f"INSERT DATA {{ GRAPH <{graph}> {{ {triples} }} }}")


_GOOD_PHYS = (
    f"<{ENT}Physician/p1> <{RDF_TYPE}> <{TYPES}Physician> ; <{RDFS_LABEL}> \"Dr P\" . "
)
_GOOD_CITY = (
    f"<{ENT}City/SF> <{RDF_TYPE}> <{TYPES}City> ; <{RDFS_LABEL}> \"San Francisco\" . "
)
# ontology declaration: located_in is a relationship (range City).
_DECL_LOCATED_IN = f"<{TYPES}Physician/attrs/located_in> <{RDFS_RANGE}> <{TYPES}City> . "


@pytest.mark.asyncio
async def test_clean_graph_has_zero_violations(n):
    """The no-false-positives property, against REALISTIC output: a correct fact
    (relationship edge on onto/<leaf> to a typed + labelled node) + the system
    predicates the resolver stamps on every entity (onto/ingested_at, onto/source —
    literals on onto/-prefixed predicates) + a real relationship DECLARATION for
    located_in must trip NOTHING. Guards the 'onto/ is a mixed namespace' FP class."""
    await _insert(
        n,
        _GOOD_PHYS + _GOOD_CITY
        + f"<{ENT}Physician/p1> <{ONTO}located_in> <{ENT}City/SF> . "
        + f'<{ENT}Physician/p1> <{ONTO}ingested_at> "2026-07-06T12:00:00+00:00" . '
        + f'<{ENT}Physician/p1> <{ONTO}source> "imdb.csv" . '
        + f'<{ENT}City/SF> <{ONTO}ingested_at> "2026-07-06T12:00:00+00:00" . ',
    )
    await _insert(n, _DECL_LOCATED_IN, graph=ONTO_G)
    assert await check_invariants(n, G, onto_graph_uri=ONTO_G) == []


@pytest.mark.asyncio
async def test_node_valued_edge_on_attrs_is_caught(n):
    """The #123/#127 bug: a node-valued relationship edge on the attrs/<leaf>
    declaration predicate (NL-invisible)."""
    await _insert(
        n,
        _GOOD_PHYS + _GOOD_CITY
        + f"<{ENT}Physician/p1> <{TYPES}Physician/attrs/located_in> <{ENT}City/SF> . ",
    )
    vs = await check_invariants(n, G, onto_graph_uri=ONTO_G)
    assert [v.invariant for v in vs] == ["node_edge_on_attrs_predicate"]
    assert vs[0].severity == "error"


@pytest.mark.asyncio
async def test_declared_relationship_edge_on_literal_is_caught(n):
    """The declaration-aware check: a property DECLARED a relationship (attrs/rating
    rdfs:range Rating) whose instance edge points at a raw literal."""
    await _insert(n, _GOOD_PHYS + f'<{ENT}Physician/p1> <{ONTO}rating> "4.6" . ')
    await _insert(n, f"<{TYPES}Physician/attrs/rating> <{RDFS_RANGE}> <{TYPES}Rating> . ", graph=ONTO_G)
    vs = await check_invariants(n, G, onto_graph_uri=ONTO_G)
    assert [v.invariant for v in vs] == ["relationship_edge_points_at_literal"]
    assert vs[0].severity == "error"


@pytest.mark.asyncio
async def test_cross_type_same_leaf_literal_attribute_not_flagged(n):
    """Type-scoping: a leaf that is a RELATIONSHIP on one type but a LITERAL attribute on
    another must not cross-contaminate. A Physician with onto/rating "4.6" is NOT flagged
    when Physician.rating is declared a literal (xsd:float), even though Movie.rating is a
    relationship (types/Rating) — only the SUBJECT's own type declaration is consulted."""
    await _insert(n, _GOOD_PHYS + f'<{ENT}Physician/p1> <{ONTO}rating> "4.6" . ')
    await _insert(
        n,
        f"<{TYPES}Physician/attrs/rating> <{RDFS_RANGE}> <http://www.w3.org/2001/XMLSchema#float> . "
        + f"<{TYPES}Movie/attrs/rating> <{RDFS_RANGE}> <{TYPES}Rating> . ",
        graph=ONTO_G,
    )
    assert await check_invariants(n, G, onto_graph_uri=ONTO_G) == []


@pytest.mark.asyncio
async def test_system_predicate_literal_on_onto_is_not_flagged(n):
    """The declaration-aware guard: onto/ingested_at is a literal on an onto/ predicate,
    but it is NOT a declared relationship — so it must NOT be flagged, even though a real
    relationship (located_in) IS declared. This is the false-positive class that killed
    the naive first attempt."""
    await _insert(
        n,
        _GOOD_PHYS
        + f'<{ENT}Physician/p1> <{ONTO}ingested_at> "2026-07-06T12:00:00+00:00" . '
        + f'<{ENT}Physician/p1> <{ONTO}source> "imdb.csv" . ',
    )
    await _insert(n, _DECL_LOCATED_IN, graph=ONTO_G)  # a real relationship decl exists
    assert await check_invariants(n, G, onto_graph_uri=ONTO_G) == []


@pytest.mark.asyncio
async def test_relationship_check_skipped_without_onto_graph(n):
    """Without the ontology graph the declaration-aware check is SKIPPED (can't know
    which onto/<leaf> are relationships), so it never runs blind."""
    await _insert(n, _GOOD_PHYS + f'<{ENT}Physician/p1> <{ONTO}rating> "4.6" . ')
    await _insert(n, f"<{TYPES}Physician/attrs/rating> <{RDFS_RANGE}> <{TYPES}Rating> . ", graph=ONTO_G)
    assert await check_invariants(n, G) == []  # skipped
    assert len(await check_invariants(n, G, onto_graph_uri=ONTO_G)) == 1  # runs


@pytest.mark.asyncio
async def test_bare_node_missing_type_is_caught(n):
    """The #125 bare-node class: an edge points at an entity node that was never typed."""
    await _insert(
        n,
        _GOOD_PHYS
        + f"<{ENT}Physician/p1> <{ONTO}works_at> <{ENT}Hospital/h1> . "
        + f'<{ENT}Hospital/h1> <{RDFS_LABEL}> "General" . ',  # labelled but untyped
    )
    vs = await check_invariants(n, G)
    assert [v.invariant for v in vs] == ["bare_entity_node_missing_type"]
    assert f"{ENT}Hospital/h1" in vs[0].detail


@pytest.mark.asyncio
async def test_bare_node_missing_label_is_a_warning(n):
    """A typed entity node with no rdfs:label — the softer (warn) half."""
    await _insert(n, f"<{ENT}City/SF> <{RDF_TYPE}> <{TYPES}City> . ")  # typed, no label
    vs = await check_invariants(n, G)
    assert [v.invariant for v in vs] == ["bare_entity_node_missing_label"]
    assert vs[0].severity == "warn"


@pytest.mark.asyncio
async def test_multiple_violations_sorted_errors_before_warns(n):
    await _insert(
        n,
        # a node-edge-on-attrs (error) + a typed-but-unlabelled node (warn)
        f"<{ENT}Physician/p1> <{RDF_TYPE}> <{TYPES}Physician> . "
        + f"<{ENT}City/SF> <{RDF_TYPE}> <{TYPES}City> . "
        + f"<{ENT}Physician/p1> <{TYPES}Physician/attrs/located_in> <{ENT}City/SF> . ",
    )
    vs = await check_invariants(n, G, onto_graph_uri=ONTO_G)
    assert {v.invariant for v in vs} == {
        "node_edge_on_attrs_predicate",
        "bare_entity_node_missing_label",
    }
    assert vs[0].severity == "error" and vs[-1].severity == "warn"


@pytest.mark.asyncio
async def test_include_filter_end_to_end(n):
    """A real violation is suppressed when its invariant is excluded from `include`."""
    await _insert(
        n,
        _GOOD_PHYS + _GOOD_CITY
        + f"<{ENT}Physician/p1> <{TYPES}Physician/attrs/located_in> <{ENT}City/SF> . ",
    )
    assert await check_invariants(n, G, include={"bare_entity_node_missing_type"}) == []
    assert len(await check_invariants(n, G, include={"node_edge_on_attrs_predicate"})) == 1

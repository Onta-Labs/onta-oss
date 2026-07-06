"""Real-store (pyoxigraph) tests for ``promote_to_node`` — validates the ACTUAL
SPARQL against genuine RDF semantics, including TYPED literals that the
string-only ``FakeNeptune`` cannot model.

This is the regression guard for the bug the fake missed: ingest stores numeric /
date / boolean attributes as TYPED literals (``"4.6"^^xsd:float``), and a delete
that reconstructs a plain ``"4.6"`` from the SELECT's lexical value never matches
the typed triple — so the original would survive and idempotency would break. The
fix clears the old literal with a datatype-agnostic PREDICATE-SCOPED delete; these
tests prove it on a real triplestore.

Skipped in CI (pyoxigraph is not a declared test dependency there); runs wherever
pyoxigraph is installed (local dev).
"""
from __future__ import annotations

import json

import pytest

pyoxigraph = pytest.importorskip("pyoxigraph")
from pyoxigraph import QueryResultsFormat, Store  # noqa: E402

from cograph_client.graph.ontology_queries import attr_uri, type_uri  # noqa: E402
from cograph_client.graph.queries import kg_graph_uri, tenant_graph_uri  # noqa: E402
from cograph_client.normalization.execute import apply_rule  # noqa: E402
from cograph_client.normalization.rules import (  # noqa: E402
    NormalizationRule,
    make_rule_id,
)

ENT = "https://cograph.tech/entities/"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
RDFS_RANGE = "http://www.w3.org/2000/01/rdf-schema#range"
RDFS_CLASS = "http://www.w3.org/2000/01/rdf-schema#Class"
XSD_FLOAT = "http://www.w3.org/2001/XMLSchema#float"
TENANT, KG = "promote-pyoxi", "k1"


class PyoxiNeptune:
    """Minimal NeptuneClient shim over an in-process pyoxigraph Store — async
    query()/update() returning SPARQL-1.1 JSON, the union-of-named-graphs default
    matching the production backend + scripts/local_sparql.py."""

    def __init__(self) -> None:
        self.store = Store()

    async def query(self, sparql: str) -> dict:
        results = self.store.query(sparql, use_default_graph_as_union=True)
        return json.loads(results.serialize(format=QueryResultsFormat.JSON))

    async def update(self, sparql: str) -> None:
        self.store.update(sparql)


def _rule(type_name: str, predicate: str, **params) -> NormalizationRule:
    return NormalizationRule(
        id=make_rule_id(KG, type_name, predicate, "promote_to_node"),
        kg_name=KG, type_name=type_name, predicate=predicate,
        target_kind="attribute", rule_type="promote_to_node",
        params=params, status="confirmed",
    )


async def _bindings(n: PyoxiNeptune, sparql: str) -> list[dict]:
    return (await n.query(sparql))["results"]["bindings"]


@pytest.mark.asyncio
async def test_owner_keyed_typed_literal_is_deleted_and_lossless():
    """The regression: a TYPED float rating must be promoted AND its original
    typed literal removed (datatype-agnostic delete), with the value preserved."""
    n = PyoxiNeptune()
    kgg, onto = kg_graph_uri(TENANT, KG), tenant_graph_uri(TENANT)
    rattr = attr_uri("CoffeeShop", "rating")
    # Seed two shops with a genuinely TYPED float literal (what ingest stores).
    await n.update(
        f'INSERT DATA {{ GRAPH <{kgg}> {{ '
        f'<{ENT}CoffeeShop/shop-1> <{RDF_TYPE}> <{type_uri("CoffeeShop")}> ; '
        f'<{rattr}> "4.6"^^<{XSD_FLOAT}> . '
        f'<{ENT}CoffeeShop/shop-2> <{RDF_TYPE}> <{type_uri("CoffeeShop")}> ; '
        f'<{rattr}> "4.6"^^<{XSD_FLOAT}> . }} }}'
    )
    await n.update(
        f'INSERT DATA {{ GRAPH <{onto}> {{ <{rattr}> <{RDFS_RANGE}> <{XSD_FLOAT}> }} }}'
    )

    summary = await apply_rule(n, TENANT, _rule("CoffeeShop", "rating",
                                                target_type="Rating", key_by="owner"))
    assert summary == {"nodes_created": 2, "edges_added": 2, "literals_promoted": 2}

    # THE REGRESSION: zero literal objects remain under the attribute (the typed
    # original was actually removed, not left behind by a plain-string delete).
    assert await _bindings(
        n, f'SELECT ?o WHERE {{ GRAPH <{kgg}> {{ ?s <{rattr}> ?o . FILTER(isLiteral(?o)) }} }}'
    ) == []

    # 2 DISTINCT Rating nodes (identical 4.6 not merged), value preserved.
    nr = (await _bindings(n, f'SELECT (COUNT(DISTINCT ?r) AS ?n) WHERE {{ GRAPH <{kgg}> {{ '
                             f'?s <https://cograph.tech/onto/rating> ?r . '
                             f'?r <{RDF_TYPE}> <{type_uri("Rating")}> }} }}'))[0]["n"]["value"]
    assert nr == "2"
    vals = sorted(b["v"]["value"] for b in await _bindings(
        n, f'SELECT ?v WHERE {{ GRAPH <{kgg}> {{ ?r <{RDF_TYPE}> <{type_uri("Rating")}> ; '
           f'<{attr_uri("Rating","value")}> ?v }} }}'))
    assert vals == ["4.6", "4.6"]

    # edge is on the onto/<leaf> relationship predicate, not attrs/<leaf>.
    assert len(await _bindings(
        n, f'SELECT ?s WHERE {{ GRAPH <{kgg}> {{ ?s <https://cograph.tech/onto/rating> ?r }} }}')) == 2

    # ontology: range flipped to types/Rating, Rating declared an rdfs:Class.
    rng = (await _bindings(n, f'SELECT ?r WHERE {{ GRAPH <{onto}> {{ <{rattr}> <{RDFS_RANGE}> ?r }} }}'))[0]["r"]["value"]
    assert rng == type_uri("Rating")
    cls = await _bindings(
        n, f'SELECT ?t WHERE {{ GRAPH <{onto}> {{ <{type_uri("Rating")}> <{RDF_TYPE}> ?t }} }}')
    assert any(b["t"]["value"] == RDFS_CLASS for b in cls)  # target declared a Class

    # idempotent: the typed literal is gone, so a re-run promotes nothing.
    assert (await apply_rule(n, TENANT, _rule("CoffeeShop", "rating",
                             target_type="Rating", key_by="owner"))) == {
        "nodes_created": 0, "edges_added": 0, "literals_promoted": 0}


@pytest.mark.asyncio
async def test_value_keyed_shares_nodes_on_typed_int():
    """Value-keyed promotion of a typed literal: shared node per value, onto edge,
    typed original removed."""
    n = PyoxiNeptune()
    kgg, onto = kg_graph_uri(TENANT, KG), tenant_graph_uri(TENANT)
    XSD_INT = "http://www.w3.org/2001/XMLSchema#integer"
    yattr = attr_uri("Movie", "year")
    await n.update(
        f'INSERT DATA {{ GRAPH <{kgg}> {{ '
        f'<{ENT}Movie/m1> <{RDF_TYPE}> <{type_uri("Movie")}> ; <{yattr}> "1999"^^<{XSD_INT}> . '
        f'<{ENT}Movie/m2> <{RDF_TYPE}> <{type_uri("Movie")}> ; <{yattr}> "1999"^^<{XSD_INT}> . '
        f'<{ENT}Movie/m3> <{RDF_TYPE}> <{type_uri("Movie")}> ; <{yattr}> "2004"^^<{XSD_INT}> . }} }}'
    )
    await n.update(f'INSERT DATA {{ GRAPH <{onto}> {{ <{yattr}> <{RDFS_RANGE}> <{XSD_INT}> }} }}')

    summary = await apply_rule(n, TENANT, _rule("Movie", "year",
                                                target_type="Year", key_by="value"))
    assert summary == {"nodes_created": 2, "edges_added": 3, "literals_promoted": 3}
    # shared: 1999 is ONE node used by m1 + m2.
    assert len(await _bindings(
        n, f'SELECT ?m WHERE {{ GRAPH <{kgg}> {{ ?m <https://cograph.tech/onto/year> '
           f'<{ENT}Year/1999> }} }}')) == 2
    # typed originals removed.
    assert await _bindings(
        n, f'SELECT ?o WHERE {{ GRAPH <{kgg}> {{ ?m <{yattr}> ?o . FILTER(isLiteral(?o)) }} }}'
    ) == []

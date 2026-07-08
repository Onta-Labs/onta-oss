"""Real-store (pyoxigraph) tests for CSV **join-by-exact-key** ingest (ONTA-250).

The gap this closes: CSV ingest + within-graph signal-ER exist, but "join this
row onto the EXISTING entity that already carries the same exact key value" did
not — so a sparse CSV (a key column + a couple of attrs) minted a DUPLICATE node
next to the matching existing entity instead of merging onto it.

These drive the ACTUAL resolver over a genuine triplestore and assert on the
mechanism with INVENTED types/keys (``Widget`` keyed on ``sku``, ``Gadget`` keyed
on ``part_no``) so nothing overfits to a domain (no NPI/Physician special-casing).

Skipped where pyoxigraph is not installed (it is not a declared CI test dep);
runs wherever it is present (local dev, matching test_normalization_promote_pyoxi).
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest

pyoxigraph = pytest.importorskip("pyoxigraph")
from pyoxigraph import QueryResultsFormat, Store  # noqa: E402

from cograph_client.graph.ontology_queries import (  # noqa: E402
    attr_uri,
    entity_uri,
    insert_attribute,
    insert_type,
    type_uri,
)
from cograph_client.graph.queries import kg_graph_uri, tenant_graph_uri  # noqa: E402
from cograph_client.resolver.models import (  # noqa: E402
    ColumnMapping,
    ColumnRole,
    CSVSchemaMapping,
    KeyJoin,
)
from cograph_client.resolver.schema_resolver import SchemaResolver  # noqa: E402
from cograph_client.resolver.verdict_cache import JsonVerdictCache  # noqa: E402

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
TENANT, KG = "keyjoin-pyoxi", "k1"


class PyoxiNeptune:
    """Minimal NeptuneClient shim over an in-process pyoxigraph Store — async
    query()/update()/batch_exists() returning SPARQL-1.1 JSON, union-of-named-
    graphs default matching production + scripts/local_sparql.py."""

    def __init__(self) -> None:
        self.store = Store()

    async def query(self, sparql: str) -> dict:
        results = self.store.query(sparql, use_default_graph_as_union=True)
        return json.loads(results.serialize(format=QueryResultsFormat.JSON))

    async def update(self, sparql: str) -> None:
        self.store.update(sparql)

    async def batch_exists(self, sparql: str) -> set[str]:
        data = await self.query(sparql)
        rows = data.get("results", {}).get("bindings", [])
        return {r["entity"]["value"] for r in rows if "entity" in r}


def _resolver(n: PyoxiNeptune) -> SchemaResolver:
    # ER off so nothing but the key-join decides merges; keys are fake (no LLM
    # call — every type is pre-seeded so _resolve_type short-circuits).
    os.environ["COGRAPH_ER_ENABLED"] = "0"
    r = SchemaResolver(
        n, "fake-key", JsonVerdictCache(pathlib.Path("/tmp/keyjoin-verdict-cache.json")),
    )
    return r


async def _bindings(n: PyoxiNeptune, sparql: str) -> list[dict]:
    return (await n.query(sparql))["results"]["bindings"]


async def _seed_type(n: PyoxiNeptune, onto: str, type_name: str, attrs: dict[str, str]) -> None:
    await n.update(insert_type(onto, type_name, ""))
    for name, dt in attrs.items():
        await n.update(insert_attribute(onto, type_name, name, "", dt))


async def _seed_entity(
    n: PyoxiNeptune, kgg: str, type_name: str, node_id: str, attrs: dict[str, str],
) -> str:
    """Seed an existing entity keyed/labelled by ``node_id`` with literal attrs."""
    uri = entity_uri(type_name, node_id)
    triples = [
        f"<{uri}> <{RDF_TYPE}> <{type_uri(type_name)}> .",
        f'<{uri}> <{RDFS_LABEL}> "{node_id}" .',
    ]
    for name, val in attrs.items():
        triples.append(f'<{uri}> <{attr_uri(type_name, name)}> "{val}" .')
    await n.update(f"INSERT DATA {{ GRAPH <{kgg}> {{ {' '.join(triples)} }} }}")
    return uri


def _mapping(type_name: str, key_col: str, attr_cols: list[str]) -> CSVSchemaMapping:
    cols = [ColumnMapping(column_name=key_col, role=ColumnRole.TYPE_ID, datatype="string")]
    for c in attr_cols:
        cols.append(ColumnMapping(column_name=c, role=ColumnRole.ATTRIBUTE, datatype="string"))
    return CSVSchemaMapping(entity_type=type_name, columns=cols)


async def _count_type(n: PyoxiNeptune, kgg: str, type_name: str) -> int:
    rows = await _bindings(
        n,
        f"SELECT (COUNT(DISTINCT ?s) AS ?c) WHERE {{ GRAPH <{kgg}> {{ "
        f"?s <{RDF_TYPE}> <{type_uri(type_name)}> }} }}",
    )
    return int(rows[0]["c"]["value"])


@pytest.mark.asyncio
async def test_key_join_merges_onto_existing_no_duplicates():
    """A sparse CSV whose key column (sku) matches existing Widgets merges the new
    attribute ONTO those exact nodes — zero duplicates minted."""
    n = PyoxiNeptune()
    kgg, onto = kg_graph_uri(TENANT, KG), tenant_graph_uri(TENANT)

    # Existing graph: two Widgets keyed by sku, minted under DIFFERENT node ids
    # than the sku (proves the join matches on the ATTRIBUTE VALUE, not the URI
    # slug) — as a prior discovery/ingest would have minted them.
    await _seed_type(n, onto, "Widget", {"sku": "string", "color": "string", "region": "string"})
    u1 = await _seed_entity(n, kgg, "Widget", "widget-alpha", {"sku": "W-1", "color": "red"})
    u2 = await _seed_entity(n, kgg, "Widget", "widget-beta", {"sku": "W-2", "color": "blue"})
    assert await _count_type(n, kgg, "Widget") == 2

    r = _resolver(n)
    # Sparse internal CSV: sku + a NEW attribute (region) not on the seed.
    rows = [{"sku": "W-1", "region": "west"}, {"sku": "W-2", "region": "east"}]
    result = await r.ingest_mapped_records(
        rows, _mapping("Widget", "sku", ["region"]), TENANT,
        instance_graph=kgg, key_join=KeyJoin(key_attribute="sku"),
    )

    # No new Widget nodes: both rows merged onto the existing two.
    assert await _count_type(n, kgg, "Widget") == 2
    assert result.rows_key_merged == 2
    assert result.rows_key_minted == 0
    assert result.rows_key_unmatched == 0

    # The new attribute landed on the EXACT existing URIs, keyed by sku.
    region_of = {
        b["s"]["value"]: b["v"]["value"]
        for b in await _bindings(
            n, f'SELECT ?s ?v WHERE {{ GRAPH <{kgg}> {{ ?s <{attr_uri("Widget","region")}> ?v }} }}'
        )
    }
    assert region_of == {u1: "west", u2: "east"}
    # Original attributes preserved (merge, not replace-node).
    colors = {
        b["s"]["value"]: b["v"]["value"]
        for b in await _bindings(
            n, f'SELECT ?s ?v WHERE {{ GRAPH <{kgg}> {{ ?s <{attr_uri("Widget","color")}> ?v }} }}'
        )
    }
    assert colors == {u1: "red", u2: "blue"}


@pytest.mark.asyncio
async def test_key_join_non_matching_key_mints_new_node():
    """A row whose key value matches no existing entity mints a NEW node
    (mint_unmatched defaults True) — never silently dropped."""
    n = PyoxiNeptune()
    kgg, onto = kg_graph_uri(TENANT, KG), tenant_graph_uri(TENANT)
    await _seed_type(n, onto, "Gadget", {"part_no": "string", "watts": "string"})
    await _seed_entity(n, kgg, "Gadget", "g-known", {"part_no": "P-1"})
    assert await _count_type(n, kgg, "Gadget") == 1

    r = _resolver(n)
    rows = [{"part_no": "P-1", "watts": "5"}, {"part_no": "P-999", "watts": "9"}]
    result = await r.ingest_mapped_records(
        rows, _mapping("Gadget", "part_no", ["watts"]), TENANT,
        instance_graph=kgg, key_join=KeyJoin(key_attribute="part_no"),
    )

    # P-1 merged onto the existing node; P-999 minted a new one → 2 total.
    assert await _count_type(n, kgg, "Gadget") == 2
    assert result.rows_key_merged == 1
    assert result.rows_key_minted == 1
    assert result.rows_key_unmatched == 0

    # The merged part carries watts on the pre-existing node.
    merged = await _bindings(
        n,
        f'SELECT ?s WHERE {{ GRAPH <{kgg}> {{ ?s <{attr_uri("Gadget","part_no")}> "P-1" ; '
        f'<{attr_uri("Gadget","watts")}> "5" }} }}',
    )
    assert len(merged) == 1
    # The new part exists and carries its own watts.
    minted = await _bindings(
        n,
        f'SELECT ?s WHERE {{ GRAPH <{kgg}> {{ ?s <{attr_uri("Gadget","part_no")}> "P-999" ; '
        f'<{attr_uri("Gadget","watts")}> "9" }} }}',
    )
    assert len(minted) == 1


@pytest.mark.asyncio
async def test_key_join_unmatched_skipped_when_mint_disabled():
    """With mint_unmatched=False an unmatched key is SKIPPED and REPORTED
    (rows_key_unmatched), never silently dropped and never minted."""
    n = PyoxiNeptune()
    kgg, onto = kg_graph_uri(TENANT, KG), tenant_graph_uri(TENANT)
    await _seed_type(n, onto, "Gadget", {"part_no": "string", "watts": "string"})
    await _seed_entity(n, kgg, "Gadget", "g-known", {"part_no": "P-1"})

    r = _resolver(n)
    rows = [{"part_no": "P-1", "watts": "5"}, {"part_no": "P-404", "watts": "4"}]
    result = await r.ingest_mapped_records(
        rows, _mapping("Gadget", "part_no", ["watts"]), TENANT,
        instance_graph=kgg,
        key_join=KeyJoin(key_attribute="part_no", mint_unmatched=False),
    )

    # Only the existing node remains — P-404 was NOT minted.
    assert await _count_type(n, kgg, "Gadget") == 1
    assert result.rows_key_merged == 1
    assert result.rows_key_minted == 0
    assert result.rows_key_unmatched == 1
    # P-404 is absent from the graph entirely.
    assert await _bindings(
        n, f'SELECT ?s WHERE {{ GRAPH <{kgg}> {{ ?s <{attr_uri("Gadget","part_no")}> "P-404" }} }}'
    ) == []


@pytest.mark.asyncio
async def test_no_key_join_still_mints_duplicate_baseline():
    """Control: WITHOUT key_join, the same sparse CSV mints a parallel node
    (the exact duplication ONTA-250 fixes) — so the merge above is load-bearing."""
    n = PyoxiNeptune()
    kgg, onto = kg_graph_uri(TENANT, KG), tenant_graph_uri(TENANT)
    await _seed_type(n, onto, "Widget", {"sku": "string", "color": "string", "region": "string"})
    await _seed_entity(n, kgg, "Widget", "widget-alpha", {"sku": "W-1", "color": "red"})

    r = _resolver(n)
    rows = [{"sku": "W-1", "region": "west"}]
    result = await r.ingest_mapped_records(
        rows, _mapping("Widget", "sku", ["region"]), TENANT, instance_graph=kgg,
    )

    # Ordinary ingest keys the URI by the sku VALUE → entities/Widget/W-1, which
    # is a DIFFERENT node than the seeded entities/Widget/widget-alpha → 2 nodes.
    assert await _count_type(n, kgg, "Widget") == 2
    assert result.rows_key_merged == 0  # accounting stays zero when key_join is off


@pytest.mark.asyncio
async def test_key_join_strict_preserves_relationship_targets():
    """A relationship-target STUB (a different type, no key value) is never a join
    candidate — so mint_unmatched=false must NOT silently drop it. Only rows that
    HAD the key value and matched nothing are skipped."""
    n = PyoxiNeptune()
    kgg, onto = kg_graph_uri(TENANT, KG), tenant_graph_uri(TENANT)
    await _seed_type(n, onto, "Widget", {"sku": "string", "maker": "Maker"})
    await _seed_type(n, onto, "Maker", {"name": "string"})
    await _seed_entity(n, kgg, "Widget", "widget-alpha", {"sku": "W-1"})

    r = _resolver(n)
    # A mapping with a relationship column (maker → Maker) minting a stub target.
    cols = [
        ColumnMapping(column_name="sku", role=ColumnRole.TYPE_ID, datatype="string"),
        ColumnMapping(
            column_name="maker", role=ColumnRole.RELATIONSHIP,
            target_type="Maker", datatype="string",
        ),
    ]
    mapping = CSVSchemaMapping(entity_type="Widget", columns=cols)
    result = await r.ingest_mapped_records(
        [{"sku": "W-1", "maker": "Acme"}], mapping, TENANT, instance_graph=kgg,
        key_join=KeyJoin(key_attribute="sku", mint_unmatched=False),
    )

    # Widget W-1 merged onto the existing node (no duplicate); the Maker stub —
    # which has NO sku key — was NOT skipped, so the relationship still lands.
    assert await _count_type(n, kgg, "Widget") == 1
    assert result.rows_key_merged == 1
    assert result.rows_key_unmatched == 0  # the stub is not a join candidate
    makers = await _bindings(
        n, f'SELECT ?m WHERE {{ GRAPH <{kgg}> {{ ?m <{RDF_TYPE}> <{type_uri("Maker")}> }} }}'
    )
    assert len(makers) == 1  # stub target preserved (NOT skipped)
    # The relationship edge lands (union across graphs — _ingest_mapped writes
    # rel triples to the base graph, an existing quirk unrelated to key-join).
    edges = await _bindings(
        n, 'SELECT ?w ?m WHERE { ?w <https://cograph.tech/onto/maker> ?m }'
    )
    assert len(edges) == 1  # relationship edge landed, keyed off the MERGED Widget


@pytest.mark.asyncio
async def test_key_join_via_resolve_and_insert_path():
    """The route path (`_resolve_and_insert`, used by POST /ingest/csv/rows) honors
    key_join too — same merge, exercised through the inner pipeline the route calls
    directly rather than through _ingest_mapped."""
    from cograph_client.resolver.csv_resolver import CSVResolver
    from cograph_client.resolver.models import ExtractionResult, IngestResult

    n = PyoxiNeptune()
    kgg, onto = kg_graph_uri(TENANT, KG), tenant_graph_uri(TENANT)
    await _seed_type(n, onto, "Widget", {"sku": "string", "color": "string", "region": "string"})
    u1 = await _seed_entity(n, kgg, "Widget", "widget-alpha", {"sku": "W-1", "color": "red"})

    r = _resolver(n)
    r._instance_graph = kgg
    r._type_matcher._graph_uri = onto
    existing_types, existing_attrs = await r._fetch_ontology(onto)
    r._parent_of = await r._fetch_parent_map(onto)

    mapping = _mapping("Widget", "sku", ["region"])
    applied = CSVResolver.apply_mapping(mapping, [{"sku": "W-1", "region": "west"}])
    extraction = ExtractionResult(entities=applied.entities, relationships=applied.relationships)
    result = await r._resolve_and_insert(
        extraction, onto, existing_types, existing_attrs, "test",
        IngestResult(), {}, {}, "",
        key_join=KeyJoin(key_attribute="sku"),
    )

    assert await _count_type(n, kgg, "Widget") == 1  # merged, no duplicate
    assert result.rows_key_merged == 1
    region = await _bindings(
        n, f'SELECT ?v WHERE {{ GRAPH <{kgg}> {{ <{u1}> <{attr_uri("Widget","region")}> ?v }} }}'
    )
    assert region[0]["v"]["value"] == "west"

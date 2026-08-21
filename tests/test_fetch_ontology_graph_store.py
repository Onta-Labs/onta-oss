"""ONTA-534: the full-ontology fetch reads GraphStore, not the retired SPARQL client.

``NLQueryPipeline._fetch_ontology`` reads EVERY visible layer's declared schema
with ``get_full_ontology_query`` over ``NeptuneClient.query``, which is RETIRED
on the shipped Neo4j backend. Every layer raised ``SparqlClientRetired``, the
per-layer ``except: continue`` swallowed it, and the fetch ended with an empty
``types`` dict — the ``layer_ontology_fetch_failed`` warnings in production.

It is not a hot path (the GraphStore catalog and the semantic retriever both
answer first on ``/ask``), but it is the WIDENING path: ``pipeline_ask_prep``
calls it as a last resort, and BOTH escalations — ``pipeline_ask_escalate``
after an empty generation and ``pipeline_ask_zero`` after a zero-row answer —
call it to widen a narrowed ontology. With every arm retired the widening
silently could not happen and the retry saw the same narrow schema that had
just failed.

These tests pin the GraphStore arm (real declared types/attributes out of a
seeded ``MemoryGraphStore``, with SPARQL wired to explode) and the residual
SPARQL arm (still consulted when the store has nothing to say), plus the
behaviours the summary assembly builds on top of the fetched types: layer
shadowing, the ``[no instances]`` annotation, and fail-soft on a corrupt label.

Anti-overfit: synthetic type / attribute / entity names only.
"""

from __future__ import annotations

import pytest

from infona_client.graph.client import SparqlClientRetired
from infona_client.graph.iri import IRI_BASE
from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.layers import Layer, public_graph_uri
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_catalog import upsert_attribute, upsert_type
from infona_client.graph.ontology_catalog_models import OntoTypeRecord
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.store import configure_graph_store
from infona_client.nlp.pipeline import NLQueryPipeline, _ontology_cache
from infona_client.nlp.pipeline_helpers import ONTOLOGY_EMPTY, ONTOLOGY_FETCH_ERROR

pytestmark = pytest.mark.asyncio

TENANT = "onto-tenant"
KG_NAME = "onto-kg"
ONTOLOGY_GRAPH = f"{IRI_BASE}/graphs/{TENANT}"
KG_GRAPH = f"{ONTOLOGY_GRAPH}/kg/{KG_NAME}"
TYPES = f"{IRI_BASE}/types/"
PUBLIC_TYPES = f"{TYPES}public/"

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"

# Synthetic vocabulary: no warehouse / persona / benchmark nouns.
T_ALPHA = "OntoAlpha"
T_BETA = "OntoBeta"
T_GAMMA = "OntoGamma"
A_CODE = "onto_code"
A_MASS = "onto_mass"
A_SPIN = "onto_spin"
R_HOLDS = "onto_holds"


class RetiredSparqlNeptune:
    """Stands in for the shipped client: every SPARQL read is retired."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def query(self, sparql: str):
        self.queries.append(sparql)
        raise SparqlClientRetired(
            "SPARQL HTTP client is retired under Neo4j GraphStore (ONTA-534)."
        )

    @property
    def schema_reads(self) -> list[str]:
        """The layer schema query, identified by its own projection."""
        return [q for q in self.queries if "?typeLabel" in q]


class SchemaSparqlNeptune:
    """Residual SPARQL arm: answers the layer schema read and nothing else."""

    def __init__(self, type_name: str, attr_name: str) -> None:
        self.type_name = type_name
        self.attr_name = attr_name
        self.schema_calls = 0

    async def query(self, sparql: str):
        if "?typeLabel" in sparql:
            self.schema_calls += 1
            return {
                "head": {"vars": ["typeLabel", "attrLabel", "range"]},
                "results": {
                    "bindings": [
                        {
                            "typeLabel": {
                                "type": "literal",
                                "value": self.type_name,
                            },
                            "attrLabel": {
                                "type": "literal",
                                "value": self.attr_name,
                            },
                            "range": {
                                "type": "uri",
                                "value": "http://www.w3.org/2001/XMLSchema#string",
                            },
                        }
                    ]
                },
            }
        return {"head": {"vars": []}, "results": {"bindings": []}}


def _pipe(neptune, store=None) -> NLQueryPipeline:
    # Empty key on purpose: nothing here reaches the LLM, and a placeholder key
    # would let a stray call attempt a real Anthropic request.
    return NLQueryPipeline(neptune, anthropic_key="", graph_store=store)


async def _declare(store: MemoryGraphStore) -> None:
    """Tenant catalog: Alpha{code,mass,holds→Beta}, Beta{}, Gamma{spin}."""
    scope = {"store": store, "layer": "tenant", "tenant_id": TENANT}
    for name in (T_ALPHA, T_BETA, T_GAMMA):
        await upsert_type(name=name, **scope)
    await upsert_attribute(
        type_name=T_ALPHA, attr_name=A_CODE, datatype="string", **scope
    )
    await upsert_attribute(
        type_name=T_ALPHA, attr_name=A_MASS, datatype="integer", **scope
    )
    await upsert_attribute(
        type_name=T_ALPHA, attr_name=R_HOLDS, datatype=T_BETA, **scope
    )
    await upsert_attribute(
        type_name=T_GAMMA, attr_name=A_SPIN, datatype="string", **scope
    )


async def _seed_instances(store: MemoryGraphStore) -> None:
    """One Alpha holding one Beta. Gamma stays declared-but-empty."""
    alpha = entity_uri(T_ALPHA, "a1")
    beta = entity_uri(T_BETA, "b1")
    await insert_facts(
        None,
        KG_GRAPH,
        [
            (alpha, RDF_TYPE, f"{TYPES}{T_ALPHA}"),
            (alpha, RDFS_LABEL, "Alpha One"),
            (alpha, f"{TYPES}{T_ALPHA}/attrs/{A_CODE}", "C-1"),
            (alpha, f"{IRI_BASE}/onto/{R_HOLDS}", beta),
            (beta, RDF_TYPE, f"{TYPES}{T_BETA}"),
            (beta, RDFS_LABEL, "Beta One"),
        ],
        store=store,
    )


@pytest.fixture
def store():
    s = MemoryGraphStore()
    configure_graph_store(s)  # replaces the autouse fixture's empty store
    _ontology_cache.clear()
    yield s
    _ontology_cache.clear()


# --------------------------------------------------------------------------- #
# The GraphStore arm (the ONTA-534 fix)
# --------------------------------------------------------------------------- #


async def test_declared_schema_comes_from_the_catalog_when_sparql_is_retired(store):
    """The regression: real declared types AND their attributes, with SPARQL dead.

    On ``main`` every layer raises, ``types`` ends up empty, and the fetch falls
    through to the schema-missing instance fallback — which can name the types
    (it reuses the active-type set) but cannot name a single DECLARED attribute,
    because its per-type predicate probes are retired SPARQL too. So the planner
    got a "schema has not been written yet" notice for a workspace whose schema
    very much had been written.
    """
    await _declare(store)
    await _seed_instances(store)
    neptune = RetiredSparqlNeptune()

    summary = await _pipe(neptune, store)._fetch_ontology(ONTOLOGY_GRAPH, KG_GRAPH)

    assert summary not in (ONTOLOGY_EMPTY, ONTOLOGY_FETCH_ERROR)
    # Declared attributes, with the datatype the catalog stored.
    assert f"{A_CODE} (string)" in summary
    assert f"{A_MASS} (integer)" in summary
    assert f"{TYPES}{T_ALPHA}/attrs/{A_CODE}" in summary
    # Not the schema-missing fallback: the schema IS available.
    assert "has not been written yet" not in summary
    # And the retired client was never asked for a layer schema.
    assert neptune.schema_reads == []


async def test_a_relationship_keeps_the_onto_instance_edge_predicate(store):
    """A type-ranged attribute renders as a RELATIONSHIP on ``onto/<leaf>``.

    That predicate is the only one the planner traverses for a relationship; a
    catalog port that emitted ``attrs/<leaf>`` would look right in the summary
    and be silently unqueryable.
    """
    await _declare(store)
    await _seed_instances(store)

    summary = await _pipe(RetiredSparqlNeptune(), store)._fetch_ontology(
        ONTOLOGY_GRAPH, KG_GRAPH
    )

    assert (
        f"{R_HOLDS} → {T_BETA} — predicate URI: <{IRI_BASE}/onto/{R_HOLDS}>"
        in summary
    )
    # A relationship must NOT be mistaken for a literal column.
    assert f"{R_HOLDS} (string)" not in summary


async def test_a_declared_type_with_no_instances_is_kept_and_annotated(store):
    """ONTA-258 survives the port: declared-but-empty is annotated, not hidden."""
    await _declare(store)
    await _seed_instances(store)

    summary = await _pipe(RetiredSparqlNeptune(), store)._fetch_ontology(
        ONTOLOGY_GRAPH, KG_GRAPH
    )

    assert f"Type: {T_GAMMA} — URI: <{TYPES}{T_GAMMA}> [no instances]" in summary
    assert A_SPIN in summary, "an empty type still shows its declared schema"
    # Populated types carry no false annotation.
    assert f"{T_ALPHA}> [no instances]" not in summary
    assert f"{T_BETA}> [no instances]" not in summary


async def test_the_first_visible_layer_wins_when_two_declare_one_name(store):
    """Shadowing precedence (ONTA-397) is a property of the ASSEMBLY loop, so
    the catalog rows have to arrive layer by layer in precedence order."""
    await _declare(store)
    await _seed_instances(store)
    # Public also declares Alpha — and a name the tenant never declares.
    await upsert_type(name=T_ALPHA, layer="public", store=store, privileged=True)
    await upsert_type(name="OntoPublicOnly", layer="public", store=store, privileged=True)

    summary = await _pipe(RetiredSparqlNeptune(), store)._fetch_ontology(
        ONTOLOGY_GRAPH,
        KG_GRAPH,
        layer_graph_uris=[ONTOLOGY_GRAPH, public_graph_uri()],
    )

    # Tenant wins the shadowed name: the TENANT type URI, not the public one.
    assert f"Type: {T_ALPHA} — URI: <{TYPES}{T_ALPHA}>" in summary
    assert f"{PUBLIC_TYPES}{T_ALPHA}" not in summary
    # The unshadowed public type still contributes, in its own namespace.
    assert f"Type: OntoPublicOnly — URI: <{PUBLIC_TYPES}OntoPublicOnly>" in summary


async def test_a_corrupt_stored_label_skips_one_type_not_the_whole_summary(
    store, monkeypatch
):
    """ONTA-425 fail-soft: one unqueryable type is the honest cost of a corrupt
    label; a blinded planner is not. The guard lives in the assembly loop, so a
    row arriving from the catalog has to hit it exactly like a SPARQL row."""
    await _declare(store)
    await _seed_instances(store)

    from infona_client.graph import ontology_catalog as catalog

    real_list_types = catalog.list_types

    async def _with_corrupt_row(*args, **kwargs):
        rows = await real_list_types(*args, **kwargs)
        return [
            OntoTypeRecord(
                name="Bad Name> .}", layer="tenant", tenant_id=TENANT, kg="__ontology__"
            ),
            *rows,
        ]

    monkeypatch.setattr(catalog, "list_types", _with_corrupt_row)

    summary = await _pipe(RetiredSparqlNeptune(), store)._fetch_ontology(
        ONTOLOGY_GRAPH, KG_GRAPH
    )

    assert "Bad Name" not in summary
    assert f"Type: {T_ALPHA}" in summary and A_CODE in summary


async def test_the_process_store_is_used_when_none_was_injected(store):
    """No ``graph_store=`` on the pipeline: resolve the process store, the shape
    ``/ask`` constructs on some paths."""
    await _declare(store)
    await _seed_instances(store)

    summary = await _pipe(RetiredSparqlNeptune())._fetch_ontology(
        ONTOLOGY_GRAPH, KG_GRAPH
    )

    assert f"Type: {T_ALPHA}" in summary and A_CODE in summary


# --------------------------------------------------------------------------- #
# The residual SPARQL arm is preserved
# --------------------------------------------------------------------------- #


async def test_an_empty_catalog_still_falls_through_to_sparql():
    """"This layer declares nothing" and "this store had nothing to say" are
    indistinguishable, so the store arm declines rather than handing the planner
    a confidently empty schema."""
    _ontology_cache.clear()
    neptune = SchemaSparqlNeptune(T_ALPHA, A_CODE)

    summary = await _pipe(neptune, MemoryGraphStore())._fetch_ontology(
        ONTOLOGY_GRAPH, ONTOLOGY_GRAPH
    )

    assert neptune.schema_calls == 1
    assert f"Type: {T_ALPHA}" in summary and f"{A_CODE} (string)" in summary


async def test_a_failing_store_degrades_to_sparql_rather_than_raising():
    """Best-effort throughout: a broken store must not take ``/ask`` down."""
    _ontology_cache.clear()

    class BrokenStore:
        def session(self, scope):  # test double
            raise RuntimeError("store is down")

    neptune = SchemaSparqlNeptune(T_BETA, A_MASS)

    summary = await _pipe(neptune, BrokenStore())._fetch_ontology(
        ONTOLOGY_GRAPH, ONTOLOGY_GRAPH
    )

    assert neptune.schema_calls == 1
    assert f"Type: {T_BETA}" in summary


async def test_a_layer_uri_naming_no_workspace_falls_through_to_sparql():
    """A tenant-layer graph URI that encodes no workspace carries no catalog
    scope, so there is nothing for the store read to scope itself to."""
    _ontology_cache.clear()
    odd = "https://example.invalid/some/other/graph"
    neptune = SchemaSparqlNeptune(T_GAMMA, A_SPIN)

    summary = await _pipe(neptune, MemoryGraphStore())._fetch_ontology(odd, odd)

    assert neptune.schema_calls == 1
    assert f"Type: {T_GAMMA}" in summary


async def test_both_arms_dead_degrades_and_never_raises_into_ask():
    """The one thing this path may never do is propagate."""
    _ontology_cache.clear()

    summary = await _pipe(RetiredSparqlNeptune(), MemoryGraphStore())._fetch_ontology(
        ONTOLOGY_GRAPH, ONTOLOGY_GRAPH
    )

    assert summary in (ONTOLOGY_EMPTY, ONTOLOGY_FETCH_ERROR)

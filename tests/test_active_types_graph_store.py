"""ONTA-534: the active-type probe reads GraphStore, not the retired SPARQL client.

``_active_types`` decides which DECLARED types get the "[no instances]" mark
that teaches the planner to prefer populated slots (ONTA-258 / ONTA-411). Both
of its SPARQL arms — the bounded LIMIT-1 probe and the unbounded
``SELECT DISTINCT ?type`` scan — go through ``NeptuneClient.query``, which is
RETIRED on the shipped Neo4j backend. In production that meant every ``/ask``
and ``/agent`` logged ``active_types_probe_failed`` and answered with the
grounding switched off.

These tests pin the GraphStore arm (real type names out of a seeded
``MemoryGraphStore``, with SPARQL wired to explode) and the residual SPARQL arm
(still consulted when the store has nothing to say), so neither can drift away.

Anti-overfit: synthetic type/entity names only.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from infona_client.graph.client import NeptuneClient, SparqlClientRetired
from infona_client.graph.iri import IRI_BASE
from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.store import configure_graph_store
from infona_client.nlp.pipeline import NLQueryPipeline

pytestmark = pytest.mark.asyncio

TENANT = "probe-tenant"
KG_NAME = "probe-kg"
ONTOLOGY_GRAPH = f"{IRI_BASE}/graphs/{TENANT}"
KG_GRAPH = f"{ONTOLOGY_GRAPH}/kg/{KG_NAME}"
TYPES = f"{IRI_BASE}/types/"

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"

# Synthetic vocabulary: no warehouse / persona / benchmark nouns.
T_SPROCKET = "ProbeSprocket"
T_FLANGE = "ProbeFlange"
T_UNDECLARED = "ProbeUndeclared"


async def _seed(store: MemoryGraphStore) -> None:
    """Two sprockets, one flange, one entity of a type nobody declared."""
    triples: list[tuple[str, str, str]] = []
    for i, (type_name, local) in enumerate(
        [
            (T_SPROCKET, "s1"),
            (T_SPROCKET, "s2"),
            (T_FLANGE, "f1"),
            (T_UNDECLARED, "u1"),
        ]
    ):
        uri = entity_uri(type_name, local)
        triples.append((uri, RDF_TYPE, f"{TYPES}{type_name}"))
        triples.append((uri, RDFS_LABEL, f"{type_name} {i}"))
    await insert_facts(None, KG_GRAPH, triples, store=store)


class RetiredSparqlNeptune:
    """Stands in for the shipped client: every SPARQL read is retired.

    Mirrors production exactly — ``NeptuneClient.query`` raises
    ``SparqlClientRetired`` whenever a process GraphStore is configured and HTTP
    was not explicitly re-enabled.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def query(self, sparql: str):
        self.calls += 1
        raise SparqlClientRetired(
            "SPARQL HTTP client is retired under Neo4j GraphStore (ONTA-534)."
        )


class TypeScanNeptune:
    """Residual SPARQL arm: answers both the bounded probe and the full scan."""

    def __init__(self, active=(T_SPROCKET,)) -> None:
        self.active = tuple(active)
        self.calls = 0

    async def query(self, sparql: str):
        if "SELECT DISTINCT ?type" in sparql:
            self.calls += 1
            return {
                "head": {"vars": ["type"]},
                "results": {
                    "bindings": [
                        {"type": {"type": "uri", "value": f"{TYPES}{t}"}}
                        for t in self.active
                    ]
                },
            }
        return {"head": {"vars": []}, "results": {"bindings": []}}


def _pipe(neptune, store=None) -> NLQueryPipeline:
    # Empty key on purpose: nothing here reaches the LLM, and a placeholder key
    # would let a stray call attempt a real Anthropic request.
    return NLQueryPipeline(neptune, anthropic_key="", graph_store=store)


@pytest.fixture
def store():
    s = MemoryGraphStore()
    yield s


# --------------------------------------------------------------------------- #
# The GraphStore arm (the ONTA-534 fix)
# --------------------------------------------------------------------------- #


async def test_probe_reads_populated_types_from_the_graph_store(store):
    """The regression: real type names, with SPARQL wired to explode.

    On ``main`` the bounded probe raises ``SparqlClientRetired``, the scan
    raises it again, and ``_active_types`` propagates — which is the production
    ``active_types_probe_failed`` warning, once per ask.
    """
    await _seed(store)
    neptune = RetiredSparqlNeptune()
    pipe = _pipe(neptune, store)

    names = await pipe._active_types(
        KG_GRAPH, ONTOLOGY_GRAPH, declared_names={T_SPROCKET, T_FLANGE}
    )

    assert names is not None, "the probe must not degrade when the store can answer"
    assert {T_SPROCKET, T_FLANGE} <= names
    assert neptune.calls == 0, "the retired SPARQL client must not be consulted"


async def test_the_store_arm_reports_every_populated_leaf(store):
    """Including types the ontology never declared — the scan's own contract.

    ``_resolve_active_types`` hands its second element to ``_fetch_ontology``'s
    schema-missing fallback, which needs types nobody declared. A store answer
    that only covered declared names would silently break that branch.
    """
    await _seed(store)
    pipe = _pipe(RetiredSparqlNeptune(), store)

    names, scanned = await pipe._resolve_active_types(
        KG_GRAPH, {T_SPROCKET}  # only one type declared
    )

    assert T_UNDECLARED in names
    assert scanned == names


async def test_scan_instance_types_takes_the_store_path(store):
    """The unbounded scan is the other retired arm; it must read the store too."""
    await _seed(store)
    neptune = RetiredSparqlNeptune()

    scanned = await _pipe(neptune, store)._scan_instance_types(KG_GRAPH)

    assert scanned == {T_SPROCKET, T_FLANGE, T_UNDECLARED}
    assert neptune.calls == 0


async def test_a_populated_subtype_is_never_marked_empty(store):
    """Multi-typed entities count under EVERY class they assert.

    A primary-type-guarded count would attribute an entity asserting both a
    subtype and its supertype to one of them, leaving the other reading as
    0 instances — a FALSE "[no instances]" on a populated type, which is the
    ONTA-258 failure this whole module exists to prevent.
    """
    both = entity_uri(T_SPROCKET, "dual")
    await insert_facts(
        None,
        KG_GRAPH,
        [
            (both, RDF_TYPE, f"{TYPES}{T_SPROCKET}"),
            (both, RDF_TYPE, f"{TYPES}{T_FLANGE}"),
            (both, RDFS_LABEL, "Dual typed"),
        ],
        store=store,
    )

    names = await _pipe(RetiredSparqlNeptune(), store)._store_instance_types(KG_GRAPH)

    assert names == {T_SPROCKET, T_FLANGE}


async def test_the_process_store_is_used_when_none_was_injected(store):
    """No ``graph_store=`` on the pipeline: resolve the process store (production
    shape — ``/ask`` constructs the pipeline without one on some paths)."""
    await _seed(store)
    configure_graph_store(store)  # the autouse fixture's store is replaced here

    names = await _pipe(RetiredSparqlNeptune())._active_types(
        KG_GRAPH, ONTOLOGY_GRAPH, declared_names={T_SPROCKET}
    )

    assert names is not None and T_SPROCKET in names


# --------------------------------------------------------------------------- #
# The residual SPARQL arm is preserved
# --------------------------------------------------------------------------- #


async def test_an_empty_store_still_falls_through_to_sparql():
    """"Nothing in the store" and "no store data to read" are indistinguishable,
    so the store arm declines rather than declaring every declared type empty."""
    neptune = TypeScanNeptune(active=(T_SPROCKET,))

    names = await _pipe(neptune, MemoryGraphStore())._active_types(
        KG_GRAPH, ONTOLOGY_GRAPH, declared_names={T_SPROCKET, T_FLANGE}
    )

    assert names == {T_SPROCKET}
    assert neptune.calls == 1


async def test_a_non_kg_graph_uri_falls_through_to_sparql(store):
    """A bare tenant / provenance graph carries no ``(tenant, kg)`` scope, so
    there is nothing for the store read to scope itself to."""
    await _seed(store)
    other = f"{IRI_BASE}/graphs/{TENANT}/provenance"
    neptune = TypeScanNeptune(active=(T_FLANGE,))

    scanned = await _pipe(neptune, store)._scan_instance_types(other)

    assert scanned == {T_FLANGE}
    assert neptune.calls == 1


async def test_a_failing_store_degrades_to_sparql_rather_than_raising():
    """Best-effort throughout: a broken store must not take ``/ask`` down."""

    class BrokenStore:
        def session(self, scope):  # test double
            raise RuntimeError("store is down")

    neptune = TypeScanNeptune(active=(T_FLANGE,))

    scanned = await _pipe(neptune, BrokenStore())._scan_instance_types(KG_GRAPH)

    assert scanned == {T_FLANGE}
    assert neptune.calls == 1


async def test_a_failing_store_and_a_retired_client_degrade_not_500(store):
    """Both arms dead: the caller's own policy applies (it catches and degrades),
    but nothing here may swallow the failure into a FALSE empty set — that would
    mark every declared type "[no instances]"."""
    neptune = AsyncMock(spec=NeptuneClient)
    neptune.query.side_effect = SparqlClientRetired("retired")

    with pytest.raises(SparqlClientRetired):
        await _pipe(neptune, MemoryGraphStore())._scan_instance_types(KG_GRAPH)

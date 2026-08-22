"""Unit tests for the shared KG existence/emptiness probe (ONTA-413).

The probe is what lets every read rail tell three states apart that a bare
SPARQL query cannot: missing KG, registered-but-empty KG, and a KG with data
whose query simply matched nothing. Its caching rules are load-bearing, so they
are pinned here rather than only exercised through the routes.
"""

from __future__ import annotations

import pytest

from infona_client.graph.kg_status import (
    KG_EMPTY,
    KG_MISSING,
    KG_OK,
    empty_kg_message,
    invalidate_kg_status,
    kg_data_status,
    list_kg_names,
    missing_kg_message,
    other_graphs_hold_instances,
)

TENANT = "t-probe"


class ProbeNeptune:
    """Models the probe's THREE distinguishable ASKs.

    ``base_instances`` is the ONTA-413 follow-up signal: whether the tenant BASE
    graph holds instance data. It defaults False so a workspace whose data lives
    only in per-KG graphs behaves as before.
    """

    def __init__(
        self,
        *,
        registered: bool,
        has_data: bool,
        base_instances: bool = False,
        names=(),
    ):
        self.registered = registered
        self.has_data = has_data
        self.base_instances = base_instances
        self.names = list(names)
        self.asks: list[str] = []
        self.queries: list[str] = []

    async def ask(self, sparql: str) -> bool:
        self.asks.append(sparql)
        if "/kg_name>" in sparql:
            return self.registered
        if "rdf-syntax-ns#type" in sparql:
            return self.base_instances
        return self.has_data

    async def query(self, sparql: str) -> dict:
        self.queries.append(sparql)
        return {
            "head": {"vars": ["name"]},
            "results": {
                "bindings": [
                    {"name": {"type": "literal", "value": n}} for n in self.names
                ]
            },
        }


@pytest.fixture(autouse=True)
def _clean_cache():
    invalidate_kg_status(TENANT)
    yield
    invalidate_kg_status(TENANT)


@pytest.mark.asyncio
async def test_missing_when_neither_registered_nor_populated():
    n = ProbeNeptune(registered=False, has_data=False)
    assert await kg_data_status(n, TENANT, "nope") == KG_MISSING


@pytest.mark.asyncio
async def test_empty_when_registered_but_no_triples():
    n = ProbeNeptune(registered=True, has_data=False)
    assert await kg_data_status(n, TENANT, "fresh") == KG_EMPTY


@pytest.mark.asyncio
async def test_ok_when_populated():
    n = ProbeNeptune(registered=True, has_data=True)
    assert await kg_data_status(n, TENANT, "imdb") == KG_OK


@pytest.mark.asyncio
async def test_populated_but_unregistered_is_ok_not_missing():
    """Legacy graphs predate ``ensure_kg_registered``; data wins over the record."""
    n = ProbeNeptune(registered=False, has_data=True)
    assert await kg_data_status(n, TENANT, "legacy") == KG_OK


@pytest.mark.asyncio
async def test_hot_path_costs_exactly_two_asks():
    """A populated KG must NOT pay for the base-graph instance check."""
    n = ProbeNeptune(registered=True, has_data=True)
    await kg_data_status(n, TENANT, "imdb")
    assert len(n.asks) == 2
    assert not any("rdf-syntax-ns#type" in q for q in n.asks)
    # And none is a COUNT scan (knowledge_graphs._live_triple_count is
    # explicitly forbidden on this hot path).
    assert not any("COUNT" in q for q in n.asks)


@pytest.mark.asyncio
async def test_third_ask_only_fires_when_a_registered_kg_graph_is_empty():
    n = ProbeNeptune(registered=True, has_data=False)
    await kg_data_status(n, TENANT, "fresh")
    assert len(n.asks) == 3
    assert any("rdf-syntax-ns#type" in q for q in n.asks)


# --------------------------------------------------------------------------- #
# The dataset is a UNION: "empty" must mean what the ANSWER QUERY means by it.
#
# /ask threads layer_stack_for(tenant).visible_graph_uris() into the pipeline,
# that stack ALWAYS contains the tenant base graph, and add_layer_from_clauses
# splices it in as an extra FROM. So an empty per-KG graph does NOT imply an
# unanswerable question when the workspace keeps its data in the base graph
# (which api/routes/ingest.py does whenever kg_name is absent).
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_base_graph_instances_rescue_an_otherwise_empty_kg():
    """Would have been a false "nothing to query" refusal; must answer instead.

    Deliberately still true after ONTA-453: this KG EXISTS (the caller picked a
    real name out of the Explorer dropdown), and a workspace that ingested
    without a ``kg_name`` keeps its instances in the base graph, so the union
    answer is plausibly about what was named. The narrower ONTA-453 rule only
    removes the rescue for a name that answers to nothing at all.
    """
    n = ProbeNeptune(registered=True, has_data=False, base_instances=True)
    assert await kg_data_status(n, TENANT, "fresh") == KG_OK


@pytest.mark.asyncio
async def test_base_graph_instances_do_not_rescue_an_unregistered_name():
    """ONTA-453: the union rescue stops at EXISTENCE.

    It used to cover this case on a do-no-harm argument ("main would have
    answered it from the base graph"). Live on demo-tenant, what main actually
    answered was 255210 for a question about a graph that does not exist, every
    row of it drawn from the tenant base graph and the global public layer. A
    rescue that fabricates an answer about a nonexistent thing is not do-no-harm;
    it is the confident-wrong-answer failure this probe exists to remove.
    """
    n = ProbeNeptune(registered=False, has_data=False, base_instances=True)
    assert await kg_data_status(n, TENANT, "typo") == KG_MISSING


@pytest.mark.asyncio
async def test_unregistered_name_costs_only_two_asks():
    """A typo settles on the two hot-path ASKs; the base check is never paid for."""
    n = ProbeNeptune(registered=False, has_data=False, base_instances=True)
    await kg_data_status(n, TENANT, "typo")
    assert len(n.asks) == 2
    assert not any("rdf-syntax-ns#type" in q for q in n.asks)


@pytest.mark.asyncio
async def test_omitted_kg_name_is_untouched_by_the_missing_rule():
    """ONTA-426 pin: naming nothing still reads the base graph, unprobed."""
    n = ProbeNeptune(registered=False, has_data=False, base_instances=True)
    assert await kg_data_status(n, TENANT, "") == KG_OK
    assert n.asks == []


@pytest.mark.asyncio
async def test_base_probe_looks_for_instances_not_any_triple():
    """A bare `?s ?p ?o` over the base graph would disable this feature entirely.

    The base graph is also the ONTOLOGY graph and always holds at least the KG's
    own registration triple, so a bare pattern would be true for every
    registered KG and KG_EMPTY could never fire. The probe must scope to
    rdf:type with an object in the types/ namespace, which excludes the
    `<types/X> rdf:type <rdfs#Class>` schema declarations.
    """
    n = ProbeNeptune(registered=True, has_data=False)
    await kg_data_status(n, TENANT, "fresh")
    base_ask = next(q for q in n.asks if "rdf-syntax-ns#type" in q)
    assert "FILTER" in base_ask
    assert "https://graph.infona.ai/types/" in base_ask
    assert "?s ?p ?o" not in base_ask


@pytest.mark.asyncio
async def test_schema_only_base_graph_still_reports_empty():
    """The whole point: an ontology with no instances must not read as data."""
    n = ProbeNeptune(registered=True, has_data=False, base_instances=False)
    assert await kg_data_status(n, TENANT, "fresh") == KG_EMPTY


@pytest.mark.asyncio
async def test_positive_verdict_is_cached():
    n = ProbeNeptune(registered=True, has_data=True)
    assert await kg_data_status(n, TENANT, "imdb") == KG_OK
    assert await kg_data_status(n, TENANT, "imdb") == KG_OK
    assert len(n.asks) == 2  # second call served from cache


@pytest.mark.asyncio
async def test_missing_verdict_is_never_cached():
    """create-KG-then-immediately-ask is the flow a cached negative would break."""
    n = ProbeNeptune(registered=False, has_data=False)
    assert await kg_data_status(n, TENANT, "brand-new") == KG_MISSING
    # ... the caller creates + ingests ...
    n.registered = True
    n.has_data = True
    assert await kg_data_status(n, TENANT, "brand-new") == KG_OK


@pytest.mark.asyncio
async def test_empty_verdict_is_never_cached():
    """ingest-then-immediately-ask must see the data on the very next turn."""
    n = ProbeNeptune(registered=True, has_data=False)
    assert await kg_data_status(n, TENANT, "fresh") == KG_EMPTY
    n.has_data = True
    assert await kg_data_status(n, TENANT, "fresh") == KG_OK


@pytest.mark.asyncio
async def test_probe_failure_fails_open():
    """A transient backend error must never become "your graph does not exist"."""

    class Broken(ProbeNeptune):
        async def ask(self, sparql: str) -> bool:
            raise RuntimeError("throttled")

    assert await kg_data_status(Broken(registered=True, has_data=True), TENANT, "x") == KG_OK


@pytest.mark.asyncio
async def test_blank_kg_name_short_circuits_to_ok():
    n = ProbeNeptune(registered=False, has_data=False)
    assert await kg_data_status(n, TENANT, "") == KG_OK
    assert n.asks == []


@pytest.mark.asyncio
async def test_invalid_kg_name_raises_before_any_query():
    """ONTA-414: fail closed rather than interpolate a hostile name into SPARQL."""
    from infona_client.graph.queries import InvalidKGName

    n = ProbeNeptune(registered=True, has_data=True)
    with pytest.raises(InvalidKGName):
        await kg_data_status(n, TENANT, "kg> FROM <https://graph.infona.ai/graphs/victim")
    assert n.asks == []


@pytest.mark.asyncio
async def test_list_kg_names_projects_names_only():
    n = ProbeNeptune(registered=True, has_data=True, names=["imdb", "events", "imdb"])
    assert await list_kg_names(n, TENANT) == ["imdb", "events"]
    assert "LIMIT" in n.queries[0]
    assert "kg_triple_count" not in n.queries[0]


@pytest.mark.asyncio
async def test_list_kg_names_is_best_effort():
    class Broken(ProbeNeptune):
        async def query(self, sparql: str) -> dict:
            raise RuntimeError("down")

    assert await list_kg_names(Broken(registered=True, has_data=True), TENANT) == []


def test_messages_name_the_kg_and_the_alternatives():
    msg = missing_kg_message("typo", ["imdb", "events"])
    assert "typo" in msg and "imdb" in msg and "events" in msg
    assert "imdb" not in missing_kg_message("typo", [])
    assert "typo" in missing_kg_message("typo", [])
    assert "fresh" in empty_kg_message("fresh")


# --------------------------------------------------------------------------
# ONTA-534 — the workspace-wide instance probe on the GraphStore
# --------------------------------------------------------------------------
# `other_graphs_hold_instances` gates the ONTA-454 coverage caveat, and on the
# shipped Neo4j backend its only arm was the RETIRED SPARQL client: every call
# raised, the `except` swallowed it, and the probe answered "no other data"
# without ever measuring. These pin the store arm, the scopes it will and will
# not read, and the fail-toward-silence rule for when the store cannot answer
# either. The store arm is only taken for a REAL NeptuneClient, so these build
# one (its `ask` raises `SparqlClientRetired` under the hermetic store, which is
# exactly production's shape).


RDF_TYPE_IRI = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL_IRI = "http://www.w3.org/2000/01/rdf-schema#label"
SYNTH_TYPE = "SynthProbeWidget"


def _retired_client():
    from infona_client.graph.client import NeptuneClient

    # Endpoint is never reached: a process GraphStore is configured (hermetic
    # conftest fixture), so `.ask()` raises SparqlClientRetired the way it does
    # in production rather than POSTing anywhere.
    return NeptuneClient("http://sparql.invalid")


async def _seed_instances(graph_uri: str, *, n: int = 1) -> None:
    """Write ``n`` typed instances into whatever scope ``graph_uri`` denotes."""
    from infona_client.graph.iri import IRI_BASE
    from infona_client.graph.kg_writer import insert_facts
    from infona_client.graph.ontology_queries import entity_uri
    from infona_client.graph.store import get_graph_store

    triples = []
    for i in range(n):
        uri = entity_uri(SYNTH_TYPE, f"probe-{i}")
        triples.append((uri, RDF_TYPE_IRI, f"{IRI_BASE}/types/{SYNTH_TYPE}"))
        triples.append((uri, RDFS_LABEL_IRI, f"Probe {i}"))
    await insert_facts(None, graph_uri, triples, store=get_graph_store())


@pytest.mark.asyncio
async def test_base_scope_instances_are_measured_not_swallowed():
    """The regression: a real client must ANSWER, not raise into silence.

    The tenant BASE graph URI is the property-graph tenant CATALOG scope
    (``kg=__ontology__``), which is where an ingest with no ``kg_name`` puts its
    entities. Before the GraphStore arm this returned False because the retired
    SPARQL ASK raised, not because the workspace was empty.
    """
    from infona_client.graph.layers import public_graph_uri
    from infona_client.graph.queries import tenant_graph_uri

    base = tenant_graph_uri(TENANT)
    await _seed_instances(base, n=2)
    assert await other_graphs_hold_instances(
        _retired_client(), TENANT, [base, public_graph_uri()]
    ) is True


@pytest.mark.asyncio
async def test_sibling_kg_graph_uri_is_measured():
    """A per-KG URI in the list resolves to that KG's instance scope."""
    from infona_client.graph.queries import kg_graph_uri

    sibling = kg_graph_uri(TENANT, "sibling-kg")
    await _seed_instances(sibling)
    assert await other_graphs_hold_instances(
        _retired_client(), TENANT, [sibling]
    ) is True


@pytest.mark.asyncio
async def test_global_layer_uris_alone_never_assert_other_data():
    """The Global layers are catalog-only on the property graph.

    ``layer_content`` permits ontology content kinds there and
    ``GraphScope.for_instance`` refuses ``__global__`` outright, so there is no
    instance scope behind those URIs. Seeding the workspace must not make a
    layers-only question answer True — that would be the caveat claiming data
    lives somewhere the model cannot put it.
    """
    from infona_client.graph.layers import enhanced_graph_uri, public_graph_uri
    from infona_client.graph.queries import tenant_graph_uri

    await _seed_instances(tenant_graph_uri(TENANT), n=3)
    assert await other_graphs_hold_instances(
        _retired_client(), TENANT, [public_graph_uri(), enhanced_graph_uri()]
    ) is False


@pytest.mark.asyncio
async def test_empty_workspace_answers_no_rather_than_failing():
    from infona_client.graph.layers import public_graph_uri
    from infona_client.graph.queries import tenant_graph_uri

    assert await other_graphs_hold_instances(
        _retired_client(), TENANT, [tenant_graph_uri(TENANT), public_graph_uri()]
    ) is False


@pytest.mark.asyncio
async def test_foreign_tenant_graph_uri_is_never_read():
    """Scope comes from the URI's OWN tenant; a mismatch is skipped, not read."""
    from infona_client.graph.queries import tenant_graph_uri

    other_base = tenant_graph_uri("t-someone-else")
    await _seed_instances(other_base, n=2)
    assert await other_graphs_hold_instances(
        _retired_client(), TENANT, [other_base]
    ) is False


@pytest.mark.asyncio
async def test_fails_toward_silence_when_the_store_cannot_answer(monkeypatch):
    """Store error → unproven → unsaid, and nothing cached.

    The SPARQL arm behind it is retired and raises too, so this pins the whole
    ladder's degradation: an unverified positive claim is never made.
    """
    import infona_client.graph.explore_store as explore_store
    from infona_client.graph.kg_status import _base_instances_cache
    from infona_client.graph.queries import tenant_graph_uri

    base = tenant_graph_uri(TENANT)
    await _seed_instances(base, n=2)

    async def _boom(session):
        raise RuntimeError("bolt pool exhausted")

    monkeypatch.setattr(explore_store, "count_entities_pg", _boom)
    assert await other_graphs_hold_instances(_retired_client(), TENANT, [base]) is False
    assert not [k for k in _base_instances_cache if k[0] == TENANT]


@pytest.mark.asyncio
async def test_duck_typed_double_keeps_the_sparql_arm():
    """Only a REAL NeptuneClient takes the store arm (unit-test doubles still ASK)."""
    from infona_client.graph.queries import tenant_graph_uri

    class Asking:
        def __init__(self, answer: bool):
            self.answer = answer
            self.asks: list[str] = []

        async def ask(self, sparql: str) -> bool:
            self.asks.append(sparql)
            return self.answer

    base = tenant_graph_uri(TENANT)
    double = Asking(True)
    assert await other_graphs_hold_instances(double, TENANT, [base]) is True
    assert double.asks and "rdf-syntax-ns#type" in double.asks[0]


@pytest.mark.asyncio
async def test_positive_verdict_is_cached_per_graph_set():
    from infona_client.graph.layers import public_graph_uri
    from infona_client.graph.queries import tenant_graph_uri

    base = tenant_graph_uri(TENANT)
    graphs = [base, public_graph_uri()]
    await _seed_instances(base)
    assert await other_graphs_hold_instances(_retired_client(), TENANT, graphs) is True

    import infona_client.graph.explore_store as explore_store

    async def _boom(session):  # pragma: no cover — must not be reached
        raise AssertionError("cached positive should not re-probe the store")

    original = explore_store.count_entities_pg
    explore_store.count_entities_pg = _boom
    try:
        assert await other_graphs_hold_instances(
            _retired_client(), TENANT, graphs
        ) is True
    finally:
        explore_store.count_entities_pg = original

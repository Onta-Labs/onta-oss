"""Unit tests for the shared KG existence/emptiness probe (ONTA-413).

The probe is what lets every read rail tell three states apart that a bare
SPARQL query cannot: missing KG, registered-but-empty KG, and a KG with data
whose query simply matched nothing. Its caching rules are load-bearing, so they
are pinned here rather than only exercised through the routes.
"""

from __future__ import annotations

import pytest

from cograph_client.graph.kg_status import (
    KG_EMPTY,
    KG_MISSING,
    KG_OK,
    empty_kg_message,
    invalidate_kg_status,
    kg_data_status,
    list_kg_names,
    missing_kg_message,
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
    assert "https://graph.onta.sh/types/" in base_ask
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
    from cograph_client.graph.queries import InvalidKGName

    n = ProbeNeptune(registered=True, has_data=True)
    with pytest.raises(InvalidKGName):
        await kg_data_status(n, TENANT, "kg> FROM <https://graph.onta.sh/graphs/victim")
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

"""ONTA-534: the last live routes that still hard-500'd on the retired SPARQL client.

Four sites were ported earlier today (#442, #445, #446, #447). These are the
ones that were left: each was a bare ``client.query`` on a LIVE production path
with no ``try`` and no store arm, so ``SparqlClientRetired`` escaped as a 500 —

* ``agent/capabilities/ontology_cap._list_types`` — ``/agent`` "show me the schema"
* ``normalization/inference._list_predicates`` — ``/normalize/suggest`` + agent
  clean / enrich planning
* ``api/routes/lambda_functions`` — function invoke (registry lookup, CIK ladder,
  investor name)
* ``api/routes/explore_entity.search_explorer`` — Explorer attribute search
* ``resolver/er/rebuild._types_in_graph`` — ER rebuild / find-duplicates

— plus one that was WORSE than a 500: ``POST /functions/investor-portfolio``
swallowed its only query and answered ``portfolio_count=0, companies=[]`` for
every investor, an emptiness it had never checked.

Every test below seeds a real ``MemoryGraphStore`` and wires the SPARQL client
to RAISE, so it fails on ``main`` (500 / wrong 0) and passes once the GraphStore
arm answers. The residual SPARQL arms stay covered by the existing dual-arm
tests, which run against an EMPTY store and therefore still fall through to
SPARQL — those two directions together are what pin the dual arm.

Anti-overfit: synthetic type / attribute / entity names wherever the production
schema does not force a specific leaf (the lambda routes key on real leaves like
``filing_cik`` / ``lead_investor``, so those are used verbatim).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from infona_client.graph.client import SparqlClientRetired
from infona_client.graph.iri import IRI_BASE
from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_catalog import upsert_attribute, upsert_type
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.store import configure_graph_store, get_optional_graph_store

TENANT = "test-tenant"
KG = "retired-kg"
KG_GRAPH = f"{IRI_BASE}/graphs/{TENANT}/kg/{KG}"
AUTH = {"X-API-Key": "test-key"}
#: The KG ``POST /functions/investor-portfolio`` searches (hardcoded demo scope
#: in ``lambda_functions_investor.PORTFOLIO_KGS``; spelled out here so this test
#: fails on ``main``, where that module does not exist yet).
PORTFOLIO_KG = "pear-backyard"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"

# Synthetic vocabulary for the schema-only sites.
T_WIDGET = "RetiredWidget"
A_TAG = "retired_tag"
A_HUE = "retired_hue"


def _retire(mock_neptune) -> None:
    """Wire the injected SPARQL client to behave like the shipped one."""

    async def _boom(*_a, **_k):
        raise SparqlClientRetired(
            "SPARQL HTTP client is retired under Neo4j GraphStore (ONTA-534)."
        )

    mock_neptune.query.side_effect = _boom


@pytest.fixture
def store():
    """A fresh MemoryGraphStore installed as the PROCESS store.

    Routes resolve the process store, so seeding the autouse one is not enough —
    this replaces it and hands the object back for direct seeding.
    """
    s = MemoryGraphStore()
    configure_graph_store(s)
    yield s
    asyncio.run(s.close())


def _run(coro):
    return asyncio.run(coro)


async def _declare_widget() -> None:
    """One synthetic type with two literal attributes in the tenant catalog."""
    store = get_optional_graph_store()
    await upsert_type(
        store=store,
        name=T_WIDGET,
        description="a synthetic widget",
        layer="tenant",
        tenant_id=TENANT,
    )
    for leaf in (A_TAG, A_HUE):
        await upsert_attribute(
            store=store,
            type_name=T_WIDGET,
            attr_name=leaf,
            datatype="string",
            layer="tenant",
            tenant_id=TENANT,
        )


# ---------------------------------------------------------------------------
# 1. Agent ontology inspect — /agent "show me the schema"
# ---------------------------------------------------------------------------


class _RetiredClient:
    """The shipped client: every SPARQL read raises."""

    async def query(self, _sparql):
        raise SparqlClientRetired("retired (ONTA-534)")


class _SparqlOnlyClient:
    """Answers the tenant type list and nothing else (residual-arm probe)."""

    def __init__(self, label: str, comment: str) -> None:
        self._label, self._comment = label, comment

    async def query(self, _sparql):
        return {
            "head": {"vars": ["label", "comment"]},
            "results": {
                "bindings": [
                    {
                        "label": {"type": "literal", "value": self._label},
                        "comment": {"type": "literal", "value": self._comment},
                    }
                ]
            },
        }


def _ontology_ctx(neptune):
    from infona_client.agent.registry import AgentContext

    return AgentContext(
        tenant_id=TENANT,
        kg_name=KG,
        neptune=neptune,
        type_name=None,
        openrouter_key="fake-key",
        anthropic_key="fake-anthropic",
        extras={},
    )


def test_agent_type_list_answers_from_catalog_when_sparql_is_retired(store):
    """``_list_types`` used to be a bare ``ctx.neptune.query`` — a 500 in prod.

    Driven through the CAPABILITY (not the extracted helper) so the assertion is
    about the shipped behaviour and fails on ``main`` by raising, not importing.
    """
    from infona_client.agent.capabilities.ontology_cap import OntologyCapability

    async def run():
        await _declare_widget()
        return await OntologyCapability()._list_types(_ontology_ctx(_RetiredClient()))

    types = _run(run())
    assert [t["name"] for t in types] == [T_WIDGET]
    assert types[0]["description"] == "a synthetic widget"


def test_agent_type_list_still_reads_sparql_when_the_catalog_is_empty(store):
    """Residual arm: an empty catalog must not swallow a SPARQL answer."""
    from infona_client.agent.capabilities.ontology_cap import OntologyCapability

    types = _run(
        OntologyCapability()._list_types(
            _ontology_ctx(_SparqlOnlyClient(T_WIDGET, "from sparql"))
        )
    )
    assert types == [{"name": T_WIDGET, "description": "from sparql"}]


# ---------------------------------------------------------------------------
# 2. Normalization inference — /normalize/suggest + agent clean planning
# ---------------------------------------------------------------------------


def test_predicate_sampling_answers_from_the_store_when_sparql_is_retired(store):
    """``_list_predicates`` 500'd; ``_sample_values`` then answered a silent ``[]``.

    ``sample_predicate_values`` drives BOTH (no LLM call), so one assertion pins
    the whole read half of ``/normalize/suggest``.
    """
    from infona_client.normalization.inference import sample_predicate_values

    async def run():
        await _declare_widget()
        w1, w2 = entity_uri(T_WIDGET, "w1"), entity_uri(T_WIDGET, "w2")
        await insert_facts(
            None,
            KG_GRAPH,
            [
                (w1, RDF_TYPE, f"{IRI_BASE}/types/{T_WIDGET}"),
                (w1, f"{IRI_BASE}/types/{T_WIDGET}/attrs/{A_TAG}", "alpha; beta"),
                (w2, RDF_TYPE, f"{IRI_BASE}/types/{T_WIDGET}"),
                (w2, f"{IRI_BASE}/types/{T_WIDGET}/attrs/{A_TAG}", "gamma"),
            ],
            store=get_optional_graph_store(),
        )

        class Retired:
            async def query(self, _sparql):
                raise SparqlClientRetired("retired (ONTA-534)")

        return await sample_predicate_values(Retired(), TENANT, KG, T_WIDGET, A_TAG)

    samples, kind = _run(run())
    assert kind == "attribute"
    assert set(samples) == {"alpha; beta", "gamma"}


# ---------------------------------------------------------------------------
# 3. Explorer attribute search — GET /graphs/{t}/explore/search?kind=attr
# ---------------------------------------------------------------------------


def test_explorer_attribute_search_answers_from_catalog(store, client, mock_neptune):
    _retire(mock_neptune)
    _run(_declare_widget())

    res = client.get(
        f"/graphs/{TENANT}/explore/search",
        params={"kg": KG, "q": "retired_", "kind": "attr"},
        headers=AUTH,
    )
    assert res.status_code == 200, res.text
    got = {(r["attr_name"], r["type_name"]) for r in res.json()}
    assert got == {(A_TAG, T_WIDGET), (A_HUE, T_WIDGET)}


def test_explorer_attribute_search_no_match_is_an_empty_list_not_an_error(
    store, client, mock_neptune
):
    """A search that matches nothing is the ORDINARY case — never a 500."""
    _retire(mock_neptune)
    _run(_declare_widget())

    res = client.get(
        f"/graphs/{TENANT}/explore/search",
        params={"kg": KG, "q": "no_such_attribute", "kind": "attr"},
        headers=AUTH,
    )
    assert res.status_code == 200, res.text
    assert res.json() == []


# ---------------------------------------------------------------------------
# 4. ER rebuild — POST /graphs/{t}/explore/kgs/{kg}/er-rebuild
# ---------------------------------------------------------------------------


def test_er_rebuild_enumerates_types_from_the_store(store, client, mock_neptune):
    """``_types_in_graph`` was the FIRST graph touch and re-raised out of the job."""
    _retire(mock_neptune)

    async def seed():
        p1, p2 = entity_uri("Person", "p1"), entity_uri("Person", "p2")
        await insert_facts(
            None,
            KG_GRAPH,
            [
                (p1, RDF_TYPE, f"{IRI_BASE}/types/Person"),
                (p1, RDFS_LABEL, "Ada Rowan"),
                (p2, RDF_TYPE, f"{IRI_BASE}/types/Person"),
                (p2, RDFS_LABEL, "Bo Vance"),
            ],
            store=get_optional_graph_store(),
        )

    _run(seed())

    res = client.post(
        f"/graphs/{TENANT}/explore/kgs/{KG}/er-rebuild", headers=AUTH
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "complete"
    # The two Persons are distinct humans, so nothing merges — the point is that
    # the type enumeration answered at all instead of raising.
    assert [t["type"] for t in body["types"]] == ["Person"]
    assert body["fragments_absorbed_total"] == 0


# ---------------------------------------------------------------------------
# 5. Lambda invoke — POST /graphs/{t}/functions/{name}/invoke
# ---------------------------------------------------------------------------


@pytest.fixture
def function_store():
    from infona_client.functions.store import (
        StoredFunction,
        make_function_store,
        reset_function_store,
    )

    reset_function_store()
    asyncio.run(
        make_function_store().upsert(
            StoredFunction(
                tenant_id=TENANT,
                name="sec-latest-filing",
                entity_type="Company",
                endpoint_url="https://example.invalid/sec",
                description="latest filing",
            )
        )
    )
    yield
    reset_function_store()


@pytest.fixture
def stub_invoke(monkeypatch):
    """Stub the executor + write path: this file is about the READS."""
    from infona_client.api.routes import lambda_functions

    class _Executor:
        async def invoke(self, func_ref, payload, headers=None):
            return SimpleNamespace(output={"latest_filing_type": "10-K", **payload})

    monkeypatch.setattr(lambda_functions, "_get_executor", lambda: _Executor())

    async def _noop(*_a, **_k):
        return None

    for name in ("delete_facts", "insert_facts", "refresh_after_write"):
        monkeypatch.setattr(lambda_functions, name, _noop)


def test_lambda_invoke_resolves_function_and_cik_from_the_store(
    store, client, mock_neptune, function_store, stub_invoke
):
    _retire(mock_neptune)
    company = entity_uri("Company", "acme")

    async def seed():
        await insert_facts(
            None,
            KG_GRAPH,
            [
                (company, RDF_TYPE, f"{IRI_BASE}/types/Company"),
                (company, RDFS_LABEL, "Acme"),
                (company, f"{IRI_BASE}/types/Company/attrs/filing_cik", "0000320193"),
            ],
            store=get_optional_graph_store(),
        )

    _run(seed())

    res = client.post(
        f"/graphs/{TENANT}/functions/sec-latest-filing/invoke",
        json={"entity_uri": company, "kg_name": KG},
        headers=AUTH,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    # The CIK the store resolved is what reached the function.
    assert body["output"]["cik"] == "0000320193"


def test_lambda_invoke_keeps_404_for_an_unregistered_function(
    store, client, mock_neptune, function_store, stub_invoke
):
    """Failure semantics preserved: unknown function is a 404, not a 500."""
    _retire(mock_neptune)
    res = client.post(
        f"/graphs/{TENANT}/functions/no-such-function/invoke",
        json={"entity_uri": entity_uri("Company", "acme"), "kg_name": KG},
        headers=AUTH,
    )
    assert res.status_code == 404, res.text


def test_lambda_invoke_keeps_422_for_an_unresolvable_cik(
    store, client, mock_neptune, function_store, stub_invoke
):
    """Failure semantics preserved: no CIK anywhere is a 422, not a 500."""
    _retire(mock_neptune)
    company = entity_uri("Company", "nocik")

    async def seed():
        await insert_facts(
            None,
            KG_GRAPH,
            [
                (company, RDF_TYPE, f"{IRI_BASE}/types/Company"),
                (company, RDFS_LABEL, "No Cik"),
            ],
            store=get_optional_graph_store(),
        )

    _run(seed())

    res = client.post(
        f"/graphs/{TENANT}/functions/sec-latest-filing/invoke",
        json={"entity_uri": company, "kg_name": KG},
        headers=AUTH,
    )
    assert res.status_code == 422, res.text


# ---------------------------------------------------------------------------
# 6. Investor portfolio — the confident wrong 0, not just a 500
# ---------------------------------------------------------------------------


async def _seed_portfolio(kg: str) -> str:
    investor = entity_uri("Investor", "orchard_capital")
    company = entity_uri("Company", "brightloom")
    fround = entity_uri("FundingRound", "brightloom_a")
    await insert_facts(
        None,
        f"{IRI_BASE}/graphs/{TENANT}/kg/{kg}",
        [
            (investor, RDF_TYPE, f"{IRI_BASE}/types/Investor"),
            (investor, RDFS_LABEL, "Orchard Capital"),
            (company, RDF_TYPE, f"{IRI_BASE}/types/Company"),
            (company, RDFS_LABEL, "Brightloom"),
            (fround, RDF_TYPE, f"{IRI_BASE}/types/FundingRound"),
            (fround, RDFS_LABEL, "Brightloom Series A"),
            (fround, f"{IRI_BASE}/types/FundingRound/attrs/amount_usd", "4200000"),
            (fround, f"{IRI_BASE}/onto/lead_investor", investor),
            (fround, f"{IRI_BASE}/onto/company_name", company),
        ],
        store=get_optional_graph_store(),
    )
    return investor


def test_investor_portfolio_walks_the_store_instead_of_answering_zero(
    store, client, mock_neptune
):
    """On ``main`` this returns ``0``/``[]`` with the graph never read."""
    _retire(mock_neptune)
    _run(_seed_portfolio(PORTFOLIO_KG))

    res = client.post(
        "/functions/investor-portfolio",
        json={"investor_name": "Orchard Capital"},
        headers=AUTH,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["companies"] == ["Brightloom"]
    assert body["portfolio_count"] == 1
    assert body["total_invested_usd"] == 4200000


def test_investor_portfolio_503s_rather_than_claim_an_unchecked_emptiness(
    client, mock_neptune
):
    """Neither arm readable ⇒ 503. Never a confident ``portfolio_count: 0``.

    No store configured at all (the ``store`` fixture is deliberately absent)
    and SPARQL retired, so nothing can read the graph.
    """
    from infona_client.graph.store import reset_graph_store_for_tests

    _retire(mock_neptune)
    reset_graph_store_for_tests()

    res = client.post(
        "/functions/investor-portfolio",
        json={"investor_name": "Orchard Capital"},
        headers=AUTH,
    )
    assert res.status_code == 503, res.text
    assert "never checked" in res.json()["detail"]


def test_invoke_investor_portfolio_resolves_name_and_walk_from_the_store(
    store, client, mock_neptune, stub_invoke
):
    """The invoke twin: name lookup + portfolio walk, both 500s on ``main``.

    The write-back runs for real against the MemoryGraphStore — the reads are
    what this file is about, and the shared write path has its own tests.
    """
    _retire(mock_neptune)
    investor = _run(_seed_portfolio(KG))

    res = client.post(
        f"/graphs/{TENANT}/functions/investor-portfolio/invoke",
        json={"entity_uri": investor, "kg_name": KG},
        headers=AUTH,
    )
    assert res.status_code == 200, res.text
    output = res.json()["output"]
    assert output["portfolio_count"] == 1
    assert output["companies"] == "Brightloom"
    assert output["total_invested_usd"] == "4200000"

"""Scenario-fuzzer tests (OSS). The ingestion pipeline (the LLM extractor) can't run in
CI, so a FAKE resolver writes controlled triples into the instance graph and the REST is
real: the real invariants, the real ``run_audit``, and the real scoped ``reset_tenant``
run over a real pyoxigraph store. That proves the harness wiring end-to-end — "a bad edge
the ingester emits shows up as a violation" — without a network/LLM dependency.

The real LLM ingest path is exercised out-of-band via
``python -m cograph_client.qc.scenario`` against a local store (see the module docstring).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from cograph_client.graph.queries import kg_graph_uri, tenant_graph_uri
from cograph_client.qc.scenario import (
    Dataset,
    _is_disposable,
    _resolve_include,
    format_scenarios,
    load_fixture_datasets,
    reset_tenant,
    run_catalog,
    run_scenario,
    scenarios_to_dict,
    worst_exit_code,
)

TENANT = "qc-scenario"
ENT = "https://cograph.tech/entities/"
TYPES = "https://cograph.tech/types/"
ONTO = "https://cograph.tech/onto/"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"


# --------------------------------------------------------------------------- #
# pyoxigraph shim + fake resolvers
# --------------------------------------------------------------------------- #
class PyoxiNeptune:
    def __init__(self) -> None:
        from pyoxigraph import Store

        self.store = Store()

    async def query(self, sparql: str) -> dict:
        from pyoxigraph import QueryResultsFormat

        return json.loads(
            self.store.query(sparql, use_default_graph_as_union=True).serialize(
                format=QueryResultsFormat.JSON
            )
        )

    async def update(self, sparql: str) -> None:
        self.store.update(sparql)

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        pass


def _triples_resolver(triples: str, **stats):
    """A resolver factory whose ``ingest`` writes fixed triples into the instance graph
    and returns an IngestResult-shaped object — stands in for the real pipeline."""

    class _Fake:
        def __init__(self, neptune):
            self.neptune = neptune

        async def ingest(self, content, tenant, *, instance_graph, **kw):
            await self.neptune.update(f"INSERT DATA {{ GRAPH <{instance_graph}> {{ {triples} }} }}")
            return SimpleNamespace(
                types_created=stats.get("types_created", ["Physician"]),
                entities_resolved=stats.get("entities_resolved", 2),
                triples_inserted=stats.get("triples_inserted", 3),
            )

    return lambda neptune: _Fake(neptune)


def _raising_resolver(exc: Exception):
    class _Boom:
        def __init__(self, neptune):
            pass

        async def ingest(self, *a, **k):
            raise exc

    return lambda neptune: _Boom(neptune)


@pytest.fixture
def n():
    pytest.importorskip("pyoxigraph")
    return PyoxiNeptune()


# a node-valued edge on the attrs/ DECLARATION predicate = the classic NL-invisible bug.
_BAD_EDGE = (
    f'<{ENT}Physician/p1> <{RDF_TYPE}> <{TYPES}Physician> ; <{RDFS_LABEL}> "Dr P" . '
    f'<{ENT}City/SF> <{RDF_TYPE}> <{TYPES}City> ; <{RDFS_LABEL}> "SF" . '
    f"<{ENT}Physician/p1> <{TYPES}Physician/attrs/located_in> <{ENT}City/SF> ."
)
# the same fact, correctly on onto/<leaf> — no invariant should fire.
_GOOD_EDGE = (
    f'<{ENT}Physician/p1> <{RDF_TYPE}> <{TYPES}Physician> ; <{RDFS_LABEL}> "Dr P" . '
    f'<{ENT}City/SF> <{RDF_TYPE}> <{TYPES}City> ; <{RDFS_LABEL}> "SF" . '
    f"<{ENT}Physician/p1> <{ONTO}located_in> <{ENT}City/SF> ."
)
_INCLUDE = {"node_edge_on_attrs_predicate"}


# --------------------------------------------------------------------------- #
# Core: ingest (faked) → real audit
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_scenario_catches_bad_edge_from_ingestion(n):
    ds = Dataset(name="providers", content="[]", domain="healthcare")
    r = await run_scenario(
        n, tenant=TENANT, dataset=ds, include=_INCLUDE,
        resolver_factory=_triples_resolver(_BAD_EDGE, triples_inserted=3, entities_resolved=2),
    )
    assert not r.ok and r.error is None
    assert r.error_count == 1
    assert r.violations[0].invariant == "node_edge_on_attrs_predicate"
    assert r.kg == "providers"
    # ingestion stats pass through from the (faked) IngestResult
    assert r.entities_resolved == 2 and r.triples_inserted == 3


@pytest.mark.asyncio
async def test_scenario_clean_when_edge_is_correct(n):
    ds = Dataset(name="providers", content="[]")
    r = await run_scenario(
        n, tenant=TENANT, dataset=ds, include=_INCLUDE,
        resolver_factory=_triples_resolver(_GOOD_EDGE),
    )
    assert r.ok and r.error is None and r.error_count == 0 and not r.violations


@pytest.mark.asyncio
async def test_scenarios_to_dict_is_json_serializable(n):
    ds = Dataset(name="providers", content="[]")
    r = await run_scenario(
        n, tenant=TENANT, dataset=ds, include=_INCLUDE,
        resolver_factory=_triples_resolver(_BAD_EDGE),
    )
    payload = scenarios_to_dict([r])
    assert json.loads(json.dumps(payload))  # round-trips
    assert payload["datasets"] == 1 and payload["error_count"] == 1
    assert payload["results"][0]["dataset"] == "providers"
    assert payload["results"][0]["audit"]["error_count"] == 1


@pytest.mark.asyncio
async def test_scenario_records_ingest_error_without_raising(n):
    ds = Dataset(name="broken", content="not json")
    r = await run_scenario(
        n, tenant=TENANT, dataset=ds,
        resolver_factory=_raising_resolver(ValueError("extractor blew up")),
    )
    assert r.error is not None and "extractor blew up" in r.error
    assert r.report is None and r.error_count == 0 and not r.ok


# --------------------------------------------------------------------------- #
# reset_tenant — scoped, never touches another tenant
# --------------------------------------------------------------------------- #
async def _count_in(n, graph: str) -> int:
    r = await n.query(f"SELECT (COUNT(*) AS ?c) WHERE {{ GRAPH <{graph}> {{ ?s ?p ?o }} }}")
    return int(r["results"]["bindings"][0]["c"]["value"])


@pytest.mark.asyncio
async def test_reset_tenant_is_scoped_to_the_tenant(n):
    mine_kg = kg_graph_uri(TENANT, "k")
    mine_base = tenant_graph_uri(TENANT)
    other_kg = kg_graph_uri("qc-other", "k")
    for g in (mine_kg, mine_base, other_kg):
        await n.update(f'INSERT DATA {{ GRAPH <{g}> {{ <{ENT}x> <{RDFS_LABEL}> "x" }} }}')

    await reset_tenant(n, TENANT)

    assert await _count_in(n, mine_kg) == 0
    assert await _count_in(n, mine_base) == 0
    assert await _count_in(n, other_kg) == 1  # a different tenant is untouched


def _ab_factory():
    """Dataset #1 writes the bad edge, dataset #2 the good edge — used to show whether the
    second ingest inherits the first's triples (it does iff they share the graph un-reset)."""
    seen: list[int] = []

    def factory(neptune):
        i = len(seen)
        seen.append(i)
        chooser = _triples_resolver(_BAD_EDGE) if i == 0 else _triples_resolver(_GOOD_EDGE)
        return chooser(neptune)

    return factory


@pytest.mark.asyncio
async def test_catalog_reset_isolates_a_shared_graph(n):
    # both datasets target the SAME kg (same name) → they share one instance graph, so
    # reset between them is the ONLY thing stopping B's audit from inheriting A's bad edge.
    datasets = [Dataset(name="shared", content="[]"), Dataset(name="shared", content="[]")]
    results = await run_catalog(
        n, tenant=TENANT, datasets=datasets, include=_INCLUDE,
        reset_between=True, resolver_factory=_ab_factory(),
    )
    assert results[0].error_count == 1  # A: bad edge caught
    assert results[1].ok                # B: reset wiped A → good edge only, clean


@pytest.mark.asyncio
async def test_catalog_no_reset_accumulates_in_a_shared_graph(n):
    # the contrast: without reset, B shares A's graph and its audit still sees A's bad edge.
    datasets = [Dataset(name="shared", content="[]"), Dataset(name="shared", content="[]")]
    results = await run_catalog(
        n, tenant=TENANT, datasets=datasets, include=_INCLUDE,
        reset_between=False, resolver_factory=_ab_factory(),
    )
    assert results[1].error_count == 1  # B inherited A's bad edge (proves reset matters)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def test_resolve_include_validates_names():
    assert _resolve_include(None) is None
    assert _resolve_include("node_edge_on_attrs_predicate") == {"node_edge_on_attrs_predicate"}
    with pytest.raises(ValueError, match="unknown invariant"):
        _resolve_include("node_edge_on_attrs_predicate,typo_here")


def test_is_disposable():
    assert _is_disposable("qc-scenario") and _is_disposable("test-x") and _is_disposable("fuzz-1")
    assert not _is_disposable("demo-tenant") and not _is_disposable("acme")


def test_worst_exit_code_ranks_infra_over_quality():
    err = ScenarioLike(error="boom")
    quality = ScenarioLike(error_count=1)
    warn = ScenarioLike(warn_count=1)
    clean = ScenarioLike()
    assert worst_exit_code([err, quality]) == 2       # ingest failure dominates
    assert worst_exit_code([quality, clean]) == 1     # a quality error
    assert worst_exit_code([warn, clean]) == 0        # warnings don't fail by default
    assert worst_exit_code([warn], strict=True) == 1  # ...unless strict


def test_format_scenarios_lists_datasets_and_errors():
    out = format_scenarios([ScenarioLike(dataset="a", error="ingest boom"),
                            ScenarioLike(dataset="b")])
    assert "a: INGEST ERROR" in out and "ingest boom" in out
    assert "1 ingest error(s)" in out


# a lightweight stand-in for ScenarioResult for the pure-function tests (no store needed).
class ScenarioLike:
    def __init__(self, dataset="d", error=None, error_count=0, warn_count=0):
        self.dataset = dataset
        self.error = error
        self._e = error_count
        self._w = warn_count
        self.types_created = []
        self.entities_resolved = 0
        self.triples_inserted = 0
        self.violations = []

    @property
    def error_count(self):
        return self._e

    @property
    def warn_count(self):
        return self._w

    @property
    def ok(self):
        return self.error is None and self._e == 0


# --------------------------------------------------------------------------- #
# Fixture catalog
# --------------------------------------------------------------------------- #
def test_load_fixture_datasets_finds_open_fixtures():
    datasets = load_fixture_datasets()
    names = {d.name for d in datasets}
    # the ONTA-199 decomp fixtures ship in the repo; seed-ontology files are excluded.
    assert "healthcare_providers" in names
    assert not any(d.name.endswith(".seed_ontology") for d in datasets)
    assert all(d.content_type == "json" and d.content for d in datasets)


def test_load_fixture_datasets_name_filter():
    only = load_fixture_datasets(names={"coffee_shops"})
    assert {d.name for d in only} == {"coffee_shops"}


def test_load_fixture_datasets_missing_dir_returns_empty(tmp_path):
    assert load_fixture_datasets(tmp_path / "nope") == []

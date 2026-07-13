"""Two-workspace interleaved-run isolation invariant (ONTA-269).

Two discovery runs in DIFFERENT workspaces, sharing one store, must never
cross-contaminate: no fact, node, or edge authored by workspace A ends up in workspace
B's graphs, and each workspace's instances land in its OWN target graph (never a
sibling's, never — the ONTA-198 class — its own tenant BASE graph).

Two layers, same split as the other qc tests:

* **CI-safe** — the ``WorkspaceScope`` graph model, ``IsolationViolation`` shape, the
  rendering, and that unrelated data on a shared store never false-positives.
* **pyoxigraph** — real end-to-end over an in-process store: interleave two ingests
  sharing infra and prove ``check_isolation`` is clean when they use per-run resolvers,
  fires loudly on injected leakage, and — the load-bearing one — CATCHES the leak the
  known SchemaResolver reentrancy produces when a SINGLE resolver is shared across the
  two interleaved runs.

The ingestion pipeline (the LLM extractor) can't run in CI, so — exactly as
``test_qc_scenario`` does — a controlled FAKE resolver writes the triples and the REST is
real: the real ``check_isolation`` over a real pyoxigraph store. Crucially the reentrant
fake is a FAITHFUL model of the real hazard (the live target graph kept on
``self._instance_graph``, set at the top of ``ingest`` and read at write time —
``schema_resolver`` L1011 + the ``getattr(self, "_instance_graph", ...)`` insert path), so
"shared resolver + interleave ⇒ leak" reproduces the production mechanism, not a strawman.
This test only OBSERVES the leak; it does not fix the resolver (out of scope for ONTA-269).
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from cograph_client.graph.queries import kg_graph_uri, tenant_graph_uri
from cograph_client.qc.isolation import (
    IsolationViolation,
    WorkspaceScope,
    check_isolation,
    format_isolation,
    isolated,
)

ENT = "https://cograph.tech/entities/"
TYPES = "https://cograph.tech/types/"
ONTO = "https://cograph.tech/onto/"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
SOURCE = "https://cograph.tech/onto/source"

# Two distinct workspaces (distinct tenants → distinct base graphs), each with its own
# provenance source and its own KG target — the "two-workspace" contract.
WS_A = WorkspaceScope(tenant="qc-alpha", source="run:alpha", kg="providers", label="alpha")
WS_B = WorkspaceScope(tenant="qc-beta", source="run:beta", kg="hospitals", label="beta")


# --------------------------------------------------------------------------- #
# pyoxigraph shim (same as the other qc tests)
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


@pytest.fixture
def n():
    pytest.importorskip("pyoxigraph")
    return PyoxiNeptune()


# --------------------------------------------------------------------------- #
# Fixture payloads — one workspace's facts: two typed+labelled entities each stamped
# onto/source, joined by a relationship edge. Distinct URIs per workspace so an edge's
# endpoints attribute unambiguously.
# --------------------------------------------------------------------------- #
def _payload(source: str, subj: str, obj: str, pred: str = "located_in") -> str:
    return (
        f'<{subj}> <{RDF_TYPE}> <{TYPES}Physician> ; <{RDFS_LABEL}> "S" ; '
        f'<{SOURCE}> "{source}" . '
        f'<{obj}> <{RDF_TYPE}> <{TYPES}City> ; <{RDFS_LABEL}> "O" ; '
        f'<{SOURCE}> "{source}" . '
        f"<{subj}> <{ONTO}{pred}> <{obj}> ."
    )


A_SUBJ, A_OBJ = f"{ENT}Physician/alpha_p1", f"{ENT}City/alpha_c1"
B_SUBJ, B_OBJ = f"{ENT}Hospital/beta_h1", f"{ENT}City/beta_c1"
_A_FACTS = _payload(WS_A.source, A_SUBJ, A_OBJ)
_B_FACTS = _payload(WS_B.source, B_SUBJ, B_OBJ)


async def _insert(n: PyoxiNeptune, triples: str, graph: str) -> None:
    await n.update(f"INSERT DATA {{ GRAPH <{graph}> {{ {triples} }} }}")


# --------------------------------------------------------------------------- #
# Fake resolvers modelling the two designs.
# --------------------------------------------------------------------------- #
class _IsolatedResolver:
    """Reentrancy-SAFE: writes to the ``instance_graph`` ARGUMENT, never instance state.
    A fresh one per workspace is the pattern the real scenario harness uses (a resolver
    factory per run)."""

    def __init__(self, neptune, facts: str):
        self.neptune = neptune
        self._facts = facts

    async def ingest(self, content, tenant, *, instance_graph, source, **kw):
        await asyncio.sleep(0)  # a real await point (LLM / store I/O)
        await self.neptune.update(
            f"INSERT DATA {{ GRAPH <{instance_graph}> {{ {self._facts} }} }}"
        )
        return SimpleNamespace(types_created=["Physician", "City"], entities_resolved=2)


class _SharedReentrantResolver:
    """Faithful model of the SchemaResolver non-reentrancy: the live target graph is kept
    on the INSTANCE (``self._instance_graph``), set at the top of ``ingest`` and read at
    write time — exactly ``schema_resolver`` L1011 + the ``getattr(self, "_instance_graph")``
    reads on the insert path. Sharing ONE of these across two interleaved ``ingest`` calls
    clobbers the field, so the later target wins and the earlier run's triples are
    misdirected into the wrong workspace's graph."""

    def __init__(self, neptune):
        self.neptune = neptune
        self._instance_graph = None

    async def ingest(self, content, tenant, *, instance_graph, source, facts, **kw):
        self._instance_graph = instance_graph  # L1011 analogue
        await asyncio.sleep(0)  # yield: the second ingest interleaves here and clobbers
        target = self._instance_graph  # getattr(self, "_instance_graph", ...) analogue
        await self.neptune.update(
            f"INSERT DATA {{ GRAPH <{target}> {{ {facts} }} }}"
        )
        return SimpleNamespace(types_created=["Physician", "City"], entities_resolved=2)


# --------------------------------------------------------------------------- #
# CI-safe: the graph model + violation shape + rendering
# --------------------------------------------------------------------------- #
def test_workspace_scope_graphs():
    assert WS_A.instance_graph == kg_graph_uri("qc-alpha", "providers")
    assert WS_A.base_graph == tenant_graph_uri("qc-alpha")
    assert WS_A.display == "alpha"
    # kg=None targets the base graph directly.
    base_only = WorkspaceScope(tenant="qc-x", source="s")
    assert base_only.instance_graph == tenant_graph_uri("qc-x") == base_only.base_graph
    assert base_only.display == "qc-x"


def test_format_isolation_clean_and_dirty():
    assert "OK" in format_isolation([])
    v = IsolationViolation("cross_workspace_fact", "error", "leaked!", "alpha", "beta")
    out = format_isolation([v])
    assert "cross_workspace_fact" in out and "leaked!" in out and "1 leak" in out


def test_isolated_helper():
    assert isolated([])
    assert not isolated([IsolationViolation("k", "error", "d", "a", "b")])


# --------------------------------------------------------------------------- #
# pyoxigraph: clean interleaved run is isolated
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_clean_interleaved_run_is_isolated(n):
    """Two ingests, SEPARATE per-run resolvers, run concurrently over one shared store —
    each writes to its own KG graph → zero cross-workspace leakage."""
    ra, rb = _IsolatedResolver(n, _A_FACTS), _IsolatedResolver(n, _B_FACTS)
    await asyncio.gather(
        ra.ingest("", WS_A.tenant, instance_graph=WS_A.instance_graph, source=WS_A.source),
        rb.ingest("", WS_B.tenant, instance_graph=WS_B.instance_graph, source=WS_B.source),
    )
    violations = await check_isolation(n, [WS_A, WS_B])
    assert violations == [], format_isolation(violations)
    # sanity: the data really did land (the clean check isn't vacuous).
    got = await n.query(
        f"SELECT (COUNT(*) AS ?c) WHERE {{ GRAPH <{WS_A.instance_graph}> {{ ?s ?p ?o }} }}"
    )
    assert int(got["results"]["bindings"][0]["c"]["value"]) > 0


# --------------------------------------------------------------------------- #
# pyoxigraph: fails LOUDLY on injected leakage
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_injected_fact_leak_is_caught(n):
    """A-authored facts correctly in A's graph, but ONE copy injected into B's graph —
    check_isolation must fire cross_workspace_fact naming alpha→beta."""
    await _insert(n, _A_FACTS, WS_A.instance_graph)
    await _insert(n, _B_FACTS, WS_B.instance_graph)
    # inject: alpha's entity (source run:alpha) smuggled into beta's KG graph
    await _insert(n, _A_FACTS, WS_B.instance_graph)

    violations = await check_isolation(n, [WS_A, WS_B])
    facts = [v for v in violations if v.kind == "cross_workspace_fact"]
    assert facts, format_isolation(violations)
    assert any(v.author == "alpha" and v.landed_in == "beta" for v in facts)


@pytest.mark.asyncio
async def test_injected_edge_leak_is_caught(n):
    """An edge in beta's graph that references an alpha-authored entity (whose own source
    triple stays in alpha's graph) is a stranded cross-workspace EDGE."""
    await _insert(n, _A_FACTS, WS_A.instance_graph)
    await _insert(n, _B_FACTS, WS_B.instance_graph)
    # beta-graph edge: beta's node -> alpha's node
    await _insert(n, f"<{B_SUBJ}> <{ONTO}refers_to> <{A_SUBJ}> .", WS_B.instance_graph)

    violations = await check_isolation(n, [WS_A, WS_B])
    edges = [v for v in violations if v.kind == "cross_workspace_edge"]
    assert edges, format_isolation(violations)
    assert edges[0].author == "alpha" and edges[0].landed_in == "beta"


@pytest.mark.asyncio
async def test_instance_leaked_to_base_graph_is_caught(n):
    """ONTA-198 target-graph correctness: A's instance entity (source run:alpha) written to
    A's own BASE graph instead of its KG target → fact_leaked_to_base."""
    await _insert(n, _A_FACTS, WS_A.base_graph)  # instance data in the ontology graph
    violations = await check_isolation(n, [WS_A, WS_B])
    kinds = {v.kind for v in violations}
    assert "fact_leaked_to_base" in kinds, format_isolation(violations)


# --------------------------------------------------------------------------- #
# pyoxigraph: the reentrancy repro — a SHARED resolver interleaved leaks, and the
# invariant catches it. This is the load-bearing case for ONTA-269.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_shared_resolver_reentrancy_leaks_and_is_caught(n):
    """ONE resolver instance shared across two interleaved ingests: the second clobbers
    ``self._instance_graph`` while the first is mid-flight, so alpha's triples are written
    into beta's graph. check_isolation must catch the resulting cross-workspace leak.

    This is the invariant EXPOSING the known SchemaResolver non-reentrancy — not a fix.
    A well-behaved (reentrancy-safe) resolver would keep the run clean, as the
    per-run-resolver test above shows."""
    shared = _SharedReentrantResolver(n)
    await asyncio.gather(
        shared.ingest(
            "", WS_A.tenant, instance_graph=WS_A.instance_graph, source=WS_A.source, facts=_A_FACTS
        ),
        shared.ingest(
            "", WS_B.tenant, instance_graph=WS_B.instance_graph, source=WS_B.source, facts=_B_FACTS
        ),
    )

    violations = await check_isolation(n, [WS_A, WS_B])
    assert not isolated(violations), (
        "expected the shared-resolver interleave to leak, but the run was clean — the "
        "reentrancy model did not clobber (scheduling changed?)"
    )
    # the leak is specifically alpha's data landing in beta's graph.
    assert any(
        v.kind == "cross_workspace_fact" and v.author == "alpha" and v.landed_in == "beta"
        for v in violations
    ), format_isolation(violations)
    # alpha's OWN KG graph ended up empty — its writes were misdirected.
    got = await n.query(
        f"SELECT (COUNT(*) AS ?c) WHERE {{ GRAPH <{WS_A.instance_graph}> {{ ?s ?p ?o }} }}"
    )
    assert int(got["results"]["bindings"][0]["c"]["value"]) == 0


# --------------------------------------------------------------------------- #
# pyoxigraph: unrelated data on a shared store never false-positives
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_unrelated_tenant_data_is_ignored(n):
    """A third tenant with its own source, sharing the store, is not under test — its data
    must not register as a leak for A/B."""
    await _insert(n, _A_FACTS, WS_A.instance_graph)
    await _insert(n, _B_FACTS, WS_B.instance_graph)
    other = _payload("run:gamma", f"{ENT}Physician/g1", f"{ENT}City/g1")
    await _insert(n, other, kg_graph_uri("qc-gamma", "misc"))

    violations = await check_isolation(n, [WS_A, WS_B])
    assert violations == [], format_isolation(violations)

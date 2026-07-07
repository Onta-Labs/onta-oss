"""Tests for the QC audit runner (`cograph_client.qc.audit`) — the layer that turns the
invariant library into something that RUNS against a tenant's graphs.

Two layers (same split as test_qc_invariants):
- **CI-safe** — report aggregation / exit-code semantics, the rendering (human + JSON),
  the KG-graph enumeration filter, and run_audit's graph-TARGETING logic driven by a
  recording fake (which graphs get audited, in what order, with the base graph last).
- **pyoxigraph** — real end-to-end: seed a bad triple in a KG graph and prove the audit
  surfaces it; seed leaked instance data in the BASE graph and prove the base-graph pass
  catches it (the ONTA-198 empty-kg_name class); exit-code semantics on real graphs.
"""
from __future__ import annotations

import json

import pytest

from cograph_client.graph.queries import kg_graph_uri, tenant_graph_uri
from cograph_client.qc import (
    AuditReport,
    GraphAudit,
    Violation,
    format_report,
    report_to_dict,
    run_audit,
)
from cograph_client.qc.audit import _BASE_GRAPH_LABEL, _list_kg_graphs
from cograph_client.qc.__main__ import _parse_args, _resolve_include, _run
from cograph_client.qc.invariants import RDF_TYPE, RDFS_LABEL, RDFS_RANGE

TENANT = "demo"
ENT = "https://cograph.tech/entities/"
ONTO = "https://cograph.tech/onto/"
TYPES = "https://cograph.tech/types/"


def _err(inv="node_edge_on_attrs_predicate") -> Violation:
    return Violation(inv, "error", "detail", {})


def _warn(inv="bare_entity_node_missing_label") -> Violation:
    return Violation(inv, "warn", "detail", {})


# --------------------------------------------------------------------------- #
# CI-safe: report aggregation + exit codes
# --------------------------------------------------------------------------- #
def test_report_counts_and_clean():
    report = AuditReport(
        TENANT,
        [
            GraphAudit("g1", "o", [_err(), _warn()]),
            GraphAudit("g2", "o", []),
        ],
    )
    assert report.error_count == 1
    assert report.warn_count == 1
    assert not report.clean
    assert len(report.violations) == 2


def test_report_clean_when_empty():
    assert AuditReport(TENANT, [GraphAudit("g", "o", [])]).clean
    assert AuditReport(TENANT, []).clean


def test_exit_code_errors_always_fail():
    r = AuditReport(TENANT, [GraphAudit("g", "o", [_err()])])
    assert r.exit_code() == 1
    assert r.exit_code(strict=True) == 1


def test_exit_code_warnings_fail_only_under_strict():
    r = AuditReport(TENANT, [GraphAudit("g", "o", [_warn()])])
    assert r.exit_code() == 0
    assert r.exit_code(strict=True) == 1


def test_exit_code_clean_passes():
    r = AuditReport(TENANT, [GraphAudit("g", "o", [])])
    assert r.exit_code() == 0
    assert r.exit_code(strict=True) == 0


def test_graph_audit_error_warn_split():
    a = GraphAudit("g", "o", [_err(), _err(), _warn()])
    assert a.errors == 2 and a.warns == 1


def test_graph_audit_display_prefers_label():
    assert GraphAudit("g", "o", [], label="(base)").display == "(base)"
    assert GraphAudit("g", "o", []).display == "g"


# --------------------------------------------------------------------------- #
# CI-safe: rendering
# --------------------------------------------------------------------------- #
def test_format_report_human_lists_violations_and_summary():
    report = AuditReport(
        TENANT,
        [
            GraphAudit("kg1", "o", [_err("node_edge_on_attrs_predicate")]),
            GraphAudit("kg2", "o", [], label=_BASE_GRAPH_LABEL),
        ],
    )
    out = format_report(report)
    assert "tenant: demo" in out
    assert "node_edge_on_attrs_predicate" in out
    assert "[error]" in out
    assert "(clean)" in out  # kg2 has no violations
    assert _BASE_GRAPH_LABEL in out
    assert "1 error(s), 0 warning(s) across 2 graph(s)" in out


def test_format_report_json_roundtrips():
    report = AuditReport(TENANT, [GraphAudit("kg1", "o", [_err(), _warn()])])
    out = format_report(report, as_json=True)
    parsed = json.loads(out)
    assert parsed == report_to_dict(report)
    assert parsed["tenant"] == TENANT
    assert parsed["error_count"] == 1 and parsed["warn_count"] == 1
    assert parsed["graphs"][0]["violations"][0]["invariant"] == "node_edge_on_attrs_predicate"


def test_report_to_dict_preserves_binding():
    v = Violation("inv", "error", "d", {"s": {"value": "x"}})
    d = report_to_dict(AuditReport(TENANT, [GraphAudit("g", "o", [v])]))
    assert d["graphs"][0]["violations"][0]["binding"] == {"s": {"value": "x"}}


# --------------------------------------------------------------------------- #
# CI-safe: enumeration + targeting via a recording fake
# --------------------------------------------------------------------------- #
class _RecordingNeptune:
    """Returns a canned graph list for the enumerate query and empty bindings for every
    invariant query — so run_audit's TARGETING (which graphs, what order) is observable
    without a real triplestore. Records all SPARQL it sees."""

    def __init__(self, graphs: list[str]):
        self._graphs = graphs
        self.queries: list[str] = []

    async def query(self, sparql: str) -> dict:
        self.queries.append(sparql)
        if "DISTINCT ?g" in sparql:
            return {
                "results": {"bindings": [{"g": {"value": g}} for g in self._graphs]}
            }
        return {"results": {"bindings": []}}


@pytest.mark.asyncio
async def test_list_kg_graphs_filters_to_tenant_kg_graphs_sorted():
    other = kg_graph_uri("other", "z")
    fake = _RecordingNeptune(
        [
            kg_graph_uri(TENANT, "imdb"),
            kg_graph_uri(TENANT, "clinical"),
            tenant_graph_uri(TENANT),  # base graph — not a KG graph
            other,  # different tenant
            f"{tenant_graph_uri(TENANT)}/kg/x/provenance",  # companion — not a KG graph
        ]
    )
    graphs = await _list_kg_graphs(fake, TENANT)
    assert graphs == [kg_graph_uri(TENANT, "clinical"), kg_graph_uri(TENANT, "imdb")]


@pytest.mark.asyncio
async def test_run_audit_single_kg_targets_only_that_graph():
    fake = _RecordingNeptune([])
    report = await run_audit(fake, tenant=TENANT, kg="imdb")
    assert [a.graph_uri for a in report.audits] == [kg_graph_uri(TENANT, "imdb")]
    assert report.audits[0].onto_graph_uri == tenant_graph_uri(TENANT)
    # no enumeration query when a single KG is named
    assert not any("DISTINCT ?g" in q for q in fake.queries)


@pytest.mark.asyncio
async def test_run_audit_enumerate_audits_every_kg_plus_base_graph_last():
    fake = _RecordingNeptune(
        [kg_graph_uri(TENANT, "imdb"), kg_graph_uri(TENANT, "clinical")]
    )
    report = await run_audit(fake, tenant=TENANT)
    graphs = [a.graph_uri for a in report.audits]
    # every KG (sorted) then the base graph last
    assert graphs == [
        kg_graph_uri(TENANT, "clinical"),
        kg_graph_uri(TENANT, "imdb"),
        tenant_graph_uri(TENANT),
    ]
    assert report.audits[-1].label == _BASE_GRAPH_LABEL
    # the base graph is audited as its own instance graph AND its own onto graph
    assert report.audits[-1].onto_graph_uri == tenant_graph_uri(TENANT)


@pytest.mark.asyncio
async def test_run_audit_passes_include_through():
    fake = _RecordingNeptune([kg_graph_uri(TENANT, "imdb")])
    await run_audit(fake, tenant=TENANT, include={"node_edge_on_attrs_predicate"})
    # only the one included invariant's query should have been issued per graph
    inv_queries = [q for q in fake.queries if "DISTINCT ?g" not in q]
    assert inv_queries, "expected at least one invariant query"
    assert all("attrs" in q for q in inv_queries)


# --------------------------------------------------------------------------- #
# CI-safe: --include validation (a typo must NOT silently pass a gate)
# --------------------------------------------------------------------------- #
def test_resolve_include_none_for_empty():
    assert _resolve_include(None) is None
    assert _resolve_include("") is None


def test_resolve_include_parses_and_trims_valid_names():
    assert _resolve_include(" node_edge_on_attrs_predicate , bare_entity_node_missing_label ") == {
        "node_edge_on_attrs_predicate",
        "bare_entity_node_missing_label",
    }


def test_resolve_include_rejects_unknown_name():
    with pytest.raises(ValueError, match="unknown invariant name"):
        _resolve_include("node_edge_on_attrs_predicate,bogus")


@pytest.mark.asyncio
async def test_cli_typoed_include_exits_2_before_touching_store():
    """A misspelled --include fails fast (exit 2) at validation, BEFORE the health check —
    so it never reports a vacuous 'clean'/exit-0, even against an unreachable endpoint."""
    args = _parse_args(
        ["--tenant", TENANT, "--include", "bogus", "--endpoint", "http://127.0.0.1:1"]
    )
    assert await _run(args) == 2


# --------------------------------------------------------------------------- #
# pyoxigraph: real end-to-end audit
# --------------------------------------------------------------------------- #
class PyoxiNeptune:
    """Minimal client shim over an in-process pyoxigraph Store (lazy import)."""

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


async def _insert(n: PyoxiNeptune, triples: str, graph: str) -> None:
    await n.update(f"INSERT DATA {{ GRAPH <{graph}> {{ {triples} }} }}")


_KG = kg_graph_uri(TENANT, "imdb")
_BASE = tenant_graph_uri(TENANT)
_GOOD_PHYS = f'<{ENT}Physician/p1> <{RDF_TYPE}> <{TYPES}Physician> ; <{RDFS_LABEL}> "Dr P" . '
_GOOD_CITY = f'<{ENT}City/SF> <{RDF_TYPE}> <{TYPES}City> ; <{RDFS_LABEL}> "SF" . '


@pytest.mark.asyncio
async def test_e2e_clean_tenant_is_clean_and_exits_zero(n):
    await _insert(
        n,
        _GOOD_PHYS + _GOOD_CITY + f"<{ENT}Physician/p1> <{ONTO}located_in> <{ENT}City/SF> . ",
        graph=_KG,
    )
    await _insert(n, f"<{TYPES}Physician/attrs/located_in> <{RDFS_RANGE}> <{TYPES}City> . ", graph=_BASE)
    report = await run_audit(n, tenant=TENANT)
    assert report.clean
    assert report.exit_code() == 0
    # both the KG graph and the base graph were audited
    assert _KG in {a.graph_uri for a in report.audits}
    assert _BASE in {a.graph_uri for a in report.audits}


@pytest.mark.asyncio
async def test_e2e_bad_edge_in_kg_is_surfaced(n):
    """A node-valued edge on attrs/<leaf> in a KG graph is found by enumerate-all."""
    await _insert(
        n,
        _GOOD_PHYS + _GOOD_CITY
        + f"<{ENT}Physician/p1> <{TYPES}Physician/attrs/located_in> <{ENT}City/SF> . ",
        graph=_KG,
    )
    report = await run_audit(n, tenant=TENANT)
    assert report.error_count == 1
    assert report.exit_code() == 1
    kg_audit = next(a for a in report.audits if a.graph_uri == _KG)
    assert [v.invariant for v in kg_audit.violations] == ["node_edge_on_attrs_predicate"]


@pytest.mark.asyncio
async def test_e2e_leaked_instance_data_in_base_graph_is_caught(n):
    """ONTA-198 class: instance data written to the tenant BASE graph (empty kg_name)
    instead of a KG graph. The base-graph pass audits it as an instance graph and the
    same bare-node invariant surfaces the untyped target."""
    await _insert(
        n,
        _GOOD_PHYS + f"<{ENT}Physician/p1> <{ONTO}works_at> <{ENT}Hospital/h1> . ",
        graph=_BASE,  # leaked into the base graph
    )
    report = await run_audit(n, tenant=TENANT)
    base_audit = next(a for a in report.audits if a.graph_uri == _BASE)
    assert "bare_entity_node_missing_type" in {v.invariant for v in base_audit.violations}
    assert report.exit_code() == 1


@pytest.mark.asyncio
async def test_e2e_base_graph_catches_node_edge_on_attrs(n):
    """The base-graph pass also catches a node-valued edge on attrs/<leaf> leaked there —
    not just the bare-node class."""
    await _insert(
        n,
        _GOOD_PHYS + _GOOD_CITY
        + f"<{ENT}Physician/p1> <{TYPES}Physician/attrs/located_in> <{ENT}City/SF> . ",
        graph=_BASE,  # leaked into the base graph
    )
    report = await run_audit(n, tenant=TENANT)
    base_audit = next(a for a in report.audits if a.graph_uri == _BASE)
    assert "node_edge_on_attrs_predicate" in {v.invariant for v in base_audit.violations}


@pytest.mark.asyncio
async def test_e2e_base_graph_declared_relationship_on_literal_self_join(n):
    """The needs_onto invariant on the base graph, where onto_graph_uri == graph_uri: a
    declared relationship (located_in range City) whose leaked instance edge points at a
    LITERAL is caught by the declaration join resolving against the base graph itself."""
    await _insert(
        n,
        _GOOD_PHYS
        + f'<{ENT}Physician/p1> <{ONTO}located_in> "somewhere" . '  # literal in a node slot
        + f"<{TYPES}Physician/attrs/located_in> <{RDFS_RANGE}> <{TYPES}City> . ",  # declared a relationship
        graph=_BASE,
    )
    report = await run_audit(n, tenant=TENANT)
    base_audit = next(a for a in report.audits if a.graph_uri == _BASE)
    assert base_audit.onto_graph_uri == _BASE  # base is its own onto graph (self-join)
    assert "relationship_edge_points_at_literal" in {v.invariant for v in base_audit.violations}


@pytest.mark.asyncio
async def test_e2e_single_kg_scope_ignores_other_kgs(n):
    """--kg imdb audits only imdb, even when another KG has a violation."""
    await _insert(
        n,
        f"<{ENT}Physician/p1> <{TYPES}Physician/attrs/located_in> <{ENT}City/SF> . ",
        graph=kg_graph_uri(TENANT, "other"),  # violation lives in a DIFFERENT kg
    )
    await _insert(n, _GOOD_PHYS, graph=_KG)  # imdb is clean
    report = await run_audit(n, tenant=TENANT, kg="imdb")
    assert [a.graph_uri for a in report.audits] == [_KG]
    assert report.clean


@pytest.mark.asyncio
async def test_e2e_warn_only_passes_unless_strict(n):
    """A typed-but-unlabelled node is a warning: exits 0 normally, 1 under strict."""
    await _insert(n, f"<{ENT}City/SF> <{RDF_TYPE}> <{TYPES}City> . ", graph=_KG)  # no label
    report = await run_audit(n, tenant=TENANT)
    assert report.error_count == 0 and report.warn_count == 1
    assert report.exit_code() == 0
    assert report.exit_code(strict=True) == 1

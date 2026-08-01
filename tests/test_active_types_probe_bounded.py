"""ONTA-427: the active-types probe must be BOUNDED, without weakening ONTA-258.

`_fetch_ontology` computes which DECLARED types actually carry instances. That
signal is what marks a declared-but-empty type "[no instances]" instead of
hiding it (ONTA-258). It used to be one unbounded `SELECT DISTINCT ?type` scan
of the whole instance graph, run on every ontology fetch, and since
`refresh_after_write` invalidates the ontology cache after every converged
write, an active ingest made it fire on essentially every /ask.

It is now one LIMIT-1 existence probe per DECLARED candidate type URI: cost
tracks the declared type count, not the entity count. These tests pin BOTH
halves: the probe is bounded, AND the empty-type signal it feeds is unchanged
(including the false-empty cases that would be the real ONTA-258 regression).

The fake Neptune here is deliberately STRICTER than the one in
test_ontology_empty_types_visible.py: it answers a bounded probe only about the
URIs the probe actually asked for, so a test cannot pass by accident on a
permissive superset.
"""

from __future__ import annotations

import re

from cograph_client.nlp import pipeline as pl
from cograph_client.nlp.pipeline import (
    NLQueryPipeline,
    ONTOLOGY_EMPTY,
    ONTOLOGY_FETCH_ERROR,
    _ontology_cache,
)

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
TYPES = "https://graph.onta.sh/types/"
ONTO = "https://graph.onta.sh/onto/"
STR = "http://www.w3.org/2001/XMLSchema#string"
INT = "http://www.w3.org/2001/XMLSchema#integer"

GRAPH = "https://graph.onta.sh/graphs/inv-tenant"
KG = "https://graph.onta.sh/graphs/inv-tenant/kg/InventedKG"

# The bounded probe's per-candidate existence subselect.
_PROBE_BLOCK = re.compile(r"SELECT \(<([^>]+)> AS \?type\)")
# The unbounded whole-graph scan (variable in object position).
_SCAN = re.compile(r"\?s <[^>]*#type> \?type\s*\}")


def _row(**cells):
    return {k: {"type": "uri", "value": v} for k, v in cells.items()}


def _results(rows):
    vars_ = sorted({k for r in rows for k in r})
    return {"head": {"vars": vars_}, "results": {"bindings": rows}}


class ProbeNeptune:
    """Declares Widget{serial} -pairs_with-> Sprocket, Sprocket{torque}, Gadget{}.

    `populated` is the set of type URIs that actually have instances, so a test
    can populate a type under ANY layer namespace. Records every query, counts
    unbounded scans separately, and answers the bounded probe using ONLY the
    URIs that probe asked about.
    """

    ONTOLOGY_ROWS = [
        _row(type=f"{TYPES}Widget", typeLabel="Widget",
             attr=f"{TYPES}Widget/attrs/serial", attrLabel="serial", range=STR),
        _row(type=f"{TYPES}Widget", typeLabel="Widget",
             attr=f"{TYPES}Widget/attrs/pairs_with", attrLabel="pairs_with",
             range=f"{TYPES}Sprocket"),
        _row(type=f"{TYPES}Sprocket", typeLabel="Sprocket",
             attr=f"{TYPES}Sprocket/attrs/torque", attrLabel="torque", range=INT),
        _row(type=f"{TYPES}Gadget", typeLabel="Gadget",
             attr=f"{TYPES}Gadget/attrs/weight", attrLabel="weight", range=INT),
    ]

    def __init__(self, populated=(f"{TYPES}Widget",), *, probe_raises=False,
                 ontology_rows=None, pred_map=None):
        self.populated = set(populated)
        self.probe_raises = probe_raises
        self.ontology_rows = (
            self.ONTOLOGY_ROWS if ontology_rows is None else list(ontology_rows)
        )
        self.pred_map: dict[str, list[str]] = dict(pred_map or {})
        self.queries: list[str] = []
        self.probe_queries: list[str] = []
        self.probed_uris: list[str] = []
        self.scans: list[str] = []

    async def query(self, sparql: str):
        self.queries.append(sparql)
        asked = _PROBE_BLOCK.findall(sparql)
        if asked:
            self.probe_queries.append(sparql)
            self.probed_uris.extend(asked)
            if self.probe_raises:
                raise RuntimeError("engine rejected the probe shape")
            return _results([_row(type=u) for u in asked if u in self.populated])
        if _SCAN.search(sparql) and "SELECT DISTINCT ?type" in sparql:
            self.scans.append(sparql)
            return _results([_row(type=u) for u in sorted(self.populated)])
        if "?typeLabel" in sparql:
            return _results(self.ontology_rows)
        if "SELECT DISTINCT ?p" in sparql:
            for leaf, preds in self.pred_map.items():
                if f"<{TYPES}{leaf}>" in sparql:
                    return _results([_row(p=p) for p in preds])
            return _results([])
        if "COUNT(DISTINCT ?val)" in sparql:
            return _results([{"cnt": {"type": "literal", "value": "5"}}])
        if "SELECT DISTINCT ?val" in sparql:
            return _results([{"val": {"type": "literal", "value": "alpha"}}])
        return _results([])


def _pipe(neptune):
    return NLQueryPipeline(neptune, anthropic_key="dummy")


def _parse_types(summary: str) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for line in summary.splitlines():
        m = re.match(r"Type: (\w+) ", line)
        if m:
            out[m.group(1)] = "[no instances]" in line
    return out


# --------------------------------------------------------------------------- #
# ONTA-258 signal, preserved under the bounded probe
# --------------------------------------------------------------------------- #

async def test_declared_empty_type_still_reported_empty():
    """The load-bearing guarantee: a DECLARED-but-empty type stays visible and
    IS marked [no instances] when the signal comes from the bounded probe."""
    _ontology_cache.clear()
    n = ProbeNeptune(populated=(f"{TYPES}Widget",))
    summary = await _pipe(n)._fetch_ontology(GRAPH, KG)

    assert summary not in (ONTOLOGY_EMPTY, ONTOLOGY_FETCH_ERROR)
    parsed = _parse_types(summary)
    assert set(parsed) == {"Widget", "Sprocket", "Gadget"}
    assert parsed["Widget"] is False
    assert parsed["Sprocket"] is True, "declared-but-empty type lost its mark"
    assert parsed["Gadget"] is True, "declared-but-empty type lost its mark"
    # The declared schema of the empty types is still exposed.
    assert "torque" in summary and "weight" in summary


async def test_populated_type_never_falsely_marked_empty():
    """Control: every declared type populated -> nothing marked [no instances]."""
    _ontology_cache.clear()
    n = ProbeNeptune(
        populated=(f"{TYPES}Widget", f"{TYPES}Sprocket", f"{TYPES}Gadget")
    )
    parsed = _parse_types(await _pipe(n)._fetch_ontology(GRAPH, KG))
    assert set(parsed) == {"Widget", "Sprocket", "Gadget"}
    assert not any(parsed.values())


async def test_instances_in_another_layer_namespace_count_as_active():
    """The old scan matched instance types to declared types by NAME, across
    layer namespaces. The bounded probe must ask about EVERY namespace for a
    declared name, or a type whose instances are typed `types/public/Widget`
    would be falsely marked [no instances], the ONTA-258 regression."""
    _ontology_cache.clear()
    n = ProbeNeptune(populated=(f"{TYPES}public/Widget",))
    parsed = _parse_types(await _pipe(n)._fetch_ontology(GRAPH, KG))
    assert parsed["Widget"] is False, "public-namespace instances must count"
    assert parsed["Sprocket"] is True and parsed["Gadget"] is True


# --------------------------------------------------------------------------- #
# boundedness
# --------------------------------------------------------------------------- #

async def test_no_unbounded_scan_on_the_hot_path():
    """The whole point of ONTA-427: an ordinary fetch issues NO unbounded
    `SELECT DISTINCT ?type` over the instance graph."""
    _ontology_cache.clear()
    n = ProbeNeptune(populated=(f"{TYPES}Widget",))
    await _pipe(n)._fetch_ontology(GRAPH, KG)
    assert n.scans == [], "the unbounded whole-graph type scan came back"
    assert n.probe_queries, "no bounded probe was issued"


async def test_probe_asks_only_about_declared_types_and_bounds_each():
    """Every probed URI belongs to a DECLARED type name, and every existence
    subselect carries LIMIT 1 (a first-match seek, not a scan)."""
    _ontology_cache.clear()
    n = ProbeNeptune(populated=(f"{TYPES}Widget",))
    await _pipe(n)._fetch_ontology(GRAPH, KG)

    declared = {"Widget", "Sprocket", "Gadget"}
    assert n.probed_uris, "probe asked about nothing"
    for uri in n.probed_uris:
        assert uri.rsplit("/", 1)[-1] in declared, f"probed undeclared type {uri}"
    # Work is bounded by declared types (3 names x 3 layer namespaces), not by
    # anything that grows with the KG's entity count.
    assert len(n.probed_uris) == len(declared) * 3
    for q in n.probe_queries:
        assert q.count("LIMIT 1") == q.count("AS ?type"), "an existence probe is unbounded"
        assert q.lstrip().upper().startswith("SELECT"), "probe must be read-only"


async def test_probe_query_is_read_only():
    """Production probes are SELECT/ASK only, never a mutation."""
    _ontology_cache.clear()
    n = ProbeNeptune(populated=(f"{TYPES}Widget",))
    await _pipe(n)._fetch_ontology(GRAPH, KG)
    for q in n.queries:
        upper = q.upper()
        for verb in ("INSERT", "DELETE", "DROP", "CLEAR", "LOAD ", "CREATE GRAPH"):
            assert verb not in upper, f"mutating verb {verb} in a probe: {q[:120]}"


async def test_over_cap_falls_back_to_one_scan(monkeypatch):
    """When a KG declares more types than the probe cap, one sequential scan is
    cheaper than hundreds of seeks, so we deliberately use the scan, and the
    empty-type signal is unchanged."""
    _ontology_cache.clear()
    monkeypatch.setattr(pl, "MAX_ACTIVE_TYPE_PROBE_URIS", 2)
    n = ProbeNeptune(populated=(f"{TYPES}Widget",))
    parsed = _parse_types(await _pipe(n)._fetch_ontology(GRAPH, KG))

    assert n.probe_queries == [], "should not probe past the cap"
    assert len(n.scans) == 1, "expected exactly one fallback scan"
    assert parsed["Widget"] is False
    assert parsed["Sprocket"] is True and parsed["Gadget"] is True


# --------------------------------------------------------------------------- #
# degradation: cost, never correctness
# --------------------------------------------------------------------------- #

async def test_probe_failure_degrades_to_the_scan_not_to_a_wrong_answer():
    """If the engine rejects the probe shape, fall back to the pre-ONTA-427
    scan. A partial/failed probe must NEVER be treated as truth, since that would
    mark populated types [no instances]."""
    _ontology_cache.clear()
    n = ProbeNeptune(populated=(f"{TYPES}Widget",), probe_raises=True)
    summary = await _pipe(n)._fetch_ontology(GRAPH, KG)

    assert summary != ONTOLOGY_FETCH_ERROR, "a probe hiccup must not kill the fetch"
    assert len(n.scans) == 1, "expected the fallback scan"
    parsed = _parse_types(summary)
    assert parsed["Widget"] is False, "populated type falsely marked empty"
    assert parsed["Sprocket"] is True and parsed["Gadget"] is True


async def test_schema_missing_fallback_still_sees_undeclared_instance_types():
    """Cold start (schema not written yet): the instance-derived fallback needs
    types the ontology never declared, so THAT path still scans, exactly once,
    and still produces the instance-derived summary."""
    _ontology_cache.clear()
    n = ProbeNeptune(
        populated=(f"{TYPES}Gizmo",),  # not in the declared ontology
        pred_map={"Gizmo": [f"{TYPES}Gizmo/attrs/spin", f"{ONTO}links"]},
    )
    summary = await _pipe(n)._fetch_ontology(GRAPH, KG)

    assert "has not been written yet" in summary
    assert "Type: Gizmo" in summary and "spin" in summary and "links" in summary
    assert len(n.scans) == 1, "the cold-start path must scan once, not twice"


async def test_genuinely_empty_kg_still_reports_no_ontology():
    """No declared type populated and no instance data at all -> the original
    ONTOLOGY_EMPTY message, not a fabricated fallback."""
    _ontology_cache.clear()
    n = ProbeNeptune(populated=())
    summary = await _pipe(n)._fetch_ontology(GRAPH, KG)
    assert summary == ONTOLOGY_EMPTY


async def test_no_probe_when_there_is_no_distinct_instance_graph():
    """Fetching the tenant ontology graph itself has no instance graph to probe,
    so no probe and no scan (unchanged behavior)."""
    _ontology_cache.clear()
    n = ProbeNeptune(populated=(f"{TYPES}Widget",))
    await _pipe(n)._fetch_ontology(GRAPH)
    assert n.probe_queries == [] and n.scans == []

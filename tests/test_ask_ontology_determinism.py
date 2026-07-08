"""ONTA-248: the ontology summary the SPARQL-generation LLM sees must be
DETERMINISTIC over a fixed graph, and a failed cardinality COUNT must degrade to
"unknown" — NEVER drop a DECLARED type / attribute / relationship.

Root cause: `_fetch_ontology` dropped an attribute/relationship whenever its
`COUNT(DISTINCT ?val)` came back 0 — which a transient Neptune throttle produces
(empty result set) exactly like a genuinely-empty predicate — so the same graph
rendered a different schema call-to-call, and the LLM "explained" a type was
absent while a real COUNT proved 18 instances. That is the trust-killer.

These tests use an INVENTED ontology (two unrelated domains) and assert on the
MECHANISM: declared types/attrs/rels always appear; a raised COUNT never deletes
them; and a transient fetch error is reported distinctly from an empty graph.
No persona tokens.
"""

from __future__ import annotations

from cograph_client.nlp.pipeline import (
    NLQueryPipeline,
    ONTOLOGY_EMPTY,
    ONTOLOGY_FETCH_ERROR,
    _ontology_cache,
)

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
TYPES = "https://cograph.tech/types/"
ONTO = "https://cograph.tech/onto/"

GRAPH = "https://cograph.tech/graphs/inv-tenant"
KG = "https://cograph.tech/graphs/inv-tenant/kg/InventedKG"


def _row(**cells):
    return {k: {"type": "uri", "value": v} for k, v in cells.items()}


def _results(rows):
    vars_ = sorted({k for r in rows for k in r})
    return {"head": {"vars": vars_}, "results": {"bindings": rows}}


class FaultNeptune:
    """Routes SPARQL by shape. Declares an invented 2-type ontology with instances.

    ``fail_counts``: when True, EVERY cardinality `COUNT(DISTINCT ?val)` query
    RAISES — the throttle/timeout fault. The declared schema must still render.
    ``count_value``: the integer every successful COUNT returns.
    """

    # Invented ontology: Sensor{reading_unit, model} + Reactor{coolant_type};
    # Sensor —monitors→ Reactor. Nothing here matches any persona token.
    ONTOLOGY_ROWS = [
        # Sensor: two attributes (string ranges) + a relationship to Reactor
        _row(type=f"{TYPES}Sensor", typeLabel="Sensor",
             attr=f"{TYPES}Sensor/attrs/reading_unit", attrLabel="reading_unit",
             range="http://www.w3.org/2001/XMLSchema#string"),
        _row(type=f"{TYPES}Sensor", typeLabel="Sensor",
             attr=f"{TYPES}Sensor/attrs/model", attrLabel="model",
             range="http://www.w3.org/2001/XMLSchema#string"),
        _row(type=f"{TYPES}Sensor", typeLabel="Sensor",
             attr=f"{TYPES}Sensor/attrs/monitors", attrLabel="monitors",
             range=f"{TYPES}Reactor"),
        # Reactor: one attribute
        _row(type=f"{TYPES}Reactor", typeLabel="Reactor",
             attr=f"{TYPES}Reactor/attrs/coolant_type", attrLabel="coolant_type",
             range="http://www.w3.org/2001/XMLSchema#string"),
    ]

    def __init__(self, *, fail_counts=False, count_value=7):
        self.fail_counts = fail_counts
        self.count_value = count_value

    async def query(self, sparql: str):
        s = sparql
        # Active-types probe (instances present for both types)
        if "SELECT DISTINCT ?type" in s and "rdf-syntax-ns#type" in s:
            return _results([
                _row(type=f"{TYPES}Sensor"),
                _row(type=f"{TYPES}Reactor"),
            ])
        # Full-ontology schema query
        if "?typeLabel" in s:
            return _results(self.ONTOLOGY_ROWS)
        # Cardinality COUNT(DISTINCT ?val)
        if "COUNT(DISTINCT ?val)" in s:
            if self.fail_counts:
                raise RuntimeError("neptune throttled (429)")
            return _results([{"cnt": {"type": "literal", "value": str(self.count_value)}}])
        # Enum value fetch
        if "SELECT DISTINCT ?val" in s:
            return _results([{"val": {"type": "literal", "value": "alpha"}}])
        return _results([])


def _pipe(neptune):
    return NLQueryPipeline(neptune, anthropic_key="dummy")


def _clear_cache():
    _ontology_cache.clear()


async def test_declared_schema_survives_count_failure():
    """A raised COUNT (throttle) must NOT delete a declared type/attr/relationship."""
    _clear_cache()
    pipe = _pipe(FaultNeptune(fail_counts=True))
    summary = await pipe._fetch_ontology(GRAPH, KG)
    # Both declared types present.
    assert "Type: Sensor" in summary
    assert "Type: Reactor" in summary
    # Declared attributes present despite every COUNT failing.
    assert "reading_unit" in summary
    assert "model" in summary
    assert "coolant_type" in summary
    # Declared relationship present (predicate URI preserved on onto/<leaf>).
    assert "monitors" in summary
    assert f"{ONTO}monitors" in summary
    # A failed fetch is NOT reported.
    assert summary != ONTOLOGY_FETCH_ERROR


async def test_summary_is_identical_across_repeated_calls():
    """Same fixed graph → byte-identical summary across N calls (determinism)."""
    _clear_cache()
    summaries = []
    for _ in range(8):
        _clear_cache()  # defeat the 60s cache so each call re-derives the summary
        pipe = _pipe(FaultNeptune(fail_counts=False, count_value=18))
        summaries.append(await pipe._fetch_ontology(GRAPH, KG))
    assert len(set(summaries)) == 1, "ontology summary flickered across identical calls"
    # And a real type is never reported absent.
    assert "Sensor" in summaries[0] and "Reactor" in summaries[0]


async def test_count_failure_matches_success_for_type_existence():
    """Type/relationship EXISTENCE is stable whether COUNTs succeed or all fail —
    the throttle path must not silently remove a type or relationship."""
    _clear_cache()
    pipe_ok = _pipe(FaultNeptune(fail_counts=False, count_value=5))
    ok = await pipe_ok._fetch_ontology(GRAPH, KG)
    _clear_cache()
    pipe_fail = _pipe(FaultNeptune(fail_counts=True))
    failed = await pipe_fail._fetch_ontology(GRAPH, KG)

    def _types(summary):
        return {l.split("—")[0].replace("Type:", "").strip()
                for l in summary.splitlines() if l.startswith("Type:")}

    def _has_rel(summary, name):
        return f"{ONTO}{name}" in summary

    assert _types(ok) == _types(failed) == {"Sensor", "Reactor"}
    assert _has_rel(ok, "monitors") and _has_rel(failed, "monitors")


async def test_empty_graph_distinct_from_fetch_error():
    """An empty graph and a transient fetch error must be REPORTED DIFFERENTLY."""
    _clear_cache()

    class EmptyNeptune:
        async def query(self, sparql):
            # No instances, no schema anywhere.
            return {"head": {"vars": []}, "results": {"bindings": []}}

    empty = await _pipe(EmptyNeptune())._fetch_ontology(GRAPH, GRAPH)
    assert empty == ONTOLOGY_EMPTY

    _clear_cache()

    class BrokenNeptune:
        async def query(self, sparql):
            raise RuntimeError("connection reset")

    err = await _pipe(BrokenNeptune())._fetch_ontology(GRAPH, KG)
    assert err == ONTOLOGY_FETCH_ERROR
    assert err != ONTOLOGY_EMPTY
    # The error marker must NOT claim the graph is empty / a type is absent.
    low = err.lower()
    assert "does not" in low or "unknown" in low
    assert "retry" in low


async def test_zero_instance_attribute_is_kept_not_dropped():
    """A CONFIRMED-empty declared attribute (cnt==0) is annotated, not deleted —
    otherwise it flickers vs a call where the same predicate returns rows."""
    _clear_cache()
    pipe = _pipe(FaultNeptune(fail_counts=False, count_value=0))
    summary = await pipe._fetch_ontology(GRAPH, KG)
    # Every declared attribute still present even though all counts are 0.
    for attr in ("reading_unit", "model", "coolant_type"):
        assert attr in summary
    assert "[no instances]" in summary

"""ONTA-280 acceptance: honest answers — per-fact verdict/confidence/recency +
coverage caveats (the P7 answer layer).

Two halves, mirroring the ticket:

1. **CI-safe pure unit half** (no LLM key, no store): ``build_citations`` against a
   tiny fake-neptune that returns canned validity + provenance SPARQL JSON, and
   ``build_coverage_caveat`` over a real ``RunManifest.coverage()``. Asserts each
   :class:`FactCitation` carries valid_from + is_current + confidence + verdict, and
   the caveat contains an "N of M" fragment + a stale count. This half is decoupled
   from the flaky ``ask_*`` LLM tests and MUST pass with NO env keys.

2. **pyoxigraph acceptance half** (``importorskip``): seed a functional predicate
   with two values via the shared ``kg_writer.insert_facts`` — one OPEN interval
   (current) and one CLOSED (superseded) — plus provenance for both, then run a
   describe-shape read and assert the answer cites BOTH facts (one is_current, one
   verdict="superseded") with confidences, AND that the coverage caveat flags the
   stale gap. A load-bearing control (a current-only fact) yields is_current=True
   and NO stale caveat — so the acceptance test fails if verdicts were faked.
"""
from __future__ import annotations

import json
import re

import pytest

from cograph_client.models.query import FactCitation
from cograph_client.nlp.answer_meta import build_citations, build_coverage_caveat

ACME = "https://cograph.tech/entities/Company/acme"
HAS_CEO = "https://cograph.tech/onto/hasCEO"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"


# --------------------------------------------------------------------------- #
# 1. Pure unit half — no store, no LLM key.
# --------------------------------------------------------------------------- #
class _CannedNeptune:
    """Dispatches ``query()`` to canned validity / provenance SPARQL JSON by the
    companion graph the query targets (``/validity`` vs ``/provenance``)."""

    def __init__(self, validity: dict, provenance: dict) -> None:
        self._validity = validity
        self._provenance = provenance
        self.queries: list[str] = []

    async def query(self, sparql: str) -> dict:
        self.queries.append(sparql)
        if "/validity" in sparql:
            return self._validity
        if "/provenance" in sparql:
            return self._provenance
        return {"head": {"vars": []}, "results": {"bindings": []}}


def _lit(value: str) -> dict:
    return {"type": "literal", "value": value}


def _uri(value: str) -> dict:
    return {"type": "uri", "value": value}


_VALIDITY_JSON = {
    "head": {"vars": ["o", "validFrom", "validTo", "supersededBy", "status"]},
    "results": {
        "bindings": [
            {  # Alice — CLOSED (superseded): carries validTo + status.
                "o": _lit("Alice"),
                "validFrom": _lit("2025-01-01T00:00:00+00:00"),
                "validTo": _lit("2026-07-01T00:00:00+00:00"),
                "supersededBy": _lit("stmt-bob"),
                "status": _lit("superseded"),
            },
            {  # Bob — OPEN (current): validFrom only, no validTo.
                "o": _lit("Bob"),
                "validFrom": _lit("2026-07-01T00:00:00+00:00"),
            },
        ]
    },
}

_PROVENANCE_JSON = {
    "head": {"vars": ["p", "o", "stmt", "source", "confidence", "timestamp", "graph", "authority"]},
    "results": {
        "bindings": [
            {"p": _uri(HAS_CEO), "o": _lit("Alice"), "stmt": _lit("s1"),
             "source": _lit("old_wiki"), "confidence": _lit("0.6")},
            {"p": _uri(HAS_CEO), "o": _lit("Bob"), "stmt": _lit("s2"),
             "source": _lit("crm_export.csv"), "confidence": _lit("0.95")},
        ]
    },
}


@pytest.mark.asyncio
async def test_build_citations_unit_carries_verdict_confidence_recency():
    """Each cited fact carries valid_from + is_current + confidence + verdict; the
    superseded fact reads not-current, the open fact reads current."""
    neptune = _CannedNeptune(_VALIDITY_JSON, _PROVENANCE_JSON)
    variables = ["s", "p", "o"]
    bindings = [
        {"s": ACME, "p": HAS_CEO, "o": "Alice"},
        {"s": ACME, "p": HAS_CEO, "o": "Bob"},
    ]

    citations = await build_citations(neptune, "https://cograph.tech/graphs/t/kg/corp", variables, bindings)
    assert len(citations) == 2
    by_obj = {c.object: c for c in citations}

    # Every citation surfaces the four honesty signals.
    for c in citations:
        assert isinstance(c, FactCitation)
        assert c.verdict, "verdict must be set"
        assert c.confidence is not None, "confidence must be read from provenance"
        assert c.valid_from, "valid_from must be read from the validity interval"
        assert isinstance(c.is_current, bool)

    # The superseded (closed) fact.
    alice = by_obj["Alice"]
    assert alice.is_current is False
    assert alice.verdict == "superseded"
    assert alice.confidence == 0.6
    assert alice.source == "old_wiki"
    assert alice.valid_from == "2025-01-01T00:00:00+00:00"

    # The current (open) fact.
    bob = by_obj["Bob"]
    assert bob.is_current is True
    assert bob.verdict == "current"
    assert bob.confidence == 0.95
    assert bob.source == "crm_export.csv"

    # Batched: one validity read + one provenance read for the single (s, p).
    assert sum("/validity" in q for q in neptune.queries) == 1
    assert sum("/provenance" in q for q in neptune.queries) == 1


@pytest.mark.asyncio
async def test_build_citations_skips_non_keyable_and_internal_rows():
    """Rows that don't expose (subject, predicate, object) yield NO citation (the
    honest-empty case), and rdf:type is not cited as a domain fact. A neptune that
    raises if queried proves non-keyable rows trigger no read at all."""
    class _Boom:
        async def query(self, sparql: str) -> dict:  # pragma: no cover - must not run
            raise AssertionError("no read should happen for non-keyable rows")

    # Non-keyable: only a projected display name, no (s, p, o).
    out = await build_citations(_Boom(), "g", ["name"], [{"name": "Central Park"}])
    assert out == []

    # rdf:type is keyable-shaped but skipped as bookkeeping.
    out = await build_citations(
        _Boom(), "g", ["s", "p", "o"],
        [{"s": ACME, "p": RDF_TYPE, "o": "https://cograph.tech/types/Company"}],
    )
    assert out == []


def test_build_coverage_caveat_composes_summary_and_stale_count():
    """The caveat joins a RunManifest coverage summary ("N of M …") with a
    validity-derived stale count."""
    from cograph_client.pipeline.manifest import HaltReasonKind, RunManifest

    manifest = RunManifest(run_id="r", stage="discovery").start(total=3)
    manifest.record_completed("a")
    manifest.record_completed("b")
    manifest.halt(HaltReasonKind.billing, "provider exhaustion — 402 Payment Required")
    coverage = manifest.coverage()

    caveat = build_coverage_caveat(coverage, stale_count=1, total_cited=2)
    assert re.search(r"\d+ of \d+", caveat), "must contain an 'N of M' fragment"
    assert "2 of 3" in caveat, "the manifest's coverage fraction is carried through"
    assert "stale" in caveat and "1" in caveat, "must state the stale-fact count"
    assert "provider exhaustion" in caveat, "the halt reason rides through"


def test_build_coverage_caveat_without_manifest_still_emits_stale():
    """No coverage manifest (the common /ask path) still yields the stale caveat."""
    caveat = build_coverage_caveat(None, stale_count=2, total_cited=5)
    assert "stale" in caveat and "2 of 5" in caveat


def test_build_coverage_caveat_empty_when_nothing_to_flag():
    """A clean, fully-fresh answer with no manifest yields no caveat at all."""
    assert build_coverage_caveat(None, stale_count=0, total_cited=3) == ""


# --------------------------------------------------------------------------- #
# 2. pyoxigraph acceptance half — real end-to-end over an in-process store.
# --------------------------------------------------------------------------- #
pyoxigraph = pytest.importorskip("pyoxigraph")
from pyoxigraph import QueryResultsFormat, Store  # noqa: E402

from cograph_client.graph.kg_writer import insert_facts  # noqa: E402
from cograph_client.graph.parser import parse_sparql_results  # noqa: E402
from cograph_client.graph.provenance import build_provenance_triples  # noqa: E402
from cograph_client.graph.queries import kg_graph_uri  # noqa: E402
from cograph_client.graph.validity import (  # noqa: E402
    STATUS_SUPERSEDED,
    build_closed_interval_triples,
    build_open_interval_triples,
)

TENANT, KG = "onta280", "corp"
INSTANCE_GRAPH = kg_graph_uri(TENANT, KG)


class PyoxiNeptune:
    """Minimal NeptuneClient shim over an in-process pyoxigraph Store (union of
    named graphs as the default graph), mirroring tests/test_supersession.py."""

    def __init__(self) -> None:
        self.store = Store()

    async def query(self, sparql: str) -> dict:
        results = self.store.query(sparql, use_default_graph_as_union=True)
        return json.loads(results.serialize(format=QueryResultsFormat.JSON))

    async def update(self, sparql: str) -> None:
        self.store.update(sparql)


@pytest.fixture(autouse=True)
def _quiet_housekeeping(monkeypatch):
    """Silence insert_facts's best-effort derived-index / embedding downstreams so
    the acceptance test isolates the citation mechanism (as test_supersession does)."""
    import cograph_client.nlp.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "get_embedding_service", lambda: None)


async def _seed_two_ceos(n: PyoxiNeptune) -> None:
    """acme hasCEO Bob (current/open) + hasCEO Alice (superseded/closed), each with
    provenance — exactly how a supersede would leave the graph."""
    instance = [
        (ACME, RDF_TYPE, "https://cograph.tech/types/Company"),
        (ACME, HAS_CEO, "Bob"),
        (ACME, HAS_CEO, "Alice"),
    ]
    validity = build_open_interval_triples(
        ACME, HAS_CEO, "Bob",
        valid_from="2026-07-01T00:00:00+00:00", graph_uri=INSTANCE_GRAPH,
    ) + build_closed_interval_triples(
        ACME, HAS_CEO, "Alice",
        valid_to="2026-07-01T00:00:00+00:00", valid_from="2025-01-01T00:00:00+00:00",
        superseded_by="stmt-bob", status=STATUS_SUPERSEDED, graph_uri=INSTANCE_GRAPH,
    )
    provenance = build_provenance_triples(
        ACME, HAS_CEO, "Bob", "crm_export.csv", confidence=0.95, graph_uri=INSTANCE_GRAPH,
    ) + build_provenance_triples(
        ACME, HAS_CEO, "Alice", "old_wiki", confidence=0.6, graph_uri=INSTANCE_GRAPH,
    )
    await insert_facts(
        n, INSTANCE_GRAPH, instance,
        provenance_triples=provenance, validity_triples=validity,
    )


async def _describe_ceo_bindings(n: PyoxiNeptune):
    """A describe-shape read that DOES expose (subject, predicate, object)."""
    raw = await n.query(
        f"SELECT ?s ?p ?o WHERE {{ GRAPH <{INSTANCE_GRAPH}> {{ "
        f"?s ?p ?o . FILTER(?s = <{ACME}> && ?p = <{HAS_CEO}>) }} }}"
    )
    return parse_sparql_results(raw)


@pytest.mark.asyncio
async def test_answer_cites_both_facts_with_freshness_and_confidence():
    """THE acceptance bar: an answer over a superseded functional attribute cites
    BOTH values — the open one current, the closed one 'superseded' — with the
    provenance confidences, and the coverage caveat flags the stale gap."""
    n = PyoxiNeptune()
    await _seed_two_ceos(n)

    variables, bindings = await _describe_ceo_bindings(n)
    citations = await build_citations(n, INSTANCE_GRAPH, variables, bindings)

    by_obj = {c.object: c for c in citations}
    assert set(by_obj) == {"Alice", "Bob"}, "both facts must be cited"

    # Bob — current/open, high confidence.
    assert by_obj["Bob"].is_current is True
    assert by_obj["Bob"].verdict == "current"
    assert by_obj["Bob"].confidence == 0.95
    assert by_obj["Bob"].source == "crm_export.csv"

    # Alice — superseded/closed, lower confidence, still cited with its recency.
    assert by_obj["Alice"].is_current is False
    assert by_obj["Alice"].verdict == "superseded"
    assert by_obj["Alice"].confidence == 0.6
    # valid_from rides through from the closed interval (the store canonicalizes the
    # dateTime's zone, e.g. +00:00 -> Z, so assert the date is carried, not byte-equal).
    assert by_obj["Alice"].valid_from.startswith("2025-01-01T00:00:00")

    # The coverage caveat flags the gap (1 of the 2 cited facts is stale).
    stale = sum(1 for c in citations if not c.is_current)
    assert stale == 1
    caveat = build_coverage_caveat(None, stale_count=stale, total_cited=len(citations))
    assert "stale" in caveat and "1 of 2" in caveat


@pytest.mark.asyncio
async def test_control_current_only_fact_is_current_and_no_stale_caveat():
    """LOAD-BEARING control: a single current fact reads is_current=True and yields
    NO stale caveat — so the acceptance test's 'superseded' verdict is produced by
    the closed interval, not fabricated."""
    n = PyoxiNeptune()
    await insert_facts(
        n, INSTANCE_GRAPH,
        [
            (ACME, RDF_TYPE, "https://cograph.tech/types/Company"),
            (ACME, HAS_CEO, "Bob"),
        ],
        validity_triples=build_open_interval_triples(
            ACME, HAS_CEO, "Bob",
            valid_from="2026-07-01T00:00:00+00:00", graph_uri=INSTANCE_GRAPH,
        ),
        provenance_triples=build_provenance_triples(
            ACME, HAS_CEO, "Bob", "crm_export.csv", confidence=0.95,
            graph_uri=INSTANCE_GRAPH,
        ),
    )

    variables, bindings = await _describe_ceo_bindings(n)
    citations = await build_citations(n, INSTANCE_GRAPH, variables, bindings)

    assert len(citations) == 1
    assert citations[0].object == "Bob"
    assert citations[0].is_current is True
    assert citations[0].verdict == "current"

    stale = sum(1 for c in citations if not c.is_current)
    assert stale == 0
    assert build_coverage_caveat(None, stale_count=stale, total_cited=len(citations)) == ""


@pytest.mark.asyncio
async def test_fact_with_no_validity_node_is_current_by_convention():
    """A fact with NO validity interval (append-only history) is current — and when
    provenance is absent, confidence degrades to None rather than failing."""
    n = PyoxiNeptune()
    await insert_facts(
        n, INSTANCE_GRAPH,
        [
            (ACME, RDF_TYPE, "https://cograph.tech/types/Company"),
            (ACME, HAS_CEO, "Bob"),
        ],
    )

    variables, bindings = await _describe_ceo_bindings(n)
    citations = await build_citations(n, INSTANCE_GRAPH, variables, bindings)

    assert len(citations) == 1
    assert citations[0].is_current is True
    assert citations[0].verdict == "current"
    assert citations[0].valid_from == ""
    assert citations[0].confidence is None  # no provenance graph seeded

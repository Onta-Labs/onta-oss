"""ONTA-373: the A3 clean+validate LEDGER (`CleanReport`) is assembled on the
discovery ingest path — closing the zero-silent-drops gap.

Before this the A3 clean/validate DECISION ran per value inside
``schema_resolver._resolve_and_insert_entity`` (``clean_value`` → ``validate_triple``)
but NO ledger was collected on discovery — a dropped/coerced value was silent.
Enrichment (``enrichment/executor.py``) and offline QC already assemble a
``CleanReport``; this test proves the SAME ledger (the SAME type, the SAME
passed/transformed/dropped partition semantics) is now surfaced on the
``IngestResult`` the discovery path returns.

Load-bearing regression controls (the acceptance bar, NOT decorative):

(a) The returned ``IngestResult.clean_report`` partitions the ingest's primitive
    values into passed / transformed / dropped, each carrying the reason.
(b) COUNT CONSERVATION: total inputs == passed + transformed + dropped — nothing
    silently disappears.
(c) BEHAVIOR-PRESERVING: the SET of written instance triples is exactly what the
    pre-373 write produced for the same input (the dropped value left NO triple,
    the transformed value stored its CANONICAL form not its raw form, the passed
    value verbatim), and ``triples_inserted`` / ``rejections`` are unchanged. The
    ledger is purely additive — it records the A3 decision the writer already
    made, it does not change the write.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import tempfile
import time

import pytest

pyoxigraph = pytest.importorskip("pyoxigraph")
from pyoxigraph import QueryResultsFormat, Store  # noqa: E402

from cograph_client.graph.ontology_queries import attr_uri, type_uri  # noqa: E402
from cograph_client.graph.queries import kg_graph_uri  # noqa: E402
from cograph_client.resolver.models import (  # noqa: E402
    CleanOutcome,
    CleanReport,
    ExtractedAttribute,
    ExtractedEntity,
    ExtractionResult,
)
from cograph_client.resolver.schema_resolver import SchemaResolver  # noqa: E402
from cograph_client.resolver.verdict_cache import JsonVerdictCache  # noqa: E402

TENANT = "onta373"
KG = "providers"
INSTANCE_GRAPH = kg_graph_uri(TENANT, KG)
SRC = "https://example.com/roster"

# One primary entity, three PRIMITIVE attributes hand-crafted so exactly one lands
# in each A3 partition (no relationships, no shared-prefix cluster → no promotion,
# so every value takes the main literal path). Values are short + non-placeholder
# so neither the free-text candidacy pass (keyless) nor the ONTA-259 anti-
# fabrication backstop touches them.
#   * "Cardiology" / string  -> PASSED     (conforms as-is)
#   * "4.6"        / integer -> TRANSFORMED (coerced to "4")
#   * "twelve"     / integer -> DROPPED     (cannot coerce)
EXTRACTION = ExtractionResult(
    entities=[
        ExtractedEntity(
            type_name="Physician",
            id="dr-alice",
            attributes=[
                ExtractedAttribute(name="specialty", value="Cardiology", datatype="string"),
                ExtractedAttribute(name="years_experience", value="4.6", datatype="integer"),
                ExtractedAttribute(name="npi", value="twelve", datatype="integer"),
            ],
        ),
    ],
    relationships=[],
)

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
XSD_INT = "http://www.w3.org/2001/XMLSchema#integer"


class PyoxiNeptune:
    """Minimal NeptuneClient shim over an in-process pyoxigraph Store — identical
    to the run-lineage / reentrancy tests."""

    def __init__(self) -> None:
        self.store = Store()

    async def query(self, sparql: str) -> dict:
        results = self.store.query(sparql, use_default_graph_as_union=True)
        return json.loads(results.serialize(format=QueryResultsFormat.JSON))

    async def update(self, sparql: str) -> None:
        self.store.update(sparql)

    async def batch_exists(self, sparql: str) -> set[str]:
        data = await self.query(sparql)
        rows = data.get("results", {}).get("bindings", [])
        return {r["entity"]["value"] for r in rows if "entity" in r}


@pytest.fixture(autouse=True)
def _er_disabled(monkeypatch):
    """Deterministic URIs (no signal-hash suffixes) via a scoped env set."""
    monkeypatch.setenv("COGRAPH_ER_ENABLED", "0")


def _make_resolver(neptune) -> SchemaResolver:
    cache_path = pathlib.Path(tempfile.gettempdir()) / f"onta373_{time.time_ns()}.json"
    return SchemaResolver(
        neptune=neptune,
        anthropic_key="unused-on-openrouter-path",
        verdict_cache=JsonVerdictCache(cache_path),
        embedding_service=None,
    )


def _stub_extract(resolver: SchemaResolver) -> None:
    async def fake_extract(content, content_type, existing_types=None, constraint=None):
        await asyncio.sleep(0)
        return EXTRACTION

    resolver._extract = fake_extract


async def _instance_triples(neptune) -> set[tuple[str, str, str]]:
    data = await neptune.query(
        f"SELECT ?s ?p ?o WHERE {{ GRAPH <{INSTANCE_GRAPH}> {{ ?s ?p ?o }} }}"
    )
    return {
        (b["s"]["value"], b["p"]["value"], b["o"]["value"])
        for b in data["results"]["bindings"]
    }


@pytest.mark.asyncio
async def test_discovery_ingest_assembles_clean_report():
    """(a) partition + reasons, (b) count conservation, (c) behavior-preserving —
    all on the live LLM-extract discovery path (`resolver.ingest`)."""
    neptune = PyoxiNeptune()
    resolver = _make_resolver(neptune)
    _stub_extract(resolver)

    result = await resolver.ingest(
        "PAYLOAD", TENANT, content_type="text", source=SRC,
        instance_graph=INSTANCE_GRAPH,
    )

    report = result.clean_report

    # --- (0) It is the SAME shared CleanReport type, not a parallel one. ---
    assert isinstance(report, CleanReport)

    # --- (a) Partition + reasons. --------------------------------------------
    assert [f.raw_value for f in report.passed] == ["Cardiology"]
    assert report.passed[0].outcome is CleanOutcome.PASSED
    assert report.passed[0].reason == "conforms as string"

    assert [f.raw_value for f in report.transformed] == ["4.6"]
    assert report.transformed[0].outcome is CleanOutcome.TRANSFORMED
    assert report.transformed[0].clean_value == "4"
    assert report.transformed[0].reason == "coerced to integer"

    assert [f.raw_value for f in report.dropped] == ["twelve"]
    assert report.dropped[0].outcome is CleanOutcome.DROPPED
    assert report.dropped[0].clean_value is None
    assert report.dropped[0].reason == "Cannot coerce 'twelve' to integer"
    # The dropped fact carries the attribute + entity it came from (a real ledger
    # entry, not an anonymous count).
    assert report.dropped[0].attribute == "npi"
    assert report.dropped[0].entity_id == "dr-alice"

    # --- (b) COUNT CONSERVATION: inputs == passed + transformed + dropped. ----
    n_inputs = len(EXTRACTION.entities[0].attributes)  # 3 primitive values fed in
    counts = report.counts()
    assert counts == {"passed": 1, "transformed": 1, "dropped": 1, "total": 3}
    assert report.total == counts["total"] == n_inputs
    assert report.total == len(report.passed) + len(report.transformed) + len(report.dropped)

    # --- (c) BEHAVIOR-PRESERVING: the write is byte-identical to pre-373. ------
    # The ledger is additive: it records the A3 decision the writer already made.
    triples = await _instance_triples(neptune)
    objects = {o for (_s, _p, o) in triples}
    # Objects keyed by their PRIMARY attribute predicate (the domain fact — not
    # the ONTA-347 surface-form companion, which is metadata OF the attribute).
    by_pred: dict[str, set[str]] = {}
    for _s, p, o in triples:
        by_pred.setdefault(p, set()).add(o)

    # PASSED value stored verbatim on its attribute predicate.
    assert by_pred.get(attr_uri("Physician", "specialty")) == {"Cardiology"}

    # TRANSFORMED value stored in its CANONICAL form ("4") on the PRIMARY
    # predicate — never the raw "4.6". (The raw "4.6" legitimately survives as
    # an ONTA-347 surface-form COMPANION on the attr_meta namespace; that
    # pre-existing behavior is untouched — this test only guards the primary.)
    assert by_pred.get(attr_uri("Physician", "years_experience")) == {"4"}

    # DROPPED value left NO trace on any attribute predicate — no primary value
    # AND no surface-form companion (companions exist only for transforms).
    assert "twelve" not in objects, "a dropped value must never be written"
    assert attr_uri("Physician", "npi") not in by_pred, "dropped attr has no instance edge"

    # The two written attributes are present as predicates; the dropped one is not.
    assert attr_uri("Physician", "specialty") in by_pred
    assert attr_uri("Physician", "years_experience") in by_pred

    # The entity itself was typed + labelled (the record's structural triples).
    assert (RDF_TYPE, type_uri("Physician")) in {(p, o) for (_s, p, o) in triples}

    # Ledger-vs-write consistency: the number of PRIMARY attribute triples that
    # actually landed in the graph equals passed + transformed (the two values
    # the writer kept) — and the DROPPED value contributed none. This ties the
    # additive ledger to the unchanged write: no value was written that the
    # ledger did not account as kept, and none was dropped that the graph kept.
    written_attr_preds = {
        attr_uri("Physician", "specialty"),
        attr_uri("Physician", "years_experience"),
        attr_uri("Physician", "npi"),
    }
    written_attr_triples = [t for t in triples if t[1] in written_attr_preds]
    assert len(written_attr_triples) == len(report.passed) + len(report.transformed) == 2

    # The result's rejection accounting is unchanged (the drop is the one
    # rejection the pre-373 path already reported — the ledger did not alter it).
    assert len(result.rejections) == 1
    assert result.rejections[0].value == "twelve"
    assert result.rejections[0].attribute == "npi"


@pytest.mark.asyncio
async def test_clean_report_empty_when_nothing_cleaned():
    """An ingest with no primitive attribute values assembles an EMPTY (but
    present, conserving) ledger — the field is always a real CleanReport, never
    None, so a consumer can rely on it unconditionally."""
    neptune = PyoxiNeptune()
    resolver = _make_resolver(neptune)

    async def fake_extract(content, content_type, existing_types=None, constraint=None):
        await asyncio.sleep(0)
        return ExtractionResult(
            entities=[ExtractedEntity(type_name="Hospital", id="general")],
            relationships=[],
        )

    resolver._extract = fake_extract

    result = await resolver.ingest(
        "PAYLOAD", TENANT, content_type="text", source=SRC,
        instance_graph=INSTANCE_GRAPH,
    )
    assert isinstance(result.clean_report, CleanReport)
    assert result.clean_report.counts() == {
        "passed": 0, "transformed": 0, "dropped": 0, "total": 0,
    }

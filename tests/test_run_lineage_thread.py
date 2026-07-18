"""ONTA-372: one run-scoped lineage threaded discovery → resolver → writer.

The discovery P1 entry (``web_ingest_cap``) mints ONE run_id and threads it into
BOTH resolver ingest paths — the LLM-extract ``ingest`` and the structured
fast-path ``ingest_structured_rows`` — so the A6 Graph Delta the resolver builds
is keyed to the SAME run_id as the A1 Source Bundle. Before this the resolver
minted its own unrelated uuid4 and the A1/A6 lineages diverged.

Load-bearing regression controls (the acceptance bar, NOT decorative):

1. Threading the run_id changes ONLY the run-scoped keying (the ``batch_id`` /
   ``ingested_at`` nonces + the A6 delta's fact_ids), NEVER the graph CONTENT.
   The SET of written DOMAIN facts (nonces projected out — exactly what the A6
   delta itself drops) is IDENTICAL whether or not a run_id is threaded, on BOTH
   the LLM-extract path AND the structured fast-path. A green suite that silently
   changed default output would FAIL this.
2. Both paths, given a run_id, key their A6 :class:`GraphDelta` receipt under it.

The A1-bundle-run_id == A6-delta-run_id assertion for one END-TO-END discovery
ingest (driven through the real ``web_ingest_cap`` wiring) lives in
``test_web_ingest_lineage.py``.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import tempfile
import time
from datetime import datetime, timezone

import pytest

from cograph_client.graph.kg_writer import DELTA_NONCE_PREDICATES

pyoxigraph = pytest.importorskip("pyoxigraph")
from pyoxigraph import QueryResultsFormat, Store  # noqa: E402

from cograph_client.graph.queries import kg_graph_uri  # noqa: E402
from cograph_client.resolver.models import (  # noqa: E402
    ExtractedAttribute,
    ExtractedEntity,
    ExtractedRelationship,
    ExtractionResult,
)
from cograph_client.resolver.schema_resolver import SchemaResolver  # noqa: E402
from cograph_client.resolver.verdict_cache import JsonVerdictCache  # noqa: E402

TENANT = "onta372"
KG = "providers"
INSTANCE_GRAPH = kg_graph_uri(TENANT, KG)
SRC = "https://example.com/roster"
RUN_ID = "run-onta372-fixed"
# Fixed ingested_at so the LLM-path comparison isolates run_id's effect (the
# structured path takes no observed_at param — its ingested_at nonce is
# wall-clock, but it is projected out of the domain-fact comparison anyway).
OBSERVED_AT = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)

# One primary entity with short (non-free-text) attributes + a node-valued
# relationship to a second entity — exercises entity triples, a relationship
# edge, and target-node materialization. Short values keep the free-text
# candidacy pass off the (keyless) LLM adjudicator.
EXTRACTION = ExtractionResult(
    entities=[
        ExtractedEntity(
            type_name="Physician",
            id="dr-alice",
            attributes=[
                ExtractedAttribute(name="city", value="SF", datatype="string"),
                ExtractedAttribute(name="specialty", value="Cardiology", datatype="string"),
            ],
        ),
        ExtractedEntity(type_name="Hospital", id="general"),
    ],
    relationships=[
        ExtractedRelationship(source_id="dr-alice", predicate="works_at", target_id="general"),
    ],
)

# Pre-structured rows for the deterministic fast-path (no LLM extraction).
STRUCTURED_ROWS = [
    {"name": "Dr Alice", "specialty": "Cardiology", "city": "SF",
     "source_url": "https://example.com/a"},
    {"name": "Dr Bob", "specialty": "Neurology", "city": "NYC",
     "source_url": "https://example.com/b"},
]


class PyoxiNeptune:
    """Minimal NeptuneClient shim over an in-process pyoxigraph Store — identical
    to the fact-id-replay / reentrancy tests."""

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
    """Deterministic URIs (no signal-hash suffixes) via a scoped env set that is
    restored at teardown — never a global mutation."""
    monkeypatch.setenv("COGRAPH_ER_ENABLED", "0")


def _make_resolver(neptune) -> SchemaResolver:
    cache_path = pathlib.Path(tempfile.gettempdir()) / f"onta372_{time.time_ns()}.json"
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


async def _all_quads(neptune) -> set[tuple[str, str, str, str]]:
    data = await neptune.query("SELECT ?g ?s ?p ?o WHERE { GRAPH ?g { ?s ?p ?o } }")
    return {
        (b["g"]["value"], b["s"]["value"], b["p"]["value"], b["o"]["value"])
        for b in data["results"]["bindings"]
    }


def _domain_quads(quads: set) -> set:
    """The graph CONTENT: drop the run-scoped bookkeeping nonces (batch_id /
    ingested_at) — exactly what ``build_graph_delta`` itself projects out — so a
    comparison sees only facts, never the run_id keying."""
    return {q for q in quads if q[2] not in DELTA_NONCE_PREDICATES}


# --------------------------------------------------------------------------- #
# 1. LLM-extract path: identical facts with vs. without a threaded run_id.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_llm_path_facts_identical_with_or_without_run_id():
    """Load-bearing regression control: threading a run_id through
    ``resolver.ingest`` changes ONLY the run-scoped keying, never the written
    graph content. The domain facts are byte-identical; only the nonces differ."""
    n_with, n_without = PyoxiNeptune(), PyoxiNeptune()

    r_with = _make_resolver(n_with)
    _stub_extract(r_with)
    res_with = await r_with.ingest(
        "PAYLOAD", TENANT, content_type="text", source=SRC,
        instance_graph=INSTANCE_GRAPH, run_id=RUN_ID, observed_at=OBSERVED_AT,
    )

    r_without = _make_resolver(n_without)
    _stub_extract(r_without)
    res_without = await r_without.ingest(
        "PAYLOAD", TENANT, content_type="text", source=SRC,
        instance_graph=INSTANCE_GRAPH, observed_at=OBSERVED_AT,  # run_id defaults
    )

    with_domain = _domain_quads(await _all_quads(n_with))
    without_domain = _domain_quads(await _all_quads(n_without))
    assert with_domain, "the run actually wrote domain facts (not vacuous)"
    assert with_domain == without_domain, (
        "threading a run_id must change ONLY the run_id keying, never the graph "
        f"content — diff: {with_domain ^ without_domain}"
    )

    # And the A6 delta IS keyed to the threaded run_id (lineage is live).
    assert res_with.graph_delta is not None
    assert res_with.graph_delta["run_id"] == RUN_ID
    # The default run keys off a fresh uuid4 — never the threaded id.
    assert res_without.graph_delta is not None
    assert res_without.graph_delta["run_id"] != RUN_ID


# --------------------------------------------------------------------------- #
# 2. Structured fast-path: identical facts + delta keyed to the run_id.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_structured_fastpath_facts_identical_with_or_without_run_id():
    """The SAME control on the deterministic structured fast-path
    (``ingest_structured_rows`` — no LLM extraction): the domain facts are
    identical with or without a threaded run_id."""
    n_with, n_without = PyoxiNeptune(), PyoxiNeptune()

    res_with = await _make_resolver(n_with).ingest_structured_rows(
        [dict(r) for r in STRUCTURED_ROWS], TENANT, "Physician",
        attributes=["name", "specialty", "city"],
        source="web:test:q", instance_graph=INSTANCE_GRAPH,
        key_attribute="name", run_id=RUN_ID,
    )
    res_without = await _make_resolver(n_without).ingest_structured_rows(
        [dict(r) for r in STRUCTURED_ROWS], TENANT, "Physician",
        attributes=["name", "specialty", "city"],
        source="web:test:q", instance_graph=INSTANCE_GRAPH,
        key_attribute="name",  # run_id defaults → fresh uuid4, per-entity insert
    )

    with_domain = _domain_quads(await _all_quads(n_with))
    without_domain = _domain_quads(await _all_quads(n_without))
    assert with_domain, "the structured run actually wrote domain facts"
    assert with_domain == without_domain, (
        "the structured fast-path must write the SAME facts regardless of run_id "
        f"threading — diff: {with_domain ^ without_domain}"
    )


@pytest.mark.asyncio
async def test_structured_fastpath_delta_keyed_to_run_id():
    """When a run_id is threaded, the structured fast-path emits an A6 Graph Delta
    keyed to it (previously it built no delta at all — dead lineage)."""
    n = PyoxiNeptune()
    res = await _make_resolver(n).ingest_structured_rows(
        [dict(r) for r in STRUCTURED_ROWS], TENANT, "Physician",
        attributes=["name", "specialty", "city"],
        source="web:test:q", instance_graph=INSTANCE_GRAPH,
        key_attribute="name", run_id=RUN_ID,
    )
    assert res.graph_delta is not None, "a run_id must yield an A6 delta"
    assert res.graph_delta["run_id"] == RUN_ID
    assert res.graph_delta["facts"], "the delta must carry the run's domain facts"


@pytest.mark.asyncio
async def test_structured_fastpath_no_run_id_builds_no_delta():
    """Control: WITHOUT a run_id the structured fast-path keeps its prior
    behavior — no A6 delta (the default CSV-shaped path is unchanged)."""
    n = PyoxiNeptune()
    res = await _make_resolver(n).ingest_structured_rows(
        [dict(r) for r in STRUCTURED_ROWS], TENANT, "Physician",
        attributes=["name", "specialty", "city"],
        source="web:test:q", instance_graph=INSTANCE_GRAPH,
        key_attribute="name",
    )
    assert res.graph_delta is None

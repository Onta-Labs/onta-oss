"""ONTA-271: stable fact_id threading → deterministic ingest under replay.

P6's "deterministic across repeated ingests" can only survive a non-deterministic
UPSTREAM replay if the pipeline threads a STABLE identity from extraction (A2)
through to the Graph Delta (A6): a retried run that PRESERVES its run_id must
reproduce a byte-identical graph, so P6 dedupes the replay instead of seeing
"novel" facts and duplicating the graph.

Two layers, mirroring ``test_qc_boundary.py`` (deterministic projection) and
``test_resolver_reentrancy.py`` (real resolver over a pyoxigraph store):

1. Pure ``build_graph_delta`` unit tests (no store): the A6 projection excludes
   the bookkeeping nonces, keys each fact by a stable fact_id, and is
   byte-identical iff the run_id + triples match.
2. Real end-to-end: the SAME stubbed extraction ingested TWICE through the real
   ``SchemaResolver`` over an in-process store. With a preserved run_id +
   observed_at the two runs yield a byte-identical Graph Delta AND reproduce the
   graph exactly (a same-store replay adds ZERO new triples). The load-bearing
   control: with the DEFAULT (fresh-per-call) run_id the delta diverges and a
   same-store replay DUPLICATES the run's bookkeeping — exactly the drift this
   ticket removes.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import tempfile
import time
from datetime import datetime, timezone

import pytest

from cograph_client.graph.kg_writer import (
    DELTA_NONCE_PREDICATES,
    build_graph_delta,
    insert_facts,
)

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"


# --------------------------------------------------------------------------- #
# 1. Pure build_graph_delta projection (no store, no pyoxigraph)
# --------------------------------------------------------------------------- #
def test_build_graph_delta_excludes_nonces_and_is_deterministic():
    g = "https://cograph.tech/graphs/t/kg/k"
    triples = [
        ("https://cograph.tech/entities/E/1", RDF_TYPE, "https://cograph.tech/types/E"),
        ("https://cograph.tech/entities/E/1", "https://cograph.tech/onto/name", "Alice"),
        # The two nonces the delta must project OUT.
        ("https://cograph.tech/entities/E/1", "https://cograph.tech/onto/ingested_at", "2026-07-13T00:00:00+00:00"),
        ("https://cograph.tech/entities/E/1", "https://cograph.tech/onto/batch_id", "batch-xyz"),
    ]
    d1 = build_graph_delta(g, triples, run_id="run-1")
    d2 = build_graph_delta(g, list(reversed(triples)), run_id="run-1")

    # Order-independent + byte-identical for the same run_id.
    assert d1.canonical_bytes() == d2.canonical_bytes()
    # Nonce predicates never appear in the delta facts.
    preds = {p for _fid, _s, p, _o in d1.facts}
    assert preds.isdisjoint(DELTA_NONCE_PREDICATES)
    assert preds == {RDF_TYPE, "https://cograph.tech/onto/name"}
    # Each fact carries a stable per-subject fact_id (uuid5 form).
    assert all(len(fid) == 36 and fid.count("-") == 4 for fid, *_ in d1.facts)


def test_build_graph_delta_run_id_scopes_the_fact_ids():
    g = "https://cograph.tech/graphs/t/kg/k"
    triples = [("https://cograph.tech/entities/E/1", "https://cograph.tech/onto/name", "Alice")]
    a = build_graph_delta(g, triples, run_id="run-A")
    b = build_graph_delta(g, triples, run_id="run-B")
    # Same facts, different run → different fact_ids → different receipt. This is
    # what makes an un-preserved run_id look "novel" to P6.
    assert a.canonical_bytes() != b.canonical_bytes()
    assert {f[1:] for f in a.facts} == {f[1:] for f in b.facts}  # (s,p,o) identical


def test_build_graph_delta_records_fan_in():
    g = "https://cograph.tech/graphs/t/kg/k"
    triples = [("https://cograph.tech/entities/E/canon", "https://cograph.tech/onto/name", "Alice")]
    d = build_graph_delta(
        g, triples, run_id="run-1",
        fan_in={"https://cograph.tech/entities/E/dup": "https://cograph.tech/entities/E/canon"},
    )
    assert len(d.fan_in) == 1
    src_fid, canon_fid = d.fan_in[0]
    assert src_fid != canon_fid  # distinct source-fact vs canonical-node ids
    # An identity mapping (src == dst) is not a merge and is dropped.
    d2 = build_graph_delta(g, triples, run_id="run-1", fan_in={"x": "x"})
    assert d2.fan_in == ()


# --------------------------------------------------------------------------- #
# 2. Real resolver end-to-end (needs pyoxigraph)
# --------------------------------------------------------------------------- #
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

TENANT = "onta271"
KG = "providers"
INSTANCE_GRAPH = kg_graph_uri(TENANT, KG)
SRC = "https://example.com/roster"
MARKER = "ROSTER_PAYLOAD"
# Fixed run identity + ingested_at stamp so a preserved-run replay is stable.
RUN_ID = "run-onta271-fixed"
OBSERVED_AT = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)

# One primary entity with short (non-free-text) attributes + a node-valued
# relationship to a second entity — exercises entity triples, a relationship
# edge, and target-node materialization in the A6 delta. Short values keep the
# free-text candidacy pass off the (keyless) LLM adjudicator, like the
# reentrancy fixture's "SF".
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


class PyoxiNeptune:
    """Minimal NeptuneClient shim over an in-process pyoxigraph Store (identical
    to the reentrancy/isolation tests): async query/update/batch_exists returning
    SPARQL-1.1 JSON, union-of-named-graphs default."""

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
    """Disable ER (deterministic URIs, no signal-hash suffixes) for every test in
    this module. Via monkeypatch so the env var is RESTORED at teardown — a bare
    ``os.environ[...] = "0"`` would leak globally and (since this file sorts before
    ``test_multityping_retail.py``) disable ER for the ER-dependent tests that run
    after it."""
    monkeypatch.setenv("COGRAPH_ER_ENABLED", "0")


def _make_resolver(neptune) -> SchemaResolver:
    """A real SchemaResolver, ER disabled (deterministic URIs), no embeddings —
    the same wiring the reentrancy tests use. ER-disable comes from the autouse
    ``_er_disabled`` fixture (read at __init__), not a global env mutation."""
    cache_path = pathlib.Path(tempfile.gettempdir()) / f"fid_verdicts_{time.time_ns()}.json"
    return SchemaResolver(
        neptune=neptune,
        anthropic_key="unused-on-openrouter-path",
        verdict_cache=JsonVerdictCache(cache_path),
        embedding_service=None,
    )


def _stub(resolver: SchemaResolver) -> None:
    async def fake_extract(content, content_type, existing_types=None, constraint=None):
        await asyncio.sleep(0)
        return EXTRACTION

    resolver._extract = fake_extract


async def _ingest(neptune, *, run_id=None, observed_at=None):
    resolver = _make_resolver(neptune)
    _stub(resolver)
    return await resolver.ingest(
        MARKER, TENANT, content_type="text", source=SRC,
        instance_graph=INSTANCE_GRAPH, run_id=run_id, observed_at=observed_at,
    )


async def _all_quads(neptune) -> set[tuple[str, str, str, str]]:
    """The whole store as a (graph, s, p, o) set — the graph's byte-level state."""
    data = await neptune.query(
        "SELECT ?g ?s ?p ?o WHERE { GRAPH ?g { ?s ?p ?o } }"
    )
    out = set()
    for b in data["results"]["bindings"]:
        out.add((b["g"]["value"], b["s"]["value"], b["p"]["value"], b["o"]["value"]))
    return out


def _delta_bytes(result) -> bytes:
    return json.dumps(result.graph_delta, sort_keys=True, ensure_ascii=False).encode("utf-8")


# --------------------------------------------------------------------------- #
# 2a. Preserved run_id → byte-identical delta AND byte-identical graph.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_replay_preserves_byte_identical_delta_and_graph():
    """Two fresh stores, SAME run_id + observed_at: the A6 Graph Delta is
    byte-identical and the whole graph is reproduced quad-for-quad. This is the
    ticket's 'replaying a run from extraction yields a byte-identical Graph
    Delta' — determinism of the PIPELINE, not just the writer."""
    n1, n2 = PyoxiNeptune(), PyoxiNeptune()
    r1 = await _ingest(n1, run_id=RUN_ID, observed_at=OBSERVED_AT)
    r2 = await _ingest(n2, run_id=RUN_ID, observed_at=OBSERVED_AT)

    assert r1.graph_delta is not None and r1.graph_delta["facts"], "an A6 delta must be emitted"
    # Byte-identical Graph Delta on replay.
    assert _delta_bytes(r1) == _delta_bytes(r2)
    # And the full graph is reproduced exactly (including the now-stabilized
    # ingested_at / batch_id nonces).
    assert await _all_quads(n1) == await _all_quads(n2)


@pytest.mark.asyncio
async def test_replay_into_same_store_adds_zero_new_triples():
    """The P6 dedupe: replaying into the store that already holds the run's data
    (SAME run_id + observed_at) adds ZERO new triples — every fact, plus the
    stabilized ingested_at/batch_id, re-inserts idempotently."""
    n = PyoxiNeptune()
    await _ingest(n, run_id=RUN_ID, observed_at=OBSERVED_AT)
    after_first = await _all_quads(n)
    assert after_first, "not vacuous — the first run actually wrote"

    await _ingest(n, run_id=RUN_ID, observed_at=OBSERVED_AT)
    after_replay = await _all_quads(n)

    assert after_replay == after_first, (
        "a preserved-run_id replay must add no new quads (P6 dedupe), but the "
        f"store grew by {len(after_replay - after_first)} quad(s)"
    )


# --------------------------------------------------------------------------- #
# 2b. Control: WITHOUT a preserved run_id the replay looks novel (load-bearing).
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_control_default_run_id_is_not_replay_stable():
    """Load-bearing control: with the DEFAULT run_id (a fresh uuid4 per call —
    no stable id) the two runs' Graph Deltas DIVERGE (fact_ids embed the run_id)
    and a same-store replay DUPLICATES the run's bookkeeping (a fresh batch_id /
    ingested_at per run). This is exactly the drift the stable-id threading
    removes — proving the mechanism is load-bearing, not decorative."""
    # Fresh stores, default run_id each → the deltas must differ.
    n1, n2 = PyoxiNeptune(), PyoxiNeptune()
    r1 = await _ingest(n1)  # run_id defaults to a fresh uuid4
    r2 = await _ingest(n2)
    assert r1.graph_delta and r2.graph_delta
    assert _delta_bytes(r1) != _delta_bytes(r2), (
        "without a preserved run_id the A6 delta must look novel on replay"
    )

    # Same store, default run_id twice → the graph GROWS (new batch/ingested
    # nonces), i.e. the replay is NOT deduped — the failure this ticket fixes.
    n = PyoxiNeptune()
    await _ingest(n)
    after_first = await _all_quads(n)
    await _ingest(n)
    after_replay = await _all_quads(n)
    assert after_replay > after_first, (
        "with a fresh run_id the replay must duplicate bookkeeping (control) — "
        "if it didn't, the same-run_id zero-growth result proves nothing"
    )

"""ONTA-374 (Wave 7, P7) regression control: thread the A9 RunManifest/RunCoverage
into the /ask answer path so the coverage caveat carries REAL "N of M" numbers.

Two load-bearing halves, mirroring the acceptance bar:

1. **Threaded manifest → real A9 caveat.** When a ``RunManifest`` (or a bare
   ``RunCoverage``) is threaded into ``NLQueryPipeline.ask``, the returned
   ``coverage_caveat`` composes the A9 "answered from N of M items" fragment
   (not just a stale count).

2. **Default (no manifest) path is byte-identical.** An ``ask`` with NO manifest
   produces the exact answer + citations + EMPTY caveat it did before ONTA-374.
   Beyond that, threading a manifest must change ONLY the caveat — the answer,
   sparql, explanation and narrative are identical to the no-manifest run.

The SPARQL generator + narrative rephrase are mocked so the whole path runs
OFFLINE (no LLM key, no network) — the same isolation the CI-safe unit tests use,
independent of the live-LLM ask tests in test_pipeline.py.

**LOST CAPABILITY (ONTA-527).** ``ask`` still ACCEPTS ``run_manifest``, but the
threading stops one line later: the Cypher branch is entered as
``self._ask_cypher(question, graph_uri=…, data_graph=…, exclude_questions=…,
layer_graph_uris=…)`` — ``run_manifest`` is not among the arguments and
``_ask_cypher`` has no parameter for it. So on the shipped ``/ask`` path a
threaded manifest is silently DROPPED and the A9 "answered from N of M items"
fragment is never composed. The three cases that need a THREADED manifest are
xfailed strictly rather than softened, so they flip to XPASS the day it is
forwarded. The no-manifest control stays green: it is still a real control,
because the Cypher path also defaults to an empty caveat and no citations. The
whole file was re-pointed at a seeded ``MemoryGraphStore`` and a per-KG instance
graph so the xfails fire on the CAVEAT rather than on scope resolution.

A silently-ignored keyword argument is the dangerous shape here: the caller sees
no error, only a missing caveat.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from infona_client.nlp.pipeline import NLQueryPipeline
from infona_client.pipeline.manifest import RunCoverage, RunManifest

_MANIFEST_NOT_THREADED = (
    "LOST CAPABILITY (ONTA-527): nlp/pipeline.py::ask drops `run_manifest` when "
    "it dispatches to _ask_cypher (the parameter is not forwarded and "
    "_ask_cypher does not accept it), so the A9 RunCoverage → "
    "answer_meta.build_coverage_caveat composition in the SPARQL branch never "
    "runs on the shipped /ask path and coverage_caveat is always ''."
)

_CANNED_CYPHER = {
    "cypher": (
        "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
        "WHERE e.primary_type IN $type_names RETURN count(*) AS n"
    ),
    "params": {"type_names": ["Place"]},
    "template": "entities_of_type_count",
    "explanation": "Counts places",
    "functions_needed": [],
}

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
TENANT_GRAPH = "https://graph.infona.ai/graphs/t1"
# A per-KG instance graph, not a bare tenant URI: since ONTA-527 `ask` derives
# (tenant, kg) from it and refuses a tenant graph outright, which would make
# every case below fail on scope resolution instead of on the caveat.
KG_GRAPH = f"{TENANT_GRAPH}/kg/places"


@pytest.fixture
def mock_neptune():
    client = AsyncMock()
    client.query.return_value = {"head": {"vars": []}, "results": {"bindings": []}}
    return client


@pytest.fixture
def store():
    import asyncio

    from infona_client.graph.iri import IRI_BASE
    from infona_client.graph.kg_writer import insert_facts
    from infona_client.graph.memory_store import MemoryGraphStore
    from infona_client.graph.ontology_queries import entity_uri

    st = MemoryGraphStore()
    park = entity_uri("Place", "p1")
    asyncio.run(
        insert_facts(
            None,
            KG_GRAPH,
            [(park, RDF_TYPE, f"{IRI_BASE}/types/Place"), (park, LABEL, "Central Park")],
            store=st,
        )
    )
    return st


@pytest.fixture
def pipeline(mock_neptune, store):
    return NLQueryPipeline(mock_neptune, "fake-key", graph_store=store)


async def _ask(pipeline, run_manifest=None):
    """Drive ``ask`` fully offline: canned generation + empty rephrase."""
    with patch.object(
        pipeline, "_try_llm_cypher", new_callable=AsyncMock, return_value=_CANNED_CYPHER
    ), patch.object(
        pipeline, "_fetch_ontology", new_callable=AsyncMock, return_value="Type: Place"
    ), patch.object(
        pipeline, "_rephrase_via_openrouter", new_callable=AsyncMock, return_value=""
    ):
        return await pipeline.ask(
            "enumerate the zzqx places",
            TENANT_GRAPH,
            KG_GRAPH,
            run_manifest=run_manifest,
        )


def _make_manifest() -> RunManifest:
    """A halted run: 2 of 3 items completed before a provider-exhaustion halt."""
    from infona_client.pipeline.manifest import HaltReasonKind

    m = RunManifest(run_id="r-374", stage="discovery").start(total=3)
    m.record_completed("a")
    m.record_completed("b")
    m.halt(HaltReasonKind.billing, "provider exhaustion — 402 Payment Required")
    return m


# --------------------------------------------------------------------------- #
# 1. Threaded manifest / coverage → REAL A9 "N of M" caveat.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_ask_with_manifest_emits_real_a9_coverage_caveat(pipeline):
    result = await _ask(pipeline, run_manifest=_make_manifest())

    assert result.coverage_caveat, "a threaded manifest must yield a coverage caveat"
    assert "answered from" in result.coverage_caveat
    # The REAL A9 fraction (2 of 3), not a fabricated or stale-only caveat.
    assert "2 of 3" in result.coverage_caveat
    assert "provider exhaustion" in result.coverage_caveat
    assert result.answer == "1"


@pytest.mark.asyncio
async def test_ask_accepts_bare_run_coverage(pipeline):
    """A pre-computed RunCoverage (not the full manifest) threads through too."""
    coverage = _make_manifest().coverage()
    assert isinstance(coverage, RunCoverage)

    result = await _ask(pipeline, run_manifest=coverage)
    assert "2 of 3" in result.coverage_caveat
    assert result.coverage_caveat == "answered from " + coverage.summary


# --------------------------------------------------------------------------- #
# 2. Default (no manifest) path is byte-identical / behavior-preserving.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_ask_without_manifest_has_empty_caveat_default(pipeline):
    """The default flagless path: no manifest ⇒ empty caveat, no citations.

    Still a real control after ONTA-527 (the Cypher path also defaults to an
    empty caveat and no citations), so it stays green — it is the manifest-
    THREADED half that is gone. The answer is now the count the seeded store
    returns rather than the canned SPARQL row.
    """
    result = await _ask(pipeline, run_manifest=None)

    assert result.coverage_caveat == ""
    assert result.citations == []
    assert result.answer == "1"


@pytest.mark.asyncio
async def test_threading_manifest_changes_ONLY_the_caveat(pipeline):
    """Byte-identical default-path control: threading a manifest changes ONLY the
    coverage_caveat — answer / sparql / explanation / narrative are unchanged, so
    the no-manifest answer is preserved exactly and the manifest is purely additive.
    """
    default = await _ask(pipeline, run_manifest=None)
    with_manifest = await _ask(pipeline, run_manifest=_make_manifest())

    # The only field that differs is the coverage caveat.
    assert default.coverage_caveat == ""
    assert with_manifest.coverage_caveat != ""

    assert with_manifest.answer == default.answer
    assert with_manifest.sparql == default.sparql
    assert with_manifest.explanation == default.explanation
    assert with_manifest.narrative_answer == default.narrative_answer
    assert with_manifest.citations == default.citations == []

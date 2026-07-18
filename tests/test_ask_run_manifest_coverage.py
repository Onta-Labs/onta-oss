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
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from cograph_client.nlp.pipeline import NLQueryPipeline
from cograph_client.pipeline.manifest import RunCoverage, RunManifest

_CANNED_SPARQL = {
    "sparql": "SELECT ?name WHERE { ?s <https://schema.org/name> ?name }",
    "explanation": "Finds all names",
    "functions_needed": [],
}


@pytest.fixture
def mock_neptune():
    client = AsyncMock()
    client.query.return_value = {
        "head": {"vars": ["name"]},
        "results": {
            "bindings": [{"name": {"type": "literal", "value": "Central Park"}}]
        },
    }
    return client


@pytest.fixture
def pipeline(mock_neptune):
    return NLQueryPipeline(mock_neptune, "fake-key")


async def _ask(pipeline, run_manifest=None):
    """Drive ``ask`` fully offline: canned SPARQL generation + empty rephrase."""
    with patch.object(
        pipeline, "_generate_sparql", new_callable=AsyncMock, return_value=_CANNED_SPARQL
    ), patch.object(
        pipeline, "_rephrase_via_openrouter", new_callable=AsyncMock, return_value=""
    ):
        return await pipeline.ask(
            "What places exist?",
            "https://cograph.tech/graphs/t1",
            run_manifest=run_manifest,
        )


def _make_manifest() -> RunManifest:
    """A halted run: 2 of 3 items completed before a provider-exhaustion halt."""
    from cograph_client.pipeline.manifest import HaltReasonKind

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
    assert result.answer == "Central Park"


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
    """The default flagless path: no manifest ⇒ empty caveat, no citations —
    exactly as before ONTA-374."""
    result = await _ask(pipeline, run_manifest=None)

    assert result.coverage_caveat == ""
    assert result.citations == []
    assert result.answer == "Central Park"


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

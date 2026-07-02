"""Tests for graph/text_markers.py + the textKind ontology builders (ONTA-177).

Covers the three deliverables around the marker itself:
- the name-blind candidacy classifier (profiler ValueShape.TEXT proposes;
  ADR 0003 litmus — values only, never names);
- the single-valued, idempotent ``textKind`` upsert (same DELETE/INSERT/WHERE
  string contract test_ontology_upsert.py pins for the other upserts);
- the per-tenant {predicate URI -> is_free_text} cache: hit / TTL miss /
  invalidate / best-effort fetch failure, plus the refresh_after_write
  invalidation hook in graph/kg_writer.py.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

import cograph_client.graph.text_markers as tm
from cograph_client.graph.ontology_queries import (
    attr_uri,
    text_kind_map_query,
    upsert_attribute_text_kind,
)
from cograph_client.graph.text_markers import (
    TextCandidacy,
    classify_text_candidacy,
    get_free_text_map,
    invalidate,
    invalidate_all,
    reset_for_tests,
)

GRAPH = "https://cograph.tech/graphs/test"

_PROSE = (
    "The committee heard extensive testimony about the proposed changes to the "
    "watershed management plan and debated the funding formula for well over "
    "two hours before adjourning without a final vote on the matter."
)
_ADDRESS = "1420 Willow Creek Road, Springfield"


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_for_tests()
    yield
    reset_for_tests()


# --- classifier (name-blind: values in, verdict out) -------------------------


class TestClassifyTextCandidacy:
    def test_long_prose_is_unambiguous_free_text(self):
        values = [f"{_PROSE} Session {i}." for i in range(5)]
        assert classify_text_candidacy(values) is TextCandidacy.FREE_TEXT

    def test_medium_text_is_ambiguous(self):
        # Text-shaped (spaces, avg > 25) but below the auto threshold — could
        # be an address, an org name, or short prose: the LLM layer decides.
        values = [f"{_ADDRESS} suite {i}" for i in range(5)]
        assert classify_text_candidacy(values) is TextCandidacy.AMBIGUOUS

    def test_codes_are_not_candidates(self):
        assert classify_text_candidacy(["SKU-1", "SKU-2", "SKU-3"]) is (
            TextCandidacy.NOT_CANDIDATE
        )

    def test_numbers_and_dates_are_not_candidates(self):
        assert classify_text_candidacy(["1.5", "2.25", "300"]) is TextCandidacy.NOT_CANDIDATE
        assert classify_text_candidacy(["2026-01-01", "2026-02-02"]) is (
            TextCandidacy.NOT_CANDIDATE
        )

    def test_short_labels_are_not_candidates(self):
        assert classify_text_candidacy(["John Smith", "Sara Khan", "Ana Lima"]) is (
            TextCandidacy.NOT_CANDIDATE
        )

    def test_empty_and_none_values_are_not_candidates(self):
        assert classify_text_candidacy([]) is TextCandidacy.NOT_CANDIDATE
        assert classify_text_candidacy([None, "", "   "]) is TextCandidacy.NOT_CANDIDATE

    def test_non_string_cells_are_normalized_not_crashed(self):
        # JSON rows may deliver numbers/None; mirror the profiler's tolerance.
        assert classify_text_candidacy([12, 34.5, None]) is TextCandidacy.NOT_CANDIDATE


# --- textKind upsert: single-valued + idempotent (string contract) -----------


class TestUpsertAttributeTextKind:
    def test_marker_is_delete_insert_where(self):
        sparql = upsert_attribute_text_kind(GRAPH, "Speech", "transcript")
        a_uri = attr_uri("Speech", "transcript")
        assert (
            f"DELETE {{ GRAPH <{GRAPH}> {{ <{a_uri}> "
            f"<https://cograph.tech/onto/textKind> ?k }} }}" in sparql
        )
        assert (
            f"INSERT {{ GRAPH <{GRAPH}> {{ <{a_uri}> "
            f'<https://cograph.tech/onto/textKind> "free_text" }} }}' in sparql
        )
        assert "OPTIONAL" in sparql and "WHERE" in sparql
        # Atomicity: DELETE precedes INSERT precedes WHERE in one operation.
        assert sparql.index("DELETE") < sparql.index("INSERT") < sparql.index("WHERE")

    def test_reingest_is_idempotent_single_valued(self):
        # Two identical upserts emit the identical DELETE-then-INSERT op — the
        # second replaces rather than stacks (the single-valued contract the
        # other upserts in ontology_queries.py pin the same way).
        first = upsert_attribute_text_kind(GRAPH, "Speech", "transcript")
        second = upsert_attribute_text_kind(GRAPH, "Speech", "transcript")
        assert first == second
        assert first.count("textKind") == 3  # DELETE + INSERT + WHERE, no extras

    def test_empty_kind_clears_without_insert(self):
        sparql = upsert_attribute_text_kind(GRAPH, "Speech", "transcript", text_kind="")
        assert "DELETE" in sparql
        assert 'textKind> "' not in sparql  # no literal inserted

    def test_kind_literal_is_escaped(self):
        sparql = upsert_attribute_text_kind(GRAPH, "T", "a", text_kind='free"text')
        assert '"free\\"text"' in sparql

    def test_map_query_selects_attr_and_kind(self):
        sparql = text_kind_map_query(GRAPH)
        assert f"FROM <{GRAPH}>" in sparql
        assert "?attr <https://cograph.tech/onto/textKind> ?kind" in sparql


# --- per-tenant marker-map cache ---------------------------------------------


def _neptune_with(rows: list[tuple[str, str]]) -> AsyncMock:
    neptune = AsyncMock()
    neptune.query.return_value = {
        "head": {"vars": ["attr", "kind"]},
        "results": {
            "bindings": [
                {
                    "attr": {"type": "uri", "value": a},
                    "kind": {"type": "literal", "value": k},
                }
                for a, k in rows
            ]
        },
    }
    return neptune


_TRANSCRIPT = attr_uri("Speech", "transcript")
_SUBJECT = attr_uri("Ticket", "subject")


class TestFreeTextMapCache:
    def test_fetch_parses_markers_and_caches(self):
        async def run():
            neptune = _neptune_with([(_TRANSCRIPT, "free_text"), (_SUBJECT, "label")])
            first = await get_free_text_map(neptune, "tenant-a")
            second = await get_free_text_map(neptune, "tenant-a")
            assert first == {_TRANSCRIPT: True, _SUBJECT: False}
            assert second == first
            # Cache HIT: one Neptune query serves both reads.
            assert neptune.query.await_count == 1

        asyncio.run(run())

    def test_cache_is_per_tenant(self):
        async def run():
            neptune = _neptune_with([(_TRANSCRIPT, "free_text")])
            await get_free_text_map(neptune, "tenant-a")
            await get_free_text_map(neptune, "tenant-b")
            assert neptune.query.await_count == 2

        asyncio.run(run())

    def test_ttl_expiry_refetches(self, monkeypatch):
        async def run():
            monkeypatch.setenv("COGRAPH_TEXT_MARKER_TTL_S", "0")
            neptune = _neptune_with([(_TRANSCRIPT, "free_text")])
            await get_free_text_map(neptune, "tenant-a")
            await get_free_text_map(neptune, "tenant-a")
            # TTL 0 → every read is a MISS (the multi-task safety valve).
            assert neptune.query.await_count == 2

        asyncio.run(run())

    def test_invalidate_forces_refetch(self):
        async def run():
            neptune = _neptune_with([(_TRANSCRIPT, "free_text")])
            await get_free_text_map(neptune, "tenant-a")
            invalidate("tenant-a")
            await get_free_text_map(neptune, "tenant-a")
            assert neptune.query.await_count == 2

        asyncio.run(run())

    def test_invalidate_other_tenant_keeps_cache(self):
        async def run():
            neptune = _neptune_with([(_TRANSCRIPT, "free_text")])
            await get_free_text_map(neptune, "tenant-a")
            invalidate("tenant-b")  # unrelated tenant
            await get_free_text_map(neptune, "tenant-a")
            assert neptune.query.await_count == 1

        asyncio.run(run())

    def test_invalidate_all_clears_everything(self):
        async def run():
            neptune = _neptune_with([])
            await get_free_text_map(neptune, "tenant-a")
            await get_free_text_map(neptune, "tenant-b")
            invalidate_all()
            await get_free_text_map(neptune, "tenant-a")
            await get_free_text_map(neptune, "tenant-b")
            assert neptune.query.await_count == 4

        asyncio.run(run())

    def test_fetch_failure_returns_empty_and_is_not_cached(self):
        async def run():
            neptune = AsyncMock()
            neptune.query.side_effect = RuntimeError("neptune down")
            out = await get_free_text_map(neptune, "tenant-a")
            assert out == {}
            # Failure NOT pinned for a TTL: the next read retries.
            neptune.query.side_effect = None
            neptune.query.return_value = {
                "head": {"vars": ["attr", "kind"]},
                "results": {"bindings": []},
            }
            assert await get_free_text_map(neptune, "tenant-a") == {}
            assert neptune.query.await_count == 2

        asyncio.run(run())


# --- refresh_after_write invalidation hook (graph/kg_writer.py) --------------


def test_refresh_after_write_invalidates_text_marker_cache(monkeypatch):
    """The ONE kg_writer block this issue adds: a converged write drops the
    tenant's marker map so fresh textKind verdicts are visible before the TTL
    (mirrors the NL-planning cache-invalidate block style)."""
    import cograph_client.nlp.pipeline as pipeline_mod
    from cograph_client.graph.kg_writer import refresh_after_write

    async def run():
        monkeypatch.setattr(
            pipeline_mod.NLQueryPipeline, "invalidate_cache", lambda graph: None,
        )
        monkeypatch.setattr(pipeline_mod, "get_embedding_service", lambda: None)

        neptune = _neptune_with([(_TRANSCRIPT, "free_text")])
        await get_free_text_map(neptune, "t")
        assert "t" in tm._cache

        # kg_name=None keeps the write tenant-graph-only (no stats recompute
        # import needed) — the marker invalidation must still fire.
        await refresh_after_write(
            AsyncMock(), tenant_id="t", kg_name=None, affected_types=set(),
        )
        assert "t" not in tm._cache
        # And the next read refetches.
        await get_free_text_map(neptune, "t")
        assert neptune.query.await_count == 2

    asyncio.run(run())

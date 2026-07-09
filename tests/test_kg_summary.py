"""Unit tests for the AI one-line KG summary (graph/kg_summary.py) and its
persistence on the KgStats row.

All deterministic: the LLM seam is monkeypatched, so no network / no key needed.
Covers the regenerate-decision gate, reply cleanup, best-effort failure modes,
the ai_description round-trip through the Postgres store's JSON serialization,
and the list-time backfill helper.
"""

from __future__ import annotations

import asyncio

import pytest

from cograph_client.graph import kg_summary
from cograph_client.graph.kg_stats_store import (
    InMemoryKgStatsStore,
    KgStats,
    PostgresKgStatsStore,
)


# ── should_generate_summary: the regeneration gate ──────────────────────────


def test_regenerate_when_no_existing_summary():
    assert kg_summary.should_generate_summary("", {}, {"Physician": 10}) is True


def test_no_regenerate_when_breakdown_empty():
    # Nothing to describe → never call the LLM, even with no existing summary.
    assert kg_summary.should_generate_summary("", {"Physician": 1}, {}) is False


def test_no_regenerate_when_type_set_unchanged():
    # Enrichment fills attributes on existing types: counts move, type SET is the
    # same → keep the existing line, no LLM spend.
    old = {"Physician": 10, "Clinic": 3}
    new = {"Physician": 42, "Clinic": 9}
    assert kg_summary.should_generate_summary("Orange County physicians", old, new) is False


def test_regenerate_when_type_set_changes():
    old = {"Physician": 10}
    new = {"Physician": 10, "Hospital": 4}
    assert kg_summary.should_generate_summary("some line", old, new) is True


# ── _clean: normalize a raw model reply ─────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Orange County physicians and their clinics.", "Orange County physicians and their clinics"),
        ('"Voice AI model registry"', "Voice AI model registry"),
        ("Physicians\nand extra reasoning text", "Physicians"),
        ("   spaced out   ", "spaced out"),
        ("", ""),
    ],
)
def test_clean(raw, expected):
    assert kg_summary._clean(raw) == expected


def test_clean_caps_length():
    long = "x" * 500
    assert len(kg_summary._clean(long)) <= kg_summary._MAX_CHARS


# ── generate_kg_summary: best-effort contract ───────────────────────────────


def test_generate_empty_breakdown_returns_blank():
    assert asyncio.run(kg_summary.generate_kg_summary("kg", {})) == ""


def test_generate_no_key_returns_blank(monkeypatch):
    monkeypatch.setattr(kg_summary, "_openrouter_key", lambda: "")
    out = asyncio.run(kg_summary.generate_kg_summary("kg", {"Physician": 5}))
    assert out == ""


def test_generate_happy_path(monkeypatch):
    async def fake_chat(api_key, system, user, **kwargs):
        assert "Physician" in user  # the breakdown reached the prompt
        return "Orange County physicians.\n"

    monkeypatch.setattr(
        "cograph_client.resolver.llm_router.openrouter_chat", fake_chat
    )
    out = asyncio.run(
        kg_summary.generate_kg_summary("oc-physicians", {"Physician": 5}, api_key="k")
    )
    assert out == "Orange County physicians"


def test_generate_swallows_llm_error(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("openrouter down")

    monkeypatch.setattr("cograph_client.resolver.llm_router.openrouter_chat", boom)
    out = asyncio.run(
        kg_summary.generate_kg_summary("kg", {"Physician": 5}, api_key="k")
    )
    assert out == ""


# ── persistence: ai_description survives the store round-trip ────────────────


def test_ai_description_survives_postgres_json_roundtrip():
    stats = KgStats(
        tenant_id="t",
        kg_name="kg",
        entity_count=5,
        type_breakdown={"Physician": 5},
        ai_description="Orange County physicians",
    )
    # PostgresKgStatsStore serializes to model_dump_json and parses back.
    back = PostgresKgStatsStore._row_to_stats({"payload": stats.model_dump_json()})
    assert back.ai_description == "Orange County physicians"


def test_inmemory_store_preserves_ai_description():
    store = InMemoryKgStatsStore()
    stats = KgStats(tenant_id="t", kg_name="kg", ai_description="hello")
    asyncio.run(store.upsert(stats))
    got = asyncio.run(store.get("t", "kg"))
    assert got is not None and got.ai_description == "hello"


# ── _backfill_kg_summaries: list-time lazy fill ─────────────────────────────


def test_backfill_only_fills_pending_rows(monkeypatch):
    from cograph_client.api.routes import knowledge_graphs as kg_routes
    from cograph_client.graph import kg_stats_store as store_mod

    store = InMemoryKgStatsStore()
    monkeypatch.setattr(store_mod, "get_kg_stats_store", lambda: store)

    calls: list[str] = []

    async def fake_gen(kg_name, breakdown, **kwargs):
        calls.append(kg_name)
        return f"summary of {kg_name}"

    monkeypatch.setattr(kg_summary, "generate_kg_summary", fake_gen)

    rows = [
        # pending: has entities, no description → should generate
        KgStats(tenant_id="t", kg_name="needs-one", type_breakdown={"A": 3}),
        # already has a description → skip
        KgStats(tenant_id="t", kg_name="has-one", type_breakdown={"A": 3}, ai_description="x"),
        # empty graph → skip (nothing to describe)
        KgStats(tenant_id="t", kg_name="empty", type_breakdown={}),
    ]
    asyncio.run(kg_routes._backfill_kg_summaries(rows))

    assert calls == ["needs-one"]
    # mutated in place for the caller
    assert rows[0].ai_description == "summary of needs-one"
    # and persisted for next time
    persisted = asyncio.run(store.get("t", "needs-one"))
    assert persisted is not None and persisted.ai_description == "summary of needs-one"

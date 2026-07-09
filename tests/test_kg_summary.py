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


# ── _summary_model: cheap default, env override ─────────────────────────────


def test_summary_model_default_and_override(monkeypatch):
    monkeypatch.delenv("OMNIX_KG_SUMMARY_MODEL", raising=False)
    assert kg_summary._summary_model() == "google/gemini-2.5-flash"
    monkeypatch.setenv("OMNIX_KG_SUMMARY_MODEL", "vendor/cheap-model")
    assert kg_summary._summary_model() == "vendor/cheap-model"


# ── resolve_summary: the recompute decision + staleness self-heal ────────────


def test_resolve_summary_generates_when_blank():
    async def gen(kg, bd):
        return "fresh line"

    desc, types = asyncio.run(
        kg_summary.resolve_summary("", [], {"A": 1, "B": 2}, "kg", generate=gen)
    )
    assert desc == "fresh line" and types == ["A", "B"]


def test_resolve_summary_keeps_when_type_set_unchanged():
    async def gen(kg, bd):
        raise AssertionError("must not regenerate when the type set is unchanged")

    # Counts moved but the type SET is the same as the description's signature.
    desc, types = asyncio.run(
        kg_summary.resolve_summary("old line", ["A", "B"], {"B": 9, "A": 3}, "kg", generate=gen)
    )
    assert desc == "old line" and types == ["A", "B"]


def test_resolve_summary_regenerates_on_type_change():
    async def gen(kg, bd):
        return "new line"

    desc, types = asyncio.run(
        kg_summary.resolve_summary("old line", ["A"], {"A": 1, "C": 2}, "kg", generate=gen)
    )
    assert desc == "new line" and types == ["A", "C"]


def test_resolve_summary_failed_regen_keeps_old_line_and_stale_signature():
    # Type set changed but generation returned "" — keep the old line AND leave
    # the signature at the OLD set, so the next recompute still sees a mismatch
    # and retries (the description never silently sticks to a defunct type set).
    async def gen(kg, bd):
        return ""

    desc, types = asyncio.run(
        kg_summary.resolve_summary("old line", ["A"], {"A": 1, "C": 2}, "kg", generate=gen)
    )
    assert desc == "old line" and types == ["A"]
    # Confirm the invariant: a mismatch remains, so we'd regenerate next time.
    assert kg_summary.should_generate_summary(desc, types, {"A": 1, "C": 2}) is True


# ── background summary backfill (off the list_kgs hot path) ──────────────────


def test_run_summary_backfill_fills_and_persists(monkeypatch):
    from cograph_client.api.routes import explore as explore_mod
    from cograph_client.graph import kg_stats_store as store_mod

    store = InMemoryKgStatsStore()
    monkeypatch.setattr(store_mod, "get_kg_stats_store", lambda: store)

    async def fake_gen(kg_name, breakdown, **kwargs):
        return f"summary of {kg_name}"

    monkeypatch.setattr(kg_summary, "generate_kg_summary", fake_gen)

    rows = [KgStats(tenant_id="t", kg_name="needs-one", type_breakdown={"A": 3})]
    asyncio.run(explore_mod._run_summary_backfill(rows))

    assert rows[0].ai_description == "summary of needs-one"
    assert rows[0].ai_description_types == ["A"]  # signature stamped on fill
    persisted = asyncio.run(store.get("t", "needs-one"))
    assert persisted is not None and persisted.ai_description == "summary of needs-one"


def test_schedule_summary_backfill_only_schedules_pending(monkeypatch):
    from cograph_client.api.routes import explore as explore_mod

    scheduled: list[list[str]] = []

    async def fake_run(pending):
        scheduled.append([r.kg_name for r in pending])

    monkeypatch.setattr(explore_mod, "_run_summary_backfill", fake_run)

    async def drive(rows):
        explore_mod.schedule_summary_backfill(rows)
        await asyncio.sleep(0)  # let the fire-and-forget task run

    # Nothing pending (all described or empty) → no task scheduled.
    asyncio.run(
        drive(
            [
                KgStats(tenant_id="t", kg_name="has", type_breakdown={"A": 1}, ai_description="x"),
                KgStats(tenant_id="t", kg_name="empty", type_breakdown={}),
            ]
        )
    )
    assert scheduled == []

    # One pending row → exactly that row is scheduled.
    asyncio.run(
        drive(
            [
                KgStats(tenant_id="t", kg_name="needs", type_breakdown={"A": 1}),
                KgStats(tenant_id="t", kg_name="has", type_breakdown={"A": 1}, ai_description="x"),
            ]
        )
    )
    assert scheduled == [["needs"]]

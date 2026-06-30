"""KG registration is part of the shared write path (ONTA-153).

The bug: the ``<kg_uri> <onto/kg_name> "name"`` record that ``list_kgs`` reads to
populate the Explorer dropdown was written in exactly ONE place — ``create_kg``,
the Explorer's "New KG" button. Any non-UI writer (agent web-discovery, CLI, MCP)
that ingested into a brand-new ``kg_name`` wrote the instance data + ontology but
the KG never appeared in the dropdown (``list_kgs`` returned ``[]``).

Fix: ``refresh_after_write`` — the shared post-write housekeeping every writer
already calls — now idempotently registers the KG via ``ensure_kg_registered``.
These tests pin (a) that a fresh write issues the guarded registration INSERT and
(b) that it never duplicates an existing registration.
"""

import asyncio
from unittest.mock import AsyncMock

import cograph_client.api.routes.explore as explore_mod
import cograph_client.nlp.pipeline as pipeline_mod
from cograph_client.graph.kg_writer import (
    _KG_NAME_PRED,
    _kg_meta_uri,
    ensure_kg_registered,
    refresh_after_write,
)


def _is_registration_stmt(sparql: str, tenant_id: str, kg_name: str) -> bool:
    """True if ``sparql`` is the guarded KG-registration INSERT for this KG."""
    kg_uri = _kg_meta_uri(tenant_id, kg_name)
    return (
        "INSERT" in sparql
        and "NOT EXISTS" in sparql
        and _KG_NAME_PRED in sparql
        and kg_uri in sparql
        and f'"{kg_name}"' in sparql
    )


def test_ensure_kg_registered_issues_guarded_insert():
    """The helper sends a single NOT-EXISTS-guarded INSERT carrying the kg_name
    record (so it can't duplicate or clobber an existing registration)."""

    async def run():
        neptune = AsyncMock()
        await ensure_kg_registered(neptune, "t", "fresh-kg")

        assert neptune.update.await_count == 1
        sparql = neptune.update.await_args.args[0]
        assert _is_registration_stmt(sparql, "t", "fresh-kg")
        # Guard present both forms-ways: it's an INSERT ... WHERE, not INSERT DATA.
        assert "INSERT DATA" not in sparql
        assert "FILTER NOT EXISTS" in sparql

    asyncio.run(run())


def test_ensure_kg_registered_is_idempotent_in_shape():
    """Calling twice yields a guarded (NOT EXISTS) statement BOTH times — the
    guard, not call-count bookkeeping, is what makes it non-duplicating."""

    async def run():
        neptune = AsyncMock()
        await ensure_kg_registered(neptune, "t", "k")
        await ensure_kg_registered(neptune, "t", "k")

        assert neptune.update.await_count == 2
        for call in neptune.update.await_args_list:
            assert _is_registration_stmt(call.args[0], "t", "k")

    asyncio.run(run())


def test_ensure_kg_registered_best_effort_on_failure():
    """A registration failure must never propagate out of the write path."""

    async def run():
        neptune = AsyncMock()
        neptune.update.side_effect = RuntimeError("neptune down")
        # Should not raise.
        await ensure_kg_registered(neptune, "t", "k")

    asyncio.run(run())


def test_ensure_kg_registered_noop_without_name():
    async def run():
        neptune = AsyncMock()
        await ensure_kg_registered(neptune, "t", "")
        neptune.update.assert_not_awaited()

    asyncio.run(run())


def test_refresh_after_write_registers_fresh_kg(monkeypatch):
    """A writer producing facts into a fresh kg_name → refresh_after_write must
    issue the registration INSERT (the part that was missing for non-UI writers).
    A SECOND refresh must NOT duplicate it: every emission carries the NOT-EXISTS
    guard, so the second one is a no-op against an already-registered KG."""

    async def run():
        # Stub out the OTHER housekeeping steps so we isolate the registration
        # write on `neptune.update`.
        monkeypatch.setattr(
            pipeline_mod.NLQueryPipeline, "invalidate_cache", lambda graph: None
        )
        monkeypatch.setattr(pipeline_mod, "get_embedding_service", lambda: None)
        monkeypatch.setattr(
            explore_mod,
            "schedule_recompute",
            lambda neptune, tenant_id, kg_name: None,
        )

        neptune = AsyncMock()
        await refresh_after_write(neptune, tenant_id="t", kg_name="brand-new")

        reg_calls = [
            c.args[0]
            for c in neptune.update.await_args_list
            if _is_registration_stmt(c.args[0], "t", "brand-new")
        ]
        assert len(reg_calls) == 1, "fresh write must register the KG exactly once"
        assert "FILTER NOT EXISTS" in reg_calls[0]

        # Second refresh: still guarded, still no duplication risk.
        await refresh_after_write(neptune, tenant_id="t", kg_name="brand-new")
        reg_calls_2 = [
            c.args[0]
            for c in neptune.update.await_args_list
            if _is_registration_stmt(c.args[0], "t", "brand-new")
        ]
        assert len(reg_calls_2) == 2
        assert all("FILTER NOT EXISTS" in s for s in reg_calls_2)

    asyncio.run(run())


def test_refresh_after_write_skips_registration_without_kg(monkeypatch):
    """A tenant-graph-only write (no kg_name) registers nothing."""

    async def run():
        monkeypatch.setattr(
            pipeline_mod.NLQueryPipeline, "invalidate_cache", lambda graph: None
        )
        monkeypatch.setattr(pipeline_mod, "get_embedding_service", lambda: None)
        monkeypatch.setattr(
            explore_mod, "schedule_recompute", lambda *a, **k: None
        )

        neptune = AsyncMock()
        await refresh_after_write(neptune, tenant_id="t", kg_name=None)
        neptune.update.assert_not_awaited()

    asyncio.run(run())

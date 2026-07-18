"""ONTA-372 (keystone) — end-to-end run-lineage wiring through ``web_ingest_cap``.

The direct acceptance assertion: for ONE discovery ingest driven through the real
capability, the A1 Source Bundle's ``run_id`` is the SAME id the A6
``build_graph_delta`` receipt is keyed under. ``web_ingest_cap`` mints ONE
run-scoped envelope at the P1 entry and threads its ``run_id`` into BOTH the A1
bundle AND the resolver ingest call; the resolver keys the A6 delta off exactly
that id. Before this fix the resolver minted its own uuid4 and the two lineages
diverged (the A6 delta was dead on the discovery path).

FILE-ORDER NOTE: this test drives the discovery ``execute`` machinery, which
exercises the shared ``kg_writer`` logger under ``cache_logger_on_first_use=True``.
structlog's ``capture_logs`` cannot intercept an already-cached bound logger, so
any ``execute``-running test that sorts BEFORE ``test_semantic_write_hook`` would
freeze it. ``test_web_ingest_*`` sorts AFTER the semantic tests — keep this
invariant if renaming (see ``test_web_ingest_fastpath.py``'s note).
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from cograph_client.agent.capabilities import web_ingest_cap
from cograph_client.agent.capabilities.web_ingest_cap import WebIngestCapability
from cograph_client.agent.registry import AgentContext, PlanStep
from cograph_client.graph.kg_writer import build_graph_delta
from cograph_client.pipeline.source_bundle import SourceBundle
from cograph_client.resolver.schema_resolver import IngestResult, SchemaResolver
from cograph_client.web_sources.base import (
    DiscoverResult,
    register_web_source,
    reset_web_sources,
)


class FakeWeb:
    """A plain open-web provider (NOT structured) so the run takes the default
    LLM-extract ``resolver.ingest`` path — the primary discovery path."""

    name = "fake-web"
    structured = False
    is_paid = False
    cost_per_call = 0.0

    async def discover(self, query, *, sample, max_rows, hint_columns, context, urls=None):
        return DiscoverResult(
            rows=[{"name": "Dr A", "specialty": "Cardiology"}],
            provenance={"Dr A": "https://reg.example/a"},
            sources=["https://reg.example/a"],
        )


def _ctx(sink) -> AgentContext:
    return AgentContext(
        tenant_id="demo-tenant", kg_name="docs", neptune=MagicMock(),
        anthropic_key="sk-ant-test", openrouter_key="",
        extras={"prior_clarify_count": 0, "source_bundle_sink": sink},
    )


def _step() -> PlanStep:
    return PlanStep(
        capability="web_ingest", action="discover_ingest",
        params={
            "query": "cardiologists",
            "subqueries": [],
            "proposed_type": "Physician",
            "attributes": ["name", "specialty"],
            "hint_columns": ["name", "specialty"],
            "max_rows": 10,
            "kg_name": "docs",
            "provider": "fake-web",
            "providers": ["fake-web"],
            "urls": [],
        },
    )


@pytest.fixture(autouse=True)
def _clean():
    reset_web_sources()
    yield
    reset_web_sources()


@pytest.mark.asyncio
async def test_a1_bundle_run_id_equals_a6_build_graph_delta_run_id(monkeypatch):
    monkeypatch.setattr(web_ingest_cap, "_DISCOVERY_STRUCTURED_FASTPATH", False)
    register_web_source(FakeWeb())

    captured: dict = {}

    async def spy_ingest(self, content, tenant_id, content_type="text", source="",
                         instance_graph=None, run_id=None, **_kw):
        rows = json.loads(content)
        captured["run_id"] = run_id
        # Exercise the ACTUAL A6 build_graph_delta with the threaded run_id — the
        # run_id its receipt is keyed under is what the real resolver would use.
        a6 = build_graph_delta(instance_graph or "g://kg", [], run_id=run_id)
        captured["a6_run_id"] = a6.run_id
        return IngestResult(entities_extracted=len(rows), entities_resolved=len(rows))

    monkeypatch.setattr(SchemaResolver, "ingest", spy_ingest)

    task_holder: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: task_holder.setdefault("task", asyncio.ensure_future(coro)),
    )

    bundles: list[SourceBundle] = []
    ack = await WebIngestCapability().execute(_ctx(bundles), _step())
    assert ack["kind"] == "ack"
    await task_holder["task"]

    # The A1 Source Bundle was materialized and resolver.ingest was reached.
    assert len(bundles) == 1
    bundle = bundles[0]
    assert captured.get("run_id"), "resolver.ingest received the threaded run_id"

    # The load-bearing acceptance assertion: ONE run lineage — the A1 bundle's
    # run_id IS the id the A6 build_graph_delta receipt is keyed under.
    assert bundle.run_id == captured["run_id"]
    assert bundle.run_id == captured["a6_run_id"]


@pytest.mark.asyncio
async def test_structured_fastpath_threads_same_run_id_as_a1_bundle(monkeypatch):
    """The structured fast-path also threads the run-scoped id: the run_id handed
    to ``ingest_structured_rows`` equals the A1 Source Bundle's run_id."""
    monkeypatch.setattr(web_ingest_cap, "_DISCOVERY_STRUCTURED_FASTPATH", True)

    class FakeStructured(FakeWeb):
        name = "fake-structured"
        structured = True

    register_web_source(FakeStructured())

    captured: dict = {}

    async def spy_structured(self, rows, tenant_id, type_name, attributes=None,
                             source="", instance_graph=None, key_attribute=None,
                             key_join=None, run_id=None, **_kw):
        captured["run_id"] = run_id
        a6 = build_graph_delta(instance_graph or "g://kg", [], run_id=run_id)
        captured["a6_run_id"] = a6.run_id
        return IngestResult(entities_extracted=len(rows), entities_resolved=len(rows))

    async def forbidden_ingest(self, *a, **k):  # pragma: no cover - guard
        raise AssertionError("the LLM ingest() path must not run under the fast-path")

    monkeypatch.setattr(SchemaResolver, "ingest_structured_rows", spy_structured)
    monkeypatch.setattr(SchemaResolver, "ingest", forbidden_ingest)

    task_holder: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: task_holder.setdefault("task", asyncio.ensure_future(coro)),
    )

    step = _step()
    step.params.update(provider="fake-structured", providers=["fake-structured"])
    bundles: list[SourceBundle] = []
    ack = await WebIngestCapability().execute(_ctx(bundles), step)
    assert ack["kind"] == "ack"
    await task_holder["task"]

    assert len(bundles) == 1
    assert captured.get("run_id"), "the fast-path received the threaded run_id"
    assert bundles[0].run_id == captured["run_id"]
    assert bundles[0].run_id == captured["a6_run_id"]

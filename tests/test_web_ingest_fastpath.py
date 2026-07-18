"""web_ingest_cap routing of PRE-STRUCTURED providers to the fast path (ONTA-272).

With the fast-path flag ON, a provider that self-declares ``structured=True``
routes its rows to ``SchemaResolver.ingest_structured_rows`` (deterministic, no
``_extract``); with the flag OFF the byte-for-byte unchanged ``resolver.ingest``
JSON path runs (freezing today's behavior — ``test_web_ingest_registry`` stays
green).

FILE-ORDER NOTE: these tests drive the discovery ``execute`` machinery, which
exercises the shared ``cograph.graph.kg_writer`` logger under the app's
``cache_logger_on_first_use=True`` config. structlog's ``capture_logs`` (used by
``test_semantic_write_hook``) cannot intercept an already-cached bound logger, so
any ``execute``-running test that sorts BEFORE ``test_semantic_write_hook`` would
freeze that logger and break it. This file is named ``test_web_ingest_*`` so it
sorts AFTER the semantic capture_logs tests — the same reason the pre-existing
``test_web_ingest_registry`` (which also runs ``execute``) lives there. Keep this
invariant if renaming.
"""
from __future__ import annotations

import asyncio

import pytest

from cograph_client.agent.capabilities import web_ingest_cap
from cograph_client.agent.capabilities.web_ingest_cap import WebIngestCapability
from cograph_client.agent.registry import AgentContext, PlanStep
from cograph_client.resolver.schema_resolver import IngestResult, SchemaResolver
from cograph_client.web_sources.base import (
    DiscoverResult,
    register_web_source,
    reset_web_sources,
)


class FakeStructured:
    """A provider whose rows are ALREADY structured (keyed by the confirmed
    attribute set) — it opts into the fast path via ``structured=True``."""

    name = "fake-structured"
    structured = True
    is_paid = False
    cost_per_call = 0.0

    async def discover(self, query, *, sample, max_rows, hint_columns, context, urls=None):
        return DiscoverResult(
            rows=[{"name": "Dr A", "specialty": "Cardiology"}],
            provenance={"Dr A": "https://reg.example/a"},
            sources=["https://reg.example/a"],
        )


def _ctx() -> AgentContext:
    from unittest.mock import MagicMock

    return AgentContext(
        tenant_id="demo-tenant", kg_name="docs", neptune=MagicMock(),
        anthropic_key="sk-ant-test", openrouter_key="", extras={},
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
            "provider": "fake-structured",
            "providers": ["fake-structured"],
            "urls": [],
        },
    )


@pytest.fixture(autouse=True)
def _clean():
    reset_web_sources()
    yield
    reset_web_sources()


def _spy_resolver(monkeypatch) -> dict:
    calls: dict = {"structured": [], "ingest": []}

    async def fake_structured(self, rows, tenant_id, type_name, attributes=None,
                              source="", instance_graph=None, key_attribute=None,
                              key_join=None, run_id=None):
        calls["structured"].append({"rows": rows, "type_name": type_name,
                                    "key_attribute": key_attribute, "run_id": run_id})
        return IngestResult(entities_extracted=len(rows), entities_resolved=len(rows))

    async def fake_ingest(self, content, tenant_id, content_type="text", source="",
                          instance_graph=None, **kw):
        calls["ingest"].append({"content": content, "content_type": content_type})
        return IngestResult()

    monkeypatch.setattr(SchemaResolver, "ingest_structured_rows", fake_structured)
    monkeypatch.setattr(SchemaResolver, "ingest", fake_ingest)
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: calls.setdefault("task", asyncio.ensure_future(coro)),
    )
    return calls


@pytest.mark.asyncio
async def test_flag_on_routes_structured_provider_to_fast_path(monkeypatch):
    monkeypatch.setattr(web_ingest_cap, "_DISCOVERY_STRUCTURED_FASTPATH", True)
    register_web_source(FakeStructured())
    calls = _spy_resolver(monkeypatch)

    ack = await WebIngestCapability().execute(_ctx(), _step())
    assert ack["kind"] == "ack"
    await calls["task"]

    # Structured rows went to the deterministic fast path; the LLM json path was
    # NOT taken.
    assert len(calls["structured"]) == 1
    assert calls["structured"][0]["type_name"] == "Physician"
    assert calls["structured"][0]["key_attribute"] == "name"
    assert calls["ingest"] == []


@pytest.mark.asyncio
async def test_flag_off_keeps_llm_json_path(monkeypatch):
    monkeypatch.setattr(web_ingest_cap, "_DISCOVERY_STRUCTURED_FASTPATH", False)
    register_web_source(FakeStructured())
    calls = _spy_resolver(monkeypatch)

    ack = await WebIngestCapability().execute(_ctx(), _step())
    assert ack["kind"] == "ack"
    await calls["task"]

    # Default OFF: the unchanged resolver.ingest json path runs; the fast path is
    # not taken (freezes today's behavior — test_web_ingest_registry stays green).
    assert calls["structured"] == []
    assert len(calls["ingest"]) == 1
    assert calls["ingest"][0]["content_type"] == "json"

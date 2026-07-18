"""ONTA-371 — Unfuse A1: the extract loop drives from the A1 Source Bundle.

Before ONTA-371 the A1 :class:`SourceBundle` was built at the Find→Extract
boundary and then DROPPED — the micro-batch extract/write loop consumed the raw
``batch`` rows, discarding the per-row ``fact_id`` / ``tier`` the bundle minted.
This file is the load-bearing regression control that the loop now:

(a) DRIVES FROM ``bundle.rows`` and FORWARDS each row's A1 ``fact_id`` + source
    ``tier`` into the resolver ingest call (assert on the args the resolver
    receives) — the real A1→A2 handoff; AND
(b) is BEHAVIOR-PRESERVING — the SAME records are extracted and the SAME domain
    facts written as when the loop drove from the raw batch (the lineage rides
    along; WHAT is extracted/written is unchanged).

Both the primary LLM-extract ``resolver.ingest`` path and the structured
fast-path ``resolver.ingest_structured_rows`` are exercised. The run is driven
fully offline (a canned provider, a monkeypatched resolver, no LLM / network),
mirroring ``test_web_ingest_lineage.py``.

FILE-ORDER NOTE: like the other ``test_web_ingest_*`` files, this drives the
discovery ``execute`` machinery (which touches the shared ``kg_writer`` logger
under ``cache_logger_on_first_use=True``); the ``test_web_ingest_`` prefix sorts
it AFTER ``test_semantic_write_hook`` so structlog's ``capture_logs`` is not
frozen — keep the prefix if renaming.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from cograph_client.agent.capabilities import web_ingest_cap
from cograph_client.agent.capabilities.web_ingest_cap import WebIngestCapability
from cograph_client.agent.registry import AgentContext, PlanStep
from cograph_client.pipeline.source_bundle import (
    TIER_AUTHORITATIVE,
    TIER_WEB,
    SourceBundle,
)
from cograph_client.resolver.schema_resolver import IngestResult, SchemaResolver
from cograph_client.web_sources.base import (
    DiscoverResult,
    register_web_source,
    reset_web_sources,
)

# Four distinct records. A SHARED source_url (one page) means the citation
# partition keeps them in ONE micro-batch, so a single resolver ingest call
# carries the full per-row fact_id LIST — the crispest demonstration of the
# handoff (the loop still works with per-row URLs; accumulation across calls is
# asserted order-preserving regardless).
SHARED_URL = "https://roster.example/page"
FULL_ROWS = [
    {"name": "anthropic/claude-opus-4-8", "context_length": "200000"},
    {"name": "openai/gpt-5", "context_length": "400000"},
    {"name": "google/gemini-2.5-flash", "context_length": "1000000"},
    {"name": "meta/llama-4", "context_length": "128000"},
]


class _FakeProvider:
    """Canned provider: projects rows to hint_columns + emits per-row provenance
    (all rows → one page). ``structured`` / ``is_source_of_truth`` / ``secret_ref``
    let one class serve both the LLM-path and the authoritative structured-path
    cases."""

    def __init__(
        self,
        *,
        name: str = "fake-web",
        structured: bool = False,
        is_source_of_truth: bool = False,
        secret_ref: str = "",
    ) -> None:
        self.name = name
        self.structured = structured
        self.is_paid = False
        self.cost_per_call = 0.0
        self.is_source_of_truth = is_source_of_truth
        self.secret_ref = secret_ref

    async def discover(self, query, *, sample, max_rows, hint_columns, context, urls=None):
        rows = [dict(r) for r in FULL_ROWS[: (5 if sample else max_rows)]]
        if hint_columns:
            rows = [{c: r.get(c, "unknown") for c in hint_columns} for r in rows]
        provenance = {r.get("name", str(i)): SHARED_URL for i, r in enumerate(rows)}
        return DiscoverResult(
            rows=rows,
            provenance=provenance,
            sources=["https://roster.example"],
            estimated_total=len(FULL_ROWS),
            is_partial=sample,
        )


def _ctx(sink) -> AgentContext:
    return AgentContext(
        tenant_id="demo-tenant", kg_name="models", neptune=MagicMock(),
        anthropic_key="sk-ant-test", openrouter_key="",
        extras={"prior_clarify_count": 0, "source_bundle_sink": sink},
    )


def _step(provider_name: str) -> PlanStep:
    return PlanStep(
        capability="web_ingest", action="discover_ingest",
        params={
            "query": "OpenRouter models",
            "subqueries": [],
            "proposed_type": "OpenRouterModel",
            "attributes": ["name", "context_length"],
            "hint_columns": ["name", "context_length"],
            "max_rows": 10,
            "kg_name": "models",
            "provider": provider_name,
            "providers": [provider_name],
            "urls": [],
        },
    )


@pytest.fixture(autouse=True)
def _clean():
    reset_web_sources()
    yield
    reset_web_sources()


# The records the resolver SHOULD receive — the provider's rows with the shared
# source_url stamped. Computed INDEPENDENTLY of the bundle, so the
# behavior-preserving assertion is a real external reference, not a tautology.
def _expected_records() -> list[dict]:
    return [
        {"name": r["name"], "context_length": r["context_length"],
         "source_url": SHARED_URL}
        for r in FULL_ROWS
    ]


@pytest.mark.asyncio
async def test_llm_path_extract_loop_drives_from_bundle_and_forwards_lineage(monkeypatch):
    """Primary (LLM-extract) path: ``resolver.ingest`` receives the bundle's
    per-row fact_ids + tier, and the records it receives are byte-identical to the
    provider's output (behavior-preserving)."""
    monkeypatch.setattr(web_ingest_cap, "_DISCOVERY_STRUCTURED_FASTPATH", False)
    register_web_source(_FakeProvider(name="fake-web"))

    calls: list[dict] = []

    async def spy_ingest(self, content, tenant_id, content_type="text", source="",
                         instance_graph=None, run_id=None, fact_ids=None, tier=None,
                         **_kw):
        calls.append({
            "rows": json.loads(content),
            "run_id": run_id,
            "fact_ids": list(fact_ids or []),
            "tier": tier,
        })
        rows = json.loads(content)
        return IngestResult(entities_extracted=len(rows), entities_resolved=len(rows))

    monkeypatch.setattr(SchemaResolver, "ingest", spy_ingest)

    task_holder: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: task_holder.setdefault("task", asyncio.ensure_future(coro)),
    )

    bundles: list[SourceBundle] = []
    ack = await WebIngestCapability().execute(_ctx(bundles), _step("fake-web"))
    assert ack["kind"] == "ack"
    await task_holder["task"]

    assert len(bundles) == 1
    bundle = bundles[0]
    assert calls, "resolver.ingest was reached"

    # Records + lineage accumulated across every ingest call, in order.
    received_rows = [row for c in calls for row in c["rows"]]
    received_fact_ids = [fid for c in calls for fid in c["fact_ids"]]
    received_tiers = {c["tier"] for c in calls}
    received_run_ids = {c["run_id"] for c in calls}

    # (a) A1→A2 HANDOFF: the per-row A1 fact_ids reach the resolver, in row order,
    # and they ARE the bundle's per-row fact_ids — proving the loop drives from
    # bundle.rows, not the built-then-dropped raw batch.
    assert received_fact_ids == list(bundle.fact_ids)
    assert len(received_fact_ids) == len(FULL_ROWS)
    assert len(set(received_fact_ids)) == len(FULL_ROWS)  # all distinct
    # A shared page keeps all rows in ONE micro-batch → ONE call carrying the full
    # per-row fact_id list (not one-fact-id-per-call).
    assert len(calls) == 1
    assert calls[0]["fact_ids"] == list(bundle.fact_ids)

    # tier reaches the resolver and matches the bundle's (plain web here).
    assert received_tiers == {TIER_WEB}
    assert received_tiers == set(bundle.tiers)

    # (b) BEHAVIOR-PRESERVING: the SAME records/domain facts are written — the rows
    # the resolver receives equal the provider's output (independent reference) AND
    # the bundle's row-data snapshot; nothing dropped, added, or altered.
    assert received_rows == _expected_records()
    assert received_rows == [dict(r.data) for r in bundle.rows]

    # ONTA-372 NOT REGRESSED: the resolver still gets the run-scoped id, and it is
    # the SAME id the A1 bundle carries (one run lineage).
    assert received_run_ids == {bundle.run_id}
    assert bundle.run_id


@pytest.mark.asyncio
async def test_structured_fastpath_forwards_per_row_lineage_and_tier(monkeypatch):
    """Structured fast-path: ``resolver.ingest_structured_rows`` receives the
    bundle's per-row fact_ids + the AUTHORITATIVE tier (a registry source-of-truth
    provider), and the rows it receives match the provider's output."""
    monkeypatch.setattr(web_ingest_cap, "_DISCOVERY_STRUCTURED_FASTPATH", True)
    register_web_source(_FakeProvider(
        name="acme-api", structured=True, is_source_of_truth=True,
        secret_ref="acme_secret",
    ))

    calls: list[dict] = []

    async def spy_structured(self, rows, tenant_id, type_name, attributes=None,
                             source="", instance_graph=None, key_attribute=None,
                             key_join=None, run_id=None, fact_ids=None, tier=None):
        calls.append({
            "rows": [dict(r) for r in rows],
            "run_id": run_id,
            "fact_ids": list(fact_ids or []),
            "tier": tier,
        })
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

    bundles: list[SourceBundle] = []
    ack = await WebIngestCapability().execute(_ctx(bundles), _step("acme-api"))
    assert ack["kind"] == "ack"
    await task_holder["task"]

    assert len(bundles) == 1
    bundle = bundles[0]
    assert calls, "the structured fast-path was reached"

    received_rows = [row for c in calls for row in c["rows"]]
    received_fact_ids = [fid for c in calls for fid in c["fact_ids"]]
    received_tiers = {c["tier"] for c in calls}

    # (a) per-row A1 fact_ids + tier reach the structured ingest call, and the
    # fact_ids ARE the bundle's (drives from bundle.rows).
    assert received_fact_ids == list(bundle.fact_ids)
    assert len(received_fact_ids) == len(FULL_ROWS)
    # AUTHORITATIVE tier (registry source-of-truth) is threaded through, matching
    # the bundle — the load-bearing contrast to the web-tier case above.
    assert received_tiers == {TIER_AUTHORITATIVE}
    assert received_tiers == set(bundle.tiers)

    # (b) behavior-preserving: the rows committed equal the provider's output and
    # the bundle's row data.
    assert received_rows == _expected_records()
    assert received_rows == [dict(r.data) for r in bundle.rows]

    # ONTA-372 not regressed on the fast-path either.
    assert {c["run_id"] for c in calls} == {bundle.run_id}

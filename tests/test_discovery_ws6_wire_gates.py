"""ONTA-465 / WS6 — wire structural quality gates into the discovery path.

Covers:

1. Pure helper :func:`apply_post_a1_structural_gates` call order
   (role-membership → discovery quality / identity merge) and counters.
2. End-to-end discovery execute surfaces ``role_drops`` / ``identity_merges``
   on stage_trace actions and the A1 contract when a provider returns a
   polluted batch (role entity + catalog/surface dup).

Hard rule: no brand/platform denylists — fixtures use synthetic multi-domain
names only.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from cograph_client.agent.capabilities import web_ingest_cap
from cograph_client.agent.capabilities.web_ingest_cap import (
    StructuralGateResult,
    WebIngestCapability,
    apply_post_a1_structural_gates,
)
from cograph_client.agent.registry import AgentContext, PlanStep
from cograph_client.enrichment.job_store import InMemoryJobStore
from cograph_client.enrichment.models import JobStatus
from cograph_client.resolver.models import IngestResult
from cograph_client.resolver.schema_resolver import SchemaResolver
from cograph_client.web_sources import (
    DiscoverResult,
    register_web_source,
    reset_web_sources,
)


# --------------------------------------------------------------------------- #
# Unit: pure helper
# --------------------------------------------------------------------------- #


def test_structural_gates_empty_batch():
    r = apply_post_a1_structural_gates([], "name", ["name", "provider"])
    # Field check (not isinstance): dual import paths can yield two dataclass
    # types for StructuralGateResult under full-suite collection.
    assert getattr(r, "rows", None) == []
    assert r.role_drops == 0
    assert r.identity_merges == 0
    assert type(r).__name__ == "StructuralGateResult"


def test_structural_gates_role_drop_before_identity():
    """Role-inverted org row is dropped; catalog↔surface pair merges to 1."""
    rows = [
        {"name": "acme/widget-pro", "provider": "Acme", "context_length": "8k"},
        {"name": "Widget Pro", "provider": "Acme"},  # surface form of catalog
        {"name": "Acme", "provider": "Acme"},  # role entity mistaken for instance
    ]
    r = apply_post_a1_structural_gates(
        rows,
        "name",
        ["name", "provider", "context_length"],
        focus_type="Model",
    )
    keys = {row.get("name") for row in r.rows}
    assert "Acme" not in keys
    assert r.role_drops == 1
    # catalog-path + free-text surface of same slug → one survivor
    assert r.identity_merges >= 1
    assert len(r.rows) == 1
    # Prefer catalog-path identity as survivor
    assert "/" in (r.rows[0].get("name") or "")


def test_structural_gates_keeps_unrelated_instances():
    rows = [
        {"name": "alpha/pkg-one", "publisher": "Alpha Org"},
        {"name": "beta/pkg-two", "publisher": "Beta Org"},
    ]
    r = apply_post_a1_structural_gates(
        rows, "name", ["name", "publisher"]
    )
    assert r.role_drops == 0
    assert r.identity_merges == 0
    assert len(r.rows) == 2


def test_structural_gates_does_not_mutate_input():
    rows = [
        {"name": "acme/widget", "provider": "Acme"},
        {"name": "Acme"},
    ]
    snapshot = [dict(r) for r in rows]
    apply_post_a1_structural_gates(rows, "name", ["name", "provider"])
    assert rows == snapshot


def test_structural_gates_physician_hospital_role_inversion():
    """Cross-domain: hospital name equals another row's organization value."""
    rows = [
        {
            "name": "Ada Lovelace, MD",
            "organization": "City General Hospital",
            "specialty": "cardiology",
        },
        {
            "name": "City General Hospital",
            "organization": "City General Hospital",
        },
    ]
    r = apply_post_a1_structural_gates(
        rows,
        "name",
        ["name", "organization", "specialty"],
        focus_type="Physician",
    )
    names = {row.get("name") for row in r.rows}
    assert "City General Hospital" not in names
    assert "Ada Lovelace, MD" in names
    assert r.role_drops >= 1


# --------------------------------------------------------------------------- #
# Integration: discovery execute surfaces counters
# --------------------------------------------------------------------------- #


class _FakeProvider:
    """Canned provider matching FakeProvider's discover signature."""

    def __init__(self, rows, name: str = "fake") -> None:
        self.name = name
        self.structured = False
        self.is_paid = False
        self.cost_per_call = 0.0
        self._rows = rows

    async def discover(
        self, query, *, sample, max_rows, hint_columns, context, urls=None
    ):
        # Return full rows (not projected) so structural gates see role attrs.
        rows = [dict(r) for r in self._rows]
        for r in rows:
            r.setdefault("source_url", "https://catalog.example/list")
        return DiscoverResult(
            rows=rows,
            sources=["https://catalog.example/list"],
            estimated_total=len(self._rows),
            is_partial=bool(sample),
        )


def _ctx_with_store(store) -> AgentContext:
    return AgentContext(
        tenant_id="demo-tenant",
        kg_name="models",
        neptune=MagicMock(),
        anthropic_key="sk-ant-test",
        openrouter_key="",
        extras={"prior_clarify_count": 0, "enrichment_job_store": store},
    )


@pytest.fixture(autouse=True)
def _reset_sources():
    reset_web_sources()
    yield
    reset_web_sources()


async def test_discovery_execute_surfaces_role_and_identity_counters(monkeypatch):
    """Polluted provider batch → role_drops + identity_merges on stage_trace."""
    polluted = [
        {
            "name": "acme/widget-pro",
            "provider": "Acme",
            "context_length": "8192",
            "source_url": "https://catalog.example/list",
        },
        {
            "name": "Widget Pro",
            "provider": "Acme",
            "source_url": "https://catalog.example/list",
        },
        {
            "name": "Acme",
            "provider": "Acme",
            "source_url": "https://catalog.example/list",
        },
    ]
    register_web_source(_FakeProvider(polluted))

    ingested_payloads: list = []

    async def fake_ingest(
        self,
        content,
        tenant_id,
        content_type="text",
        source="",
        instance_graph=None,
        **_kw,
    ):
        rows = json.loads(content) if isinstance(content, str) else content
        if isinstance(rows, list):
            ingested_payloads.append(rows)
            n = len(rows)
        else:
            n = 1
        return IngestResult(entities_extracted=n, entities_resolved=n)

    monkeypatch.setattr(SchemaResolver, "ingest", fake_ingest)
    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap,
        "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    step = PlanStep(
        capability="web_ingest",
        action="discover_ingest",
        params={
            "query": "widget models from catalog",
            "subqueries": ["widget models from catalog"],
            "proposed_type": "Model",
            "attributes": ["name", "provider", "context_length"],
            "hint_columns": ["name", "provider", "context_length"],
            "max_rows": 50,
            "kg_name": "models",
            "provider": "fake",
            "urls": [],
        },
        rationale="ws6",
        confidence=1.0,
    )
    store = InMemoryJobStore()
    ack = await WebIngestCapability().execute(_ctx_with_store(store), step)
    await spawned["task"]

    done = await store.get(ack["job_id"])
    assert done.status == JobStatus.applied

    # SourceBundle / ingest should only see the catalog survivor (role drop + merge).
    flat = [r for batch in ingested_payloads for r in batch]
    names = {r.get("name") for r in flat if isinstance(r, dict)}
    assert "Acme" not in names
    assert len(names) == 1
    assert any("/" in (n or "") for n in names)

    # Stage-trace actions carry counters.
    role_actions = []
    quality_actions = []
    if done.stage_trace is not None:
        for proj in done.stage_trace.projects or []:
            for act in proj.actions or []:
                if getattr(act, "name", None) == "role_membership_gate":
                    role_actions.append(act)
                if getattr(act, "name", None) == "quality_gate":
                    quality_actions.append(act)

    assert role_actions, "expected role_membership_gate stage-trace action"
    assert any((a.meta or {}).get("role_drops", 0) >= 1 for a in role_actions)

    assert quality_actions, "expected quality_gate stage-trace action"
    assert any(
        (a.meta or {}).get("identity_merges", 0) >= 1
        or (a.meta or {}).get("near_dups_merged", 0) >= 1
        for a in quality_actions
    )

    # A1 contract / summary surface run-level counters.
    summary = dict(done.stage_trace.summary or {}) if done.stage_trace else {}
    # Counters may land on summary and/or P1 output contract.
    p1_out = {}
    if done.stage_trace is not None:
        for proj in done.stage_trace.projects or []:
            if getattr(proj, "project_id", None) == "p1" or str(
                getattr(proj, "project_id", "")
            ).endswith("p1"):
                p1_out = dict(getattr(proj, "output", None) or {})
    role_on_surface = (
        summary.get("role_drops")
        or p1_out.get("role_drops")
        or sum((a.meta or {}).get("role_drops", 0) for a in role_actions)
    )
    id_on_surface = (
        summary.get("identity_merges")
        or p1_out.get("identity_merges")
        or sum(
            (a.meta or {}).get("identity_merges", 0)
            or (a.meta or {}).get("near_dups_merged", 0)
            for a in quality_actions
        )
    )
    assert int(role_on_surface or 0) >= 1
    assert int(id_on_surface or 0) >= 1

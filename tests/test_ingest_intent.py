"""OSS seam for the premium ingest-steward capability (INF-600).

The intent name lives in OSS so hosted can ``register_capability`` without a
forked planner. OSS must NOT register the capability itself.
"""

from __future__ import annotations

import asyncio

import pytest

from infona_client.agent import planner as planner_mod
from infona_client.agent.planner import (
    handle,
    register_default_capabilities,
)
from infona_client.agent.planner_intent import (
    _CLASSIFY_SYSTEM,
    _INTENT_PLAN_ORDER,
    _INTENT_TO_CAPABILITY,
    _INGEST_STEWARD_HOSTED_ONLY,
)
from infona_client.agent.registry import (
    AgentContext,
    get_capability,
    reset_capabilities,
)


def test_ingest_intent_maps_to_ingest_steward():
    assert _INTENT_TO_CAPABILITY["ingest"] == "ingest_steward"
    assert "ingest" in _INTENT_PLAN_ORDER
    assert _INTENT_PLAN_ORDER["ingest"] < _INTENT_PLAN_ORDER["discover"]


def test_classify_prompt_has_ingest_bullet_distinct_from_discover():
    assert '- "ingest":' in _CLASSIFY_SYSTEM
    assert "FILE ingest" in _CLASSIFY_SYSTEM
    assert '- "discover":' in _CLASSIFY_SYSTEM
    ingest_at = _CLASSIFY_SYSTEM.index('- "ingest":')
    discover_at = _CLASSIFY_SYSTEM.index('- "discover":')
    assert ingest_at < discover_at


def test_oss_does_not_register_ingest_steward():
    reset_capabilities()
    register_default_capabilities()
    assert get_capability("ingest_steward") is None
    reset_capabilities()


class _FakeNeptune:
    async def query(self, q):
        return {"head": {"vars": []}, "results": {"bindings": []}}

    async def update(self, q):
        return None


def _ctx():
    return AgentContext(
        tenant_id="t1",
        kg_name="kg1",
        neptune=_FakeNeptune(),
        openrouter_key="fake-key",
    )


@pytest.mark.asyncio
async def test_ingest_intent_without_capability_is_hosted_only(monkeypatch):
    reset_capabilities()
    register_default_capabilities()

    async def fake_classify(*_a, **_k):
        return {"intents": ["ingest"], "clarify": ""}

    monkeypatch.setattr(planner_mod, "_classify", fake_classify)
    out = await asyncio.wait_for(handle(_ctx(), "ingest this csv"), 5)
    assert out["kind"] == "answer"
    body = (out.get("answer") or "").lower()
    assert "oss" in body or "hosted" in body or "infona ingest" in body
    assert "csv" in _INGEST_STEWARD_HOSTED_ONLY.lower()
    reset_capabilities()

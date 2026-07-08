"""Reachability tests for the ONTA-235 subscribe capability (agent-intent path).

A conversational "set up a standing weekly alert …" request routes to the
subscribe capability, which persists a recurring ``notify`` :class:`Schedule` row
through the SAME schedule store the canonical ``/schedules`` route uses — no
bespoke endpoint. Uses INVENTED data (Widget type, example.test webhook), so the
mechanism is proven general (no persona tokens).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from cograph_client.agent.planner import (
    handle,
    execute_plan,
    register_default_capabilities,
    reset_plan_store,
)
from cograph_client.agent.registry import (
    AgentContext,
    get_capability,
    reset_capabilities,
)
import cograph_client.agent.planner as planner_mod
from cograph_client.agent.conversation_store import reset_conversation_store
from cograph_client.scheduling.store import InMemoryScheduleStore

TIMEOUT = 5.0


class _FakeNeptune:
    async def query(self, q):
        return {"head": {"vars": []}, "results": {"bindings": []}}

    async def update(self, q):
        return None


def _ctx(schedule_store):
    return AgentContext(
        tenant_id="t1",
        kg_name="widgets-kg",
        neptune=_FakeNeptune(),
        openrouter_key="fake-key",
        anthropic_key="fake-anthropic",
        extras={"schedule_store": schedule_store},
    )


def _stub_classifier(monkeypatch, intent: str):
    async def fake_chat(*args, **kwargs):
        return json.dumps({"intent": intent, "clarify": ""})

    monkeypatch.setattr(planner_mod, "openrouter_chat", fake_chat)


@pytest.fixture(autouse=True)
def _fresh_registry():
    reset_capabilities()
    reset_plan_store()
    reset_conversation_store()
    register_default_capabilities()
    yield
    reset_capabilities()
    reset_plan_store()
    reset_conversation_store()


# ---------------------------------------------------------------------------
# capability is registered + describes itself
# ---------------------------------------------------------------------------


def test_subscribe_capability_registered():
    cap = get_capability("subscribe")
    assert cap is not None
    assert cap.name == "subscribe"
    assert "recurring" in cap.describe().lower()


# ---------------------------------------------------------------------------
# plan → execute persists a notify Schedule via the shared store
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_request_creates_schedule_row(monkeypatch):
    """A conversational standing-alert ask plans a notify schedule; confirming it
    persists a recurring Schedule row through the shared schedule store."""
    # Even if the classifier mis-files this as a plain question, the deterministic
    # subscribe guard forces the subscribe intent (cadence + alert signal present).
    _stub_classifier(monkeypatch, "question")

    store = InMemoryScheduleStore()
    ctx = _ctx(store)

    plan_out = await asyncio.wait_for(
        handle(
            ctx,
            "Set up a standing weekly alert that notifies "
            "https://example.test/hook whenever a Widget changes price.",
        ),
        TIMEOUT,
    )
    assert plan_out["kind"] == "plan"
    step = plan_out["steps"][0]
    assert step["capability"] == "subscribe"
    assert step["action"] == "subscribe"

    plan_id = plan_out["plan_id"]
    result = await asyncio.wait_for(execute_plan(ctx, plan_id), TIMEOUT)
    assert result["kind"] == "result"
    ack = result["steps"][0]
    assert ack["capability"] == "subscribe"
    assert ack["status"] == "ok"
    schedule_id = ack["schedule_id"]

    # A recurring notify Schedule was persisted via the SHARED store — the exact
    # store the canonical /schedules route uses (interface convergence).
    persisted = await store.get(schedule_id)
    assert persisted is not None
    assert persisted.action == "notify"
    assert persisted.tenant_id == "t1"
    assert persisted.kg_name == "widgets-kg"
    assert persisted.interval_seconds == 604_800  # weekly
    assert persisted.enabled is True
    assert persisted.next_run is not None  # computed via compute_next_run
    # The concrete delivery URL rode through into the sink config.
    assert persisted.params["sink"]["url"] == "https://example.test/hook"


@pytest.mark.asyncio
async def test_subscribe_detects_daily_cadence(monkeypatch):
    """Cadence is parsed from the instruction — daily → 86400s interval."""
    _stub_classifier(monkeypatch, "subscribe")
    store = InMemoryScheduleStore()
    ctx = _ctx(store)

    plan_out = await asyncio.wait_for(
        handle(ctx, "Notify me daily when a Sprocket's status changes, automatically."),
        TIMEOUT,
    )
    assert plan_out["kind"] == "plan"
    plan_id = plan_out["plan_id"]
    result = await asyncio.wait_for(execute_plan(ctx, plan_id), TIMEOUT)
    schedule_id = result["steps"][0]["schedule_id"]
    persisted = await store.get(schedule_id)
    assert persisted.interval_seconds == 86_400  # daily


@pytest.mark.asyncio
async def test_subscribe_without_url_still_creates_schedule(monkeypatch):
    """A symbolic target (no concrete URL) still creates the standing trigger —
    delivery is inactive until a URL is added, but the subscribe-able schedule
    exists (the deliverable is a recurring trigger set up once)."""
    _stub_classifier(monkeypatch, "subscribe")
    store = InMemoryScheduleStore()
    ctx = _ctx(store)

    plan_out = await asyncio.wait_for(
        handle(
            ctx,
            "Set up a standing weekly alert to my orchestrator-webhook when a "
            "routed Widget changes price or gets a deprecation date.",
        ),
        TIMEOUT,
    )
    assert plan_out["kind"] == "plan"
    result = await asyncio.wait_for(
        execute_plan(ctx, plan_out["plan_id"]), TIMEOUT
    )
    schedule_id = result["steps"][0]["schedule_id"]
    persisted = await store.get(schedule_id)
    assert persisted.action == "notify"
    # No concrete http(s) URL → no sink config yet, but the schedule is real.
    assert "sink" not in persisted.params
    # The human condition is recorded so a watch-compiler can resolve it later.
    assert "price" in persisted.params["condition"].lower()


# ---------------------------------------------------------------------------
# deterministic subscribe guard
# ---------------------------------------------------------------------------


def test_subscribe_guard_fires_on_standing_alert():
    from cograph_client.agent.planner import _is_subscribe_request

    assert _is_subscribe_request(
        "Set up a standing weekly alert that notifies my webhook when X changes"
    )
    assert _is_subscribe_request(
        "I don't want to re-run this by hand — I want a standing trigger, "
        "a weekly alert."
    )
    # A one-off notify (no cadence) is NOT a subscribe.
    assert not _is_subscribe_request("Notify me the current count of Widgets")
    # A read-only question is never hijacked.
    assert not _is_subscribe_request("What changed this week?")

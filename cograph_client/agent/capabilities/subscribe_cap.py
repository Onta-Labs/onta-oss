"""Subscribe capability — set up a recurring standing alert / weekly refresh
through the agent (ONTA-235).

This is the conversational front door to the ``notify`` schedule: the persona
says "set up a standing weekly alert that notifies my orchestrator whenever a
model I route to changes price or gets a deprecation date" ONCE, and the agent
creates a recurring, subscribe-able schedule that fires on a cadence and delivers
a change payload only when the watched value(s) change. NOT a one-off query.

Convergence: this capability persists the schedule through the SAME schedule store
the canonical ``/graphs/{tenant}/schedules`` route uses (``ctx.extras[
'schedule_store']``), building the identical :class:`Schedule` model and computing
``next_run`` with the identical :func:`compute_next_run` — no bespoke endpoint, no
duplicated recurrence logic. The MCP ``schedule`` tool reaches the SAME route via
the SDK. One canonical operation, reached the same way from every interface.

General by construction — no persona tokens. The cadence, the change condition
(carried as a human note), the watched cells, and the delivery URL are all parsed
from the instruction / passed through; the mechanism works for ANY watched
attribute and ANY sink URL. Where the watch cells can't be derived, the schedule
still records the human ``condition`` so a downstream/premium watch-compiler can
fill the concrete cells later — the schedule is created either way (a standing,
subscribe-able trigger is the deliverable).

Boundary: OSS. Imports only stdlib / ``cograph_client.*``.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import structlog

from cograph_client.agent.registry import AgentContext, PlanStep
from cograph_client.enrichment.models import JobCategory
from cograph_client.scheduling.models import Schedule
from cograph_client.scheduling.next_run import compute_next_run

logger = structlog.stdlib.get_logger("cograph.agent.subscribe")

# Cadence → interval in seconds. Interval is the v1 recurrence (no croniter
# dependency); a cron string could be threaded later without changing this seam.
_CADENCE_SECONDS = {
    "hourly": 3600,
    "daily": 86_400,
    "weekly": 604_800,
    "monthly": 2_592_000,  # 30 days
}
_DEFAULT_CADENCE = "weekly"

# Pull a plain http(s) delivery URL out of the instruction (the persona may paste
# their webhook). A symbolic target like "orchestrator-webhook" is NOT a URL — it
# is kept as a human ``deliver_to`` note and the schedule is still created (the
# concrete URL is wired via a follow-up / the MCP tool / the Explorer).
_URL_RE = re.compile(r"https?://[^\s\"'<>)]+", re.IGNORECASE)


def _detect_cadence(text: str) -> str:
    lower = (text or "").lower()
    for cadence in _CADENCE_SECONDS:
        if cadence in lower:
            return cadence
    # "every week" / "each week" style phrasing → weekly, etc.
    for word, cadence in (
        ("week", "weekly"),
        ("day", "daily"),
        ("hour", "hourly"),
        ("month", "monthly"),
    ):
        if re.search(rf"\b(every|each)\s+{word}\b", lower):
            return cadence
    return _DEFAULT_CADENCE


def _detect_sink_url(instruction: str, ctx: AgentContext) -> Optional[str]:
    """A concrete http(s) delivery URL from the message or attached urls, else None."""
    for url in getattr(ctx, "urls", None) or []:
        if isinstance(url, str) and _URL_RE.match(url.strip()):
            return url.strip()
    m = _URL_RE.search(instruction or "")
    return m.group(0) if m else None


class SubscribeCapability:
    """Set-up-a-standing-alert capability behind the single agent endpoint."""

    name = "subscribe"

    def describe(self) -> str:
        return (
            "Set up a RECURRING standing alert / scheduled refresh that runs on a "
            "cadence (weekly, daily, …) and NOTIFIES / delivers automatically when "
            "watched values CHANGE — so the user sets it once instead of re-running "
            "a query. Use for 'set up a standing weekly alert', 'notify me/my "
            "webhook when X changes', 'a weekly refresh delivered to me "
            "automatically', 'subscribe me to changes in …'. This creates a "
            "recurring trigger, NOT a one-off answer."
        )

    async def plan(self, ctx: AgentContext, instruction: str) -> list[PlanStep]:
        """Propose ONE subscribe step — a recurring ``notify`` schedule.

        Reads the cadence + (optional) concrete delivery URL from the instruction.
        The watched value(s) are carried as a human ``condition`` note (a
        downstream watch-compiler resolves them to concrete cells); the schedule is
        created regardless, because the deliverable is a standing, subscribe-able
        trigger the user sets once — the exact watch expression can be refined
        without another one-off round trip.
        """
        if not ctx.kg_name:
            return []

        cadence = _detect_cadence(instruction)
        interval = _CADENCE_SECONDS[cadence]
        sink_url = _detect_sink_url(instruction, ctx)
        # The human change-condition note — the whole ask, so a downstream/premium
        # watch-compiler (or a human) can see exactly what to watch. Domain-neutral.
        condition = (instruction or "").strip()

        deliver_note = (
            f"delivered to {sink_url}"
            if sink_url
            else "delivery target not yet wired (add a webhook URL to activate delivery)"
        )
        return [
            PlanStep(
                capability=self.name,
                action="subscribe",
                params={
                    "kg_name": ctx.kg_name,
                    "cadence": cadence,
                    "interval_seconds": interval,
                    "condition": condition,
                    # Concrete delivery sink (only when a real URL was supplied).
                    "sink_url": sink_url,
                    # Watch descriptor: the human condition now; concrete cells /
                    # sparql are filled by a downstream watch-compiler or a later
                    # turn. Kept generic so ANY watched attribute works.
                    "watch": {"condition": condition},
                },
                rationale=(
                    f"Create a recurring {cadence} alert on {ctx.kg_name} that "
                    "checks the watched values each run and notifies only when they "
                    f"change — {deliver_note}. You set this once; it recurs on its "
                    "own."
                ),
                confidence=0.75,
                preview={
                    "summary": (
                        f"Set up a standing {cadence} alert on {ctx.kg_name}: on "
                        "each run it re-checks the watched values and delivers a "
                        "change notification only when something changed since the "
                        f"last run ({deliver_note}). Recurring and subscribe-able — "
                        "not a one-off query."
                    ),
                    "cadence": cadence,
                    "condition": condition,
                    "deliver_to": sink_url,
                },
                cost={
                    "paid_calls": 0,
                    "estimated_usd": 0.0,
                    "note": "Scheduling a recurring alert is free (no paid calls).",
                },
            )
        ]

    async def execute(self, ctx: AgentContext, step: PlanStep) -> dict:
        """Persist the recurring ``notify`` schedule via the shared schedule store.

        Builds the IDENTICAL :class:`Schedule` model + computes ``next_run`` the
        canonical ``POST /schedules`` route builds — same store, same model, same
        recurrence math (interface convergence, no bespoke endpoint). Returns an
        ack with the created schedule id so the client can manage it.
        """
        store = ctx.extras.get("schedule_store")
        if store is None:
            raise RuntimeError(
                "schedule_store not available in agent context"
            )
        p = step.params
        kg_name = p.get("kg_name") or ctx.kg_name
        interval = int(p.get("interval_seconds") or _CADENCE_SECONDS[_DEFAULT_CADENCE])
        watch = p.get("watch") or {}
        sink_url = p.get("sink_url")

        params: dict = {"watch": watch, "condition": p.get("condition", "")}
        if sink_url:
            params["sink"] = {"url": sink_url}

        now = datetime.now(timezone.utc)
        schedule = Schedule(
            id=str(uuid.uuid4()),
            tenant_id=ctx.tenant_id,
            kg_name=kg_name,
            # notify creates no enrich-style job; category is carried for the model
            # + the unified feed only. enrichment is a neutral default.
            category=JobCategory.enrichment,
            action="notify",
            params=params,
            interval_seconds=interval,
            enabled=True,
            created_at=now,
        )
        schedule.next_run = compute_next_run(schedule, now)
        await store.create(schedule)
        logger.info(
            "agent_subscribe_created",
            tenant=ctx.tenant_id,
            kg=kg_name,
            schedule_id=schedule.id,
            cadence=p.get("cadence"),
            has_sink=bool(sink_url),
        )
        return {
            "kind": "ack",
            "capability": self.name,
            "action": step.action,
            "schedule_id": schedule.id,
            "cadence": p.get("cadence"),
            "next_run": schedule.next_run.isoformat() if schedule.next_run else None,
            "message": (
                f"Created a standing {p.get('cadence', 'recurring')} alert on "
                f"{kg_name}. It runs on its own and notifies when the watched "
                "values change"
                + (f", delivering to {sink_url}." if sink_url else
                   " (add a webhook URL to activate automatic delivery).")
            ),
        }

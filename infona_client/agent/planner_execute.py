"""Confirm / execute path for the agent planner.

Looks up ``get_capability`` / ``order_steps`` / ``register_capability``
on the :mod:`infona_client.agent.planner` facade at call time so existing
monkeypatches keep working.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import structlog

from infona_client.agent.conversation_store import (
    Turn,
    make_conversation_store,
)
from infona_client.agent.kg_scope import CODE_KG_MISSING, check_kg_scope
from infona_client.agent.plan_store import (
    StoredPlan,
    claim_plan_for_execution,
    make_plan_store,
)
from infona_client.agent.planner_intent import _INTENT_PLAN_ORDER
from infona_client.agent.registry import (
    AgentContext,
    ReadOnlyMembershipError,
    mutating_step_capabilities,
)

logger = structlog.stdlib.get_logger("infona.agent.planner")


def _host():
    """Call-time lookup of the public planner module (monkeypatch surface)."""
    from infona_client.agent import planner as _mod

    return _mod


# One-shot confirm guard: how long an ``executing`` claim on a plan stays
# exclusive before a re-confirm may assume the executor died mid-run (crash,
# redeploy) and claim it again. execute_plan itself finishes in seconds (long
# work is spawned as background jobs), so anything past this is a dead claim,
# not a slow one. Env-overridable so ops can retune without a deploy.
_EXECUTING_STALE_S = float(os.environ.get("INFONA_PLAN_EXECUTING_STALE_S", "600"))

def _assert_may_commit(ctx: AgentContext, steps: list) -> None:
    """Refuse to commit a mutating plan for a read-only member (ONTA-451).

    ``/agent`` is the one read/write MIXED surface: the same endpoint answers a
    question and ingests a dataset, so a blanket ``require_tenant_write`` route
    dependency would lock a reader out of the read-only turns their role is
    supposed to allow. The gate therefore sits at CAPABILITY DISPATCH — here,
    where a mutating plan is about to be persisted, and again in
    :func:`execute_plan`, the only path that actually runs one.

    Everything upstream of this point is read-only by contract: the classifier,
    ``QueryCapability.answer``, a capability's ``plan()`` (protocol: "NO
    writes"), and the ``answer`` / ``clarify`` short-circuits — all of which
    return before this call. So a reader keeps questions, web research, ontology
    inspection and clarify rounds, and loses exactly the mutations.

    Capability classification is deny-by-default
    (:func:`~infona_client.agent.registry.capability_writes`): an unknown or
    undeclared capability counts as mutating.

    Scope note: this governs PLAN STEPS. The question fast-path in
    :func:`_respond` dispatches ``_host().get_capability("query").answer`` by NAME
    without consulting ``capability_writes`` — sound in OSS because
    ``QueryCapability.answer`` only reads, but a downstream that replaces the
    ``query`` capability with a writing one would not be gated by this. Give a
    replacement the same read-only contract, or gate it there too.
    """
    if ctx.can_write():
        return
    blocked = mutating_step_capabilities(steps)
    if not blocked:
        return
    logger.info(
        "agent_write_denied_read_only",
        tenant=ctx.tenant_id,
        kg=getattr(ctx, "kg_name", ""),
        capabilities=blocked,
    )
    raise ReadOnlyMembershipError(blocked)


async def _assert_confirm_may_commit(ctx: AgentContext, store, plan_id: str) -> None:
    """The ONTA-451 authorization gate for a CONFIRM. Fails CLOSED.

    Re-checked here and not only at plan time because a plan can sit
    un-confirmed indefinitely: one persisted while the caller had write access
    must not stay runnable after their role is downgraded to reader.

    Runs BEFORE ``claim_for_execution`` (like the KG-scope gate) so a refused
    confirm leaves the plan ``proposed`` and re-confirmable by a writer, rather
    than stranding it in ``executing`` until the stale cutoff.

    Unlike the KG-scope gate — best-effort, deferring to the authoritative claim
    read — the PLAN READ here fails closed: a plan that cannot be read is a plan
    that cannot be shown to be read-only, so a read-only caller is refused rather
    than allowed through to the claim.

    That is a property of this read only, not of the gate end-to-end: the ROLE
    this depends on is resolved by ``resolve_member_role``, which deliberately
    fails OPEN to ``writer`` on a workspace-registry outage so a DB blip cannot
    freeze production for entitled users. Same posture as ``require_tenant_write``
    — this change neither introduces nor worsens it.
    """
    if ctx.can_write():
        return
    try:
        plan = await store.get(plan_id, ctx.tenant_id)
    except Exception as exc:  # noqa: BLE001 — unreadable ⇒ unprovable ⇒ refuse
        logger.warning(
            "agent_plan_capability_read_failed", plan_id=plan_id, exc_info=True
        )
        raise ReadOnlyMembershipError() from exc
    if plan is None:
        # Nothing to run anyway; refusing rather than reporting "not found" also
        # keeps the confirm path from being a plan-existence oracle for a reader.
        raise ReadOnlyMembershipError()
    _assert_may_commit(ctx, plan.steps)


async def _plan_intents(
    ctx: AgentContext,
    available: list[tuple[str, object]],
    instruction: str,
) -> list:
    """Plan each requested capability and compose them into one ordered plan.

    Capabilities are planned clean-first (``_INTENT_PLAN_ORDER``) so a "clean and
    dedup"/"clean and enrich" ask wires the dedup/enrich step's ``depends_on`` to
    the clean (normalize) step(s) — the documented clean-before-* pattern — and
    :func:`order_steps` then runs normalize first. A capability that can't ground
    a concrete step (returns ``[]``) simply contributes nothing; as long as ANY
    requested capability produces a step the turn converges to a plan instead of
    re-asking. A single requested intent collapses to exactly the prior
    single-capability behavior (no cross-capability dependency is added).
    """
    available = sorted(
        available, key=lambda pair: _INTENT_PLAN_ORDER.get(pair[0], 9)
    )
    all_steps: list = []
    normalize_ids: list[str] = []
    for intent, cap in available:
        steps = await cap.plan(ctx, instruction)  # type: ignore[attr-defined]
        if not steps:
            continue
        # A capability can ask for a brief clarification instead of proposing a
        # plan (e.g. enrich couldn't resolve a described subset, or the scope
        # matched 0 entities). Surface that immediately rather than composing a
        # partial plan around it — the user's reply re-runs resolution next turn.
        if len(steps) == 1 and getattr(steps[0], "action", "") == "clarify":
            return steps
        if intent in ("dedup", "enrich") and normalize_ids:
            for s in steps:
                s.depends_on = list(dict.fromkeys([*s.depends_on, *normalize_ids]))
        if intent == "clean":
            normalize_ids.extend(
                s.id for s in steps if s.capability == "normalize"
            )
        all_steps.extend(steps)
    return all_steps


async def _load_history(ctx: AgentContext, session_id: str | None) -> list[Turn]:
    """Load the session transcript; never fail the turn on a store hiccup."""
    if not session_id:
        return []
    try:
        return await make_conversation_store().load(session_id, ctx.tenant_id)
    except Exception:  # noqa: BLE001 — a transcript read must never 500 the turn
        logger.warning("agent_history_load_failed", exc_info=True)
        return []


def _result_summary(result: dict) -> tuple[str, str | None]:
    """Derive (assistant_text, intent_label) to store for an agent response."""
    kind = result.get("kind")
    if kind == "clarify":
        return result.get("question", ""), None
    if kind == "answer":
        # Store the human summary, not the raw bindings dump: `narrative` is the
        # rephrased 2-3 sentence answer; `answer` is the formatted-table fallback
        # (only stored when the rephrase failed open to ""). Clients re-render
        # this text verbatim on thread reload, so the precedence matters.
        return result.get("narrative") or result.get("answer") or "", "question"
    if kind == "plan":
        caps = ", ".join(
            dict.fromkeys(s.get("capability", "") for s in result.get("steps", []))
        )
        return f"Proposed a plan ({caps}).", caps or None
    return "", None


async def _record_turn(
    ctx: AgentContext,
    session_id: str | None,
    message: str,
    result: dict,
    owner: str | None = None,
) -> None:
    """Append the user message + assistant reply to the session transcript.

    ``owner`` (the auth subject) tags the thread so a signed-in user can find it
    in their history; it's None for ownerless (demo) sessions.

    Both turns are stamped with the knowledge graph they were made against
    (ONTA-419) so a later turn on a different graph can scope them out.
    """
    if not session_id:
        return
    text, intent = _result_summary(result)
    kg_name = getattr(ctx, "kg_name", None) or None
    turns = [
        Turn(role="user", text=message, kg_name=kg_name),
        Turn(
            role="assistant",
            text=text,
            kind=result.get("kind"),
            intent=intent,
            kg_name=kg_name,
        ),
    ]
    try:
        await make_conversation_store().append(
            session_id, ctx.tenant_id, turns, owner=owner
        )
    except Exception:  # noqa: BLE001 — persistence is best-effort, never 500
        logger.warning("agent_history_append_failed", exc_info=True)


async def execute_plan(ctx: AgentContext, plan_id: str) -> dict:
    """Run a persisted plan's steps in dependency order. The ONLY mutating path.

    ONE-SHOT: a plan is executable exactly once. The confirm claims the plan
    via an atomic status transition (``proposed`` → ``executing``) in the plan
    store, so a duplicate confirm — the Explorer auto-confirm firing twice, a
    client retry after a gateway timeout whose first request DID spawn the
    work — can never run the steps again (a re-run re-issues the paid provider
    fan-out and re-ingests every row; discovery/enrich executes are not
    idempotent). A re-confirm of a finished plan replays the persisted
    acks/job ids verbatim, marked ``"replayed": True``, so a retried confirm
    converges to the same response; a confirm that races a still-running
    execution gets ``{kind:"error", code:"plan_already_executing"}``. A claim
    older than ``INFONA_PLAN_EXECUTING_STALE_S`` (default 600s — the executor
    presumably died mid-run) is claimable again so a crash cannot strand the
    plan un-runnable forever.

    Each step runs via its capability's ``execute`` (long work is spawned as a
    background job inside the capability). Records per-step status; the result
    payload is persisted on the plan for the duplicate-confirm replay above.
    """
    store = make_plan_store()
    # Authorization before validation ON THIS PATH: a refused confirm is a plain
    # 403 rather than a KG-scope error about a plan the caller could never run.
    # (The plan-time path deliberately runs check_kg_scope first — a reader may
    # list graphs, so its clarify reveals nothing the read routes don't.)
    await _assert_confirm_may_commit(ctx, store, plan_id)
    gate = await _kg_scope_gate_for_confirm(ctx, store, plan_id)
    if gate is not None:
        return gate
    stale_before = datetime.now(timezone.utc) - timedelta(
        seconds=_EXECUTING_STALE_S
    )
    plan, claimed = await claim_plan_for_execution(
        store, plan_id, ctx.tenant_id, stale_before=stale_before
    )
    if plan is None:
        return {"kind": "error", "error": "plan not found", "plan_id": plan_id}
    if not claimed:
        logger.info(
            "agent_plan_duplicate_confirm", plan_id=plan_id, status=plan.status
        )
        return _already_confirmed_response(plan)

    summaries: list[dict] = []
    try:
        ordered = _host().order_steps(plan.steps)
        for step in ordered:
            cap = _host().get_capability(step.capability)
            if cap is None:
                summaries.append(
                    {
                        "step_id": step.id,
                        "capability": step.capability,
                        "status": "skipped",
                        "error": "capability not registered",
                    }
                )
                continue
            try:
                result = await cap.execute(ctx, step)
                # Spread the capability ack first, then stamp the orchestration
                # status LAST so a capability's own "status" field (e.g. a job's
                # "queued") can't clobber the step-level success marker.
                summaries.append({"step_id": step.id, **result, "status": "ok"})
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "agent_step_failed",
                    step_id=step.id,
                    capability=step.capability,
                    exc_info=True,
                )
                summaries.append(
                    {
                        "step_id": step.id,
                        "capability": step.capability,
                        "status": "failed",
                        "error": str(exc),
                    }
                )
    except Exception:
        # Catastrophic (non-step) failure. Persist the terminal status so the
        # one-shot guard still refuses a re-confirm — steps that DID run may
        # have spawned paid work, so a retry could double-bill. Best-effort: if
        # even this save fails, the stale-claim cutoff above is the backstop.
        plan.status = "failed"
        try:
            await store.save(plan)
        except Exception:  # noqa: BLE001
            logger.warning(
                "agent_plan_fail_persist_failed", plan_id=plan_id, exc_info=True
            )
        raise
    result = {"kind": "result", "plan_id": plan_id, "steps": summaries}
    plan.status = "done"
    plan.result = result
    try:
        await store.save(plan)
    except Exception:  # noqa: BLE001
        # The steps RAN — a store blip on this save must not turn a successful
        # execution into a 500 that discards the acks/job ids (the client would
        # treat the confirm as failed and retry; past the stale cutoff that
        # retry would re-run paid work). Return the result anyway: the caller
        # converges, and the plan merely stays `executing` (no replay) until
        # the stale cutoff — the documented backstop for a lost terminal save.
        logger.warning(
            "agent_plan_done_persist_failed", plan_id=plan_id, exc_info=True
        )
    return result


async def _kg_scope_gate_for_confirm(ctx, store, plan_id: str) -> dict | None:
    """Re-check the KG scope of a plan at CONFIRM time (ONTA-426 / ONTA-428).

    ``execute_plan`` is the only mutating path, so the guarantee "no /agent turn
    writes to a graph that does not exist" has to hold here too, not only at plan
    time. Two things this catches that the plan-time gate cannot:

    * the graph existed when the plan was proposed and was deleted before the user
      confirmed (a plan can sit un-confirmed indefinitely);
    * a confirm whose request body carries NO ``context.kg_name``, in which case
      the plan's own recorded scope is restored onto the context instead of
      letting the execution fall through to the tenant base graph.

    This VALIDATES, it never re-resolves (``resolve_omitted=False``). A plan
    proposed with no KG scope at all was already gated when it was proposed, in a
    workspace that had no graphs to choose between; inferring one now, because the
    workspace has since grown a graph, would silently retarget a plan the user
    already approved, and would do it without the ``kg_scope_note`` the plan-time
    path attaches.

    Runs BEFORE ``claim_for_execution`` deliberately: a refused confirm must leave
    the plan ``proposed`` and re-confirmable once the user creates (or corrects)
    the graph, rather than stranding it in ``executing`` until the stale cutoff.
    Returns ``None`` to proceed, or a typed ``{"kind": "error"}`` payload.
    ``execute_plan``'s contract is ``{kind: result|error}``, so a clarify would be
    off-contract here even though the plan-time gate returns one.

    Best-effort: an unreadable plan (or any store hiccup) falls through to the
    normal claim path, which reports "plan not found" as it always did.
    """
    try:
        plan = await store.get(plan_id, ctx.tenant_id)
    except Exception:  # noqa: BLE001 - the claim below is the authoritative read
        logger.warning("agent_plan_scope_read_failed", plan_id=plan_id, exc_info=True)
        return None
    if plan is None or plan.status != "proposed":
        # Not found, or already claimed/finished. Leave the duplicate-confirm
        # replay and the not-found response exactly as they were.
        return None
    if not getattr(ctx, "kg_name", "") and plan.kg_name:
        ctx.kg_name = plan.kg_name
    caps = [s.capability for s in plan.steps]
    clarify = await check_kg_scope(ctx, caps, resolve_omitted=False)
    if clarify is None:
        return None
    logger.info("agent_plan_kg_scope_refused", plan_id=plan_id, kg_name=ctx.kg_name)
    return {
        "kind": "error",
        # The gate's own typed reason (kg_missing / kg_ambiguous) rather than a
        # second, hardcoded copy that could drift from it.
        "code": clarify.get("code", CODE_KG_MISSING),
        "error": clarify.get("question", ""),
        "plan_id": plan_id,
        "options": clarify.get("options", []),
    }


def _already_confirmed_response(plan: StoredPlan) -> dict:
    """The duplicate-confirm response for a plan another confirm already claimed.

    Finished with a persisted result → replay the SAME acks/job ids (marked
    ``replayed`` so clients/telemetry can tell) — a retried confirm converges
    instead of erroring. Still executing → a typed error; the work is already
    in flight and its jobs are visible on the jobs feed. Finished without a
    persisted result (a catastrophic failure, or a plan finished by a build
    predating result persistence) → a typed error. Nothing re-runs in any case.
    """
    if plan.result is not None:
        return {**plan.result, "replayed": True}
    if plan.status == "executing":
        return {
            "kind": "error",
            "code": "plan_already_executing",
            "error": (
                "This plan is already being executed — the first confirm is "
                "still in flight, and confirming again will not run it twice. "
                "Check the running jobs for progress."
            ),
            "plan_id": plan.plan_id,
            "status": plan.status,
        }
    return {
        "kind": "error",
        "code": "plan_already_executed",
        "error": (
            f"This plan already ran (status: {plan.status}) and cannot run "
            "again. Ask again to get a fresh plan if you want to repeat it."
        ),
        "plan_id": plan.plan_id,
        "status": plan.status,
    }


def _new_plan_id() -> str:
    import uuid

    return str(uuid.uuid4())

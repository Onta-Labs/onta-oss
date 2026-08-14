"""Classify → dispatch: handle / _respond.

Looks up ``openrouter_chat`` / ``get_capability`` / ``order_steps`` /
``_classify`` on the :mod:`infona_client.agent.planner` facade at call
time so existing monkeypatches keep working. The hosted-only web-ingest
discover path stays here (web_ingest is not in OSS).
"""
from __future__ import annotations

import structlog

from infona_client.agent.capabilities.query import QueryCapability
from infona_client.agent.conversation_store import Turn
from infona_client.agent.kg_scope import (
    CTX_KG_RESOLVED,
    check_kg_scope,
    resolved_kg_note,
)
from infona_client.agent.plan_store import StoredPlan, make_plan_store
from infona_client.agent.planner_execute import (
    _assert_may_commit,
    _load_history,
    _new_plan_id,
    _plan_intents,
    _record_turn,
)
from infona_client.agent.planner_history import (
    _effective_instruction,
    _recent_window,
    _same_kg_turns,
)
from infona_client.agent.planner_intent import (
    _DEFAULT_ACTION_OPTIONS,
    _INTENT_TO_CAPABILITY,
    _hosted_only_web_ingest_answer,
    _is_interrogative,
    _is_refresh_existing_request,
    _is_subscribe_request,
    _is_web_discovery_request,
    _message_has_urls,
    _url_intent,
)
from infona_client.agent.registry import AgentContext

logger = structlog.stdlib.get_logger("infona.agent.planner")


def _host():
    """Call-time lookup of the public planner module (monkeypatch surface)."""
    from infona_client.agent import planner as _mod

    return _mod


async def handle(ctx: AgentContext, message: str, session: dict | None = None) -> dict:
    """Classify the message and respond — answer, clarify, or propose a plan.

    Multi-turn aware (COG-130): when ``session.id`` is supplied, the running
    transcript is loaded and threaded into both the classifier and the
    capabilities so a clarify→answer exchange converges instead of looping. Each
    turn (the user message + the assistant's reply) is appended to the session's
    transcript. NO data writes happen here — an action returns a persisted plan
    the caller confirms via :func:`execute_plan`.

    KG-aware (ONTA-419): one session id can span several knowledge graphs, so
    history is scoped to ``ctx.kg_name`` before it grounds the classifier, the
    accumulation window, or the clarify-convergence guard.
    """
    session_id = (session or {}).get("id")
    owner = (session or {}).get("owner")
    history = await _load_history(ctx, session_id)
    # Count only the clarify rounds spent on THIS graph (ONTA-419). The guard
    # exists to stop the agent re-asking the SAME question; clarifies spent on
    # another graph are a different ask, and letting them count would force the
    # classifier to commit blind on the first message against a new graph.
    prior_clarify_count = sum(
        1
        for t in _same_kg_turns(history, getattr(ctx, "kg_name", None))
        if t.role == "assistant" and t.kind == "clarify"
    )

    result = await _respond(ctx, message, session_id, history, prior_clarify_count)

    # ONTA-426: when the turn named no knowledge graph and the gate inferred one,
    # SAY so on the response. The inference is only ever made when the workspace
    # has exactly one graph (there is nothing to get wrong), but the user still has
    # to be able to see which dataset an action landed on. An unannounced default
    # is the silent-scoping bug in a politer costume.
    resolved = (getattr(ctx, "extras", None) or {}).get(CTX_KG_RESOLVED)
    if resolved:
        result.setdefault("kg_name", resolved)
        result.setdefault("kg_scope_note", resolved_kg_note(resolved))

    await _record_turn(ctx, session_id, message, result, owner)
    if session_id:
        result.setdefault("session_id", session_id)
    return result


async def _respond(
    ctx: AgentContext,
    message: str,
    session_id: str | None,
    history: list[Turn],
    prior_clarify_count: int,
) -> dict:
    """The classify → dispatch core, factored out of transcript bookkeeping."""
    # Expose the running clarify count to capabilities so a capability that asks
    # its own clarifying question (e.g. web discovery confirming which attributes
    # to collect) can commit to its suggested default instead of re-asking once
    # it has already asked — the capability-level analogue of the classifier's
    # _MAX_CLARIFY_ROUNDS guard.
    ctx.extras["prior_clarify_count"] = prior_clarify_count
    # Expose the CURRENT message (distinct from the accumulated instruction) so a
    # capability can prefer a target named in the live turn over one that appears
    # only in prior-turn history — the enrich cap uses this to resolve the target
    # TYPE from what the user just said, not a stale mention still in the window.
    ctx.extras["current_message"] = message
    # Only the recent tail grounds the prompt — a long history-backed thread
    # shouldn't blow up the classifier context (COG-131). The tail is chosen
    # KG-aware (ONTA-419) so a long detour onto another graph can't push this
    # graph's own open ask off the end of the window.
    recent = _recent_window(history, getattr(ctx, "kg_name", None))
    classification = await _host()._classify(ctx, message, recent, prior_clarify_count)
    intents = classification.get("intents", ["ambiguous"])

    # Deterministic web-discovery override: an explicit "… from the web" ingest
    # (or a "discover …" / "new discovery, not enrichment" framing — ONTA-244)
    # must route to discovery even if the classifier filed it as question /
    # ambiguous (the payload often reads like a query — "list … with …") or
    # word-triggered "enrich" (a "discover … then enrich each" ask contains the
    # word "enrich"). Force discover and drop the read-only intents so it can't be
    # hijacked by the question fast-path below. We ALSO drop a co-classified
    # "enrich" here: on a mint-new-records ask the entities do not exist yet, so an
    # enrich pass in the SAME turn would match 0 and premature-clarify (the
    # enrich-plan-order-1 short-circuit that beat discovery). Discovery mints them
    # first; enriching the fresh entities is a natural follow-up turn.
    # Always rewrite intents here (even if web_ingest is unregistered) so later
    # guards — subscribe, URL, refresh — still run. The hosted-only answer is
    # emitted later if discover is the only unresolved intent. An early return
    # would swallow a cadence+alert "from the web" subscribe ask in OSS.
    if _is_web_discovery_request(message):
        intents = [
            "discover",
            *[
                i
                for i in intents
                if i not in ("discover", "enrich", "question", "ambiguous")
            ],
        ]

    # Deterministic refresh-EXISTING override (ONTA-245): a "refresh / re-verify /
    # update the <attrs> for <existing subset>" ask is ENRICHMENT in re-verify
    # mode — it re-confirms values on records that ALREADY exist and advances their
    # freshness stamp, scoped to the matching subset. The LLM classifier keeps
    # mis-filing it as "discover" (the goal reads like "pull current numbers from
    # the web for OpenAI, Google, …"), which mints a fresh dataset instead of
    # refreshing the existing rows (the reported gap: ran_enrich=false,
    # ran_build=true). Force enrich to the front and DROP a mis-classified
    # discover/question so the refresh path fires regardless of the LLM. Deliberately
    # defers to BOTH the web-discovery guard above (a genuine "… from the web" /
    # leading "discover" mint-new still wins) and the subscribe guard below (a
    # recurring standing "weekly refresh" still routes to subscribe) — so this only
    # rescues the plain scoped-refresh case the classifier drops. Only when enrich
    # is registered.
    if (
        _is_refresh_existing_request(message)
        and not _is_web_discovery_request(message)
        and not _is_subscribe_request(message)
        and _host().get_capability(_INTENT_TO_CAPABILITY["enrich"]) is not None
    ):
        intents = [
            "enrich",
            *[
                i
                for i in intents
                if i not in ("enrich", "discover", "question", "ambiguous")
            ],
        ]

    # Deterministic subscribe override: an unmistakable "set up a standing/
    # recurring alert on a cadence, delivered automatically" ask must route to the
    # subscribe capability even if the classifier filed it as a plain question /
    # research (the payload reads like "notify me whenever …"). Force subscribe to
    # the front and drop the read-only intents so the question fast-path can't
    # hijack it. Only when the subscribe capability is registered.
    if _is_subscribe_request(message) and _host().get_capability(
        _INTENT_TO_CAPABILITY["subscribe"]
    ) is not None:
        intents = [
            "subscribe",
            *[
                i
                for i in intents
                if i not in ("subscribe", "question", "research", "ambiguous")
            ],
        ]

    # Deterministic links-to-parse override: when the user hands us explicit URLs
    # (in the message or as structured request context), this is a URL-targeted
    # web extraction, not a plain question — route it by intent ourselves. An
    # enrich-type verb fills attributes on existing entities ("enrich"); anything
    # else brings in a NEW set of records ("discover"). Force the chosen intent to
    # the front and drop question/ambiguous so it can't be hijacked by the
    # question fast-path below. Only when the target capability is registered.
    # A subscribe/standing-alert request whose URL is the DELIVERY webhook (not a
    # page to scrape) must NOT be hijacked by the links-to-parse override below —
    # "notify https://ex/hook when X changes" is a delivery target, not an ingest
    # source. When the subscribe override already fired, skip the URL routing.
    _subscribe_forced = intents and intents[0] == "subscribe"
    _ctx_urls = getattr(ctx, "urls", None)
    if not _subscribe_forced and _message_has_urls(message, _ctx_urls):
        url_intent = _url_intent(message)
        # Don't hijack a genuine read-only question that merely contains a link in
        # its TEXT (e.g. "what does https://acme/about say about pricing?"). A link
        # ATTACHED as structured request context (ctx.urls) or an explicit enrich
        # verb is an unambiguous action and still routes; a bare interrogative
        # whose only action signal is a URL falls through to the classifier, which
        # answers it.
        defer_to_classifier = (
            not _ctx_urls
            and url_intent != "enrich"
            and _is_interrogative(message)
        )
        if not defer_to_classifier:
            url_cap = _host().get_capability(_INTENT_TO_CAPABILITY[url_intent])
            if url_cap is None and url_intent == "discover":
                # URL-bearing mint-new must not fall through to /ask in OSS.
                return _hosted_only_web_ingest_answer()
            if url_cap is not None:
                intents = [
                    url_intent,
                    *[
                        i
                        for i in intents
                        if i not in (url_intent, "question", "ambiguous")
                    ],
                ]

    # A read-only question is terminal and does not compose with actions.
    if "question" in intents:
        cap = _host().get_capability("query") or QueryCapability()
        out = await cap.answer(ctx, message)  # type: ignore[attr-defined]
        # The capability may return its OWN kind. ONTA-413 short-circuits a
        # question about a NONEXISTENT kg_name to {kind:"clarify"} naming the
        # available graphs, instead of an "answer" that is really a silent
        # "No results found." Defaulting to "answer" keeps every other path
        # byte-identical.
        return {**out, "kind": out.get("kind", "answer")}

    actionable = [i for i in intents if i in _INTENT_TO_CAPABILITY]
    if not actionable:
        return {
            "kind": "clarify",
            "question": classification.get("clarify")
            or "Could you clarify what you'd like me to do?",
            # Offer the model's own choices when it gave them, else the generic
            # action menu — so the user can click instead of typing.
            "options": classification.get("options") or list(_DEFAULT_ACTION_OPTIONS),
        }

    # Resolve the registered capabilities. A recognized intent with no capability
    # registered in THIS deployment (a downstream may map an intent it hasn't
    # registered) is skipped; if none resolve, clarify rather than fail.
    available = [
        (i, _host().get_capability(_INTENT_TO_CAPABILITY[i])) for i in actionable
    ]
    available = [(i, c) for i, c in available if c is not None]
    if not available:
        if actionable[0] == "discover":
            return _hosted_only_web_ingest_answer()
        return {
            "kind": "clarify",
            "question": (
                f"I can't yet handle '{actionable[0]}' requests. I can answer "
                "questions, enrich attributes, clean up values, merge duplicates, "
                "and inspect or extend the ontology — what would you like?"
            ),
            "options": list(_DEFAULT_ACTION_OPTIONS),
        }

    # ONTA-426 / ONTA-428: resolve and validate the KG scope BEFORE any capability
    # plans. A typo'd kg_name would otherwise plan (and, after a confirm, run) work
    # against a graph that does not exist (SPARQL returns zero rows rather than an
    # error, so the turn reports success over nothing), and an OMITTED kg_name
    # would fall through to the tenant base graph, acting on a dataset the user
    # never named. Returns a clarify to surface either case; may resolve an omitted
    # name to the workspace's only graph. See agent/kg_scope.py for the policy and
    # for why this lives here rather than at the kg_writer seam.
    scope_clarify = await check_kg_scope(ctx, [c for _, c in available])
    if scope_clarify is not None:
        return scope_clarify

    # Accumulate the user's answers across the dialogue so each capability's
    # field/attribute extraction sees the full ask, not just the latest reply.
    instruction = _effective_instruction(
        recent, message, getattr(ctx, "kg_name", None)
    )
    steps = await _plan_intents(ctx, available, instruction)
    if not steps:
        labels = " and ".join(i for i, _ in available)
        return {
            "kind": "clarify",
            "question": (
                f"I understood you want to {labels}, but I couldn't determine the "
                "specifics (which field/attribute and value). Could you be more "
                "specific?"
            ),
        }

    # Read-only answer step: a capability may answer a question-like request
    # directly (e.g. the ontology capability's INSPECT op renders the schema)
    # instead of proposing a mutation. Such a step carries action="answer" and an
    # ``answer_payload``; surface it as {kind:"answer"} (no confirm round-trip),
    # exactly like the question fast-path. Only a SINGLE no-write answer step
    # short-circuits — a mutation plan always goes through confirm.
    if len(steps) == 1 and steps[0].action == "answer":
        payload = steps[0].params.get("answer_payload")
        if payload is not None:
            return {"kind": "answer", **payload}

    # A capability may need one round of clarification before it can plan — e.g.
    # enrich couldn't pin down WHICH entities the user means, or web discovery
    # needs to confirm which attributes to collect. A lone clarify step
    # short-circuits to {kind:"clarify"} (the same shape the classifier emits) so
    # the panel renders the question + clickable options; the running transcript
    # accumulates the user's reply, so next turn the capability re-resolves.
    if len(steps) == 1 and steps[0].action == "clarify":
        p = steps[0].params
        out = {
            "kind": "clarify",
            "question": p.get("question")
            or "Could you clarify which entities you mean?",
            "options": p.get("options") or [],
        }
        # A capability may ask SEVERAL questions (each with its own options) — pass
        # the structured list through for clients that render more than one; the
        # singular question/options above keep older clients working.
        if p.get("questions"):
            out["questions"] = p["questions"]
        return out

    _assert_may_commit(ctx, steps)

    steps = _host().order_steps(steps)
    plan_id = _new_plan_id()
    await _host().make_plan_store().save(
        StoredPlan(
            plan_id=plan_id,
            tenant_id=ctx.tenant_id,
            kg_name=ctx.kg_name,
            type_name=ctx.type_name,
            message=message,
            steps=steps,
            session_id=session_id,
        )
    )
    return {
        "kind": "plan",
        "plan_id": plan_id,
        "steps": [s.to_dict() for s in steps],
    }

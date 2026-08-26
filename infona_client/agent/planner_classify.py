"""Intent classifier (one bounded LLM call) for the agent planner.

Looks up ``openrouter_chat`` / ``get_capabilities`` on the
:mod:`infona_client.agent.planner` facade at call time so existing
monkeypatches keep working.
"""
from __future__ import annotations

import json

import structlog

from infona_client.agent.conversation_store import Turn
from infona_client.agent.planner_history import _format_history
from infona_client.agent.planner_intent import (
    _CLASSIFY_SYSTEM,
    _DEFAULT_ACTION_OPTIONS,
)
from infona_client.agent.registry import AgentContext
from infona_client.obs import timed
from infona_client.resolver.llm_router import PRIMARY_MODEL
from infona_client.skills.inject import entitled_from
from infona_client.skills.resolve import skills_prompt_block

logger = structlog.stdlib.get_logger("infona.agent.planner")

# Convergence guard (COG-130): once the agent has asked this many clarifying
# questions in a session, the classifier is told to STOP asking and commit.
_MAX_CLARIFY_ROUNDS = 1


def _host():
    """Call-time lookup of the public planner module (monkeypatch surface)."""
    from infona_client.agent import planner as _mod

    return _mod


async def _classify(
    ctx: AgentContext,
    message: str,
    history: list[Turn] | None = None,
    prior_clarify_count: int = 0,
) -> dict:
    """One bounded LLM call → {"intents": [...], "clarify": ...}.

    Sees the running transcript (``history``) so a terse answer to a prior
    clarify is classified in context instead of in isolation. On any error /
    missing key we degrade to "ambiguous" with a generic clarify so the agent
    never 500s on classification.
    """
    caps = "\n".join(f"- {c.name}: {c.describe()}" for c in _host().get_capabilities())
    convo = _format_history(history, getattr(ctx, "kg_name", None))
    guard = ""
    if prior_clarify_count >= _MAX_CLARIFY_ROUNDS:
        guard = (
            f"You have ALREADY asked {prior_clarify_count} clarifying "
            "question(s) in this conversation and the user has responded. Do NOT "
            "ask again — use their answers above and commit to the intent(s).\n\n"
        )
    user = (
        f"Available capabilities:\n{caps}\n\n{convo}{guard}"
        f"Latest user message: {message}"
    )
    type_name = getattr(ctx, "type_name", None)
    if type_name:
        block = await skills_prompt_block(
            [type_name],
            tenant_id=ctx.tenant_id,
            entitled=entitled_from(ctx),
        )
        if block:
            user = f"{user}\n\n{block}"
    if not ctx.openrouter_key:
        return _ambiguous()
    try:
        async with timed(logger, "classify"):
            text = await _host().openrouter_chat(
                ctx.openrouter_key,
                _CLASSIFY_SYSTEM,
                user,
                model=PRIMARY_MODEL,
                temperature=0,
                max_tokens=200,
                timeout=30,
            )
    except Exception:
        logger.warning("agent_classify_failed", exc_info=True)
        return _ambiguous()
    return _parse_classification(text)


def _ambiguous(clarify: str = "What would you like me to do?") -> dict:
    return {
        "intents": ["ambiguous"],
        "clarify": clarify,
        "options": list(_DEFAULT_ACTION_OPTIONS),
    }


def _parse_classification(text: str) -> dict:
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = "\n".join(
            l for l in stripped.split("\n") if not l.strip().startswith("```")
        )
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        stripped = stripped[start : end + 1]
    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return _ambiguous()
    return _normalize_classification(data)


def _normalize_classification(data: dict) -> dict:
    """Coerce a classifier reply to {"intents": [...], "clarify": str}.

    Accepts both the new ``intents`` array and the legacy single ``intent`` key
    (so older prompts/clients — and the existing test stubs — keep working).
    De-dupes preserving order and never returns an empty list.
    """
    raw = data.get("intents")
    if not isinstance(raw, list) or not raw:
        one = data.get("intent")
        raw = [one] if one else []
    intents: list[str] = []
    seen: set[str] = set()
    for i in raw:
        s = str(i).strip().lower()
        if s and s not in seen:
            seen.add(s)
            intents.append(s)
    if not intents:
        intents = ["ambiguous"]
    return {
        "intents": intents,
        "clarify": data.get("clarify", "") or "",
        "options": _clean_options(data.get("options")),
    }


def _clean_options(raw) -> list[str]:
    """Sanitize classifier-suggested clickable options: strings, capped at 4."""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for o in raw:
        s = str(o).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
        if len(out) >= 4:
            break
    return out

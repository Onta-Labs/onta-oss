"""Central LLM routing for the resolver / governance / query pipeline.

Every decision and inference LLM call goes through OpenRouter with a **primary
model + automatic fallback** (OpenRouter's ``models`` array, tried in order on
error). Both ids are env-overridable; the defaults set the production
primary/fallback. Nothing is hardcoded at the call sites — they pass their
per-role model (which itself defaults to ``PRIMARY_MODEL``) and the fallback is
applied here uniformly.
"""

from __future__ import annotations

import os

import httpx

OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# Primary model for all LLM calls, and the automatic fallback applied via
# OpenRouter's `models` routing. Env-overridable; defaults are the production
# choice. Per-role knobs (OMNIX_EXTRACT_MODEL, OMNIX_MATCH_MODEL, …) default to
# PRIMARY_MODEL, so OMNIX_LLM_MODEL flips every role at once unless individually
# overridden.
PRIMARY_MODEL = os.environ.get("OMNIX_LLM_MODEL", "anthropic/claude-opus-4.8")
FALLBACK_MODEL = os.environ.get("OMNIX_LLM_FALLBACK_MODEL", "openai/gpt-5.5")


def model_chain(primary: str | None = None) -> list[str]:
    """``[primary, fallback]`` for OpenRouter's ``models`` routing — fallback
    de-duplicated and dropped when empty or equal to the primary."""
    head = primary or PRIMARY_MODEL
    chain = [head]
    if FALLBACK_MODEL and FALLBACK_MODEL != head:
        chain.append(FALLBACK_MODEL)
    return chain


async def openrouter_chat(
    api_key: str,
    system: str,
    user: str,
    *,
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = 4096,
    response_format: dict | None = None,
    timeout: float = 120.0,
    return_finish_reason: bool = False,
    return_usage: bool = False,
) -> str | tuple[str, str | None] | tuple[str, dict | None] | tuple[str, str | None, dict | None]:
    """One OpenRouter chat completion with primary→fallback model routing.

    Returns the raw message content (callers parse). Raises on HTTP error after
    the fallback chain is exhausted.

    When ``return_finish_reason`` is True, returns ``(content, finish_reason)``
    instead — where ``finish_reason`` is the provider's stop signal (``"length"``
    when the model hit ``max_tokens`` mid-output, ``"stop"`` for a clean finish,
    or ``None`` if the provider omitted it). This lets a caller distinguish a
    TRUNCATED reply (recover by splitting + retrying) from a genuinely malformed
    one. Default False keeps the plain-string contract for every existing caller.

    When ``return_usage`` is True, the provider's ``usage`` object (the dict with
    ``prompt_tokens`` / ``completion_tokens`` / ``total_tokens``, or ``None`` if
    the provider omitted it) is appended as the LAST tuple element — for
    per-call token accounting (ONTA-200). The two flags compose:

    ======================  ===================  ================================
    return_finish_reason    return_usage         return type
    ======================  ===================  ================================
    False (default)         False (default)      ``content`` (bare str)
    True                    False                ``(content, finish_reason)``
    False                   True                 ``(content, usage)``
    True                    True                 ``(content, finish_reason, usage)``
    ======================  ===================  ================================

    Every existing caller (both bare-string and ``return_finish_reason``-only)
    keeps its current return shape untouched.
    """
    chain = model_chain(model)
    body: dict = {
        "model": chain[0],
        "models": chain,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format is not None:
        body["response_format"] = response_format
    async with httpx.AsyncClient(timeout=timeout) as client:
        res = await client.post(
            f"{OPENROUTER_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        res.raise_for_status()
        payload = res.json()
        choice = payload["choices"][0]
        content = choice["message"]["content"]
        if return_finish_reason and return_usage:
            return content, choice.get("finish_reason"), payload.get("usage")
        if return_finish_reason:
            return content, choice.get("finish_reason")
        if return_usage:
            return content, payload.get("usage")
        return content

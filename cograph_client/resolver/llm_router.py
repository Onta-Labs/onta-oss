"""Central LLM routing for the resolver / governance / query pipeline.

Every decision and inference (extraction) LLM call goes through this module. The
default backend is **OpenRouter** with a **primary model + automatic fallback**
(OpenRouter's ``models`` array, tried in order on error). Both ids are
env-overridable; the defaults set the production primary/fallback. Nothing is
hardcoded at the call sites — they pass their per-role model (which itself
defaults to ``PRIMARY_MODEL``) and the fallback is applied here uniformly.

**Provider selection (``OMNIX_LLM_PROVIDER``).** The backend is chosen by
``OMNIX_LLM_PROVIDER`` (``openrouter`` | ``cerebras``), *defaulting to
``openrouter``* so behaviour is byte-identical to the historical hardcoded
OpenRouter path when the env is unset. When set to ``cerebras`` this routes the
same OpenAI-shaped chat request to Cerebras (``https://api.cerebras.ai/v1``)
instead — the auth key becomes ``CEREBRAS_API_KEY`` and the model is the bare
``OMNIX_LLM_MODEL`` slug (e.g. ``gpt-oss-120b``, no ``openai/`` prefix). This
mirrors the query path's Cerebras support (``nlp/pipeline.py``); one env flip
switches ALL extraction call sites at once because they all funnel through
:func:`openrouter_chat` here. The query path has its OWN Cerebras selector
(``OMNIX_QUERY_PROVIDER``) and is unaffected.

The flip is guarded per call by SLUG SHAPE: Cerebras serves only bare slugs, so
a call whose effective model contains ``/`` (an OpenRouter ``vendor/model`` id —
per-role knobs like CSV schema inference's ``google/gemini-2.5-flash`` default)
stays on OpenRouter even under ``cerebras``. Without this, the provider flip
sends OpenRouter-only slugs to ``api.cerebras.ai``, which 404s them — in
production that broke every rail whose per-role model kept a ``vendor/model``
default (CSV schema inference, enrichment extraction, research extraction)
while bare-slug callers kept working.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import httpx

from cograph_client.retrieval.errors import classify_llm_status_error

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
CEREBRAS_BASE = "https://api.cerebras.ai/v1"

# Primary model for all LLM calls, and the automatic fallback applied via
# OpenRouter's `models` routing. Env-overridable; defaults are the production
# choice. Per-role knobs (OMNIX_EXTRACT_MODEL, OMNIX_MATCH_MODEL, …) default to
# PRIMARY_MODEL, so OMNIX_LLM_MODEL flips every role at once unless individually
# overridden.
PRIMARY_MODEL = os.environ.get("OMNIX_LLM_MODEL", "anthropic/claude-opus-4.8")
FALLBACK_MODEL = os.environ.get("OMNIX_LLM_FALLBACK_MODEL", "openai/gpt-5.5")


def _llm_provider() -> str:
    """The extraction LLM backend: ``openrouter`` (default) or ``cerebras``.

    Read live from the env on every call (not cached at import) so a test — or a
    runtime reconfigure — can flip it without re-importing the module. Any value
    other than ``cerebras`` (including unset) means ``openrouter``, preserving the
    historical default byte-for-byte."""
    return os.environ.get("OMNIX_LLM_PROVIDER", "openrouter").strip().lower()


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

    **Provider routing.** When ``OMNIX_LLM_PROVIDER=cerebras`` the request is sent
    to Cerebras (``api.cerebras.ai``) with the ``CEREBRAS_API_KEY`` and the bare
    ``OMNIX_LLM_MODEL`` slug instead of OpenRouter — see the module docstring.
    Cerebras only serves BARE slugs, so the flip applies per call by slug shape
    (the same heuristic the query path uses): a bare effective model
    (``model or PRIMARY_MODEL``) goes to Cerebras; a ``vendor/model`` slug —
    e.g. a per-role knob like ``OMNIX_CSV_SCHEMA_MODEL``'s
    ``google/gemini-2.5-flash`` default — can only be served by OpenRouter and
    keeps routing there with the caller's ``api_key``. Everything else (request
    params, return contract, error handling) is identical. Default (unset /
    ``openrouter``) is byte-identical to before.
    """
    provider = _llm_provider()
    effective_model = model or PRIMARY_MODEL
    if provider == "cerebras" and "/" not in effective_model:
        cerebras_key = os.environ.get("CEREBRAS_API_KEY", "")
        if not cerebras_key:
            # Fail loud — do NOT silently fall back to OpenRouter. If the operator
            # asked for Cerebras, running on OpenRouter instead would hide a
            # misconfiguration (wrong key, wrong model slug) behind "it worked".
            raise RuntimeError(
                "OMNIX_LLM_PROVIDER=cerebras but CEREBRAS_API_KEY is not set — "
                "set the Cerebras key or unset OMNIX_LLM_PROVIDER to use OpenRouter."
            )
        base = CEREBRAS_BASE
        request_key = cerebras_key
        # Cerebras takes a BARE model slug (e.g. "gpt-oss-120b"), not an
        # OpenRouter-prefixed one, and has no `models` fallback array. Use the
        # caller's per-role model when supplied, else PRIMARY_MODEL (OMNIX_LLM_MODEL).
        body: dict = {
            "model": effective_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
    else:
        base = OPENROUTER_BASE
        request_key = api_key
        chain = model_chain(model)
        body = {
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
            f"{base}/chat/completions",
            headers={
                "Authorization": f"Bearer {request_key}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        try:
            res.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # A 402 (prepaid balance exhausted) or 401 (bad/revoked key) is
            # SYSTEMIC — the next call will fail identically. Re-raise it as a
            # distinct, typed FATAL error (ONTA-201) so a caller in a
            # split-and-retry / per-batch loop can short-circuit the whole run
            # instead of burning more doomed calls and reporting "complete".
            # Every OTHER status (429, 5xx, …) keeps propagating as the raw
            # HTTPStatusError, so existing transient handling is unchanged.
            # Thread the ACTIVE provider + host so the message names the account
            # that actually returned the 402/401 (Cerebras vs OpenRouter) — not a
            # hardcoded backend that may be the wrong one to go check.
            fatal = classify_llm_status_error(
                exc.response.status_code,
                detail=_error_detail(exc.response),
                provider=provider,
                host=urlparse(base).hostname,
            )
            if fatal is not None:
                raise fatal from exc
            raise
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


def _error_detail(response: httpx.Response) -> str:
    """Best-effort short reason from an error response body for the fatal-error
    message. OpenRouter returns ``{"error": {"message": "..."}}``; fall back to a
    trimmed raw body. Never raises — a detail is purely additive context."""
    try:
        data = response.json()
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict) and err.get("message"):
                return str(err["message"])[:200]
            if isinstance(err, str) and err:
                return err[:200]
    except Exception:  # noqa: BLE001 — a malformed body must not mask the error
        pass
    try:
        return (response.text or "").strip()[:200]
    except Exception:  # noqa: BLE001
        return ""

"""Token-usage instrumentation for the NL→SPARQL /ask path.

Whitepaper v3 (and any holdout eval) needs **tokens-to-complete-task** as a
first-class metric: per-LLM-call ``prompt_tokens`` / ``completion_tokens`` plus
model id, attempt index, and stage. This module is the ONE place those events
are shaped — generators attach raw provider usage, ``ask()`` normalizes into
:class:`TokenUsageEvent`s, and eval harnesses serialize via :func:`events_to_json`.

Stages (canonical string set for the paper):

* ``sparql_gen`` — first SPARQL-generation LLM call (attempt index 0)
* ``retry``      — subsequent SPARQL-generation attempts (attempt index ≥ 1)
* ``rephrase``   — optional narrative rephrase LLM call after execution
* ``example``    — reserved for example-bank embedding calls (not yet plumbed)
* ``ontology``   — reserved; ontology fetch is SPARQL today (no LLM tokens)

Pure data + helpers — no I/O, no side effects. Missing provider usage is
represented as ``None`` fields, never invented zeros (so aggregates can
distinguish "unknown" from "free").
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping, Optional


# Stages the whitepaper protocol recognizes. Unknown stages are allowed through
# (forward-compat) but helpers that label SPARQL-gen attempts stick to these.
STAGE_SPARQL_GEN = "sparql_gen"
STAGE_RETRY = "retry"
STAGE_REPHRASE = "rephrase"
STAGE_EXAMPLE = "example"
STAGE_ONTOLOGY = "ontology"

# Internal key generators stash on their returned dict so `ask()` can collect
# usage without changing every caller's return type. Stripped before any
# downstream consumer sees the generation payload.
USAGE_DICT_KEY = "_token_usage"


@dataclass(frozen=True)
class TokenUsageEvent:
    """One provider LLM call on the /ask path."""

    stage: str
    attempt: int
    model: str
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    provider: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict (None values kept so unknowns stay unknowns)."""
        return asdict(self)


@dataclass
class TokenUsageLedger:
    """Ordered list of per-call events for one /ask invocation."""

    events: list[TokenUsageEvent] = field(default_factory=list)

    def add(self, event: TokenUsageEvent) -> None:
        self.events.append(event)

    def record(
        self,
        *,
        stage: str,
        attempt: int,
        model: str = "",
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        total_tokens: Optional[int] = None,
        provider: str = "",
        raw: Mapping[str, Any] | None = None,
    ) -> TokenUsageEvent:
        """Append one event. ``raw`` (provider usage dict) fills missing counts."""
        if raw:
            parsed = parse_provider_usage(raw)
            if prompt_tokens is None:
                prompt_tokens = parsed.get("prompt_tokens")
            if completion_tokens is None:
                completion_tokens = parsed.get("completion_tokens")
            if total_tokens is None:
                total_tokens = parsed.get("total_tokens")
            if not model:
                model = str(parsed.get("model") or "")
            if not provider:
                provider = str(parsed.get("provider") or "")
        event = TokenUsageEvent(
            stage=stage,
            attempt=attempt,
            model=model or "",
            prompt_tokens=_as_optional_int(prompt_tokens),
            completion_tokens=_as_optional_int(completion_tokens),
            total_tokens=_as_optional_int(total_tokens),
            provider=provider or "",
        )
        self.events.append(event)
        return event

    def record_from_attached(
        self,
        payload: Mapping[str, Any] | None,
        *,
        stage: str,
        attempt: int,
        default_model: str = "",
        default_provider: str = "",
    ) -> Optional[TokenUsageEvent]:
        """Consume a generator-attached ``_token_usage`` blob if present.

        Does **not** mutate ``payload`` — callers should ``pop`` the key if they
        want it gone from the generation dict before further processing.
        """
        if not payload:
            return None
        raw = payload.get(USAGE_DICT_KEY)
        if not isinstance(raw, Mapping):
            return None
        return self.record(
            stage=stage,
            attempt=attempt,
            model=str(raw.get("model") or default_model or ""),
            provider=str(raw.get("provider") or default_provider or ""),
            prompt_tokens=raw.get("prompt_tokens"),
            completion_tokens=raw.get("completion_tokens"),
            total_tokens=raw.get("total_tokens"),
        )

    @property
    def prompt_tokens(self) -> Optional[int]:
        return _sum_optional(e.prompt_tokens for e in self.events)

    @property
    def completion_tokens(self) -> Optional[int]:
        return _sum_optional(e.completion_tokens for e in self.events)

    @property
    def total_tokens(self) -> Optional[int]:
        """Sum of per-event totals when every event has one; else prompt+completion
        if both aggregates are known; else None."""
        per_event = _sum_optional(e.total_tokens for e in self.events)
        if per_event is not None:
            return per_event
        p, c = self.prompt_tokens, self.completion_tokens
        if p is None and c is None:
            return None
        return (p or 0) + (c or 0)

    def to_list(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.events]

    def totals_for_timing(self) -> dict[str, float]:
        """Numeric aggregates safe to merge into ``NLResult.timing`` (float values).

        Only includes keys whose value is known. Empty ledger → empty dict so
        production payloads without any LLM usage stay byte-identical.
        """
        out: dict[str, float] = {}
        if not self.events:
            return out
        p, c, t = self.prompt_tokens, self.completion_tokens, self.total_tokens
        if p is not None:
            out["prompt_tokens"] = float(p)
        if c is not None:
            out["completion_tokens"] = float(c)
        if t is not None:
            out["total_tokens"] = float(t)
        out["llm_calls"] = float(len(self.events))
        return out


def stage_for_attempt(attempt: int) -> str:
    """Map 0-based attempt index → sparql_gen | retry."""
    return STAGE_SPARQL_GEN if attempt <= 0 else STAGE_RETRY


def parse_provider_usage(usage: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize OpenRouter / Cerebras / OpenAI-shaped usage dicts.

    Accepts both OpenAI-style (``prompt_tokens`` / ``completion_tokens``) and
    Anthropic-style (``input_tokens`` / ``output_tokens``) keys. Unknown keys
    are ignored; missing counts stay absent (not zeroed).
    """
    if not usage:
        return {}
    prompt = usage.get("prompt_tokens")
    if prompt is None:
        prompt = usage.get("input_tokens")
    completion = usage.get("completion_tokens")
    if completion is None:
        completion = usage.get("output_tokens")
    total = usage.get("total_tokens")
    out: dict[str, Any] = {}
    p = _as_optional_int(prompt)
    c = _as_optional_int(completion)
    t = _as_optional_int(total)
    if p is not None:
        out["prompt_tokens"] = p
    if c is not None:
        out["completion_tokens"] = c
    if t is not None:
        out["total_tokens"] = t
    elif p is not None or c is not None:
        out["total_tokens"] = (p or 0) + (c or 0)
    model = usage.get("model")
    if model:
        out["model"] = str(model)
    provider = usage.get("provider")
    if provider:
        out["provider"] = str(provider)
    return out


def attach_usage(
    result: dict[str, Any],
    *,
    usage: Mapping[str, Any] | None,
    model: str = "",
    provider: str = "",
    response_model: str = "",
) -> dict[str, Any]:
    """Stamp ``USAGE_DICT_KEY`` onto a SPARQL-gen result dict (in place + return).

    Safe when ``usage`` is missing/empty: still records model/provider so the
    ledger knows *which* call ran even if the provider omitted counts.
    """
    parsed = parse_provider_usage(usage)
    blob: dict[str, Any] = {
        "prompt_tokens": parsed.get("prompt_tokens"),
        "completion_tokens": parsed.get("completion_tokens"),
        "total_tokens": parsed.get("total_tokens"),
        "model": response_model or model or parsed.get("model") or "",
        "provider": provider or parsed.get("provider") or "",
    }
    result[USAGE_DICT_KEY] = blob
    return result


def pop_attached_usage(payload: dict[str, Any] | None) -> Optional[dict[str, Any]]:
    """Remove and return the attached usage blob, or None."""
    if not isinstance(payload, dict):
        return None
    raw = payload.pop(USAGE_DICT_KEY, None)
    return raw if isinstance(raw, dict) else None


def events_to_json(events: Iterable[TokenUsageEvent | Mapping[str, Any]]) -> list[dict]:
    """Serialize events for NLResult / holdout JSON."""
    out: list[dict] = []
    for e in events:
        if isinstance(e, TokenUsageEvent):
            out.append(e.to_dict())
        elif isinstance(e, Mapping):
            out.append(dict(e))
    return out


def summarize_events(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate a list of event dicts (as stored on NLResult / eval JSON).

    Used by holdout harnesses that never import the dataclass path.
    """
    ledger = TokenUsageLedger()
    for e in events:
        if not isinstance(e, Mapping):
            continue
        ledger.record(
            stage=str(e.get("stage") or ""),
            attempt=int(e.get("attempt") or 0),
            model=str(e.get("model") or ""),
            provider=str(e.get("provider") or ""),
            prompt_tokens=e.get("prompt_tokens"),
            completion_tokens=e.get("completion_tokens"),
            total_tokens=e.get("total_tokens"),
        )
    return {
        "events": ledger.to_list(),
        "prompt_tokens": ledger.prompt_tokens,
        "completion_tokens": ledger.completion_tokens,
        "total_tokens": ledger.total_tokens,
        "llm_calls": len(ledger.events),
    }


def estimate_cost_usd(
    prompt_tokens: Optional[int],
    completion_tokens: Optional[int],
    *,
    input_per_1m: float,
    output_per_1m: float,
) -> Optional[float]:
    """USD cost from token counts and a pricing row (per 1M tokens).

    Returns None when either count is unknown so we never invent a $0 cost.
    """
    if prompt_tokens is None or completion_tokens is None:
        return None
    return (prompt_tokens / 1_000_000.0) * input_per_1m + (
        completion_tokens / 1_000_000.0
    ) * output_per_1m


def _as_optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sum_optional(values: Iterable[Optional[int]]) -> Optional[int]:
    """Sum known ints; return None only when *every* value is None/empty.

    Partial knowledge still sums the known parts (a rephrase with no usage
    must not zero out a known SPARQL-gen call).
    """
    total = 0
    saw = False
    for v in values:
        if v is None:
            continue
        saw = True
        total += int(v)
    return total if saw else None

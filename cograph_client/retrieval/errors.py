"""Retrieval error taxonomy + the per-request error policy (ONTA-193 P1).

The three rails historically forked on ONE decision — *what happens when a web
read fails* — and baked opposite answers into separate integrations:

* discovery wanted a **raise** so it could tell "the scraper errored / the page
  was unreachable" apart from "the page had no records" and show an honest,
  URL-appropriate message;
* enrichment wanted a **degrade-to-empty** so a bad page in a per-URL loop just
  skips to the next one instead of aborting the whole lookup.

That fork lived as a bare ``raise_on_error: bool`` chosen at each call site. It is
now a first-class, named **per-request policy** — :class:`FetchErrorPolicy` — that
any rail passes to any substrate/provider read, with a single substrate exception
type (:class:`RetrievalError` / :class:`FetchError`) so a rail that opts into
``RAISE`` catches ONE error regardless of which provider produced it. Premium
providers raise a subclass of :class:`RetrievalError`, so nothing rail-specific
leaks into a caller's ``except``.

Boundary: OSS. Imports only stdlib.
"""

from __future__ import annotations

import enum


class RetrievalError(Exception):
    """Base for every recoverable failure raised out of the retrieval substrate
    (or a premium provider on top of it) under the ``RAISE`` error policy.

    A rail that opts into raising catches THIS type, so it never has to know which
    concrete provider (Firecrawl, a future scraper, …) produced the failure —
    provider-specific exceptions subclass it.
    """


class FetchError(RetrievalError):
    """A page/URL could not be fetched (transport error, timeout, non-2xx, blocked
    host). Raised by the fetch layer only under :attr:`FetchErrorPolicy.RAISE`."""


# --- LLM-backend billing / auth (ONTA-201) ---------------------------------- #
# A 402/401 from the active LLM account is SYSTEMIC, not a per-record hiccup: the
# prepaid balance hit $0, or the key is invalid/revoked. It will recur on the
# very next call, so any split-and-retry / per-batch degrade just burns more
# (equally-doomed) calls and hides the real cause behind a vague per-batch
# "failed" while the run still reports "complete". These typed errors are
# distinct, FATAL signals a rail short-circuits on: abort the remaining
# chunks/sub-queries and fail the whole job with a clear, actionable message.
#
# They subclass :class:`LLMError` (itself a :class:`RetrievalError`) so the ONE
# retrieval error hierarchy stays the single ``except`` a rail needs, and OSS's
# stdlib-only boundary for this module is preserved.


class LLMError(RetrievalError):
    """Base for a failure of the LLM backend (as opposed to a web fetch). A rail
    that needs to treat ANY LLM-backend problem uniformly catches this."""


class LLMBillingError(LLMError):
    """The LLM backend refused the call for BILLING reasons — HTTP 402 Payment
    Required (the shared prepaid balance is exhausted). Systemic and recurring:
    never enter split-and-retry, fail the run fast so the balance can be topped
    up."""


class LLMAuthError(LLMError):
    """The LLM backend refused the call for AUTH reasons — HTTP 401 Unauthorized
    (the API key is missing, invalid, or revoked). Systemic and recurring, same
    fail-fast treatment as :class:`LLMBillingError`."""


class LLMRateLimitError(LLMError):
    """SUSTAINED rate-limiting — repeated HTTP 429 that has crossed the run-level
    escalation threshold (ONTA-273).

    Distinct from a single 429 blip: one (or a few) 429s are a normal transient the
    caller RETRIES; this is raised ONLY by :class:`RateLimitEscalator` once a run
    has hit ``threshold`` consecutive 429s — i.e. the account is being throttled to
    a standstill, which is a form of PROVIDER EXHAUSTION for this run. Treated like
    :class:`LLMBillingError`: halt the run with a user-visible reason instead of
    burning more equally-throttled calls forever. It is NEVER returned by
    :func:`classify_llm_status_error` (a single-call classifier can't know a 429 is
    sustained), so a lone rate-limit still retries as before."""


#: HTTP status → typed fatal LLM error. 402/401 are the two systemic,
#: will-recur-immediately codes worth aborting a whole run on. 429 is deliberately
#: ABSENT — see :func:`classify_llm_status_error` / :class:`RateLimitEscalator`.
_FATAL_LLM_STATUS: dict[int, type[LLMError]] = {
    402: LLMBillingError,
    401: LLMAuthError,
}

#: Default number of CONSECUTIVE 429s within ONE run that escalates from
#: "transient, retry" to "provider exhaustion, halt". A single blip stays a
#: transient; only a sustained streak halts (ONTA-273).
RATE_LIMIT_HALT_THRESHOLD = 5


def is_rate_limit_status(status_code: int) -> bool:
    """True for HTTP 429 (Too Many Requests) — the rate-limit signal a run tracks
    via :class:`RateLimitEscalator` to distinguish a blip from sustained
    exhaustion."""
    return status_code == 429


class RateLimitEscalator:
    """Run-scoped 429 tracker deciding when sustained rate-limiting becomes fatal.

    **The 429 policy (ONTA-273).** A single / occasional 429 is a normal transient:
    the per-call / per-batch handling already retries or degrades, so it must NOT
    abort the run — making *every* 429 fatal would kill a run on one rate-limit
    blip. But a run that keeps getting 429 after 429 is NOT making progress; that
    sustained throttling is provider exhaustion for this run and should halt with a
    user-visible reason rather than spin forever.

    This tracker draws that line WITHOUT making the stateless
    :func:`classify_llm_status_error` fatal on 429 (it can't see a streak). The run
    owns one escalator: it calls :meth:`record_success` on any non-429 outcome
    (resetting the streak) and :meth:`record_rate_limited` on each 429; the latter
    returns a fatal :class:`LLMRateLimitError` ONLY once ``threshold`` consecutive
    429s accumulate, which the run treats exactly like a 402 (halt-and-report).
    """

    def __init__(self, threshold: int = RATE_LIMIT_HALT_THRESHOLD) -> None:
        self._threshold = max(1, int(threshold))
        self._streak = 0

    @property
    def consecutive(self) -> int:
        return self._streak

    def reset(self) -> None:
        self._streak = 0

    def record_success(self) -> None:
        """Any non-429 outcome (a success, or a different error) breaks the
        streak — 429s must be CONSECUTIVE to escalate."""
        self._streak = 0

    def record_rate_limited(
        self,
        *,
        provider: str | None = None,
        host: str | None = None,
        detail: str = "",
    ) -> LLMRateLimitError | None:
        """Record one 429. Returns a fatal :class:`LLMRateLimitError` once the
        streak reaches ``threshold``, else ``None`` (keep retrying)."""
        self._streak += 1
        if self._streak < self._threshold:
            return None
        who = _provider_phrase(provider, host)
        msg = (
            f"LLM backend returned HTTP 429 Too Many Requests "
            f"{self._streak} times in a row — {who} is sustained rate-limited "
            "(provider exhaustion). Back off / raise the account's rate limit."
        )
        if detail:
            msg = f"{msg} ({detail})"
        return LLMRateLimitError(msg)

#: User-facing message templates keyed by status, with a ``{who}`` slot for the
#: PROVIDER-ACCURATE phrase (see :func:`_provider_phrase`). Surfaced verbatim on
#: the terminal job/error state so the fix (top up / rotate the key) is obvious —
#: and points at the account that ACTUALLY rejected the call.
#:
#: Cautionary tale (the bug this template fixes): the message used to hardcode
#: "OpenRouter", but :func:`openrouter_chat` routes to Cerebras when
#: ``OMNIX_LLM_PROVIDER=cerebras``. On 2026-07-08 a Cerebras 402 (Cerebras out of
#: credits) surfaced telling the operator to check the *OpenRouter* balance,
#: sending debugging in the wrong direction. The provider is now DERIVED from the
#: caller-supplied slug/host — no provider name is hardcoded here.
_FATAL_LLM_MESSAGE: dict[int, str] = {
    402: (
        "LLM extraction backend returned 402 Payment Required — check {who} "
        "balance (the prepaid account is likely at $0)."
    ),
    401: (
        "LLM extraction backend returned 401 Unauthorized — check {who} "
        "API key (missing, invalid, or revoked)."
    ),
}


def _provider_phrase(provider: str | None, host: str | None) -> str:
    """A human phrase naming the LLM backend that returned the error.

    DERIVED purely from the caller-supplied ``provider`` slug (e.g. the value of
    ``OMNIX_LLM_PROVIDER`` — ``"cerebras"`` / ``"openrouter"`` / any future
    backend) and/or the base ``host`` (``api.cerebras.ai`` / ``openrouter.ai``);
    nothing provider-specific is hardcoded here, so a brand-new backend names
    itself correctly with no code change. Degrades to a generic phrase when the
    caller supplies neither (every pre-existing caller), so old call sites keep a
    sensible, non-misleading message."""
    label = provider.strip() if provider else ""
    host = host.strip() if host else ""
    if label and host:
        return f"the {label.title()} account ({host})"
    if label:
        return f"the {label.title()} account"
    if host:
        return f"the LLM provider account at {host}"
    return "the LLM provider account"


def classify_llm_status_error(
    status_code: int,
    *,
    detail: str = "",
    provider: str | None = None,
    host: str | None = None,
) -> LLMError | None:
    """Return the typed FATAL LLM error for a systemic status code, else ``None``.

    ``status_code`` is the HTTP status the LLM backend returned. 402 → billing,
    401 → auth; every other code (429 rate-limit, 5xx, timeouts) is a normal,
    per-call transient the caller keeps handling as before — this returns
    ``None`` so nothing is escalated to a run-level abort. ``detail`` is folded
    into the message when supplied (e.g. the provider's error body).

    429 is intentionally NOT fatal here: a single rate-limit blip must still
    retry. SUSTAINED 429 (a run that keeps getting throttled) is escalated to a
    run-level halt separately, by :class:`RateLimitEscalator` — which the run owns
    and which alone has the streak context a per-call classifier lacks (ONTA-273).

    ``provider`` (the backend slug, e.g. ``OMNIX_LLM_PROVIDER``) and ``host`` (the
    base API host, e.g. ``api.cerebras.ai``) make the message name the account
    that ACTUALLY rejected the call — a Cerebras 402 says "Cerebras", an
    OpenRouter 402 says "OpenRouter". Both are optional and default to ``None`` so
    every existing caller keeps working and gets a generic (still non-misleading)
    phrase; the provider name is derived, never hardcoded."""
    kind = _FATAL_LLM_STATUS.get(status_code)
    if kind is None:
        return None
    msg = _FATAL_LLM_MESSAGE[status_code].format(who=_provider_phrase(provider, host))
    if detail:
        msg = f"{msg} ({detail})"
    return kind(msg)


class FetchErrorPolicy(enum.Enum):
    """What a retrieval read does when the web read FAILS (per request).

    * :attr:`DEGRADE` — return an empty/failed result (``FetchedPage(ok=False)`` /
      ``{}`` / ``[]``) and let the caller carry on. The resilient default: a bad
      page in a loop is skipped, a paid lookup never breaks ingest or the agent.
    * :attr:`RAISE` — raise :class:`RetrievalError` (or a provider subclass) so the
      caller can distinguish "couldn't READ it" from "read it, found nothing" and
      surface an honest message.

    This is a decision the *caller* owns per request; it is NOT a property of a
    provider. The same provider serves both policies.
    """

    DEGRADE = "degrade"
    RAISE = "raise"

    @classmethod
    def from_raise_on_error(cls, raise_on_error: bool) -> "FetchErrorPolicy":
        """Bridge the legacy ``raise_on_error: bool`` to the named policy."""
        return cls.RAISE if raise_on_error else cls.DEGRADE

    @property
    def raises(self) -> bool:
        """True when a failure should propagate as an exception."""
        return self is FetchErrorPolicy.RAISE


__all__ = [
    "RATE_LIMIT_HALT_THRESHOLD",
    "FetchError",
    "FetchErrorPolicy",
    "LLMAuthError",
    "LLMBillingError",
    "LLMError",
    "LLMRateLimitError",
    "RateLimitEscalator",
    "RetrievalError",
    "classify_llm_status_error",
    "is_rate_limit_status",
]

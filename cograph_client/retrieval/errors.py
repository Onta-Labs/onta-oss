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
# A 402/401 from the shared OpenRouter account is SYSTEMIC, not a per-record
# hiccup: the prepaid balance hit $0, or the key is invalid/revoked. It will
# recur on the very next call, so any split-and-retry / per-batch degrade just
# burns more (equally-doomed) calls and hides the real cause behind a vague
# per-batch "failed" while the run still reports "complete". These typed errors
# are distinct, FATAL signals a rail short-circuits on: abort the remaining
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


#: HTTP status → typed fatal LLM error. 402/401 are the two systemic,
#: will-recur-immediately codes worth aborting a whole run on.
_FATAL_LLM_STATUS: dict[int, type[LLMError]] = {
    402: LLMBillingError,
    401: LLMAuthError,
}

#: User-facing message templates keyed by status — surfaced verbatim on the
#: terminal job/error state so the fix (top up / rotate the key) is obvious.
_FATAL_LLM_MESSAGE: dict[int, str] = {
    402: (
        "LLM extraction backend returned 402 Payment Required — check the "
        "OpenRouter balance (the shared prepaid account is likely at $0)."
    ),
    401: (
        "LLM extraction backend returned 401 Unauthorized — check the "
        "OpenRouter API key (missing, invalid, or revoked)."
    ),
}


def classify_llm_status_error(status_code: int, *, detail: str = "") -> LLMError | None:
    """Return the typed FATAL LLM error for a systemic status code, else ``None``.

    ``status_code`` is the HTTP status the LLM backend returned. 402 → billing,
    401 → auth; every other code (429 rate-limit, 5xx, timeouts) is a normal,
    per-call transient the caller keeps handling as before — this returns
    ``None`` so nothing is escalated to a run-level abort. ``detail`` is folded
    into the message when supplied (e.g. the provider's error body)."""
    kind = _FATAL_LLM_STATUS.get(status_code)
    if kind is None:
        return None
    msg = _FATAL_LLM_MESSAGE[status_code]
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
    "FetchError",
    "FetchErrorPolicy",
    "LLMAuthError",
    "LLMBillingError",
    "LLMError",
    "RetrievalError",
    "classify_llm_status_error",
]

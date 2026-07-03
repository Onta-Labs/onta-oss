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
    "RetrievalError",
]

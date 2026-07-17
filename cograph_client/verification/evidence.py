"""A4 (Verify) evidence gathering — corroborate a fact from INDEPENDENT sources,
THROUGH the one shared retrieval substrate (ONTA-364).

The offline default verifier (``verifier.DefaultOfflineVerifier``) can only ever
return :attr:`~cograph_client.verification.types.TruthVerdict.UNVERIFIABLE`: with no
evidence it has nothing to weigh. This module builds the verifier that actually
GATHERS evidence — it fetches candidate corroborating pages for a fact, reads their
text, and decides whether an INDEPENDENT source supports, refutes, or fails to
corroborate the fact's value.

**The anti-gaming crux — independence.** A fact is never SUPPORTED by its OWN
source's say-so; corroboration must come from a source DISTINCT from where the fact
was ingested (see :class:`~cograph_client.verification.types.TruthVerdict`). This
verifier enforces that structurally: the fact's own source host is supplied to the
verifier, and any candidate evidence whose host equals it is REJECTED before it is
even fetched. If the only corroboration is same-host, the verdict is UNVERIFIABLE —
never a false SUPPORTED. That guard is FAIL-CLOSED: with no source host supplied,
independence cannot be proven, so SUPPORTED/REFUTED are withheld and the verdict is
UNVERIFIABLE.

**Retrieval-path convergence (ADR 0008).** Every web read here goes through the ONE
shared retrieval substrate — the fetch ladder (:func:`default_ladder` /
:class:`~cograph_client.retrieval.fetch.PageFetcher`), the ONE SSRF + HTML-safety
module (via the fetcher, plus :func:`~cograph_client.retrieval.safety.html_to_text`),
and the ONE cost reducer (:func:`~cograph_client.retrieval.cost.source_cost`). This
module implements NO second fetch, SSRF guard, pagination loop, or cost seam.

**Sync/async bridge.** The substrate fetch is async but the
:class:`~cograph_client.verification.verifier.FactVerifier` protocol's
:meth:`verify` is SYNC. The real gathering lives in the async :meth:`averify`;
:meth:`verify` drives it (``asyncio.run`` when no loop is running, else a dedicated
worker thread) and FAILS CLOSED to UNVERIFIABLE if it cannot.

Boundary: OSS. Imports only stdlib + ``cograph_client.*`` (the substrate transitively
pulls ``httpx``) — never ``from cograph.*``. The OSS default is offline/BYOK: it
fetches only URLs it is handed; paid fact-check / web-search adapters belong in
``cograph/`` behind ``register_fact_verifier`` (never here).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import re
from typing import Callable, Iterable, Optional, Sequence
from urllib.parse import urlparse

from cograph_client.resolver.models import CleanFact
from cograph_client.retrieval.cost import source_cost
from cograph_client.retrieval.errors import CostCeilingExceeded
from cograph_client.retrieval.fetch import PageFetcher, default_ladder
from cograph_client.retrieval.safety import html_to_text
from cograph_client.retrieval.types import FetchedPage
from cograph_client.verification.types import (
    EvidenceRef,
    TruthVerdict,
    VerifierResult,
    VerifyContext,
)

__all__ = ["EvidenceVerifier"]

# A candidate-URL provider: given the fact (+ optional context), propose URLs to
# check for corroboration. The OSS default supplies NONE (offline/BYOK — the caller
# hands the verifier candidates, or a premium search adapter registers one).
CandidateProvider = Callable[
    [CleanFact, Optional[VerifyContext]], Sequence[str]
]

# Generous default per-verify spend ceiling (USD). Only PAID fetch rungs draw it
# down; the OSS static rung is free, so the default budget never bites offline.
_DEFAULT_BUDGET_USD = 1.0
# Cap on candidate URLs examined per fact — bounds fan-out / cost / latency.
_DEFAULT_MAX_CANDIDATES = 8
# Chars of context kept around a matched value for the evidence snippet.
_SNIPPET_WIDTH = 200


def _degrade(reason: str) -> VerifierResult:
    """A fail-closed UNVERIFIABLE with no evidence and zero confidence."""
    return VerifierResult(
        verdict=TruthVerdict.UNVERIFIABLE,
        confidence=0.0,
        evidence=(),
        reason=reason,
    )


def _norm_host(value: str) -> str:
    """Normalize a host or URL to a comparable host string (netloc, casefolded).

    Accepts either a bare host (``example.com``) or a full URL (``https://…``); a
    URL is reduced to its ``netloc`` so a caller may pass the fact's source_url OR
    its host interchangeably. Casefolded because hosts are case-insensitive."""
    v = (value or "").strip()
    if "://" in v:
        v = urlparse(v).netloc
    return v.casefold()


def _value_forms(fact: CleanFact) -> list[str]:
    """The value strings evidence is matched against — BOTH the persisted surface
    form (``raw_value``, the ONTA-347 pre-coercion original) AND the coerced
    ``clean_value``, deduped and non-empty.

    Comparing against the surface form (not only the coerced value) matters: a
    source states ``"42%"`` while A3 canonicalized it to ``"0.42"`` — evidence on
    the web carries the surface form, so it must be a first-class match target."""
    forms: list[str] = []
    for candidate in (fact.raw_value, fact.clean_value):
        if candidate is None:
            continue
        s = str(candidate).strip()
        if s and s not in forms:
            forms.append(s)
    return forms


def _norm_text(text: str) -> str:
    """Casefold + collapse whitespace for substring matching."""
    return re.sub(r"\s+", " ", (text or "")).casefold()


def _plain_text(page: FetchedPage) -> str:
    """The page's citable plain text. The fetch ladder already reduces HTML→text,
    but if a rung handed back raw markup we defensively route it through the ONE
    shared :func:`~cograph_client.retrieval.safety.html_to_text` (never a private
    HTML reducer)."""
    text = page.text or ""
    if "<" in text and ">" in text:
        _title, reduced = html_to_text(text)
        if reduced:
            text = reduced
    return text


def _snippet(text: str, form: str) -> str:
    """A short window of ``text`` around the first occurrence of ``form`` (or the
    head of the text when the match location is unclear)."""
    norm = _norm_text(text)
    idx = norm.find(_norm_text(form))
    if idx < 0:
        return (text or "")[:_SNIPPET_WIDTH].strip()
    lo = max(0, idx - _SNIPPET_WIDTH // 2)
    hi = min(len(text), idx + len(form) + _SNIPPET_WIDTH // 2)
    return text[lo:hi].strip()


def _confidence(distinct_hosts: int) -> float:
    """Confidence from the number of DISTINCT independent hosts that agree/disagree:
    one host → 0.5, saturating at 0.9 (never certainty — this is a substring-match
    default; a premium LLM-judge verifier calibrates better)."""
    if distinct_hosts <= 0:
        return 0.0
    return min(0.9, 0.5 + 0.2 * (distinct_hosts - 1))


class EvidenceVerifier:
    """A :class:`~cograph_client.verification.verifier.FactVerifier` that GATHERS
    independent corroborating evidence via the shared retrieval substrate.

    It fetches candidate corroborating pages through the fetch ladder, extracts
    their text, and — for every page whose host is DISTINCT from the fact's own
    source host — checks whether the fact's value (surface form or coerced value)
    is present (agreement) or a known competing value is present without it
    (disagreement). Aggregated:

      * ≥1 independent host AGREES        → :attr:`TruthVerdict.SUPPORTED`
      * no agreement, ≥1 independent host DISAGREES → :attr:`TruthVerdict.REFUTED`
      * otherwise (incl. only same-host, no source host, or budget exhausted)
                                          → :attr:`TruthVerdict.UNVERIFIABLE`

    Contradiction detection in the OSS default is EXPLICIT: the caller (or a
    premium extractor) supplies ``contradicting_values`` — the known competing
    value(s) for the attribute — since reliably inferring "a different value for
    the same attribute" from free text is a premium/LLM concern, not a
    deterministic-offline one.

    Independence + budget are the two structural guards:

      * **Independence** — ``source_host`` (the fact's own ingest source host) is
        supplied at construction or per call; any candidate whose host equals it is
        rejected BEFORE it is fetched, so same-host corroboration can never produce
        a false SUPPORTED. With NO source host, independence is unprovable and the
        verdict is capped at UNVERIFIABLE (fail-closed).
      * **Budget** — every fetch's cost is metered through the ONE shared
        :func:`~cograph_client.retrieval.cost.source_cost` reducer against
        ``budget_usd``; a paid rung that would exceed the budget is skipped, and a
        :class:`~cograph_client.retrieval.errors.CostCeilingExceeded` from a rung
        degrades cleanly to UNVERIFIABLE (never raised).

    Constructor args double as per-call defaults; :meth:`averify` accepts the same
    knobs as explicit overrides for direct programmatic use.
    """

    def __init__(
        self,
        *,
        source_host: str = "",
        source_url: str = "",
        candidate_urls: Sequence[str] = (),
        candidate_provider: Optional[CandidateProvider] = None,
        contradicting_values: Iterable[str] = (),
        fetchers: Optional[Sequence[PageFetcher]] = None,
        budget_usd: float = _DEFAULT_BUDGET_USD,
        max_candidates: int = _DEFAULT_MAX_CANDIDATES,
    ) -> None:
        # source_host wins; else derive it from a full source_url. Stored normalized.
        self._source_host = _norm_host(source_host or source_url)
        self._candidate_urls = tuple(candidate_urls)
        self._candidate_provider = candidate_provider
        self._contradicting_values = tuple(contradicting_values)
        # None → resolve default_ladder() lazily per call, so a fetcher registered
        # AFTER construction (e.g. app boot, or a test) is still picked up.
        self._fetchers = tuple(fetchers) if fetchers is not None else None
        self._budget_usd = float(budget_usd)
        self._max_candidates = max(1, int(max_candidates))

    # -- FactVerifier protocol (sync) ------------------------------------------ #

    def verify(
        self, fact: CleanFact, context: Optional[VerifyContext] = None
    ) -> VerifierResult:
        """Sync entry point (the ``FactVerifier`` protocol). Drives the async
        :meth:`averify` and FAILS CLOSED to UNVERIFIABLE on any bridge failure —
        the protocol forbids raising."""
        try:
            return self._drive(lambda: self.averify(fact, context))
        except Exception as exc:  # pragma: no cover - defensive; averify is guarded
            return _degrade(
                f"evidence verification unavailable (sync bridge failed: "
                f"{type(exc).__name__})"
            )

    @staticmethod
    def _drive(make_coro: Callable[[], "asyncio.Future"]) -> VerifierResult:
        """Run an async ``averify`` coroutine to completion from sync code.

        No loop running (the common case — CLI, sync API handler, tests) →
        ``asyncio.run``. A loop already running in this thread → run the coroutine
        in a dedicated worker thread with its OWN loop, so we neither re-enter the
        caller's loop nor need a false-negative fail-close. ``make_coro`` is a
        factory so the coroutine is created exactly once, on the path that awaits
        it (no un-awaited-coroutine warning on the fallback path)."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(make_coro())
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(make_coro())).result()

    # -- real gathering (async) ------------------------------------------------ #

    async def averify(
        self,
        fact: CleanFact,
        context: Optional[VerifyContext] = None,
        *,
        source_host: Optional[str] = None,
        source_url: str = "",
        candidate_urls: Optional[Sequence[str]] = None,
        contradicting_values: Optional[Iterable[str]] = None,
        budget_usd: Optional[float] = None,
    ) -> VerifierResult:
        """Gather independent evidence for ``fact`` and decide a verdict.

        Fetches candidate corroborating pages through the shared fetch ladder,
        rejecting any whose host equals the fact's source host, and returns
        SUPPORTED / REFUTED / UNVERIFIABLE. NEVER raises (fail-closed to
        UNVERIFIABLE) so a fetch/provider outage degrades to "unverified"."""
        try:
            return await self._averify(
                fact,
                context,
                source_host=source_host,
                source_url=source_url,
                candidate_urls=candidate_urls,
                contradicting_values=contradicting_values,
                budget_usd=budget_usd,
            )
        except CostCeilingExceeded as exc:
            return _degrade(f"run cost ceiling reached during evidence gathering: {exc}")
        except Exception as exc:  # provider/fetch error → fail closed
            return _degrade(
                f"evidence gathering failed ({type(exc).__name__}: {exc})"
            )

    async def _averify(
        self,
        fact: CleanFact,
        context: Optional[VerifyContext],
        *,
        source_host: Optional[str],
        source_url: str,
        candidate_urls: Optional[Sequence[str]],
        contradicting_values: Optional[Iterable[str]],
        budget_usd: Optional[float],
    ) -> VerifierResult:
        src_host = (
            _norm_host(source_host or source_url)
            if (source_host or source_url)
            else self._source_host
        )
        forms = _value_forms(fact)
        if not forms:
            return _degrade("fact has no value to corroborate")
        # Independence is a HARD precondition for SUPPORTED/REFUTED: without the
        # fact's own source host we cannot prove any evidence is independent of it.
        if not src_host:
            return _degrade(
                "fact source host not provided — evidence independence cannot be "
                "proven, so corroboration is withheld (fail-closed)"
            )

        candidates = self._resolve_candidates(fact, context, candidate_urls)
        if not candidates:
            return _degrade("no candidate evidence sources to consult")

        contradictions = [
            c.strip()
            for c in (
                contradicting_values
                if contradicting_values is not None
                else self._contradicting_values
            )
            if c and str(c).strip()
        ]
        budget = self._budget_usd if budget_usd is None else float(budget_usd)
        fetchers = list(self._fetchers) if self._fetchers is not None else default_ladder()

        spent = 0.0
        supporting: list[EvidenceRef] = []
        refuting: list[EvidenceRef] = []
        support_hosts: set[str] = set()
        refute_hosts: set[str] = set()
        rejected_same_host = 0
        examined = 0
        budget_halted = False

        want = f"{fact.attribute}: {forms[0]}".strip(": ")
        for url in candidates:
            cand_host = _norm_host(url)
            if not cand_host:
                continue
            # INDEPENDENCE GUARD: a candidate on the fact's OWN source host can never
            # corroborate it — reject BEFORE spending any fetch cost.
            if cand_host == src_host:
                rejected_same_host += 1
                continue

            page, cost, halted = await self._fetch(url, fetchers, want, budget - spent)
            spent += cost
            if halted:
                budget_halted = True
                break
            if page is None or not page.has_content():
                continue
            examined += 1
            text = _plain_text(page)
            agree_form = next((f for f in forms if _norm_text(f) in _norm_text(text)), None)
            evidence_url = page.url or url
            if agree_form is not None:
                if cand_host not in support_hosts:
                    support_hosts.add(cand_host)
                    supporting.append(
                        EvidenceRef.from_url(evidence_url, _snippet(text, agree_form))
                    )
                continue
            disagree = next(
                (c for c in contradictions if _norm_text(c) in _norm_text(text)), None
            )
            if disagree is not None and cand_host not in refute_hosts:
                refute_hosts.add(cand_host)
                refuting.append(
                    EvidenceRef.from_url(evidence_url, _snippet(text, disagree))
                )

        return self._decide(
            supporting=supporting,
            refuting=refuting,
            support_hosts=support_hosts,
            refute_hosts=refute_hosts,
            rejected_same_host=rejected_same_host,
            examined=examined,
            budget_halted=budget_halted,
        )

    # -- helpers --------------------------------------------------------------- #

    def _resolve_candidates(
        self,
        fact: CleanFact,
        context: Optional[VerifyContext],
        candidate_urls: Optional[Sequence[str]],
    ) -> list[str]:
        """The candidate URLs to consult: an explicit per-call list wins; else the
        constructor list plus any ``candidate_provider`` output. Deduped in order,
        capped at ``max_candidates``."""
        raw: list[str] = []
        if candidate_urls is not None:
            raw.extend(candidate_urls)
        else:
            raw.extend(self._candidate_urls)
            if self._candidate_provider is not None:
                try:
                    raw.extend(self._candidate_provider(fact, context) or ())
                except Exception:
                    pass  # a provider hiccup must not sink verification
        out: list[str] = []
        seen: set[str] = set()
        for u in raw:
            if not isinstance(u, str):
                continue
            u = u.strip()
            if not u or u in seen:
                continue
            seen.add(u)
            out.append(u)
            if len(out) >= self._max_candidates:
                break
        return out

    async def _fetch(
        self,
        url: str,
        fetchers: Sequence[PageFetcher],
        want: str,
        remaining_budget: float,
    ) -> tuple[Optional[FetchedPage], float, bool]:
        """Fetch ONE url by walking the ladder cheapest-first, metering each rung's
        cost through the shared :func:`source_cost` reducer against
        ``remaining_budget``.

        Returns ``(best_page, cost_spent, budget_halted)``. Stops at the first rung
        that returns content. A paid rung that would exceed the remaining budget is
        NOT attempted (``budget_halted=True``); a
        :class:`~cograph_client.retrieval.errors.CostCeilingExceeded` from a rung is
        caught and reported as a halt. Never raises."""
        spent = 0.0
        best: Optional[FetchedPage] = None
        for fetcher in fetchers:
            _is_paid, rung_cost = source_cost(fetcher)  # THE one shared cost seam
            if rung_cost > 0.0 and spent + rung_cost > remaining_budget:
                return best, spent, True
            try:
                page = await fetcher.fetch(url, want=want)
            except CostCeilingExceeded:
                return best, spent, True
            except Exception:
                continue  # fetchers shouldn't raise, but never trust — try next rung
            spent += rung_cost
            if page is None:
                continue
            if page.has_content():
                return page, spent, False
            if best is None:
                best = page
        return best, spent, False

    @staticmethod
    def _decide(
        *,
        supporting: list[EvidenceRef],
        refuting: list[EvidenceRef],
        support_hosts: set[str],
        refute_hosts: set[str],
        rejected_same_host: int,
        examined: int,
        budget_halted: bool,
    ) -> VerifierResult:
        """Aggregate per-page findings into one verdict."""
        same_host_note = (
            f" ({rejected_same_host} same-host candidate(s) rejected as non-independent)"
            if rejected_same_host
            else ""
        )
        if supporting:
            return VerifierResult(
                verdict=TruthVerdict.SUPPORTED,
                confidence=_confidence(len(support_hosts)),
                evidence=tuple(supporting),
                reason=(
                    f"corroborated by {len(support_hosts)} independent host(s)"
                    + same_host_note
                ),
            )
        if refuting:
            return VerifierResult(
                verdict=TruthVerdict.REFUTED,
                confidence=_confidence(len(refute_hosts)),
                evidence=tuple(refuting),
                reason=(
                    f"contradicted by {len(refute_hosts)} independent host(s)"
                    + same_host_note
                ),
            )
        if budget_halted:
            return _degrade(
                "budget/cost ceiling reached before independent corroboration was found"
                + same_host_note
            )
        if examined == 0 and rejected_same_host:
            return _degrade(
                "only same-host evidence was available — a fact is not SUPPORTED by "
                "its own source" + same_host_note
            )
        return _degrade("no independent evidence corroborated or refuted the fact" + same_host_note)

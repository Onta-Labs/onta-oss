"""A9 Run Manifest — the run as a first-class object (ONTA-273).

Onta's pipeline (P0-P9) emits eleven inter-stage artifacts (A0-A10). **A9 is the
Run Manifest**: the one artifact that answers "what did this run actually do?" —
per-item status, drops, retries, and spend-to-date — so a run that halted partway
can HONESTLY caveat coverage it could not otherwise see.

**Why this exists (the triggering incident).** On 2026-07-04 OpenRouter returned
``402 Payment Required`` (prepaid credits exhausted) mid-run. Extraction correctly
errored, but nothing *owned the run's fate*: the run sat "Running" with partial /
zero results and no component "at fault". No artifact carried "failed for 30% of
items", so downstream could not caveat coverage it could not see. The manifest is
that artifact — and the :class:`RunState` machine here GUARANTEES a run always
reaches a terminal state (``completed`` / ``failed`` / ``cancelled``), never a
stuck spinner.

**Run state machine.** ``pending → running → {completed, failed, cancelled}``.
The three latter are terminal; :meth:`RunState.is_terminal` is the single source
of truth. On provider exhaustion (402 billing / sustained 429 rate-limiting) the
run halts to ``failed`` with a user-visible reason AND a coverage view showing
"N of M items completed before halt".

**Relationship to the existing job model.** The "run" today is an
:class:`~cograph_client.enrichment.models.EnrichJob` (it already covers
enrichment / dedupe / reconciliation / discovery). The manifest hangs off the job
(``EnrichJob.manifest``) so it persists via the existing jsonb job payload with no
schema migration, and composes with the job's existing ``ProviderLog`` /
``JobErrorItem`` / ``RowResult`` surfaces (which answer "which providers / what
errors / which rows") rather than duplicating them — the manifest owns the
run-level per-item COVERAGE + spend + terminal reason.

Boundary: OSS. Imports only stdlib / pydantic / ``cograph_client.retrieval.errors``
(itself stdlib-only), so there is no import cycle with ``enrichment.models``
(which imports THIS module).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field

from cograph_client.retrieval.errors import (
    CostCeilingExceeded,
    LLMAuthError,
    LLMBillingError,
    LLMError,
    LLMRateLimitError,
)

# Bound on the per-item detail we PERSIST in ``items`` — a huge run must not bloat
# the job payload. The integer counters (``completed`` / ``dropped`` / ``total``)
# stay EXACT regardless; ``items`` is a capped diagnostic sample (dropped items are
# the diagnostic gold, so they are retained preferentially — see ``_append_item``).
_MAX_ITEMS = 2000


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RunState(str, Enum):
    """The A9 run's lifecycle state.

    ``pending → running → {completed, failed, cancelled}``. A run ALWAYS reaches
    one of the three terminal states — that is the whole point of ONTA-273 (no run
    may hang in ``running`` forever). Mirrors — but does not replace —
    :class:`~cograph_client.enrichment.models.JobStatus`; the job status stays the
    authoritative lifecycle field, the manifest state is the run-artifact view.
    """

    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"

    def is_terminal(self) -> bool:
        return self in (RunState.completed, RunState.failed, RunState.cancelled)


class HaltReasonKind(str, Enum):
    """Why a run halted before completing every item.

    ``billing`` (402) and ``rate_limit`` (sustained 429) are the two PROVIDER
    EXHAUSTION modes (:attr:`is_provider_exhaustion`) — the credits ran out or the
    account is being throttled to a standstill; both surface a "top up / back off"
    reason. ``auth`` (401) is a bad/revoked key. ``timeout`` is the wall-clock
    guard. ``cost_ceiling`` (ONTA-282) is a GOVERNANCE halt — the run reached its
    HARD per-run spend ceiling (the operator-set cost envelope); the account is
    fine, so it is deliberately NOT :attr:`is_provider_exhaustion` (the fix is
    "raise the budget", not "top up / back off"). ``error`` is any other fatal.
    ``none`` means no halt (a clean run).
    """

    none = "none"
    billing = "billing"
    auth = "auth"
    rate_limit = "rate_limit"
    timeout = "timeout"
    cost_ceiling = "cost_ceiling"
    error = "error"
    cancelled = "cancelled"

    @property
    def is_provider_exhaustion(self) -> bool:
        return self in (HaltReasonKind.billing, HaltReasonKind.rate_limit)


ItemStatus = Literal["pending", "completed", "dropped", "skipped"]


def classify_halt(exc: BaseException) -> HaltReasonKind:
    """Map a fatal exception to the manifest's :class:`HaltReasonKind`.

    Provider-exhaustion errors (:class:`LLMBillingError` 402,
    :class:`LLMRateLimitError` sustained 429) and :class:`LLMAuthError` (401) are
    the systemic LLM-backend failures; a :class:`CostCeilingExceeded` maps to
    ``cost_ceiling`` (the run's HARD spend envelope, ONTA-282 — a governance halt,
    NOT provider exhaustion); a timeout maps to ``timeout``; everything else is a
    generic ``error``. Kept here (not on the exceptions) so the stdlib-only error
    module stays UI-free.
    """
    if isinstance(exc, CostCeilingExceeded):
        return HaltReasonKind.cost_ceiling
    if isinstance(exc, LLMBillingError):
        return HaltReasonKind.billing
    if isinstance(exc, LLMRateLimitError):
        return HaltReasonKind.rate_limit
    if isinstance(exc, LLMAuthError):
        return HaltReasonKind.auth
    # A generic LLMError with no more-specific subclass is still an LLM-backend
    # fatal; classify it as an error (its message carries the detail).
    if isinstance(exc, (TimeoutError,)):
        return HaltReasonKind.timeout
    return HaltReasonKind.error


def resolve_spend_ceiling(
    explicit: Optional[float], default: float
) -> Optional[float]:
    """Pick a run's effective HARD per-run spend ceiling (USD), for both driver
    loops (ONTA-282).

    An explicit per-run value (e.g. ``EnrichJob.spend_ceiling_usd``) wins when
    set; otherwise the deployment default (config ``enrich_spend_ceiling_usd``) is
    used. A non-positive / unset / malformed result normalises to ``None`` —
    UNLIMITED — so a run with no configured envelope NEVER halts on cost (every
    existing run is unchanged). Returned value feeds ``RunManifest.spend_ceiling_usd``.
    """
    val = explicit if explicit is not None else default
    try:
        val = float(val or 0.0)
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


class ManifestItem(BaseModel):
    """One item's outcome in the run (a diagnostic sample entry — see ``_MAX_ITEMS``).

    ``ref`` identifies the item (an entity URI, a row key, or a ``batch:<n>``
    label). ``status`` is ``completed`` (processed), ``dropped`` (lost to a halt /
    error), ``skipped`` (deliberately not processed), or ``pending``. ``reason``
    carries a drop/skip cause; ``spend_usd`` the item's attributable spend.
    """

    ref: str = ""
    status: ItemStatus = "pending"
    retries: int = 0
    reason: Optional[str] = None
    spend_usd: float = 0.0


class RunCoverage(BaseModel):
    """The manifest's headline coverage view — "N of M items; K dropped; reason".

    This is what downstream reads to caveat partial coverage (the whole reason A9
    exists). ``complete`` is True only for a clean, fully-covered run.
    """

    total: int = 0
    completed: int = 0
    dropped: int = 0
    pending: int = 0
    complete: bool = False
    summary: str = ""


class RunManifest(BaseModel):
    """A9 Run Manifest — per-item status, drops, retries, spend-to-date, terminal
    reason. Attached to the run (:class:`EnrichJob.manifest`).

    The integer counters are authoritative; ``items`` is a bounded sample. Use the
    mutating helpers (:meth:`start`, :meth:`record_completed`,
    :meth:`record_dropped`, :meth:`add_spend`, :meth:`complete`, :meth:`halt`,
    :meth:`halt_from_exception`, :meth:`cancel`) rather than poking fields, so the
    state machine + counters stay consistent, then read :meth:`coverage`.
    """

    run_id: str
    stage: str = ""
    state: RunState = RunState.pending
    total: int = 0
    completed: int = 0
    dropped: int = 0
    retries: int = 0
    spend_usd: float = 0.0
    # HARD per-run spend ceiling in USD (the A9 cost envelope, ONTA-282). ``None``
    # (or ``0``) ⇒ UNLIMITED — no ceiling, so a run with no configured envelope is
    # unchanged. When set (> 0), a driver loop checks :meth:`check_ceiling` after
    # each item's spend lands and HALTS CLEANLY (terminal ``failed``,
    # ``HaltReasonKind.cost_ceiling``, honest partial coverage) once cumulative
    # ``spend_usd`` reaches it — never a silent overspend. Set at mint time from
    # :func:`resolve_spend_ceiling`.
    spend_ceiling_usd: Optional[float] = None
    halt_reason_kind: HaltReasonKind = HaltReasonKind.none
    # The user-visible reason a run halted (surfaced on the failed job). Names
    # "provider exhaustion" for a 402/sustained-429 so the fix (top up / back off)
    # is obvious — never a bare "failed".
    halt_reason: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    items: list[ManifestItem] = Field(default_factory=list)

    # -- lifecycle ---------------------------------------------------------- #
    def start(self, *, total: Optional[int] = None) -> "RunManifest":
        """Enter ``running``; optionally seed the planned item total (M)."""
        self.state = RunState.running
        self.started_at = self.started_at or _now()
        if total is not None:
            self.set_total(total)
        return self

    def set_total(self, n: int) -> None:
        """Set the planned item denominator (M). Never below what already ran."""
        self.total = max(0, int(n), self.completed + self.dropped)

    # -- per-item accounting ------------------------------------------------ #
    def _append_item(self, item: ManifestItem) -> None:
        # Persist a bounded sample; keep dropped items preferentially since they
        # are the diagnostic value (a full run of completed items is uninteresting).
        if len(self.items) < _MAX_ITEMS:
            self.items.append(item)
        elif item.status == "dropped":
            for i, existing in enumerate(self.items):
                if existing.status == "completed":
                    self.items[i] = item
                    break

    def record_completed(
        self, ref: str = "", *, spend_usd: float = 0.0, retries: int = 0
    ) -> None:
        self.completed += 1
        self.spend_usd += max(0.0, spend_usd)
        self.retries += max(0, retries)
        if self.total < self.completed + self.dropped:
            self.total = self.completed + self.dropped
        self._append_item(
            ManifestItem(
                ref=ref, status="completed", retries=retries, spend_usd=spend_usd
            )
        )

    def record_dropped(
        self, ref: str = "", *, reason: Optional[str] = None, retries: int = 0
    ) -> None:
        self.dropped += 1
        self.retries += max(0, retries)
        if self.total < self.completed + self.dropped:
            self.total = self.completed + self.dropped
        self._append_item(
            ManifestItem(ref=ref, status="dropped", reason=reason, retries=retries)
        )

    def record_skipped(self, ref: str = "", *, reason: Optional[str] = None) -> None:
        self._append_item(ManifestItem(ref=ref, status="skipped", reason=reason))

    def record_retry(self, ref: str = "") -> None:
        self.retries += 1

    def add_spend(self, usd: float) -> None:
        self.spend_usd += max(0.0, usd)

    # -- cost envelope (ONTA-282) ------------------------------------------ #
    def over_ceiling(self) -> bool:
        """True when a HARD per-run ceiling is set (> 0) AND cumulative
        ``spend_usd`` has reached it. ``None`` / ``0`` ceiling ⇒ UNLIMITED ⇒
        always False (a run with no envelope never trips)."""
        return (
            self.spend_ceiling_usd is not None
            and self.spend_ceiling_usd > 0
            and self.spend_usd >= self.spend_ceiling_usd
        )

    def check_ceiling(self) -> Optional["CostCeilingExceeded"]:
        """Return a typed :class:`CostCeilingExceeded` when the run has reached its
        HARD per-run spend ceiling, else ``None`` (headroom left / no ceiling).

        The driver loops call this after each item's spend lands and, on a
        non-``None`` return, ``raise`` it (or set the fatal flag) so it flows
        through the SAME terminal-halt machinery a 402 does — ``classify_halt`` →
        ``cost_ceiling`` → ``halt_from_exception`` — reaching terminal ``failed``
        with an honest partial-coverage manifest. The message names the spend and
        the ceiling so the halt reason is self-explanatory ("top up the budget")."""
        if not self.over_ceiling():
            return None
        return CostCeilingExceeded(
            f"run spend ${self.spend_usd:.2f} reached the "
            f"${float(self.spend_ceiling_usd):.2f} cost ceiling"
        )

    # -- terminal transitions ---------------------------------------------- #
    def complete(self) -> "RunManifest":
        """Terminal ``completed`` — a clean run. Settles the total DOWN to the
        items that actually existed (``completed + dropped``): a planned upper
        bound seeded at :meth:`start` (e.g. a discovery cap of 200 that only found
        50 rows) must not read as "50 of 200 — 150 dropped" on a run that in fact
        finished cleanly. A halt keeps the planned denominator instead (see
        :meth:`halt`)."""
        self.state = RunState.completed
        self.total = self.completed + self.dropped
        self.ended_at = _now()
        return self

    def cancel(self, reason: Optional[str] = None) -> "RunManifest":
        self.state = RunState.cancelled
        self.halt_reason_kind = HaltReasonKind.cancelled
        self.halt_reason = reason or "run cancelled"
        self.ended_at = _now()
        return self

    def halt(self, kind: HaltReasonKind, reason: str) -> "RunManifest":
        """Terminal ``failed`` with a user-visible ``reason``. Rolls the planned
        remainder (M − N already accounted) into ``dropped`` so coverage reflects
        "N of M items completed before halt"."""
        self.state = RunState.failed
        self.halt_reason_kind = kind
        self.halt_reason = reason
        remainder = self.total - self.completed - self.dropped
        if remainder > 0:
            self.dropped += remainder
        self.ended_at = _now()
        return self

    def halt_from_exception(
        self, exc: BaseException, *, landed_note: str = ""
    ) -> "RunManifest":
        """Halt to ``failed``, deriving the kind + a user-visible reason from a
        fatal exception. Provider-exhaustion errors get the explicit
        "provider exhaustion" phrase so the reason is unambiguous."""
        kind = classify_halt(exc)
        detail = str(exc).strip()
        if kind.is_provider_exhaustion:
            reason = f"provider exhaustion — {detail}"
        elif kind is HaltReasonKind.cost_ceiling:
            # A governance halt, not a provider failure: the run hit its HARD
            # per-run spend envelope. Phrase it so the fix (raise the budget) is
            # obvious and distinct from a 402 "top up the account".
            reason = f"cost envelope exceeded — {detail}"
        elif kind is HaltReasonKind.auth:
            reason = f"provider authentication failure — {detail}"
        elif kind is HaltReasonKind.timeout:
            reason = f"run timed out — {detail}"
        else:
            reason = detail or "run failed"
        if landed_note:
            reason = f"{reason} {landed_note.strip()}"
        return self.halt(kind, reason)

    # -- views -------------------------------------------------------------- #
    def coverage(self) -> RunCoverage:
        total = max(self.total, self.completed + self.dropped)
        pending = max(0, total - self.completed - self.dropped)
        complete = (
            self.state == RunState.completed and self.dropped == 0 and pending == 0
        )
        parts = [f"{self.completed} of {total} items completed"]
        if self.dropped:
            parts.append(f"{self.dropped} dropped")
        if pending:
            parts.append(f"{pending} not started")
        if self.halt_reason:
            parts.append(self.halt_reason)
        return RunCoverage(
            total=total,
            completed=self.completed,
            dropped=self.dropped,
            pending=pending,
            complete=complete,
            summary="; ".join(parts),
        )


__all__ = [
    "HaltReasonKind",
    "ItemStatus",
    "ManifestItem",
    "RunCoverage",
    "RunManifest",
    "RunState",
    "classify_halt",
    "resolve_spend_ceiling",
]

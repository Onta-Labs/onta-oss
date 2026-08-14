"""Per-provider outcome tally for a single enrichment run."""

from __future__ import annotations

from typing import Optional

from infona_client.enrichment.executor_const import _MAX_ERROR_MSG
from infona_client.enrichment.models import JobErrorItem, ProviderLog


class _ProviderTally:
    """Accumulates per-provider outcomes across a single enrichment run so the
    job can carry a ``provider_logs`` (what each provider we used did) and an
    ``error_summary`` (the potential errors, aggregated) for the run-detail view.

    Concurrency: the executor's worker pool runs cooperatively under one event
    loop and every ``record*`` mutation is synchronous (no ``await`` between read
    and write), so the plain counters here are race-free — the same property the
    existing ``job.progress`` increments rely on. No lock needed.
    """

    def __init__(self) -> None:
        self._by_provider: dict[str, ProviderLog] = {}
        # (provider, kind) -> [count, first_sample_message]
        self._errors: dict[tuple[str, str], list] = {}

    def _log(self, provider: str) -> ProviderLog:
        pl = self._by_provider.get(provider)
        if pl is None:
            pl = ProviderLog(provider=provider)
            self._by_provider[provider] = pl
        return pl

    def _bump_error(self, provider: str, kind: str, message: str) -> None:
        key = (provider, kind)
        rec = self._errors.get(key)
        if rec is None:
            self._errors[key] = [1, (message or "")[:_MAX_ERROR_MSG]]
        else:
            rec[0] += 1  # keep the first representative message

    def record_missing(self, provider: str) -> None:
        """A chain named a provider that isn't registered here (call once per
        provider per job — the caller already gates on a 'missing' set)."""
        self._log(provider).status = "skipped"
        self._bump_error(
            provider,
            "missing",
            f"provider '{provider}' is not registered on this deployment",
        )

    def record_attempt(
        self,
        provider: str,
        *,
        cache_hit: bool,
        outcome: str,  # "match" | "no_match" | "timeout" | "error"
        error_msg: Optional[str] = None,
    ) -> None:
        pl = self._log(provider)
        if cache_hit:
            pl.cache_hits += 1
        else:
            pl.attempts += 1
        if outcome == "match":
            pl.matches += 1
        elif outcome == "no_match":
            pl.no_match += 1
        elif outcome == "timeout":
            pl.timeouts += 1
            pl.last_error = (error_msg or "lookup timed out")[:_MAX_ERROR_MSG]
            self._bump_error(provider, "timeout", error_msg or "lookup timed out")
        elif outcome == "error":
            pl.errors += 1
            if error_msg:
                pl.last_error = error_msg[:_MAX_ERROR_MSG]
            self._bump_error(provider, "error", error_msg or "lookup failed")

    def to_logs(self) -> list[ProviderLog]:
        out: list[ProviderLog] = []
        for pl in self._by_provider.values():
            if pl.status != "skipped":
                if pl.matches > 0:
                    pl.status = "ok"
                elif pl.errors > 0 or pl.timeouts > 0:
                    pl.status = "error"
                else:
                    pl.status = "no_match"
            out.append(pl)
        return out

    def to_error_summary(self) -> list[JobErrorItem]:
        items = [
            JobErrorItem(provider=prov, kind=kind, message=msg, count=count)  # type: ignore[arg-type]
            for (prov, kind), (count, msg) in self._errors.items()
        ]
        items.sort(key=lambda e: e.count, reverse=True)
        return items

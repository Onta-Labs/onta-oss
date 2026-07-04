"""Scheduled catalog freshness + live-smoke audit (ONTA-194, phase 4 — OSS).

The read-path mirror of the premium pricing-freshness audit
(``cograph/enrichment/pricing_audit.py``): where that keeps paid-adapter *prices*
honest, this keeps the API-source *catalog* honest. A declarative entry can
silently rot — the upstream endpoint moves, the auth scheme changes, the result
shape shifts — and nothing surfaces it until a live request quietly returns
nothing. This module is the machine that keeps entries fresh:

  * :func:`audit_catalog` flags each entry OFFLINE as ``UNVERIFIED``
    (``verified_at`` empty/unparseable) or ``STALE`` (``verified_at`` older than
    ``max_age_days``). When ``live_smoke=True`` and an executor is supplied it
    also runs each entry's declared ``smoke_bindings`` through the generic
    executor and adds a live status (``OK`` / ``EMPTY`` / ``UNREACHABLE`` /
    ``DORMANT`` / ``NO_SMOKE``); ``EMPTY`` / ``UNREACHABLE`` also raise a flag.
  * :func:`format_markdown` renders a human-readable report.
  * The :func:`main` CLI prints that report and exits non-zero when any *enabled*
    entry is ``UNVERIFIED`` or ``STALE``, so the GitHub Actions schedule (see
    ``.github/workflows/catalog-freshness.yml``) can gate on it and open an issue.

**The gate is deliberately offline.** Freshness (``verified_at``) is
deterministic and network-free, so the weekly CI job is reproducible and never
flakes on a transient upstream outage. ``--live-smoke`` is an opt-in richer
diagnostic (network) for a human re-confirming an entry; its findings are
reported but do **not** by themselves change the exit code.

Boundary (CLAUDE.md): OSS. Imports only stdlib + ``cograph_client.*`` — no
``from cograph.*`` — so it ships in the public package and audits the public seed
catalog. Premium overlay entries (registered via ``register_api_source_layer``)
are audited by the same function whenever the premium package is importable.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date
from typing import Optional

from .catalog import ApiSourceCatalog, get_api_source_catalog
from .executor import RegistryApiSource
from .spec import ApiSourceSpec

logger = logging.getLogger(__name__)

# Curated authoritative APIs change far less often than paid-adapter prices, so
# the default freshness horizon is longer than pricing_audit's 90 days.
_DEFAULT_MAX_AGE_DAYS = 180
_SMOKE_MAX_ROWS = 5


def _parse_verified(v: str) -> Optional[date]:
    """Parse ``verified_at`` (``YYYY-MM-DD``) to a date, or None if empty/bad."""
    if not v:
        return None
    try:
        return date.fromisoformat(v.strip())
    except ValueError:
        return None


async def _smoke_entry(spec: ApiSourceSpec, executor: RegistryApiSource, max_rows: int) -> str:
    """Run an entry's declared ``smoke_bindings`` through the executor once.

    Returns a coarse status string:
      * ``NO_SMOKE``   — no endpoint or no ``smoke_bindings`` authored.
      * ``DORMANT``    — the entry needs an env key that isn't set (expected for a
                         paid entry in CI); not a failure.
      * ``UNREACHABLE``— the call errored (HTTP >= 400, transport, bad JSON …).
      * ``EMPTY``      — the call succeeded but returned zero rows (the spec's
                         result_path / field_mappings may have drifted).
      * ``OK``         — at least one row came back.
    """
    ep = spec.endpoint()
    if ep is None or not ep.smoke_bindings:
        return "NO_SMOKE"
    try:
        res = await executor.execute(
            spec,
            dict(ep.smoke_bindings),
            endpoint_name=ep.name,
            max_rows=max_rows,
            sample=True,
        )
    except Exception:  # noqa: BLE001 - the audit must never raise out of one entry
        logger.debug("api_registry smoke failed slug=%s", spec.slug, exc_info=True)
        return "UNREACHABLE"
    if res.dormant:
        return "DORMANT"
    if res.error:
        return "UNREACHABLE"
    if not res.rows:
        return "EMPTY"
    return "OK"


async def audit_catalog(
    max_age_days: int = _DEFAULT_MAX_AGE_DAYS,
    *,
    catalog: Optional[ApiSourceCatalog] = None,
    today: Optional[date] = None,
    live_smoke: bool = False,
    executor: Optional[RegistryApiSource] = None,
    smoke_max_rows: int = _SMOKE_MAX_ROWS,
) -> list[dict]:
    """Audit every catalog entry for freshness (and optionally live reachability).

    For each entry from ``catalog`` (default: the process catalog) this computes:

    * ``unverified`` — ``verified_at`` empty or unparseable (never hand-verified).
    * ``stale`` — ``verified_at`` older than ``max_age_days``.
    * ``age_days`` — integer age of ``verified_at`` (``None`` when unverified).
    * ``flags`` — human-readable flags raised (``"UNVERIFIED"`` / ``"STALE"`` and,
      under ``live_smoke``, ``"EMPTY"`` / ``"UNREACHABLE"``).
    * ``smoke`` — the live status (only when ``live_smoke=True``).

    Live smoke is OFF by default so the audit is fully offline (tests never hit
    the network, and the CI gate never flakes). When ``live_smoke=True`` each
    entry's ``smoke_bindings`` runs through ``executor`` (default a fresh
    ``RegistryApiSource``); inject a ``MockTransport``-backed executor in tests.
    """
    cat = catalog or get_api_source_catalog()
    ref = today or date.today()
    ex = executor or (RegistryApiSource() if live_smoke else None)

    findings: list[dict] = []
    for spec in cat.all():
        finding: dict = {
            "slug": spec.slug,
            "title": spec.title,
            "layer": spec.layer,
            "enabled": spec.enabled,
            "entitlement": spec.entitlement.value,
            "authority_level": spec.authority_level.value,
            "verified_at": spec.verified_at,
        }
        flags: list[str] = []

        vd = _parse_verified(spec.verified_at)
        if vd is None:
            finding["age_days"] = None
            finding["unverified"] = True
            finding["stale"] = False
            flags.append("UNVERIFIED")
        else:
            age = (ref - vd).days
            finding["age_days"] = age
            finding["unverified"] = False
            stale = age > max_age_days
            finding["stale"] = stale
            if stale:
                flags.append("STALE")

        if live_smoke and ex is not None:
            status = await _smoke_entry(spec, ex, smoke_max_rows)
            finding["smoke"] = status
            if status in ("EMPTY", "UNREACHABLE"):
                flags.append(status)

        finding["flags"] = flags
        findings.append(finding)

    return findings


def _needs_review(findings: list[dict]) -> bool:
    """Whether the offline freshness gate should fail (enabled entries only).

    A disabled entry that's unverified/stale is reported but does not fail CI —
    it isn't being served, so its freshness is not load-bearing.
    """
    return any(f.get("enabled") and (f.get("unverified") or f.get("stale")) for f in findings)


def format_markdown(findings: list[dict], *, max_age_days: int = _DEFAULT_MAX_AGE_DAYS) -> str:
    """Render a readable Markdown report grouped by layer."""
    n_unverified = sum(1 for f in findings if f.get("unverified"))
    n_stale = sum(1 for f in findings if f.get("stale"))
    n_empty = sum(1 for f in findings if "EMPTY" in f.get("flags", []))
    n_unreachable = sum(1 for f in findings if "UNREACHABLE" in f.get("flags", []))
    has_smoke = any("smoke" in f for f in findings)

    lines: list[str] = []
    lines.append("# API source catalog freshness report")
    lines.append("")
    lines.append(f"- Entries audited: **{len(findings)}**")
    lines.append(f"- UNVERIFIED (`verified_at` empty): **{n_unverified}**")
    lines.append(f"- STALE (older than {max_age_days} days): **{n_stale}**")
    if has_smoke:
        lines.append(f"- EMPTY (live smoke returned no rows): **{n_empty}**")
        lines.append(f"- UNREACHABLE (live smoke errored): **{n_unreachable}**")
    lines.append("")

    if not (n_unverified or n_stale or n_empty or n_unreachable):
        lines.append("All entries are fresh and verified. No action needed.")
        lines.append("")

    # Group by layer, preserving first-seen order.
    order: list[str] = []
    by_layer: dict[str, list[dict]] = {}
    for f in findings:
        layer = f.get("layer", "?")
        if layer not in by_layer:
            by_layer[layer] = []
            order.append(layer)
        by_layer[layer].append(f)

    for layer in order:
        lines.append(f"## {layer}")
        lines.append("")
        for f in sorted(by_layer[layer], key=lambda x: x.get("slug", "")):
            flags = f.get("flags") or ["OK"]
            flag_str = " ".join(f"`{x}`" for x in flags)
            disabled = "" if f.get("enabled", True) else " _(disabled)_"
            lines.append(f"- **{f.get('slug')}** — {flag_str}{disabled}")
            va = f.get("verified_at")
            age = f.get("age_days")
            if not va:
                lines.append("  - verified_at: _never (no date stamped)_")
            else:
                age_str = f" ({age} days old)" if age is not None else ""
                lines.append(f"  - verified_at: {va}{age_str}")
            if "smoke" in f:
                lines.append(f"  - live smoke: `{f['smoke']}`")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit the API-source catalog for freshness (ONTA-194 phase 4).",
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=_DEFAULT_MAX_AGE_DAYS,
        help=f"Flag an entry STALE when verified_at is older than this "
        f"(default: {_DEFAULT_MAX_AGE_DAYS}).",
    )
    parser.add_argument(
        "--live-smoke",
        action="store_true",
        help="Also run each entry's smoke_bindings through the real executor and "
        "report live status (network; off by default; does not affect exit code).",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entrypoint: print the Markdown report; exit 1 if anything needs review.

    Exit code is 1 when any *enabled* entry is UNVERIFIED or STALE (so CI can gate
    and open an issue), else 0. Live-smoke findings (EMPTY / UNREACHABLE) are
    reported but do NOT change the exit code — a transient upstream outage
    shouldn't page a human.
    """
    args = _build_parser().parse_args(argv)
    findings = asyncio.run(
        audit_catalog(
            max_age_days=args.max_age_days,
            live_smoke=args.live_smoke,
            executor=RegistryApiSource() if args.live_smoke else None,
        )
    )
    sys.stdout.write(format_markdown(findings, max_age_days=args.max_age_days))
    return 1 if _needs_review(findings) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

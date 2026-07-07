"""CLI for the deterministic QC audit — ``python -m cograph_client.qc``.

Runs the invariant library over a live tenant's graphs and prints violations, exiting
non-zero when errors are found so it can gate a backfill or a CI step. The SPARQL
endpoint is always an argument (never baked in): point ``--endpoint`` at a local
Fuseki/pyoxigraph store, or at a reachable Neptune endpoint for a production audit.

  # audit one KG
  python -m cograph_client.qc --endpoint http://127.0.0.1:3030 --tenant demo --kg imdb

  # audit every KG in a tenant (+ the base graph, catching leaked instance data)
  python -m cograph_client.qc --endpoint <url> --tenant demo --json

  # hard gate: fail on warnings too, restrict to one invariant
  python -m cograph_client.qc --endpoint <url> --tenant demo --strict \
      --include node_edge_on_attrs_predicate
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import structlog

from cograph_client.graph.client import NeptuneClient
from cograph_client.qc.audit import format_report, run_audit


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="python -m cograph_client.qc",
        description="Run deterministic KG invariant checks over a tenant's graphs.",
    )
    ap.add_argument(
        "--endpoint",
        default="http://127.0.0.1:3030",
        help="SPARQL 1.1 endpoint (local Fuseki/pyoxigraph or a reachable Neptune).",
    )
    ap.add_argument(
        "--backend",
        default="neptune",
        choices=["neptune", "fuseki"],
        help="Endpoint dialect (query/update paths). Default: neptune.",
    )
    ap.add_argument("--tenant", required=True, help="Tenant id to audit.")
    ap.add_argument(
        "--kg",
        default=None,
        help="Audit a single KG by name. Omit to audit every KG in the tenant + base graph.",
    )
    ap.add_argument(
        "--include",
        default=None,
        help="Comma-separated invariant names to restrict to (default: all).",
    )
    ap.add_argument("--json", action="store_true", help="Emit the report as JSON.")
    ap.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero on warnings too (default: only errors fail).",
    )
    ap.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show routine query logs (default: quiet — only the report on stdout).",
    )
    return ap.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    neptune = NeptuneClient(endpoint=args.endpoint, backend=args.backend)
    if not await neptune.health():
        print(f"!! store not reachable at {args.endpoint}", file=sys.stderr)
        return 2
    include = (
        {name.strip() for name in args.include.split(",") if name.strip()}
        if args.include
        else None
    )
    report = await run_audit(
        neptune, tenant=args.tenant, kg=args.kg, include=include
    )
    print(format_report(report, as_json=args.json))
    return report.exit_code(strict=args.strict)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    # Quiet the library's routine info logs so stdout is just the report (keeps
    # `--json | jq` clean); -v restores them for debugging a slow/stuck audit.
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.INFO if args.verbose else logging.WARNING
        )
    )
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())

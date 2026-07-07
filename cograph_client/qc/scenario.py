"""Ingestion QC fuzzer — run REAL datasets through the REAL ingestion pipeline into a
disposable tenant, then run the deterministic invariants over what the ingester actually
produced.

`audit.py` answers "is an EXISTING graph well-formed." This answers the harder,
forward-looking question: "does the INGESTER produce a well-formed graph when it meets a
fresh dataset it has never seen." It is property-based testing (fuzzing) for the write
path — the ingester is the system under test, a real dataset is the input, and the
invariants are the properties that must hold on the output. Any violation is a
reproducing case: this dataset, ingested now, yields a structural defect.

Why this generalizes where a golden-output harness can't: the invariants
(`cograph_client.qc.invariants`) are structural laws, not per-domain expected shapes, so
you can point this at ANY dataset — no hand-authored answer key — and still get a hard,
un-gameable pass/fail. That is the whole reason the loop can be fed diverse, even
agent-sourced, data without overfitting to a fixture.

Shape (mirrors the OSS audit → premium nightly split): this module is the reusable OSS
core — ingest one dataset, audit it, return a structured result. The premium
orchestration (track new/persisting/resolved across runs via the gap ledger; source
datasets from the live web) wraps this; it does not live here.

Safety: this WRITES into — and, between datasets, DROPs the graphs of — the target
tenant. It only ever touches the tenant you name (scoped drops, never `DROP ALL`), and
the CLI refuses a non-disposable-looking tenant unless explicitly overridden. Point it at
a local, disposable store.

OSS: imports only stdlib + `cograph_client.*`. No proprietary identifiers, no endpoint
baked in. See docs/specs/continuous_kg_qc_eval_spec.md.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from cograph_client.graph.queries import (
    kg_graph_uri,
    parse_kg_graph_uri,
    tenant_graph_uri,
)
from cograph_client.qc.audit import AuditReport, run_audit
from cograph_client.qc.invariants import INVARIANTS, Violation

# Default tenant is disposable by name; the CLI's safety guard keys off this prefix set.
DEFAULT_TENANT = "qc-scenario"
_DISPOSABLE_PREFIXES = ("qc-", "test-", "scenario-", "tmp-", "fuzz-", "bench-")


# --------------------------------------------------------------------------- #
# Inputs / outputs
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Dataset:
    """One dataset to ingest and audit. ``content`` is the raw payload the ingester
    accepts (a JSON array of row objects, CSV text, or free text per ``content_type``);
    ``name`` becomes the per-KG graph the instances land in; ``domain`` is a free label
    for reporting."""

    name: str
    content: str
    content_type: str = "json"
    domain: str = ""


@dataclass
class ScenarioResult:
    """One dataset's outcome: the ingestion stats + the invariant audit — or an ingestion
    ``error`` (a bad dataset must not sink the sweep; it is recorded, not raised)."""

    dataset: str
    tenant: str
    kg: str
    report: Optional[AuditReport] = None
    types_created: list[str] = field(default_factory=list)
    entities_resolved: int = 0
    triples_inserted: int = 0
    error: Optional[str] = None

    @property
    def error_count(self) -> int:
        return self.report.error_count if self.report else 0

    @property
    def warn_count(self) -> int:
        return self.report.warn_count if self.report else 0

    @property
    def violations(self) -> list[Violation]:
        return self.report.violations if self.report else []

    @property
    def ok(self) -> bool:
        """Clean = ingested without error AND no error-severity invariant fired."""
        return self.error is None and self.error_count == 0


# A resolver factory keeps the ingestion dependency injectable — the real pipeline in
# production/local runs, a controlled fake in CI tests (which cannot call the LLM).
ResolverFactory = Callable[[object], object]


def _make_resolver(neptune) -> object:
    """The real ingestion resolver, wired the way the decomposition harness wires it:
    a fresh per-run verdict cache (so type-match decisions don't leak across datasets),
    no embeddings, key unused on the OpenRouter extraction path."""
    from cograph_client.resolver.schema_resolver import SchemaResolver
    from cograph_client.resolver.verdict_cache import JsonVerdictCache

    cache_path = Path(tempfile.gettempdir()) / f"qc_scenario_verdicts_{time.time_ns()}.json"
    return SchemaResolver(
        neptune=neptune,
        anthropic_key="unused-on-openrouter-path",
        verdict_cache=JsonVerdictCache(cache_path),
        embedding_service=None,
    )


# --------------------------------------------------------------------------- #
# Reset — scoped, never DROP ALL
# --------------------------------------------------------------------------- #
async def _tenant_graphs(neptune, tenant: str) -> list[str]:
    """This tenant's base graph plus every per-KG instance graph that currently holds
    data for it — discovered from the store, so a reset covers whatever a prior run
    left. Membership is decided by the canonical ``parse_kg_graph_uri`` (other tenants'
    graphs and companion graphs don't match), so the set can only ever be this tenant's."""
    graphs = {tenant_graph_uri(tenant)}
    result = await neptune.query("SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }")
    for binding in result.get("results", {}).get("bindings", []):
        cell = binding.get("g")
        uri = cell.get("value", "") if isinstance(cell, dict) else ""
        parsed = parse_kg_graph_uri(uri)
        if parsed and parsed[0] == tenant:
            graphs.add(uri)
    return sorted(graphs)


async def reset_tenant(neptune, tenant: str) -> None:
    """DROP just THIS tenant's graphs so a fresh dataset starts from a clean ontology.
    Scoped by construction (never ``DROP ALL``): it can only affect ``tenant``, so it is
    safe even against a shared store. For a disposable tenant this is a full reset."""
    for g in await _tenant_graphs(neptune, tenant):
        await neptune.update(f"DROP SILENT GRAPH <{g}>")


# --------------------------------------------------------------------------- #
# Core: ingest one dataset, audit the result
# --------------------------------------------------------------------------- #
async def run_scenario(
    neptune,
    *,
    tenant: str,
    dataset: Dataset,
    include: Optional[set[str]] = None,
    resolver_factory: ResolverFactory = _make_resolver,
) -> ScenarioResult:
    """Ingest ``dataset`` through the real pipeline into ``kg_graph_uri(tenant, name)``
    (instances there, ontology in the tenant base graph — the realistic per-KG path),
    then run the invariants over that KG. Ingestion failures are captured on the result,
    not raised, so a sweep continues past one bad dataset."""
    kg = dataset.name
    instance_graph = kg_graph_uri(tenant, kg)
    try:
        resolver = resolver_factory(neptune)
        res = await resolver.ingest(
            dataset.content,
            tenant,
            content_type=dataset.content_type,
            source=f"qc-scenario:{dataset.name}",
            instance_graph=instance_graph,
        )
    except Exception as exc:  # noqa: BLE001 — one bad dataset must not sink the sweep
        return ScenarioResult(
            dataset=dataset.name, tenant=tenant, kg=kg,
            error=f"{type(exc).__name__}: {exc}",
        )

    report = await run_audit(neptune, tenant=tenant, kg=kg, include=include)
    return ScenarioResult(
        dataset=dataset.name, tenant=tenant, kg=kg, report=report,
        types_created=list(getattr(res, "types_created", []) or []),
        entities_resolved=int(getattr(res, "entities_resolved", 0) or 0),
        triples_inserted=int(getattr(res, "triples_inserted", 0) or 0),
    )


async def run_catalog(
    neptune,
    *,
    tenant: str,
    datasets: list[Dataset],
    include: Optional[set[str]] = None,
    reset_between: bool = True,
    resolver_factory: ResolverFactory = _make_resolver,
) -> list[ScenarioResult]:
    """Run each dataset through :func:`run_scenario`. With ``reset_between`` (default) the
    tenant is wiped before each dataset so every ingest meets a clean ontology and the
    audit reflects that dataset alone — no cross-dataset type reuse or accumulation."""
    results: list[ScenarioResult] = []
    for dataset in datasets:
        if reset_between:
            await reset_tenant(neptune, tenant)
        results.append(
            await run_scenario(
                neptune, tenant=tenant, dataset=dataset,
                include=include, resolver_factory=resolver_factory,
            )
        )
    return results


# --------------------------------------------------------------------------- #
# Fixture catalog (open data shipped with the OSS repo)
# --------------------------------------------------------------------------- #
def _default_fixtures_dir() -> Path:
    """``tests/fixtures/decomp`` relative to the OSS repo root — the trappy, real-domain
    decomposition fixtures (ONTA-199). They double as a runnable default catalog."""
    return Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "decomp"


def load_fixture_datasets(
    fixtures_dir: Optional[str | Path] = None,
    *,
    names: Optional[set[str]] = None,
) -> list[Dataset]:
    """Load the open JSON fixtures as datasets (excludes ``*.seed_ontology.json`` seed
    files). Returns ``[]`` if the directory is absent (fixtures aren't packaged into the
    installed wheel) — callers then supply their own datasets."""
    base = Path(fixtures_dir) if fixtures_dir else _default_fixtures_dir()
    if not base.is_dir():
        return []
    datasets: list[Dataset] = []
    for path in sorted(base.glob("*.json")):
        if path.name.endswith(".seed_ontology.json"):
            continue
        if names and path.stem not in names:
            continue
        datasets.append(
            Dataset(name=path.stem, content=path.read_text(), content_type="json", domain=path.stem)
        )
    return datasets


# --------------------------------------------------------------------------- #
# Rendering + exit code
# --------------------------------------------------------------------------- #
_MARK = {"error": "x", "warn": "!"}


def format_scenarios(results: list[ScenarioResult]) -> str:
    lines = ["QC scenario sweep"]
    total_err = total_warn = 0
    for r in results:
        if r.error:
            lines.append(f"  ✗ {r.dataset}: INGEST ERROR — {r.error}")
            continue
        total_err += r.error_count
        total_warn += r.warn_count
        status = "clean" if r.ok else f"{r.error_count} error(s), {r.warn_count} warning(s)"
        lines.append(
            f"  {r.dataset}: {status}  "
            f"[types={len(r.types_created)}, entities={r.entities_resolved}, triples={r.triples_inserted}]"
        )
        for v in r.violations:
            lines.append(f"      {_MARK.get(v.severity, '?')} [{v.severity}] {v.invariant}: {v.detail}")
    ingest_errors = sum(1 for r in results if r.error)
    tail = f", {ingest_errors} ingest error(s)" if ingest_errors else ""
    lines.append(
        f"\nSummary: {total_err} error(s), {total_warn} warning(s){tail} "
        f"across {len(results)} dataset(s)."
    )
    return "\n".join(lines)


def scenarios_to_dict(results: list[ScenarioResult]) -> dict:
    from cograph_client.qc.audit import report_to_dict

    return {
        "datasets": len(results),
        "error_count": sum(r.error_count for r in results),
        "warn_count": sum(r.warn_count for r in results),
        "ingest_errors": sum(1 for r in results if r.error),
        "results": [
            {
                "dataset": r.dataset, "tenant": r.tenant, "kg": r.kg,
                "error": r.error, "types_created": r.types_created,
                "entities_resolved": r.entities_resolved, "triples_inserted": r.triples_inserted,
                "audit": report_to_dict(r.report) if r.report else None,
            }
            for r in results
        ],
    }


def worst_exit_code(results: list[ScenarioResult], *, strict: bool = False) -> int:
    """0 safe · 1 a quality violation fired · 2 an ingestion failed (infra, not a KG
    defect — e.g. the LLM/credits were unavailable), so a scheduler can tell "the
    ingester produced a bad graph" from "the run couldn't complete." """
    if any(r.error for r in results):
        return 2
    if any(r.error_count for r in results):
        return 1
    if strict and any(r.warn_count for r in results):
        return 1
    return 0


# --------------------------------------------------------------------------- #
# CLI — `python -m cograph_client.qc.scenario`
# --------------------------------------------------------------------------- #
def _resolve_include(raw: Optional[str]) -> Optional[set[str]]:
    """Validate ``--include`` names against the catalogue so a typo errors loudly instead
    of silently selecting zero invariants and reporting a vacuous pass."""
    if not raw:
        return None
    known = {i.name for i in INVARIANTS}
    requested = {name.strip() for name in raw.split(",") if name.strip()}
    # An all-separator/whitespace string (e.g. "," or " , ") yields NO names. Fall back to
    # run-all (None), never an empty set — an empty `include` would select ZERO invariants
    # and report a vacuous pass on a genuinely bad graph (the #135 failure class).
    if not requested:
        return None
    unknown = requested - known
    if unknown:
        raise ValueError(
            f"unknown invariant(s): {', '.join(sorted(unknown))}. "
            f"known: {', '.join(sorted(known))}"
        )
    return requested


def _is_disposable(tenant: str) -> bool:
    return tenant.startswith(_DISPOSABLE_PREFIXES)


async def _run_cli(args: argparse.Namespace) -> int:
    from cograph_client.graph.client import NeptuneClient

    if not _is_disposable(args.tenant) and not args.allow_unsafe_tenant:
        print(
            f"!! refusing to ingest into non-disposable tenant {args.tenant!r} — this "
            f"harness WRITES and DROPs graphs. Use a tenant prefixed "
            f"{'/'.join(_DISPOSABLE_PREFIXES)} or pass --allow-unsafe-tenant.",
            file=sys.stderr,
        )
        return 2

    try:
        include = _resolve_include(args.include)
    except ValueError as exc:
        print(f"!! {exc}", file=sys.stderr)
        return 2

    neptune = NeptuneClient(endpoint=args.endpoint, backend=args.backend)
    try:
        if not await neptune.health():
            print(f"!! store not healthy at {args.endpoint}", file=sys.stderr)
            return 2

        datasets = load_fixture_datasets(
            args.fixtures_dir, names=set(args.dataset) if args.dataset else None
        )
        if not datasets:
            where = args.fixtures_dir or "the bundled decomp fixtures"
            print(f"!! no datasets found in {where}", file=sys.stderr)
            return 2

        results = await run_catalog(
            neptune, tenant=args.tenant, datasets=datasets,
            include=include, reset_between=not args.keep,
        )
    finally:
        await neptune.close()

    print(json.dumps(scenarios_to_dict(results), indent=2) if args.json else format_scenarios(results))
    return worst_exit_code(results, strict=args.strict)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Ingest real datasets through the real pipeline into a DISPOSABLE tenant and "
            "run the deterministic QC invariants over the result. WRITES and DROPs the "
            "target tenant's graphs — point --endpoint at a local, disposable store."
        )
    )
    ap.add_argument("--endpoint", required=True, help="SPARQL store endpoint (local + disposable)")
    ap.add_argument("--backend", default="fuseki", help="graph backend (default: fuseki, i.e. the local store)")
    ap.add_argument("--tenant", default=DEFAULT_TENANT, help=f"disposable tenant (default: {DEFAULT_TENANT})")
    ap.add_argument("--fixtures-dir", default=None, help="datasets dir (default: bundled decomp fixtures)")
    ap.add_argument("--dataset", action="append", help="only this dataset name (repeatable)")
    ap.add_argument("--include", default=None, help="comma list of invariant names to run (default: all)")
    ap.add_argument("--keep", action="store_true", help="don't reset the tenant between datasets")
    ap.add_argument("--strict", action="store_true", help="exit non-zero on warnings too")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    ap.add_argument("--allow-unsafe-tenant", action="store_true", help="permit a non-disposable-looking tenant")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(_run_cli(args)))


if __name__ == "__main__":
    main()

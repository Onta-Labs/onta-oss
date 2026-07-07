"""Run the deterministic QC invariants over a tenant's real graphs — the thing that
turns ``cograph_client.qc.invariants`` from a library into a usable audit.

Two callers, one core:
  * programmatic — the nightly QC loop and the harness call ``run_audit(neptune, ...)``
    and consume the structured :class:`AuditReport`;
  * human / one-off — ``python -m cograph_client.qc --endpoint <url> --tenant <t>``
    (see ``__main__.py``) prints the report and exits non-zero when errors are found,
    so it can gate a PR or a backfill.

Graph model (mirrors ``graph/queries``): a tenant's ONTOLOGY declarations live in the
tenant BASE graph (``tenant_graph_uri``); INSTANCE data lives in per-KG named graphs
(``kg_graph_uri``). So an invariant run over one KG passes ``graph_uri=<that KG>`` and
``onto_graph_uri=<tenant base>``. With no ``--kg`` we ENUMERATE the tenant's KG graphs
(store-agnostic, via ``parse_kg_graph_uri``) and audit each, plus the base graph itself
— the base graph should hold only declarations, so any instance edge found there is
leaked instance data (the ONTA-198 empty-``kg_name`` class), which the same invariants
surface for free.

OSS: imports only stdlib + ``cograph_client.*``. No proprietary identifiers, no endpoint
baked in — the endpoint is always a caller/CLI argument. See
docs/specs/continuous_kg_qc_eval_spec.md §4a.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from cograph_client.graph.queries import (
    kg_graph_uri,
    parse_kg_graph_uri,
    tenant_graph_uri,
)
from cograph_client.qc.invariants import Violation, check_invariants

# Label used in reports for the tenant base graph audited as an instance graph.
_BASE_GRAPH_LABEL = "(tenant base graph)"


@dataclass(frozen=True)
class GraphAudit:
    """The invariant violations found in one graph, with the ontology graph joined."""

    graph_uri: str
    onto_graph_uri: Optional[str]
    violations: list[Violation]
    # Human label when ``graph_uri`` is not a per-KG graph (e.g. the base graph).
    label: Optional[str] = None

    @property
    def display(self) -> str:
        return self.label or self.graph_uri

    @property
    def errors(self) -> int:
        return sum(1 for v in self.violations if v.severity == "error")

    @property
    def warns(self) -> int:
        return sum(1 for v in self.violations if v.severity == "warn")


@dataclass(frozen=True)
class AuditReport:
    """The full audit across every graph checked for a tenant."""

    tenant: str
    audits: list[GraphAudit] = field(default_factory=list)

    @property
    def violations(self) -> list[Violation]:
        return [v for a in self.audits for v in a.violations]

    @property
    def error_count(self) -> int:
        return sum(a.errors for a in self.audits)

    @property
    def warn_count(self) -> int:
        return sum(a.warns for a in self.audits)

    @property
    def clean(self) -> bool:
        return self.error_count == 0 and self.warn_count == 0

    def exit_code(self, *, strict: bool = False) -> int:
        """0 when safe to pass, non-zero to gate. Errors always fail; warnings fail only
        under ``strict`` (so a hard PR gate can opt into zero-warning enforcement)."""
        if self.error_count:
            return 1
        if strict and self.warn_count:
            return 1
        return 0


async def _list_kg_graphs(neptune, tenant: str) -> list[str]:
    """Every per-KG instance graph that actually has data for ``tenant``, discovered
    from the store rather than assumed — so the audit covers whatever exists.

    Enumerates all named graphs and keeps those whose URI parses to this tenant via the
    canonical ``parse_kg_graph_uri`` (companion graphs — provenance, the base graph —
    don't match and are correctly excluded). Sorted for stable report ordering."""
    result = await neptune.query(
        "SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }"
    )
    graphs: list[str] = []
    for binding in result.get("results", {}).get("bindings", []):
        cell = binding.get("g")
        uri = cell.get("value", "") if isinstance(cell, dict) else ""
        parsed = parse_kg_graph_uri(uri)
        if parsed and parsed[0] == tenant:
            graphs.append(uri)
    return sorted(graphs)


async def run_audit(
    neptune,
    *,
    tenant: str,
    kg: Optional[str] = None,
    include: Optional[set[str]] = None,
) -> AuditReport:
    """Audit ``tenant`` — one KG when ``kg`` is given, otherwise every KG graph plus the
    tenant base graph — and return a structured :class:`AuditReport`.

    ``neptune`` is any client exposing ``async query(sparql) -> dict`` (the production
    ``NeptuneClient``, the harness store, or the pyoxigraph test shim). ``include``
    restricts to a subset of invariant names (passed through to ``check_invariants``).
    The tenant base graph (``tenant_graph_uri``) holds the ontology DECLARATIONS, so it
    is always the ``onto_graph_uri`` and — in the enumerate-all path — is itself audited
    as an instance graph to catch instance data leaked there (empty ``kg_name``)."""
    onto = tenant_graph_uri(tenant)
    targets: list[tuple[str, Optional[str], Optional[str]]]  # (graph, onto, label)
    if kg is not None:
        targets = [(kg_graph_uri(tenant, kg), onto, None)]
    else:
        targets = [(g, onto, None) for g in await _list_kg_graphs(neptune, tenant)]
        # Audit the base graph too: it should be declarations only, so any instance
        # violation there is leaked instance data (ONTA-198). It is its own onto graph.
        targets.append((onto, onto, _BASE_GRAPH_LABEL))

    audits: list[GraphAudit] = []
    for graph_uri, onto_graph_uri, label in targets:
        violations = await check_invariants(
            neptune, graph_uri, onto_graph_uri=onto_graph_uri, include=include
        )
        audits.append(GraphAudit(graph_uri, onto_graph_uri, violations, label))
    return AuditReport(tenant=tenant, audits=audits)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def report_to_dict(report: AuditReport) -> dict:
    """JSON-serializable form of the report — what the nightly loop / CI consume."""
    return {
        "tenant": report.tenant,
        "error_count": report.error_count,
        "warn_count": report.warn_count,
        "clean": report.clean,
        "graphs": [
            {
                "graph_uri": a.graph_uri,
                "onto_graph_uri": a.onto_graph_uri,
                "label": a.label,
                "errors": a.errors,
                "warns": a.warns,
                "violations": [
                    {
                        "invariant": v.invariant,
                        "severity": v.severity,
                        "detail": v.detail,
                        "binding": v.binding,
                    }
                    for v in a.violations
                ],
            }
            for a in report.audits
        ],
    }


_MARK = {"error": "x", "warn": "!"}


def format_report(report: AuditReport, *, as_json: bool = False) -> str:
    """Human-readable (default) or JSON rendering of an :class:`AuditReport`."""
    if as_json:
        return json.dumps(report_to_dict(report), indent=2)

    lines = [f"QC audit — tenant: {report.tenant}"]
    for a in report.audits:
        onto = f"  (onto: {a.onto_graph_uri})" if a.onto_graph_uri else ""
        lines.append(f"  graph: {a.display}{onto}")
        if not a.violations:
            lines.append("    (clean)")
            continue
        for v in a.violations:
            mark = _MARK.get(v.severity, "?")
            lines.append(f"    {mark} [{v.severity}] {v.invariant}: {v.detail}")

    graph_count = len(report.audits)
    lines.append(
        f"\nSummary: {report.error_count} error(s), {report.warn_count} warning(s) "
        f"across {graph_count} graph(s)."
    )
    return "\n".join(lines)

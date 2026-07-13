"""Deterministic KG quality-control checks — the un-gameable backbone of the
continuous QC eval loop (docs/specs/continuous_kg_qc_eval_spec.md).

Structural invariants any faithfully-produced KG must satisfy, expressed as SPARQL
graph-pattern queries that RETURN violations (RDFUnit-style). No LLM, no golden
output — properties, so they generalize across domains rather than overfitting to
fixtures.
"""
from cograph_client.qc.audit import (
    AuditReport,
    GraphAudit,
    format_report,
    report_to_dict,
    run_audit,
)
from cograph_client.qc.invariants import (
    INVARIANTS,
    Invariant,
    Violation,
    check_invariants,
)
from cograph_client.qc.isolation import (
    IsolationViolation,
    WorkspaceScope,
    check_isolation,
    format_isolation,
    isolated,
)

__all__ = [
    "INVARIANTS",
    "Invariant",
    "Violation",
    "check_invariants",
    "AuditReport",
    "GraphAudit",
    "run_audit",
    "format_report",
    "report_to_dict",
    "WorkspaceScope",
    "IsolationViolation",
    "check_isolation",
    "isolated",
    "format_isolation",
]

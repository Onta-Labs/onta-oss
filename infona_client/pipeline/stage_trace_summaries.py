"""Contract-shaped A1 / A2 / A3 / A6 summaries for the operator UI.

Look up sibling / facade names via :func:`_host` so tests that monkeypatch
``infona_client.pipeline.stage_trace.<name>`` keep working.
"""

from __future__ import annotations

from typing import Any, Optional


def _host():
    from infona_client.pipeline import stage_trace as _mod

    return _mod

# --------------------------------------------------------------------------- #
# Contract-shaped artifact summaries (A1 / A2 / A3 / A6) for operator UI
# --------------------------------------------------------------------------- #
# Notion Stage Contracts name the inter-stage artifacts A0–A10. Live discovery
# instrumentation stamps compact, JSON-safe summaries of those shapes onto
# StageProjectTrace.input / .output so the Job Trace page can render "what P1
# emitted (A1)", "what P2 emitted (A2)", etc. without dumping full payloads.
# Keep samples capped — these ride on the job jsonb row.


_SAMPLE_CAP = 8


def _cap_list(values: Any, *, n: int = _SAMPLE_CAP) -> list[Any]:
    if not values:
        return []
    out: list[Any] = []
    for v in values:
        if v is None or v == "":
            continue
        out.append(v)
        if len(out) >= n:
            break
    return out


def summarize_a1_source_bundle(bundle: Any) -> dict[str, Any]:
    """Compact A1 Source Bundle summary for P1 output / P2 input.

    Accepts a :class:`~infona_client.pipeline.source_bundle.SourceBundle` or any
    duck-typed object with ``envelope`` / ``rows`` / ``secret_refs``. Never raises
    on partial shapes — returns whatever fields are available.
    """
    envelope = getattr(bundle, "envelope", None)
    rows = list(getattr(bundle, "rows", None) or ())
    secret_refs = list(getattr(bundle, "secret_refs", None) or ())
    tiers: list[str] = []
    providers: list[str] = []
    source_urls: list[str] = []
    fact_ids: list[str] = []
    for r in rows:
        t = getattr(r, "tier", None) or (r.get("tier") if isinstance(r, dict) else None)
        if t and t not in tiers:
            tiers.append(str(t))
        p = getattr(r, "provider", None) or (
            r.get("provider") if isinstance(r, dict) else None
        )
        if p and p not in providers:
            providers.append(str(p))
        su = getattr(r, "source_url", None) or (
            r.get("source_url") if isinstance(r, dict) else None
        )
        if su:
            source_urls.append(str(su))
        fid = getattr(r, "fact_id", None) or (
            r.get("fact_id") if isinstance(r, dict) else None
        )
        if fid:
            fact_ids.append(str(fid))
    return {
        "artifact": "A1",
        "name": "Source Bundle",
        "run_id": getattr(envelope, "run_id", None)
        or getattr(bundle, "run_id", None),
        "workspace_id": getattr(envelope, "workspace_id", None)
        or getattr(bundle, "workspace_id", None),
        "root_fact_id": getattr(envelope, "fact_id", None),
        "row_count": len(rows),
        "providers": providers,
        "tiers": tiers,
        "secret_refs": secret_refs,  # logical refs only (SourceBundle invariant)
        "source_urls_sample": _cap_list(source_urls),
        "fact_ids_sample": _cap_list(fact_ids),
    }


def merge_a1_summaries(
    acc: Optional[dict[str, Any]], piece: dict[str, Any]
) -> dict[str, Any]:
    """Fold one per-batch A1 summary into a run-level A1 aggregate for P1 end."""
    if not acc:
        base = dict(piece)
        base["bundles_emitted"] = 1
        base["row_count"] = int(piece.get("row_count") or 0)
        return base
    out = dict(acc)
    out["bundles_emitted"] = int(out.get("bundles_emitted") or 0) + 1
    out["row_count"] = int(out.get("row_count") or 0) + int(piece.get("row_count") or 0)
    # Prefer the first non-empty run identity (one run_id per discovery run).
    for k in ("run_id", "workspace_id", "root_fact_id"):
        if not out.get(k) and piece.get(k):
            out[k] = piece[k]
    for list_key in ("providers", "tiers", "secret_refs"):
        seen = list(out.get(list_key) or [])
        for v in piece.get(list_key) or []:
            if v not in seen:
                seen.append(v)
        out[list_key] = seen
    for sample_key in ("source_urls_sample", "fact_ids_sample"):
        out[sample_key] = _cap_list(
            list(out.get(sample_key) or []) + list(piece.get(sample_key) or [])
        )
    out["artifact"] = "A1"
    out["name"] = "Source Bundle"
    return out


def summarize_a2_candidates(
    *,
    entities_extracted: int = 0,
    entities_resolved: int = 0,
    source_row_count: int = 0,
    focus_type: Optional[str] = None,
    focus_attributes: Optional[list[str]] = None,
    run_id: Optional[str] = None,
    soft_typed: bool = True,
    evidence_linked: bool = True,
    structured_fastpath: bool = False,
    batches: int = 0,
) -> dict[str, Any]:
    """Compact A2 Candidate Facts summary for P2 output."""
    return {
        "artifact": "A2",
        "name": "Candidate Facts",
        "entities_extracted": int(entities_extracted or 0),
        "entities_resolved": int(entities_resolved or 0),
        "source_row_count": int(source_row_count or 0),
        "focus_type": focus_type,
        "focus_attributes": list(focus_attributes or [])[:40],
        "soft_typed": bool(soft_typed),
        "evidence_linked": bool(evidence_linked),
        "structured_fastpath": bool(structured_fastpath),
        "run_id": run_id,
        "batches": int(batches or 0),
    }


def summarize_a3_clean_report(report: Any) -> Optional[dict[str, Any]]:
    """Compact A3 Clean Facts summary from a CleanReport-like ledger.

    Returns ``None`` when there is no ledger / total is 0 — caller should
    ``skip`` P3 rather than complete an empty clean stage.
    """
    if report is None:
        return None
    counts: Optional[dict[str, Any]] = None
    if hasattr(report, "counts") and callable(report.counts):
        try:
            counts = dict(report.counts())
        except Exception:  # pragma: no cover
            counts = None
    if counts is None and isinstance(report, dict):
        counts = {
            "passed": len(report.get("passed") or []),
            "transformed": len(report.get("transformed") or []),
            "dropped": len(report.get("dropped") or []),
            "total": int(report.get("total") or 0)
            or (
                len(report.get("passed") or [])
                + len(report.get("transformed") or [])
                + len(report.get("dropped") or [])
            ),
        }
    if counts is None:
        # Duck-typed attribute access
        passed = list(getattr(report, "passed", None) or [])
        transformed = list(getattr(report, "transformed", None) or [])
        dropped = list(getattr(report, "dropped", None) or [])
        counts = {
            "passed": len(passed),
            "transformed": len(transformed),
            "dropped": len(dropped),
            "total": len(passed) + len(transformed) + len(dropped),
        }
    if int(counts.get("total") or 0) <= 0:
        return None
    # Sample drop reasons for the operator UI (not full CleanFact dumps).
    drop_reasons: list[str] = []
    dropped_items = getattr(report, "dropped", None)
    if dropped_items is None and isinstance(report, dict):
        dropped_items = report.get("dropped")
    for fact in list(dropped_items or [])[:20]:
        reason = getattr(fact, "reason", None) or (
            fact.get("reason") if isinstance(fact, dict) else None
        )
        if reason and reason not in drop_reasons:
            drop_reasons.append(str(reason))
        if len(drop_reasons) >= _SAMPLE_CAP:
            break
    transform_sample: list[dict[str, Any]] = []
    transformed_items = getattr(report, "transformed", None)
    if transformed_items is None and isinstance(report, dict):
        transformed_items = report.get("transformed")
    for fact in list(transformed_items or [])[:_SAMPLE_CAP]:
        raw = getattr(fact, "raw_value", None) or (
            fact.get("raw_value") if isinstance(fact, dict) else None
        )
        clean = getattr(fact, "clean_value", None) or (
            fact.get("clean_value") if isinstance(fact, dict) else None
        )
        attr = getattr(fact, "attribute", None) or (
            fact.get("attribute") if isinstance(fact, dict) else None
        )
        transform_sample.append(
            {"attribute": attr, "raw": raw, "clean": clean}
        )
    return {
        "artifact": "A3",
        "name": "Clean Facts",
        "counts": counts,
        "drop_reasons_sample": drop_reasons,
        "transforms_sample": transform_sample,
    }


def merge_a3_counts(
    acc: Optional[dict[str, int]], piece: Optional[dict[str, Any]]
) -> Optional[dict[str, int]]:
    """Sum partition counts across micro-batch clean ledgers."""
    if piece is None:
        return acc
    counts = piece.get("counts") if "counts" in piece else piece
    if not counts:
        return acc
    if acc is None:
        return {
            "passed": int(counts.get("passed") or 0),
            "transformed": int(counts.get("transformed") or 0),
            "dropped": int(counts.get("dropped") or 0),
            "total": int(counts.get("total") or 0),
        }
    return {
        "passed": int(acc.get("passed") or 0) + int(counts.get("passed") or 0),
        "transformed": int(acc.get("transformed") or 0)
        + int(counts.get("transformed") or 0),
        "dropped": int(acc.get("dropped") or 0) + int(counts.get("dropped") or 0),
        "total": int(acc.get("total") or 0) + int(counts.get("total") or 0),
    }


def summarize_a6_graph_delta(
    *,
    graph_delta: Any = None,
    run_id: Optional[str] = None,
    instance_graph: Optional[str] = None,
    entities_written: int = 0,
    triples_inserted: int = 0,
    status: Optional[str] = None,
) -> dict[str, Any]:
    """Compact A6 Graph Delta summary for P6 output.

    ``graph_delta`` may be a :class:`~infona_client.graph.kg_writer.GraphDelta`,
    its ``to_dict()`` form, or ``None`` (then only counters are emitted).
    """
    gd = graph_delta
    if gd is not None and hasattr(gd, "to_dict") and not isinstance(gd, dict):
        try:
            gd = gd.to_dict()
        except Exception:  # pragma: no cover
            gd = None
    facts: list[Any] = []
    fan_in: list[Any] = []
    gd_run_id = run_id
    gd_graph = instance_graph
    if isinstance(gd, dict):
        facts = list(gd.get("facts") or [])
        fan_in = list(gd.get("fan_in") or [])
        gd_run_id = gd.get("run_id") or run_id
        gd_graph = gd.get("instance_graph") or instance_graph
    return {
        "artifact": "A6",
        "name": "Graph Delta",
        "run_id": gd_run_id,
        "instance_graph": gd_graph,
        "fact_count": len(facts),
        "fan_in_count": len(fan_in),
        "entities_written": int(entities_written or 0),
        "triples_inserted": int(triples_inserted or 0),
        "status": status,
        # Tiny sample so operators can see shape without dumping the full delta.
        "facts_sample": _cap_list(facts, n=3),
    }

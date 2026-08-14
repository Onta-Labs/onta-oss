"""Eval report formatting, JSON serialization, and CLI entry.

Implementation sibling of :mod:`infona_client.eval`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from infona_client.eval_models import EVAL_MODEL, EvalReport
from infona_client.eval_run import run_full_eval


TIER_NAMES = {1: "Count/Lookup", 2: "Filter", 3: "Join", 4: "Multi-hop"}


def format_report(report: EvalReport) -> str:
    """Format an EvalReport as a human-readable string.

    This produces the summary table printed to stdout after eval completes.
    The raw report data is also saved as JSON for programmatic analysis.
    """
    lines = []
    lines.append("")
    lines.append("INFONA EVAL REPORT")
    lines.append("=" * 70)
    lines.append(f"  Dataset:      {', '.join(report.dataset_names) or '(none)'}")
    lines.append(f"  KG:           {report.kg_name}")
    lines.append(f"  Duration:     {report.duration_s}s")
    lines.append(f"  Timestamp:    {report.timestamp}")
    lines.append("")
    lines.append("  Models:")
    lines.append(f"    Extraction:     {report.models.extraction}")
    lines.append(f"    Query (SPARQL): {report.models.query_model}")
    lines.append(f"    Eval judge:     {report.models.eval_judge}")
    lines.append(f"    Question gen:   {report.models.question_gen}")

    # Ontology quality
    if report.ontology and report.ontology.dimensions:
        onto = report.ontology
        lines.append("")
        lines.append(f"ONTOLOGY QUALITY ({onto.total}/{onto.max_total})")
        lines.append("-" * 70)
        for d in onto.dimensions:
            issues_str = f"  ← {d.issues[0]}" if d.issues else ""
            lines.append(f"  {d.name:<25s} {d.score:>2d}/10  {d.explanation}{issues_str}")

        if onto.weak_points:
            lines.append("")
            lines.append("  Weak points:")
            for wp in onto.weak_points:
                lines.append(f"    - {wp}")

    # Query accuracy
    if report.queries and report.queries.results:
        queries = report.queries
        total_correct = sum(1 for r in queries.results if r.verdict == "correct")
        total = len(queries.results)

        lines.append("")
        lines.append(f"QUERY ACCURACY ({total_correct}/{total})")
        lines.append("-" * 70)

        for tier in range(1, 5):
            stats = queries.by_tier.get(tier, {"total": 0, "passed": 0, "avg_ms": 0})
            if stats["total"] == 0:
                continue
            name = TIER_NAMES.get(tier, f"Tier {tier}")
            pct = round(100 * stats["passed"] / stats["total"]) if stats["total"] else 0
            lines.append(
                f"  Tier {tier} ({name:<12s}): "
                f"{stats['passed']}/{stats['total']}  "
                f"({pct}%)  "
                f"avg {stats['avg_ms']}ms"
            )

        # Failure category summary
        failures = [r for r in queries.results if r.verdict != "correct"]
        if failures:
            from collections import Counter
            cats = Counter(r.failure_category for r in failures if r.failure_category)
            if cats:
                lines.append("")
                lines.append("  Failure categories:")
                for cat, count in cats.most_common():
                    lines.append(f"    {cat:<25s} {count}x")

            lines.append("")
            lines.append("  Failed questions:")
            for r in failures:
                cat_tag = f" [{r.failure_category}]" if r.failure_category else ""
                lines.append(f"    T{r.tier}: {r.question}")
                lines.append(f"         Expected: {r.expected}")
                lines.append(f"         Got:      {r.answer[:80]}")
                lines.append(f"         Verdict:  {r.verdict}{cat_tag} — {r.explanation}")
                if r.sparql:
                    sparql_preview = r.sparql.split("\n")[0][:70]
                    lines.append(f"         SPARQL:   {sparql_preview}...")
                if r.corrected_sparql:
                    lines.append(f"         Fix:      {r.corrected_sparql.split(chr(10))[0][:70]}...")
                lines.append("")

    lines.append("=" * 70)
    return "\n".join(lines)


def report_to_json(report: EvalReport) -> dict:
    """Convert an EvalReport to a JSON-serializable dict.

    This is saved to disk for programmatic analysis and trend tracking
    across multiple eval runs.
    """
    data: dict = {
        "dataset_names": report.dataset_names,
        "kg_name": report.kg_name,
        "models": report.models.to_dict(),
        "timestamp": report.timestamp,
        "duration_s": report.duration_s,
    }

    if report.ontology:
        data["ontology"] = {
            "total": report.ontology.total,
            "max_total": report.ontology.max_total,
            "dimensions": [
                {
                    "name": d.name,
                    "score": d.score,
                    "explanation": d.explanation,
                    "issues": d.issues,
                }
                for d in report.ontology.dimensions
            ],
            "weak_points": report.ontology.weak_points,
        }

    if report.queries:
        data["queries"] = {
            "by_tier": report.queries.by_tier,
            "results": [
                {
                    "tier": r.tier,
                    "question": r.question,
                    "expected": r.expected,
                    "answer": r.answer,
                    "sparql": r.sparql,
                    "verdict": r.verdict,
                    "explanation": r.explanation,
                    "failure_category": r.failure_category,
                    "corrected_sparql": r.corrected_sparql,
                    "timing_ms": r.timing_ms,
                }
                for r in report.queries.results
            ],
        }

    return data


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


async def eval_cli(args) -> None:
    """CLI handler for `infona eval`.

    This function is async because the eval pipeline makes concurrent
    API calls. The parent-repo CLI wrapper runs it with asyncio.run().
    """
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not openrouter_key:
        print("OPENROUTER_API_KEY required for eval (LLM judge calls)", file=__import__("sys").stderr)
        __import__("sys").exit(1)

    api_url = os.environ.get("INFONA_API_URL", "http://localhost:8000")
    api_key = os.environ.get("INFONA_API_KEY", "dev-key-001")
    tenant = os.environ.get("INFONA_TENANT", "demo-tenant")

    dataset_paths = args.files if hasattr(args, "files") and args.files else []
    kg_name = args.kg if hasattr(args, "kg") else None
    num_questions = args.questions if hasattr(args, "questions") else 20
    query_model = args.model if hasattr(args, "model") else None
    ontology_only = getattr(args, "ontology_only", False)
    query_only = getattr(args, "query_only", False)
    cache_questions = getattr(args, "cache_questions", False)
    fast_judge = getattr(args, "fast_judge", False)
    concurrency = getattr(args, "concurrency", 10)

    print(f"Running eval...")
    print(f"  Datasets:   {', '.join(dataset_paths) or '(using existing KG)'}")
    print(f"  KG:         {kg_name or '(default)'}")
    print(f"  Mode:       {'ontology only' if ontology_only else 'query only' if query_only else 'full'}")
    print(f"  Judge:      {'programmatic (fast)' if fast_judge else EVAL_MODEL}")
    if not ontology_only:
        print(f"  Questions:  {num_questions}")
        print(f"  Concurrency: {concurrency}")
        if cache_questions:
            print(f"  Question cache: ON (reusing cached questions if available)")
    print()

    report = await run_full_eval(
        api_url=api_url,
        api_key=api_key,
        tenant=tenant,
        kg_name=kg_name,
        dataset_paths=dataset_paths,
        num_questions=num_questions,
        query_model=query_model,
        ontology_only=ontology_only,
        query_only=query_only,
        openrouter_key=openrouter_key,
        cache_questions=cache_questions,
        fast_judge=fast_judge,
        concurrency=concurrency,
    )

    # Print human-readable report
    print(format_report(report))

    # Save JSON report
    report_dir = Path("eval_reports")
    report_dir.mkdir(exist_ok=True)
    timestamp = report.timestamp.replace(":", "-").replace(".", "-")[:19]
    report_path = report_dir / f"eval-{timestamp}.json"
    report_path.write_text(json.dumps(report_to_json(report), indent=2))
    print(f"\nJSON report saved: {report_path}")

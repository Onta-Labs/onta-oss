#!/usr/bin/env python3
"""Reproduce the published Infona query-accuracy eval (ONTA-541).

Thin wrapper around ``infona_client.eval.run_full_eval``. OSS does **not**
ship an ``infona eval`` CLI.

Prereqs: ``./scripts/oss_up.sh``, an ingested KG, ``OPENROUTER_API_KEY``,
``pandas`` (ground-truth step).

    infona ingest examples/trials.csv --kg eval-public-trials -y
    export OPENROUTER_API_KEY=sk-or-...
    export INFONA_QUERY_MODEL=openai/gpt-oss-120b
    export INFONA_EVAL_MODEL=deepseek/deepseek-v4-pro-0813
    python scripts/run_public_eval.py \\
        --dataset examples/trials.csv --kg eval-public-trials --questions 8 \\
        --out docs/eval/public_results.json

Your own CSV: same command, swap ``--dataset`` and ``--kg``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "examples" / "trials.csv"
DEFAULT_OUT = ROOT / "docs" / "eval" / "public_results.json"
DEFAULT_QUERY_MODEL = "openai/gpt-oss-120b"
DEFAULT_EVAL_MODEL = "deepseek/deepseek-v4-pro-0813"
DEFAULT_KG = "eval-public-trials"
TIER_NAMES = {1: "Count/Lookup", 2: "Filter", 3: "Join", 4: "Multi-hop"}
SCHEMA_VERSION = 1


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _csv_rows(path: Path) -> int:
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    return max(0, len(lines) - 1)


def _dataset_block(path: Path) -> dict:
    rel = str(path.relative_to(ROOT) if path.exists() and path.is_relative_to(ROOT) else path)
    return {
        "path": rel,
        "name": path.name,
        "rows": _csv_rows(path) if path.exists() else None,
        "note": "Synthetic TRIAL-* IDs; public program names; no patient data.",
    }


def public_artifact(report, *, dataset: Path, questions: int, tenant: str) -> dict:
    """Per-tier scores plus every miss (not filtered)."""
    from infona_client.eval import report_to_json

    raw = report_to_json(report)
    results = (raw.get("queries") or {}).get("results") or []
    by_tier = (raw.get("queries") or {}).get("by_tier") or {}
    tiers, failures = [], []
    for n, name in TIER_NAMES.items():
        stats = by_tier.get(n) or by_tier.get(str(n)) or {"total": 0, "passed": 0}
        miss_rows = []
        for r in results:
            if int(r.get("tier") or 0) != n or r.get("verdict") == "correct":
                continue
            miss = {
                "tier": n,
                "question": r.get("question") or "",
                "expected": r.get("expected") or "",
                "got": (r.get("answer") or "")[:240],
                "verdict": r.get("verdict") or "error",
                "failure_category": r.get("failure_category") or "",
                "explanation": (r.get("explanation") or "")[:240],
            }
            miss_rows.append(miss)
            failures.append(miss)
        total, passed = int(stats.get("total") or 0), int(stats.get("passed") or 0)
        tiers.append({
            "tier": n, "name": name, "passed": passed, "total": total,
            "accuracy_pct": round(100 * passed / total) if total else None,
            "misses": [m["question"] for m in miss_rows],
        })
    kg = raw.get("kg_name") or DEFAULT_KG
    return {
        "schema_version": SCHEMA_VERSION, "status": "live",
        "dataset": _dataset_block(dataset), "models": raw.get("models") or {},
        "kg_name": kg, "tenant": tenant, "num_questions": questions,
        "timestamp": raw.get("timestamp") or "", "duration_s": raw.get("duration_s"),
        "git_sha": _git_sha(), "ontology": raw.get("ontology"),
        "tiers": tiers, "failures": failures,
        "repro": {
            "command": f"python scripts/run_public_eval.py --dataset {dataset} --kg {kg} --questions {questions}",
            "ingest": f"infona ingest {dataset} --kg {kg} -y",
        },
    }


def pending_artifact(*, dataset: Path, kg: str, questions: int, tenant: str) -> dict:
    """Schema-complete placeholder — no invented scores."""
    return {
        "schema_version": SCHEMA_VERSION, "status": "partial",
        "note": "Not yet run on this tree — command below.",
        "dataset": _dataset_block(dataset) if dataset.exists() else {
            "path": "examples/trials.csv", "name": "trials.csv", "rows": 16,
            "note": "Synthetic TRIAL-* IDs; public program names; no patient data.",
        },
        "models": {
            "query_model": DEFAULT_QUERY_MODEL,
            "eval_judge": DEFAULT_EVAL_MODEL,
            "question_gen": DEFAULT_EVAL_MODEL,
        },
        "kg_name": kg, "tenant": tenant, "num_questions": questions,
        "timestamp": "", "duration_s": None, "git_sha": _git_sha(),
        "ontology": None,
        "tiers": [
            {"tier": n, "name": name, "passed": None, "total": None,
             "accuracy_pct": None, "misses": []}
            for n, name in TIER_NAMES.items()
        ],
        "failures": [],
        "repro": {
            "command": f"python scripts/run_public_eval.py --dataset examples/trials.csv --kg {kg} --questions {questions}",
            "ingest": f"infona ingest examples/trials.csv --kg {kg} -y",
        },
    }


def _parse() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    p.add_argument("--kg", default=DEFAULT_KG)
    p.add_argument("--questions", type=int, default=8)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--api-url", default=os.environ.get("INFONA_API_URL", "http://localhost:8000"))
    p.add_argument("--api-key", default=os.environ.get("INFONA_API_KEY", "dev-key-001"))
    p.add_argument("--tenant", default=os.environ.get("INFONA_TENANT", "default"))
    p.add_argument("--query-model", default=os.environ.get("INFONA_QUERY_MODEL", DEFAULT_QUERY_MODEL))
    p.add_argument("--eval-model", default=os.environ.get("INFONA_EVAL_MODEL", DEFAULT_EVAL_MODEL))
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--cache-questions", action="store_true",
                   help="Reuse eval_reports/question_cache/{kg}-{n}.json")
    p.add_argument("--pending", action="store_true", help="Write schema-only JSON; skip the API")
    return p.parse_args()


async def _run(args: argparse.Namespace) -> dict:
    from infona_client.eval import format_report, run_full_eval

    report = await run_full_eval(
        api_url=args.api_url.rstrip("/"), api_key=args.api_key, tenant=args.tenant,
        kg_name=args.kg, dataset_paths=[str(args.dataset)],
        num_questions=args.questions, query_model=args.query_model,
        openrouter_key=os.environ.get("OPENROUTER_API_KEY", ""),
        concurrency=args.concurrency, cache_questions=args.cache_questions,
    )
    print(format_report(report))
    return public_artifact(report, dataset=args.dataset, questions=args.questions, tenant=args.tenant)


def main() -> int:
    args = _parse()
    os.environ["INFONA_EVAL_MODEL"] = args.eval_model
    os.environ.setdefault("INFONA_QUERY_MODEL", args.query_model)
    if args.pending:
        artifact = pending_artifact(
            dataset=args.dataset, kg=args.kg, questions=args.questions, tenant=args.tenant
        )
    else:
        if not os.environ.get("OPENROUTER_API_KEY"):
            print("OPENROUTER_API_KEY required (LLM judge + question gen)", file=sys.stderr)
            return 1
        if not args.dataset.exists():
            print(f"dataset not found: {args.dataset}", file=sys.stderr)
            return 1
        artifact = asyncio.run(_run(args))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(artifact, indent=2) + "\n")
    print(f"\nPublic artifact: {args.out}  status={artifact['status']}  misses={len(artifact['failures'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

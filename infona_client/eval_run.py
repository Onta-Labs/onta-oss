"""Full-eval orchestrator.

Looks up evaluators and ``rebuild_example_bank`` on the
:mod:`infona_client.eval` facade at call time so existing monkeypatches
keep working. The ``await rebuild_example_bank(ft_path)`` call site is
kept verbatim (tests pin that string).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
import structlog

from infona_client.eval_bank import rebuild_example_bank
from infona_client.eval_models import (
    EVAL_MODEL,
    DatasetStats,
    EvalReport,
    ModelConfig,
)
from infona_client.graph.queries import kg_graph_uri, tenant_graph_uri

logger = structlog.stdlib.get_logger("infona.eval")


def _host():
    """Call-time lookup of the public eval module (monkeypatch surface)."""
    from infona_client import eval as _mod

    return _mod


async def run_full_eval(
    api_url: str,
    api_key: str,
    tenant: str,
    kg_name: str | None = None,
    dataset_paths: list[str] | None = None,
    num_questions: int = 20,
    query_model: str | None = None,
    ontology_only: bool = False,
    query_only: bool = False,
    openrouter_key: str = "",
    cache_questions: bool = False,
    fast_judge: bool = False,
    concurrency: int = 10,
) -> EvalReport:
    """Run the full evaluation pipeline.

    This is the main entry point for the eval framework. It orchestrates:
      1. Reading source data for ground truth
      2. Ontology quality evaluation (unless query_only)
      3. Query accuracy evaluation (unless ontology_only)
      4. Report generation

    Performance optimizations (designed for <10 min eval cycles):
      - Pre-warm: throwaway /ask call warms ontology cache before eval
      - Concurrent execution: run N questions in parallel (default 10)
      - Question caching: save questions + ground truth to disk, reuse on re-runs
      - Fast judge: programmatic numeric comparison instead of LLM judge
      See the flags below for the iteration vs. final-validation split.

    Args:
        api_url: Infona API base URL.
        api_key: API authentication key.
        tenant: Tenant ID.
        kg_name: Knowledge graph name.
        dataset_paths: Paths to source data files (for ground truth).
        num_questions: Number of test questions to generate.
        query_model: Override model for /ask queries.
        ontology_only: Skip query evaluation.
        query_only: Skip ontology evaluation.
        openrouter_key: OpenRouter API key for LLM judge calls.
        cache_questions: If True, cache generated questions to eval_reports/question_cache/.
        fast_judge: If True, use programmatic judge (numeric tolerance) instead of LLM.
        concurrency: Max concurrent API calls for question execution (default 10).

    Returns:
        EvalReport with ontology and query scores.
    """
    import datetime

    t0 = time.time()
    openrouter_key = openrouter_key or os.environ.get("OPENROUTER_API_KEY", "")

    # Read source data and compute full dataset stats
    source_sample = ""
    dataset_names = []
    all_stats: list[DatasetStats] = []
    for path_str in (dataset_paths or []):
        path = Path(path_str)
        if path.exists():
            if path.suffix.lower() == ".csv":
                stats = DatasetStats.from_csv(path)
            else:
                stats = DatasetStats.from_text(path)
            all_stats.append(stats)
            source_sample += stats.sample_text + "\n\n"
            dataset_names.append(path.name)

    # Merge stats for question generation (use first dataset's stats as primary)
    dataset_stats = all_stats[0] if all_stats else None

    # Determine which models are used for each role
    extraction_model = os.environ.get("INFONA_EXTRACT_MODEL", "deepseek/deepseek-v3.2")
    query_model_resolved = query_model or os.environ.get("INFONA_QUERY_MODEL", "google/gemini-2.5-flash")
    models = ModelConfig(
        eval_judge=EVAL_MODEL,
        question_gen=EVAL_MODEL,
        query_model=query_model_resolved,
        extraction=extraction_model,
    )

    report = EvalReport(
        dataset_names=dataset_names,
        kg_name=kg_name or "(default)",
        model=query_model_resolved,
        models=models,
        timestamp=datetime.datetime.now().isoformat(),
    )

    # Fetch ontology text (shared between both evaluators)
    onto_eval = _host().OntologyEvaluator(api_url, api_key, tenant, openrouter_key)
    ontology_text = await onto_eval._fetch_ontology(kg_name)

    # Ontology quality
    if not query_only:
        logger.info("eval_ontology_start")
        report.ontology = await onto_eval.evaluate(kg_name, source_sample)
        logger.info("eval_ontology_complete", score=report.ontology.total)

    # Query accuracy
    if not ontology_only and source_sample:
        logger.info("eval_query_start", num_questions=num_questions)

        # Pre-warm: throwaway /ask call warms ontology cache + embeddings
        # so the first real question doesn't pay the cold start penalty (~5-11s)
        try:
            async with httpx.AsyncClient(timeout=30) as warm_client:
                warm_body: dict = {"question": "How many entities are there?"}
                if kg_name:
                    warm_body["kg_name"] = kg_name
                await warm_client.post(
                    f"{api_url}/graphs/{tenant}/ask",
                    headers={"X-API-Key": api_key, "Content-Type": "application/json"},
                    json=warm_body,
                )
            logger.info("eval_pre_warm_done")
        except Exception:
            pass  # non-blocking

        # Use first CSV path for deterministic ground truth computation
        csv_path = None
        for path_str in (dataset_paths or []):
            p = Path(path_str)
            if p.exists() and p.suffix.lower() == ".csv":
                csv_path = p
                break
        query_eval = _host().QueryEvaluator(api_url, api_key, tenant, openrouter_key, csv_path=csv_path)
        report.queries = await query_eval.evaluate(
            kg_name=kg_name,
            source_sample=source_sample,
            ontology_text=ontology_text,
            num_questions=num_questions,
            model=query_model,
            dataset_stats=dataset_stats,
            cache_questions=cache_questions,
            fast_judge=fast_judge,
            concurrency=concurrency,
        )
        logger.info("eval_query_complete", results=len(report.queries.results))

    report.duration_s = round(time.time() - t0, 1)

    # Collect fine-tuning pairs: (prompt, correct_sparql) for future LLM training.
    # Only saves pairs where the judge provided a corrected SPARQL (wrong/error verdicts)
    # Fine-tuning data collection — see eval_reports/FINETUNE_DATA.md for format and usage.
    # Saves correct pairs (finetune_pairs.jsonl) and wrong pairs (finetune_negatives.jsonl).
    # Dedup: keyed on (question, graph_uri) — newer pair replaces older for same question.
    if report.queries and report.queries.results:
        ft_path = Path("eval_reports/finetune_pairs.jsonl")
        ft_path.parent.mkdir(exist_ok=True)

        # Load existing pairs and index by (question, graph_uri)
        existing: dict[tuple[str, str], str] = {}
        if ft_path.exists():
            for line in ft_path.read_text().splitlines():
                if line.strip():
                    try:
                        entry = json.loads(line)
                        key = (entry["question"], entry.get("graph_uri", ""))
                        existing[key] = line
                    except (json.JSONDecodeError, KeyError):
                        pass

        # Shared builders, not a hand-rolled IRI (ONTA-422) — see the note in
        # eval_diagnosis.py.
        graph_uri = (
            kg_graph_uri(tenant, kg_name) if kg_name else tenant_graph_uri(tenant)
        )
        added = 0
        for r in report.queries.results:
            if r.verdict == "correct" and r.sparql:
                target_sparql = r.sparql
            elif r.corrected_sparql:
                target_sparql = r.corrected_sparql
            else:
                continue
            pair = {
                "question": r.question,
                "ontology": ontology_text,
                "graph_uri": graph_uri,
                "sparql": target_sparql,
                "source": "eval",
                "dataset": ",".join(dataset_names),
                "timestamp": report.timestamp,
            }
            key = (r.question, graph_uri)
            existing[key] = json.dumps(pair)
            added += 1

        # Rewrite file with deduped pairs
        ft_path.write_text("\n".join(existing.values()) + "\n")
        logger.info("finetune_pairs_saved", count=added, total=len(existing), path=str(ft_path))

        # Save negative examples (wrong SPARQL + failure category) for fine-tuning
        neg_path = Path("eval_reports/finetune_negatives.jsonl")
        neg_existing: dict[tuple[str, str], str] = {}
        if neg_path.exists():
            for line in neg_path.read_text().splitlines():
                if line.strip():
                    try:
                        entry = json.loads(line)
                        key = (entry["question"], entry.get("graph_uri", ""))
                        neg_existing[key] = line
                    except (json.JSONDecodeError, KeyError):
                        pass

        neg_added = 0
        for r in report.queries.results:
            if r.verdict in ("wrong", "error") and r.sparql:
                neg = {
                    "question": r.question,
                    "ontology": ontology_text,
                    "graph_uri": graph_uri,
                    "sparql": r.sparql,
                    "answer": r.answer[:200] if r.answer else "",
                    "expected": r.expected[:200] if r.expected else "",
                    "failure_category": r.failure_category if hasattr(r, "failure_category") else "unknown",
                    "verdict": r.verdict,
                    "source": "eval",
                    "timestamp": report.timestamp,
                }
                key = (r.question, graph_uri)
                neg_existing[key] = json.dumps(neg)
                neg_added += 1

        if neg_added:
            neg_path.write_text("\n".join(neg_existing.values()) + "\n")
            logger.info("finetune_negatives_saved", count=neg_added, total=len(neg_existing), path=str(neg_path))

        # Merge this run's finetune pairs into the example bank, so the bank
        # stays in sync when KGs are reingested with new ontology types or
        # schema changes. NOT a regenerate -- see rebuild_example_bank.
        try:
            await rebuild_example_bank(ft_path)
        except Exception:
            logger.warning("example_bank_rebuild_failed", exc_info=True)

    return report

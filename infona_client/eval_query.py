"""Query-accuracy evaluator (generate / ask / judge).

Looks up ``_llm_call`` / ``_parse_json`` / ``_compute_ground_truth`` on
the :mod:`infona_client.eval` facade at call time so existing
monkeypatches keep working.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import structlog

from infona_client.eval_models import (
    SOURCE_SAMPLE_CHARS,
    DatasetStats,
    QuestionResult,
    QueryScore,
)
from infona_client.eval_prompts import QUESTION_GEN_PROMPT, QUERY_JUDGE_PROMPT
from infona_client.graph.iri import IRI_BASE

logger = structlog.stdlib.get_logger("infona.eval")


def _host():
    """Call-time lookup of the public eval module (monkeypatch surface)."""
    from infona_client import eval as _mod

    return _mod


class QueryEvaluator:
    """Evaluates query accuracy by generating questions and judging answers.

    Usage::

        evaluator = QueryEvaluator(api_url, api_key, tenant)
        score = await evaluator.evaluate(
            kg_name="test-kg",
            source_sample="...",
            ontology_text="...",
            num_questions=20,
        )

    The evaluator:
      1. Sends the ontology + source sample to an LLM to generate questions
      2. Computes deterministic ground truth from the CSV for each question
      3. Executes each question against the /ask endpoint
      4. Sends each (question, answer, ground_truth) triple to an LLM judge
      5. Returns structured scores by difficulty tier
    """

    def __init__(self, api_url: str, api_key: str, tenant: str, openrouter_key: str = "",
                 csv_path: Path | None = None):
        self._api_url = api_url
        self._api_key = api_key
        self._tenant = tenant
        self._openrouter_key = openrouter_key
        self._csv_path = csv_path

    async def evaluate(
        self,
        kg_name: str | None = None,
        source_sample: str = "",
        ontology_text: str = "",
        num_questions: int = 20,
        model: str | None = None,
        dataset_stats: DatasetStats | None = None,
        cache_questions: bool = False,
        fast_judge: bool = False,
        concurrency: int = 10,
    ) -> QueryScore:
        """Generate questions, execute them, and judge the answers.

        Performance design decisions (see ARCHITECTURE.md):
          - Question caching: saves ~30-60s on re-runs by skipping LLM question
            generation and ground truth computation. Cache key: kg_name + num_questions.
          - Fast judge: compares answers programmatically (numeric tolerance ±2% for
            counts, ±5% for averages, case-insensitive string match). Saves ~2s per
            question vs LLM judge. Use for iteration; LLM judge for final validation.
          - Concurrency: runs N /ask calls in parallel. Default 10. Neptune handles
            parallel reads well. Bottleneck is LLM SPARQL generation, not Neptune.

        Args:
            kg_name: Knowledge graph to query.
            source_sample: Source data for ground truth derivation.
            ontology_text: Ontology schema text (for question generation).
            num_questions: Total number of questions to generate.
            model: Override model for query generation (passed to /ask).
            dataset_stats: Full dataset statistics for accurate ground truth.
            cache_questions: Reuse cached questions if available.
            fast_judge: Use programmatic comparison instead of LLM judge.
            concurrency: Max concurrent /ask calls (default 10).

        Returns:
            QueryScore with per-question results and tier summaries.
        """
        import asyncio

        # Question cache path
        cache_dir = Path("eval_reports/question_cache")
        cache_key = f"{kg_name or 'default'}-{num_questions}"
        cache_path = cache_dir / f"{cache_key}.json"

        questions = None

        # Try loading from cache
        if cache_questions and cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text())
                questions = cached.get("questions", [])
                logger.info("questions_loaded_from_cache", count=len(questions), path=str(cache_path))
            except Exception:
                questions = None

        # Generate fresh questions if no cache
        if questions is None:
            t1 = max(2, num_questions // 4 + num_questions % 4)
            t2 = max(2, num_questions // 4)
            t3 = max(2, num_questions // 4)
            t4 = max(1, num_questions - t1 - t2 - t3)

            questions = await self._generate_questions(
                ontology_text, source_sample, num_questions, t1, t2, t3, t4,
                dataset_stats=dataset_stats,
            )
            if not questions:
                return QueryScore(results=[])

            logger.info("questions_generated", count=len(questions))

            # Compute deterministic ground truth from CSV
            if self._csv_path and self._csv_path.exists():
                gt_tasks = [
                    _host()._compute_ground_truth(q["question"], self._csv_path, self._openrouter_key)
                    for q in questions
                ]
                gt_results = await asyncio.gather(*gt_tasks)
                gt_computed = 0
                for q, gt in zip(questions, gt_results):
                    if gt is not None:
                        q["expected_answer"] = gt
                        gt_computed += 1
                logger.info("ground_truth_computed_all", computed=gt_computed, total=len(questions))

            # Save to cache for re-runs
            if cache_questions:
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps({"questions": questions}, indent=2))
                logger.info("questions_cached", path=str(cache_path))

        # Execute and judge concurrently
        # Collect all eval question texts for anti-cheat exclusion:
        # the /ask endpoint will exclude these from example bank retrieval
        # so the model can't copy SPARQL from a near-identical example.
        all_eval_questions = [q["question"] for q in questions]
        semaphore = asyncio.Semaphore(concurrency)

        async def _run_one(q: dict) -> QuestionResult:
            async with semaphore:
                if fast_judge:
                    result = await self._execute_and_fast_judge(q, kg_name, model, all_eval_questions=all_eval_questions)
                else:
                    result = await self._execute_and_judge(
                        q, kg_name, source_sample, model,
                        ontology_text=ontology_text,
                        dataset_stats=dataset_stats,
                        all_eval_questions=all_eval_questions,
                    )
                status = "✓" if result.verdict == "correct" else "✗"
                logger.info(
                    "question_evaluated",
                    tier=result.tier,
                    verdict=result.verdict,
                    status=status,
                    question=result.question[:60],
                )
                return result

        results = await asyncio.gather(*[_run_one(q) for q in questions])
        return QueryScore(results=list(results))

    async def _generate_questions(
        self,
        ontology_text: str,
        source_sample: str,
        num_questions: int,
        t1: int, t2: int, t3: int, t4: int,
        dataset_stats: DatasetStats | None = None,
    ) -> list[dict]:
        """Use an LLM to generate test questions from ontology + dataset stats.

        When dataset_stats is provided (computed from the full file), the question
        generator uses accurate counts and distributions for expected answers.
        """
        prompt = QUESTION_GEN_PROMPT.format(
            num_questions=num_questions, t1=t1, t2=t2, t3=t3, t4=t4,
        )

        if dataset_stats and dataset_stats.stats_summary:
            user_content = (
                f"Ontology schema:\n{ontology_text}\n\n"
                f"Dataset statistics (computed from ALL {dataset_stats.total_rows} rows):\n"
                f"{dataset_stats.stats_summary}\n\n"
                f"Sample rows (for format reference):\n{dataset_stats.sample_text}"
            )
        else:
            user_content = (
                f"Ontology schema:\n{ontology_text}\n\n"
                f"Source data sample:\n{source_sample[:SOURCE_SAMPLE_CHARS]}"
            )

        response = await _host()._llm_call(
            prompt=user_content,
            system=prompt,
            api_key=self._openrouter_key,
        )

        try:
            return _host()._parse_json(response)
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("question_gen_parse_error", error=str(e))
            return []

    async def _execute_and_judge(
        self,
        question: dict,
        kg_name: str | None,
        source_sample: str,
        model: str | None,
        ontology_text: str = "",
        dataset_stats: DatasetStats | None = None,
        all_eval_questions: list[str] | None = None,
    ) -> QuestionResult:
        """Execute one question via /ask and have an LLM judge the result.

        The judge receives ontology context (so it can diagnose predicate URI
        mismatches) and full dataset stats (so ground truth is accurate).
        """
        tier = question.get("tier", 1)
        q_text = question["question"]
        expected = question.get("expected_answer", "")

        # Execute via API
        base = f"{self._api_url}/graphs/{self._tenant}"
        headers = {"X-API-Key": self._api_key, "Content-Type": "application/json"}
        body: dict = {"question": q_text}
        if kg_name:
            body["kg_name"] = kg_name
        if model:
            body["model"] = model
        if all_eval_questions:
            body["exclude_questions"] = all_eval_questions

        t0 = time.time()
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                res = await client.post(f"{base}/ask", headers=headers, json=body)
            timing_ms = round((time.time() - t0) * 1000, 1)

            if res.status_code != 200:
                return QuestionResult(
                    tier=tier, question=q_text, expected=expected,
                    answer=f"HTTP {res.status_code}", sparql="",
                    verdict="error", explanation=f"API returned {res.status_code}",
                    timing_ms=timing_ms,
                )
            result = res.json()
        except Exception as e:
            return QuestionResult(
                tier=tier, question=q_text, expected=expected,
                answer=str(e), sparql="",
                verdict="error", explanation=f"Request failed: {e}",
                timing_ms=round((time.time() - t0) * 1000, 1),
            )

        answer = result.get("answer", "")
        sparql = result.get("sparql", "")
        total_ms = result.get("timing", {}).get("total_ms", timing_ms)

        # Judge the answer — include ontology context and full dataset stats
        stats_section = ""
        if dataset_stats and dataset_stats.stats_summary:
            stats_section = (
                f"\nDataset statistics (computed from ALL {dataset_stats.total_rows} rows):\n"
                f"{dataset_stats.stats_summary}\n"
            )

        judge_prompt = (
            f"Question: {q_text}\n"
            f"Expected answer (computed from source data): {expected}\n"
            f"Generated SPARQL:\n{sparql}\n\n"
            f"System's answer: {answer}\n\n"
            f"Ontology schema (types and predicates available in the graph):\n"
            f"{ontology_text}\n"
            f"{stats_section}\n"
            f"Sample source data rows (for format reference):\n"
            f"{source_sample[:SOURCE_SAMPLE_CHARS]}"
        )

        try:
            judge_response = await _host()._llm_call(
                prompt=judge_prompt,
                system=QUERY_JUDGE_PROMPT,
                api_key=self._openrouter_key,
            )
            judgment = _host()._parse_json(judge_response)
            return QuestionResult(
                tier=tier, question=q_text, expected=expected,
                answer=answer, sparql=sparql,
                verdict=judgment.get("verdict", "error"),
                explanation=judgment.get("explanation", ""),
                corrected_sparql=judgment.get("corrected_sparql", ""),
                failure_category=judgment.get("failure_category", ""),
                timing_ms=total_ms,
            )
        except Exception as e:
            # Judge failed, but we still have the answer
            return QuestionResult(
                tier=tier, question=q_text, expected=expected,
                answer=answer, sparql=sparql,
                verdict="error", explanation=f"Judge failed: {e}",
                timing_ms=total_ms,
            )

    async def _execute_and_fast_judge(
        self,
        question: dict,
        kg_name: str | None,
        model: str | None,
        all_eval_questions: list[str] | None = None,
    ) -> QuestionResult:
        """Execute a question and judge programmatically (no LLM judge).

        Fast judge uses numeric tolerance for comparison:
          - Counts (integers): ±2% tolerance
          - Averages/floats: ±5% tolerance
          - Strings: case-insensitive exact match or CONTAINS
          - Lists: check if answer contains expected items

        This is ~50x faster than the LLM judge (~5ms vs ~2s per question).
        Use for rapid iteration. Switch to LLM judge for final validation.
        """
        import re

        tier = question.get("tier", 1)
        q_text = question["question"]
        expected = str(question.get("expected_answer", ""))

        # Execute via API
        base = f"{self._api_url}/graphs/{self._tenant}"
        headers = {"X-API-Key": self._api_key, "Content-Type": "application/json"}
        body: dict = {"question": q_text}
        if kg_name:
            body["kg_name"] = kg_name
        if model:
            body["model"] = model
        if all_eval_questions:
            body["exclude_questions"] = all_eval_questions

        t0 = time.time()
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                res = await client.post(f"{base}/ask", headers=headers, json=body)
            timing_ms = round((time.time() - t0) * 1000, 1)
            if res.status_code != 200:
                return QuestionResult(
                    tier=tier, question=q_text, expected=expected,
                    answer=f"HTTP {res.status_code}", sparql="",
                    verdict="error", explanation=f"API returned {res.status_code}",
                    timing_ms=timing_ms,
                )
            result = res.json()
        except Exception as e:
            return QuestionResult(
                tier=tier, question=q_text, expected=expected,
                answer=str(e), sparql="",
                verdict="error", explanation=f"Request failed: {e}",
                timing_ms=round((time.time() - t0) * 1000, 1),
            )

        answer = result.get("answer", "")
        sparql = result.get("sparql", "")
        total_ms = result.get("timing", {}).get("total_ms", timing_ms)

        if not expected:
            return QuestionResult(
                tier=tier, question=q_text, expected=expected,
                answer=answer, sparql=sparql,
                verdict="error", explanation="No ground truth available",
                timing_ms=total_ms,
            )

        # Programmatic comparison
        verdict = "wrong"
        explanation = ""

        # Detect if expected is a descriptive sentence vs a pure number.
        # Descriptive answers contain many alpha chars and shouldn't be
        # reduced to a number by stripping all non-digits.
        alpha_ratio = sum(1 for c in expected if c.isalpha()) / max(len(expected), 1)
        expected_is_numeric = alpha_ratio < 0.3

        # Try numeric comparison (only when expected looks like a number)
        try:
            if not expected_is_numeric:
                raise ValueError("Expected is descriptive text, skip numeric comparison")
            expected_num = float(re.sub(r"[^\d.\-eE]", "", expected))
            # Extract first number from answer (support scientific notation)
            answer_nums = re.findall(r"-?[\d]+\.?\d*(?:[eE][+-]?\d+)?", answer)
            if answer_nums:
                answer_num = float(answer_nums[0])
                # Compare absolute values to handle sign differences
                a_abs, e_abs = abs(answer_num), abs(expected_num)
                # Tolerance: ±2% for counts (integers), ±5% for floats
                if e_abs == 0:
                    if a_abs == 0:
                        verdict = "correct"
                        explanation = "Both zero"
                elif "." in expected and expected.split(".")[-1] != "0":
                    # Float comparison (averages, etc.)
                    tolerance = 0.05
                    if abs(a_abs - e_abs) / e_abs <= tolerance:
                        verdict = "correct"
                        explanation = f"Within {tolerance*100}% tolerance ({answer_num} vs {expected_num})"
                    else:
                        explanation = f"Outside tolerance: {answer_num} vs {expected_num} (diff: {abs(a_abs - e_abs) / e_abs * 100:.1f}%)"
                else:
                    # Integer comparison (counts)
                    tolerance = 0.02
                    if abs(a_abs - e_abs) / max(e_abs, 1) <= tolerance:
                        verdict = "correct"
                        explanation = f"Within {tolerance*100}% tolerance ({answer_num} vs {expected_num})"
                    else:
                        explanation = f"Count mismatch: {answer_num} vs {expected_num} (diff: {abs(a_abs - e_abs) / max(e_abs, 1) * 100:.1f}%)"
        except (ValueError, IndexError):
            # String comparison — try multiple strategies
            exp_lower = expected.lower().strip().strip("'\"")
            ans_lower = answer.lower().strip()

            # Strategy 1: substring match
            if exp_lower in ans_lower or ans_lower in exp_lower:
                verdict = "correct"
                explanation = "String match"
            else:
                # Strategy 2: extract ALL numbers from both and compare pairwise
                exp_nums = re.findall(r"-?[\d]+\.?\d*(?:[eE][+-]?\d+)?", expected)
                ans_nums = re.findall(r"-?[\d]+\.?\d*(?:[eE][+-]?\d+)?", answer)
                if exp_nums and ans_nums and len(exp_nums) <= len(ans_nums):
                    all_match = True
                    for en in exp_nums:
                        e_val = float(en)
                        matched = False
                        for an in ans_nums:
                            a_val = float(an)
                            if e_val == 0 and a_val == 0:
                                matched = True
                            elif e_val != 0 and abs(abs(a_val) - abs(e_val)) / abs(e_val) <= 0.05:
                                matched = True
                            if matched:
                                break
                        if not matched:
                            all_match = False
                            break
                    if all_match:
                        verdict = "correct"
                        explanation = f"All {len(exp_nums)} expected numbers found in answer (±5%)"

                # Strategy 3: word overlap
                if verdict != "correct":
                    exp_words = set(re.findall(r"[a-z]{3,}", exp_lower))
                    ans_words = set(re.findall(r"[a-z]{3,}", ans_lower))
                    if exp_words and ans_words:
                        overlap = len(exp_words & ans_words) / len(exp_words)
                        if overlap >= 0.6:
                            verdict = "correct"
                            explanation = f"Word overlap: {overlap*100:.0f}%"
                        else:
                            explanation = f"String mismatch: '{answer[:50]}' vs '{expected[:50]}'"
                    else:
                        explanation = f"String mismatch: '{answer[:50]}' vs '{expected[:50]}'"

        # Classify failure category for negative fine-tuning examples
        failure_cat = ""
        if verdict != "correct":
            ans_lower = answer.lower() if answer else ""
            if not answer or ans_lower in ("no results found.", "no results found"):
                failure_cat = "empty_result"
            elif answer.startswith("Could not answer") or answer.startswith("ERROR"):
                failure_cat = "error"
            elif "http" in ans_lower and (IRI_BASE.split("://",1)[-1] in ans_lower or "graph.infona.ai" in ans_lower or "graph.infona.ai" in ans_lower):
                failure_cat = "uri_instead_of_value"
            else:
                failure_cat = "wrong_answer"

        return QuestionResult(
            tier=tier, question=q_text, expected=expected,
            answer=answer, sparql=sparql,
            verdict=verdict, explanation=explanation,
            timing_ms=total_ms,
            failure_category=failure_cat,
        )

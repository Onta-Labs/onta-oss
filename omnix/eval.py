#!/usr/bin/env python3
"""Omnix Eval — automated evaluation of ontology quality and query accuracy.

Overview
========
This module evaluates two dimensions of the Omnix ingestion + query pipeline:

  1. **Ontology Quality** — Does the system create a well-structured, reusable
     knowledge graph? Are entities properly decomposed (address → city/state/zip)?
     Are types connected in a hierarchy? Are predicates consistent?

  2. **Query Accuracy** — Can the system answer natural language questions correctly?
     Does it generate valid SPARQL? Are answers factually correct against ground truth?

Architecture
============
The eval runs in a loop designed for iterative improvement:

    ┌──────────────────────────────────────────────────────────────────┐
    │  1. Ingest dataset(s) via CLI                                   │
    │  2. Eval ontology quality (LLM judge scores structure)          │
    │  3. LLM generates questions at 4 difficulty tiers               │
    │  4. Execute each question via /ask endpoint                     │
    │  5. LLM judge evaluates answers against source data             │
    │  6. Report: scores, failures, weak points                       │
    │  7. Fix gaps in pipeline → repeat from step 2                   │
    └──────────────────────────────────────────────────────────────────┘

Usage
=====
Via CLI::

    # Full eval: ontology quality + query accuracy
    omnix eval data/listings.csv --kg real-estate --questions 20

    # Ontology quality only (no questions)
    omnix eval --ontology-only --kg real-estate

    # Query accuracy only (ontology already ingested)
    omnix eval --query-only data/listings.csv --kg real-estate

    # Multi-domain test: ingest two datasets, check ontology reuse
    omnix eval data/listings.csv data/restaurants.csv --kg combined

Via Python::

    from omnix.eval import OntologyEvaluator, QueryEvaluator, run_full_eval
    report = await run_full_eval(
        api_url="http://localhost:8000",
        tenant="demo-tenant",
        kg_name="test-kg",
        dataset_paths=["data/listings.csv"],
        num_questions=20,
    )

Question Difficulty Tiers
=========================
The LLM generates questions across 4 tiers to test increasing complexity:

  **Tier 1 — Count/Lookup** (basic aggregation)
      "How many properties are there?"
      Tests: SELECT COUNT, basic entity retrieval

  **Tier 2 — Filter** (WHERE clauses)
      "How many properties have 3 or more bedrooms?"
      Tests: Comparison operators, attribute filtering

  **Tier 3 — Join/Relationship** (graph traversal)
      "Which brokers have listings in Austin?"
      Tests: Relationship traversal, multi-entity queries

  **Tier 4 — Multi-hop/Complex** (chained reasoning)
      "What is the average price of condos listed by brokers in zip 78745?"
      Tests: Multiple joins, aggregation over filtered relationships

Ontology Quality Dimensions
============================
The ontology judge scores 6 dimensions (each 0-10):

  **Decomposition** — Are composite values broken into entities?
      Bad:  Property.address = "123 Main St, Austin, TX"
      Good: Property → located_in → City("Austin") → located_in → State("TX")

  **Reusability** — Are entities created that other datasets would share?
      Bad:  Property.city_name = "Austin" (dead-end string)
      Good: City("Austin") as its own entity (reusable across domains)

  **Hierarchy** — Are types connected via subClassOf, not orphaned?
      Bad:  Broker and Person as unrelated types
      Good: Broker subClassOf Person

  **Predicate Consistency** — No duplicate predicates for the same relationship?
      Bad:  located_in AND is_located_in AND location_of
      Good: Single canonical predicate per relationship

  **Entity-First Compliance** — Are real-world things entities, not literals?
      Bad:  Property.agent = "John Smith" (string literal)
      Good: Property → listed_by → Person("John Smith")

  **Type Naming** — PascalCase, singular, descriptive?
      Bad:  "properties", "LISTING", "real_estate_listing"
      Good: "Property", "Listing"

LLM Judge Design
================
The judge is a separate LLM call that receives:
  - The question asked
  - The generated SPARQL
  - The answer returned
  - A relevant slice of the source data (for ground truth derivation)

The judge does NOT have access to a pre-computed answer key. It derives ground truth
from the source data on the fly. This means it works for any dataset without manual
annotation.

For ontology quality, the judge receives:
  - The full ontology schema (types, attributes, relationships)
  - The source data sample (to understand what was ingested)
  - The 6 scoring dimensions with examples

The judge uses a reasoning model (DeepSeek R1 or Claude Sonnet 4.6) for accuracy.

Report Format
=============
The eval produces a JSON report and a human-readable summary::

    OMNIX EVAL REPORT
    ════════════════════════════════════════════════════════════
    Dataset:      listings.csv (1000 rows)
    KG:           real-estate
    Model:        deepseek/deepseek-v3.2

    ONTOLOGY QUALITY (45/60)
    ────────────────────────────────────────────────────────────
    Decomposition:          7/10  Address decomposed, but phone not
    Reusability:            8/10  City, State, ZipCode as entities
    Hierarchy:              6/10  Broker→Person missing
    Predicate Consistency:  9/10  No duplicates found
    Entity-First:           8/10  Agent is entity, but office is string
    Type Naming:            7/10  "Property" good, "ZipCode" → "PostalCode"?

    Weak points:
      - Property.office_name should be Company entity
      - No Broker→Person subtype relationship
      - Phone numbers stored as strings, not ContactInfo entity

    QUERY ACCURACY (16/20)
    ────────────────────────────────────────────────────────────
    Tier 1 (Count):      5/5   avg 1.2s
    Tier 2 (Filter):     4/5   avg 2.1s  ← bedroom filter missed
    Tier 3 (Join):       4/5   avg 3.4s  ← broker relationship failed
    Tier 4 (Multi-hop):  3/5   avg 4.8s  ← avg price + filter + join

    Failed questions:
      Q7:  "How many 3-bed properties under $500K?"
           Expected: 245  Got: 367  (filter not applied)
           SPARQL: SELECT (COUNT(...)) — missing price < 500000
      ...
    ════════════════════════════════════════════════════════════

Extending the Eval
==================
To add a new ontology quality dimension:
  1. Add it to ONTOLOGY_DIMENSIONS in this file
  2. Add scoring criteria to ONTOLOGY_JUDGE_PROMPT
  3. Update OntologyScore dataclass

To add a new question tier:
  1. Add it to QUESTION_TIERS in this file
  2. Add generation instructions to QUESTION_GEN_PROMPT
  3. Update the report formatting in _format_report()

To change the judge model:
  Set OMNIX_EVAL_MODEL env var (default: uses the same provider as query generation)
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import structlog

logger = structlog.stdlib.get_logger("omnix.eval")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

EVAL_MODEL = os.environ.get("OMNIX_EVAL_MODEL", "deepseek/deepseek-v3.2")
EVAL_PROVIDER = os.environ.get("OMNIX_EVAL_PROVIDER", "openrouter")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Maximum chars of source data to include in judge context.
# For question generation, we compute dataset stats from the FULL file
# and include those stats + a sample. The judge never sees only a slice.
SOURCE_SAMPLE_CHARS = 8000

# Maximum rows to include as raw sample in prompts (header + N rows)
SOURCE_SAMPLE_ROWS = 30

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class ModelConfig:
    """Tracks which LLM model is used for each role in the eval pipeline.

    This appears in the report so every eval run is fully reproducible.
    """
    eval_judge: str = ""      # ontology scoring + query judging
    question_gen: str = ""    # question generation
    query_model: str = ""     # the model used by /ask to generate SPARQL
    extraction: str = ""      # the model used during ingestion (from env)

    def to_dict(self) -> dict:
        return {k: v for k, v in {
            "eval_judge": self.eval_judge,
            "question_gen": self.question_gen,
            "query_model": self.query_model,
            "extraction": self.extraction,
        }.items() if v}


@dataclass
class DatasetStats:
    """Statistics computed from the FULL source dataset.

    Unlike the raw sample (which is truncated), these stats cover every row.
    The question generator and query judge use these stats for accurate
    ground truth instead of deriving answers from a partial sample.
    """
    total_rows: int = 0
    columns: list[str] = field(default_factory=list)
    sample_text: str = ""       # first N rows as text for LLM context
    stats_summary: str = ""     # computed stats (counts, distributions, etc.)

    @staticmethod
    def from_csv(path: Path) -> "DatasetStats":
        """Compute stats from a full CSV file.

        Reads the entire file to produce accurate counts, distributions,
        and value ranges. The question generator uses these stats (not the
        sample rows) to set expected answers, so ground truth is correct
        even for questions about the full dataset.
        """
        import csv

        content = path.read_text()
        reader = csv.DictReader(content.splitlines())
        rows = list(reader)
        if not rows:
            return DatasetStats()

        columns = list(rows[0].keys())
        total_rows = len(rows)

        # Build sample text (header + first N rows)
        lines = content.split("\n")
        sample_lines = lines[:SOURCE_SAMPLE_ROWS + 1]  # +1 for header
        sample_text = "\n".join(sample_lines)

        # Compute per-column stats
        stats_parts = [f"Total rows: {total_rows}", f"Columns: {', '.join(columns)}", ""]

        for col in columns:
            values = [(r.get(col) or "").strip() for r in rows if (r.get(col) or "").strip()]
            if not values:
                continue

            # Try numeric stats
            nums = []
            for v in values:
                try:
                    nums.append(float(v.replace(",", "")))
                except ValueError:
                    pass

            if len(nums) > len(values) * 0.5:
                # Numeric column
                avg = sum(nums) / len(nums)
                mn, mx = min(nums), max(nums)
                stats_parts.append(
                    f"{col}: numeric, {len(values)} non-empty, "
                    f"min={mn:g}, max={mx:g}, avg={avg:,.1f}"
                )
            else:
                # Categorical column — show value distribution (top 10)
                from collections import Counter
                counts = Counter(values)
                top = counts.most_common(10)
                unique = len(counts)
                dist = ", ".join(f"{v}={c}" for v, c in top)
                stats_parts.append(
                    f"{col}: {unique} unique values, {len(values)} non-empty. "
                    f"Top: {dist}"
                )

        return DatasetStats(
            total_rows=total_rows,
            columns=columns,
            sample_text=sample_text,
            stats_summary="\n".join(stats_parts),
        )

    @staticmethod
    def from_text(path: Path) -> "DatasetStats":
        """For non-CSV files, just read a sample."""
        content = path.read_text()
        return DatasetStats(
            sample_text=content[:SOURCE_SAMPLE_CHARS],
            stats_summary=f"Text file: {len(content)} chars",
        )


@dataclass
class OntologyDimension:
    """One scored dimension of ontology quality."""
    name: str
    score: int  # 0-10
    explanation: str
    issues: list[str] = field(default_factory=list)


@dataclass
class OntologyScore:
    """Full ontology quality evaluation."""
    dimensions: list[OntologyDimension]
    total: int = 0
    max_total: int = 60  # 6 dimensions × 10
    weak_points: list[str] = field(default_factory=list)
    raw_judge_response: str = ""

    def __post_init__(self):
        self.total = sum(d.score for d in self.dimensions)


@dataclass
class QuestionResult:
    """Result of evaluating one question.

    When a query fails, the judge provides a corrected_sparql showing what
    the SPARQL *should* look like. This makes failures actionable — you can
    see exactly where the generated query diverged from the correct one.
    """
    tier: int
    question: str
    expected: str
    answer: str
    sparql: str
    verdict: str  # "correct", "partial", "wrong", "error"
    explanation: str
    corrected_sparql: str = ""  # what the SPARQL should have been (judge output)
    failure_category: str = ""  # "bad_predicate", "missing_join", "wrong_filter", etc.
    timing_ms: float = 0.0


@dataclass
class QueryScore:
    """Full query accuracy evaluation."""
    results: list[QuestionResult]
    by_tier: dict[int, dict] = field(default_factory=dict)  # tier → {total, passed, avg_ms}

    def __post_init__(self):
        for tier in range(1, 5):
            tier_results = [r for r in self.results if r.tier == tier]
            passed = sum(1 for r in tier_results if r.verdict == "correct")
            avg_ms = (
                sum(r.timing_ms for r in tier_results) / len(tier_results)
                if tier_results else 0
            )
            self.by_tier[tier] = {
                "total": len(tier_results),
                "passed": passed,
                "avg_ms": round(avg_ms, 1),
            }


@dataclass
class EvalReport:
    """Complete evaluation report."""
    dataset_names: list[str]
    kg_name: str
    model: str
    models: ModelConfig = field(default_factory=ModelConfig)
    ontology: OntologyScore | None = None
    queries: QueryScore | None = None
    timestamp: str = ""
    duration_s: float = 0.0


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

ONTOLOGY_JUDGE_PROMPT = """\
You are an expert knowledge graph ontologist evaluating the quality of an \
automatically-generated ontology. You will receive the ontology schema and a \
sample of the source data that was ingested.

Score each dimension 0-10. Be strict. A 10 means perfect, production-ready. \
A 5 means "works but has clear structural problems." Below 5 means "needs \
significant rework."

Dimensions:

1. DECOMPOSITION (0-10)
   Are composite values broken into separate entities?
   Bad: Property.address = "123 Main St, Austin, TX" (flat string)
   Good: Property → located_in → City → located_in → State
   Score 10 if ALL composite values are properly decomposed.
   Score 5 if some are decomposed but others remain flat.
   Score 0 if everything is flat string attributes.

2. REUSABILITY (0-10)
   Are entities created that other datasets would naturally share?
   Cities, states, countries, people, organizations should be their own entities.
   Score 10 if all shareable concepts are entities.
   Score 5 if some are entities but others are buried as string attributes.

3. HIERARCHY (0-10)
   Are types connected via subClassOf relationships?
   Broker should be subClassOf Person. Condo subClassOf Property.
   Score 10 if all natural hierarchies exist.
   Score 5 if some exist but obvious ones are missing.
   Score 0 if all types are flat/orphaned.

4. PREDICATE_CONSISTENCY (0-10)
   Is there exactly one predicate per semantic relationship?
   Bad: both "located_in" and "is_located_in" exist.
   Score 10 if no duplicates. Score 5 if a few duplicates exist.

5. ENTITY_FIRST (0-10)
   Are real-world things modeled as entities, not string literals?
   Bad: Property.agent_name = "John Smith" (dead-end string)
   Good: Property → listed_by → Person("John Smith")
   Score 10 if all real-world references are entities.

6. TYPE_NAMING (0-10)
   Are type names PascalCase, singular, descriptive?
   Bad: "properties", "LISTING", "real_estate_listing"
   Good: "Property", "Listing", "RealEstateBroker"

For each dimension, provide:
- The score (integer 0-10)
- A one-sentence explanation
- Specific issues found (if score < 10)

Also list the top 3-5 "weak points" — the most impactful improvements that \
would make this ontology significantly better.

Respond with valid JSON only:
{
  "dimensions": [
    {"name": "decomposition", "score": N, "explanation": "...", "issues": ["..."]},
    {"name": "reusability", "score": N, "explanation": "...", "issues": ["..."]},
    {"name": "hierarchy", "score": N, "explanation": "...", "issues": ["..."]},
    {"name": "predicate_consistency", "score": N, "explanation": "...", "issues": ["..."]},
    {"name": "entity_first", "score": N, "explanation": "...", "issues": ["..."]},
    {"name": "type_naming", "score": N, "explanation": "...", "issues": ["..."]}
  ],
  "weak_points": ["...", "...", "..."]
}"""

QUESTION_GEN_PROMPT = """\
You are generating test questions for a knowledge graph query system. Given the \
ontology schema and a sample of the source data, generate exactly {num_questions} \
questions distributed across 4 difficulty tiers.

Distribution:
- Tier 1 (count/lookup): {t1} questions — basic COUNT, simple retrieval
- Tier 2 (filter): {t2} questions — WHERE clauses with comparisons
- Tier 3 (join): {t3} questions — relationship traversal across entities
- Tier 4 (multi-hop): {t4} questions — chained joins + aggregation

Rules:
- Each question must be answerable from the data that was ingested
- Include the expected answer derived from the DATASET STATISTICS provided \
  (these cover the FULL dataset, not just a sample)
- Tier 1 questions should have exact numeric answers
- Tier 2 questions should test numeric/string/date filters
- Tier 3 questions should require traversing at least one relationship
- Tier 4 questions should combine filtering + joins + aggregation
- Questions should feel natural, like a human analyst would ask them
- Vary the entity types and attributes tested across questions

Respond with valid JSON only:
[
  {{
    "tier": 1,
    "question": "How many properties are there?",
    "expected_answer": "1000",
    "reasoning": "COUNT of all Property entities"
  }},
  ...
]"""

QUERY_JUDGE_PROMPT = """\
You are evaluating whether a knowledge graph query system answered a question \
correctly. You will receive:
1. The question asked
2. The generated SPARQL query
3. The system's answer
4. The ontology schema (types, attributes, predicates available in the graph)
5. Dataset statistics computed from the FULL source data (use these for ground truth)
6. A sample of raw source data rows

IMPORTANT: The expected answer was computed deterministically from the full source \
CSV using pandas. Trust it as ground truth. Compare the system's answer against \
this expected answer. The dataset statistics and sample rows are provided for \
context only.

Evaluate the answer and respond with valid JSON:
{{
  "verdict": "correct" | "partial" | "wrong" | "error",
  "expected": "the correct answer based on dataset statistics",
  "explanation": "one sentence explaining your judgment",
  "failure_category": "none" | "bad_predicate_uri" | "missing_join" | "wrong_filter" | "wrong_aggregation" | "empty_result" | "uri_instead_of_value" | "other",
  "corrected_sparql": "if verdict is not correct, write the SPARQL that WOULD produce the correct answer using the ontology schema provided. Use exact predicate URIs from the ontology. If verdict is correct, leave empty."
}}

Failure categories:
- "bad_predicate_uri": SPARQL uses wrong predicate URIs (e.g., <price> instead of <https://omnix.dev/types/Property/attrs/price>)
- "missing_join": Query doesn't traverse a relationship that's needed
- "wrong_filter": Filter condition is malformed or uses wrong comparison
- "wrong_aggregation": COUNT/AVG/SUM is wrong or applied to wrong variable
- "empty_result": Query returns no results when data exists
- "uri_instead_of_value": Returns entity URIs instead of human-readable attribute values
- "none": Answer is correct
- "other": Doesn't fit the above categories

Scoring:
- "correct": Answer matches expected value (within 2% for counts, within 5% for averages/sums)
- "partial": Answer is in the right direction but imprecise or incomplete
- "wrong": Answer is factually incorrect
- "error": System returned an error, empty result, or nonsensical response

For counts, allow a tolerance of up to 2% (to account for minor data normalization \
differences between the CSV and the knowledge graph, such as case sensitivity or \
whitespace). For averages and sums, allow up to 5%. Be lenient on text answers \
(paraphrasing is fine if facts are correct)."""


GROUND_TRUTH_PROMPT = """\
You are generating a pandas expression to compute the exact answer to a question \
about a CSV dataset. The DataFrame is already loaded as `df` with these columns:

{columns}

Column dtypes (auto-detected):
{dtypes}

Rules:
- Return ONLY valid Python that evaluates to a single scalar value (int, float, or str)
- Use pandas operations on `df`
- For counts, return an int
- For averages/sums, return a float rounded to 2 decimal places
- For text answers, return a string
- Handle NaN/empty values (use .dropna() when needed)
- String comparisons should be case-insensitive where appropriate
- Numeric columns may have been loaded as strings — cast if needed
- When the question asks "which X has the most Y", return the name/label, not a ratio
- When asked for a count, always return len(...) or .count(), never .mean() or ratios
- Match column values exactly as shown in the sample data (case-sensitive unless question implies otherwise)

Example questions and expressions:
- "How many properties are there?" → len(df)
- "How many have 4+ bedrooms?" → len(df[df['bedrooms'] >= 4])
- "Average price of SINGLE_FAMILY in Austin?" → round(df[(df['home_type']=='SINGLE_FAMILY') & (df['city'].str.upper()=='AUSTIN')]['price'].mean(), 2)
- "How many listings by broker X?" → len(df[df['broker'].str.contains('X', case=False, na=False)])
- When using str.contains with values that have special regex chars like (), always add regex=False

Respond with ONLY the Python expression, nothing else. No markdown, no explanation."""


# ---------------------------------------------------------------------------
# LLM client
# ---------------------------------------------------------------------------


async def _llm_call(
    prompt: str,
    system: str = "",
    api_key: str = "",
    model: str = "",
    max_tokens: int = 4096,
) -> str:
    """Call an LLM via OpenRouter. Returns the response text."""
    model = model or EVAL_MODEL
    api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    async with httpx.AsyncClient(timeout=300) as client:
        res = await client.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0,
            },
        )
        res.raise_for_status()
        return res.json()["choices"][0]["message"]["content"]


def _parse_json(text: str) -> dict | list:
    """Parse JSON from LLM response, stripping code fences if present."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = [l for l in stripped.split("\n") if not l.strip().startswith("```")]
        stripped = "\n".join(lines)
    return json.loads(stripped)


async def _compute_ground_truth(
    question: str,
    csv_path: Path,
    api_key: str = "",
) -> str | None:
    """Compute deterministic ground truth by running pandas on the CSV.

    Uses an LLM to translate the question into a pandas expression,
    then executes it against the full DataFrame. Returns the exact answer
    as a string, or None if computation fails.
    """
    import pandas as pd

    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        logger.warning("ground_truth_csv_read_error", error=str(e))
        return None

    # Coerce numeric columns
    for col in df.columns:
        try:
            df[col] = pd.to_numeric(df[col], errors="ignore")
        except Exception:
            pass

    columns = ", ".join(df.columns.tolist())
    dtypes = "\n".join(f"  {col}: {df[col].dtype}" for col in df.columns)

    prompt = GROUND_TRUTH_PROMPT.format(columns=columns, dtypes=dtypes)
    # Show sample values and basic stats for numeric columns
    col_info = []
    for col in df.columns:
        sample_vals = df[col].dropna().head(5).tolist()
        if pd.api.types.is_numeric_dtype(df[col]):
            col_info.append(f"  {col}: numeric, sample={sample_vals}, min={df[col].min()}, max={df[col].max()}")
        else:
            col_info.append(f"  {col}: string, sample={sample_vals}")
    col_details = "\n".join(col_info)

    user_content = (
        f"Question: {question}\n\n"
        f"Column details:\n{col_details}\n\n"
        f"First 3 rows:\n{df.head(3).to_string()}"
    )

    try:
        expression = await _llm_call(
            prompt=user_content,
            system=prompt,
            api_key=api_key,
            max_tokens=512,
        )
        # Strip markdown fences if present
        expression = expression.strip()
        if expression.startswith("```"):
            lines = [l for l in expression.split("\n") if not l.strip().startswith("```")]
            expression = "\n".join(lines).strip()

        safe_builtins = {
            "len": len, "round": round, "sum": sum, "min": min, "max": max,
            "int": int, "float": float, "str": str, "abs": abs, "sorted": sorted,
            "list": list, "dict": dict, "tuple": tuple, "set": set, "bool": bool,
            "enumerate": enumerate, "zip": zip, "range": range, "map": map,
            "filter": filter, "isinstance": isinstance, "type": type,
            "True": True, "False": False, "None": None,
        }
        import numpy as np
        result = eval(expression, {"df": df, "pd": pd, "np": np, "__builtins__": safe_builtins})  # noqa: S307
        answer = str(result)
        logger.info("ground_truth_computed", question=question[:60], answer=answer[:60], expr=expression[:80])
        return answer
    except Exception as e:
        logger.warning("ground_truth_compute_error", question=question[:60], error=str(e))
        return None


# ---------------------------------------------------------------------------
# Ontology Evaluator
# ---------------------------------------------------------------------------


class OntologyEvaluator:
    """Evaluates the quality of an ontology created by ingestion.

    Usage::

        evaluator = OntologyEvaluator(api_url, api_key, tenant)
        score = await evaluator.evaluate(kg_name, source_sample)

    The evaluator fetches the current ontology schema from the API, sends it
    to an LLM judge along with a sample of the source data, and returns a
    structured score across 6 dimensions.
    """

    def __init__(self, api_url: str, api_key: str, tenant: str, openrouter_key: str = ""):
        self._api_url = api_url
        self._api_key = api_key
        self._tenant = tenant
        self._openrouter_key = openrouter_key

    async def evaluate(
        self,
        kg_name: str | None = None,
        source_sample: str = "",
    ) -> OntologyScore:
        """Fetch ontology and evaluate its quality.

        Args:
            kg_name: Knowledge graph name (None for default graph).
            source_sample: A sample of the source data that was ingested,
                so the judge can assess whether the ontology captures it well.

        Returns:
            OntologyScore with per-dimension scores and weak points.
        """
        # Fetch ontology schema
        ontology_text = await self._fetch_ontology(kg_name)
        if not ontology_text:
            return OntologyScore(
                dimensions=[],
                weak_points=["No ontology found — nothing to evaluate"],
            )

        # Build judge prompt
        user_prompt = (
            f"Ontology schema:\n{ontology_text}\n\n"
            f"Source data sample (first {SOURCE_SAMPLE_CHARS} chars):\n"
            f"{source_sample[:SOURCE_SAMPLE_CHARS]}"
        )

        # Call LLM judge
        response = await _llm_call(
            prompt=user_prompt,
            system=ONTOLOGY_JUDGE_PROMPT,
            api_key=self._openrouter_key,
        )

        # Parse response
        try:
            data = _parse_json(response)
            dimensions = [
                OntologyDimension(
                    name=d["name"],
                    score=int(d["score"]),
                    explanation=d.get("explanation", ""),
                    issues=d.get("issues", []),
                )
                for d in data.get("dimensions", [])
            ]
            weak_points = data.get("weak_points", [])
            return OntologyScore(
                dimensions=dimensions,
                weak_points=weak_points,
                raw_judge_response=response,
            )
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("ontology_judge_parse_error", error=str(e))
            return OntologyScore(
                dimensions=[],
                weak_points=[f"Judge response parse error: {e}"],
                raw_judge_response=response,
            )

    async def _fetch_ontology(self, kg_name: str | None) -> str:
        """Fetch ontology types from the API, formatted as text."""
        base = f"{self._api_url}/graphs/{self._tenant}"
        headers = {"X-API-Key": self._api_key, "Content-Type": "application/json"}

        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.get(f"{base}/ontology/types", headers=headers)
            if res.status_code != 200:
                logger.warning("ontology_fetch_failed", status=res.status_code)
                return ""
            types = res.json()

        if not types:
            return ""

        lines = []
        for t in types:
            parent = f" (subClassOf {t['parent_type']})" if t.get("parent_type") else ""
            lines.append(f"Type: {t['name']}{parent}")
            for attr in t.get("attributes", []):
                datatype = attr.get("datatype", "string")
                lines.append(f"  .{attr['name']} ({datatype})")
            for sub in t.get("subtypes", []):
                lines.append(f"  subtype: {sub}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Query Evaluator
# ---------------------------------------------------------------------------


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
                    _compute_ground_truth(q["question"], self._csv_path, self._openrouter_key)
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
        semaphore = asyncio.Semaphore(concurrency)

        async def _run_one(q: dict) -> QuestionResult:
            async with semaphore:
                if fast_judge:
                    result = await self._execute_and_fast_judge(q, kg_name, model)
                else:
                    result = await self._execute_and_judge(
                        q, kg_name, source_sample, model,
                        ontology_text=ontology_text,
                        dataset_stats=dataset_stats,
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

        response = await _llm_call(
            prompt=user_content,
            system=prompt,
            api_key=self._openrouter_key,
        )

        try:
            return _parse_json(response)
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
            judge_response = await _llm_call(
                prompt=judge_prompt,
                system=QUERY_JUDGE_PROMPT,
                api_key=self._openrouter_key,
            )
            judgment = _parse_json(judge_response)
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

        return QuestionResult(
            tier=tier, question=q_text, expected=expected,
            answer=answer, sparql=sparql,
            verdict=verdict, explanation=explanation,
            timing_ms=total_ms,
        )


# ---------------------------------------------------------------------------
# Full eval orchestrator
# ---------------------------------------------------------------------------


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
      See ARCHITECTURE.md for design rationale.

    Args:
        api_url: Omnix API base URL.
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
    extraction_model = os.environ.get("OMNIX_EXTRACT_MODEL", "deepseek/deepseek-v3.2")
    query_model_resolved = query_model or os.environ.get("OMNIX_QUERY_MODEL", "google/gemini-2.5-flash")
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
    onto_eval = OntologyEvaluator(api_url, api_key, tenant, openrouter_key)
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
        query_eval = QueryEvaluator(api_url, api_key, tenant, openrouter_key, csv_path=csv_path)
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
    # or where the original SPARQL was correct. This builds a dataset of
    # (question + ontology → correct SPARQL) pairs across many eval runs.
    # Dedup: keyed on (question, graph_uri) — a newer pair for the same question
    # replaces the old one (the corrected SPARQL may improve across runs).
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

        graph_uri = f"https://omnix.dev/graphs/{tenant}" + (f"/kg/{kg_name}" if kg_name else "")
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

        # Auto-rebuild example bank from finetune pairs.
        # This ensures the bank stays in sync when KGs are reingested
        # with new ontology types or schema changes.
        try:
            from omnix.nlp.example_bank import ExampleBank, DEFAULT_BANK_PATH
            import os
            bank = ExampleBank(openrouter_api_key=os.environ.get("OPENROUTER_API_KEY", ""))
            items = []
            for line in ft_path.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    p = json.loads(line)
                    kg = p.get("graph_uri", "").split("/kg/")[-1] if "/kg/" in p.get("graph_uri", "") else ""
                    items.append({
                        "question": p["question"],
                        "sparql": p["sparql"],
                        "kg_name": kg,
                        "ontology_context": p.get("ontology", ""),
                    })
                except (json.JSONDecodeError, KeyError):
                    continue
            if items:
                rebuilt = await bank.add_batch(items)
                bank.save()
                logger.info("example_bank_rebuilt", added=rebuilt, total=bank.size)
        except Exception:
            logger.warning("example_bank_rebuild_failed", exc_info=True)

    return report


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

TIER_NAMES = {1: "Count/Lookup", 2: "Filter", 3: "Join", 4: "Multi-hop"}


def format_report(report: EvalReport) -> str:
    """Format an EvalReport as a human-readable string.

    This produces the summary table printed to stdout after eval completes.
    The raw report data is also saved as JSON for programmatic analysis.
    """
    lines = []
    lines.append("")
    lines.append("OMNIX EVAL REPORT")
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
    """CLI handler for `omnix eval`. Called from omnix/cli.py.

    This function is async because the eval pipeline makes concurrent
    API calls. The CLI wrapper in cli.py runs it with asyncio.run().
    """
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not openrouter_key:
        print("OPENROUTER_API_KEY required for eval (LLM judge calls)", file=__import__("sys").stderr)
        __import__("sys").exit(1)

    api_url = os.environ.get("OMNIX_API_URL", "http://localhost:8000")
    api_key = os.environ.get("OMNIX_API_KEY", "dev-key-001")
    tenant = os.environ.get("OMNIX_TENANT", "demo-tenant")

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

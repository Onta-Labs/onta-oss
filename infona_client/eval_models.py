"""Eval data models and configuration constants.

Implementation sibling of :mod:`infona_client.eval`. Public names are
re-exported from that facade.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


EVAL_MODEL = os.environ.get("INFONA_EVAL_MODEL", "deepseek/deepseek-v3.2")
EVAL_PROVIDER = os.environ.get("INFONA_EVAL_PROVIDER", "openrouter")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Maximum chars of source data to include in judge context.
# For question generation, we compute dataset stats from the FULL file
# and include those stats + a sample. The judge never sees only a slice.
SOURCE_SAMPLE_CHARS = 8000

# Maximum rows to include as raw sample in prompts (header + N rows)
SOURCE_SAMPLE_ROWS = 30


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


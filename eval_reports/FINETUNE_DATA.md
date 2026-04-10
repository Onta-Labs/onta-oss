# Fine-tuning Data for SPARQL Generation

## Overview

This directory contains training data for fine-tuning a small model to replace
the LLM in the SPARQL generation step of the query pipeline.

## Files

### `finetune_pairs.jsonl` — Correct examples

Each line is a JSON object with a verified correct (question → SPARQL) pair:

```json
{
  "question": "How many movies have runtime > 180?",
  "ontology": "Type: Movie — URI: <...>\n  Attributes: runtime (float)...",
  "graph_uri": "https://omnix.dev/graphs/demo-tenant/kg/imdb-movies",
  "sparql": "SELECT (COUNT(...)) FROM <...> WHERE { ... }",
  "source": "eval",
  "dataset": "imdb-top-1000-clean.csv",
  "timestamp": "2026-04-09T..."
}
```

**How it grows:**
- Automatically after every eval run (`omnix eval` saves correct pairs)
- Automatically after Spider4SPARQL benchmark runs
- Deduped by (question, graph_uri) — newer SPARQL replaces older for same question

### `finetune_negatives.jsonl` — Incorrect examples

Each line is a JSON object with a failed (question → wrong SPARQL) pair and
the reason it failed:

```json
{
  "question": "How many singers are from France?",
  "ontology": "Type: Singer — URI: <...>...",
  "graph_uri": "https://omnix.dev/graphs/demo-tenant/kg/...",
  "sparql": "SELECT ... (the wrong query)",
  "answer": "0",
  "expected": "3",
  "failure_category": "missing_join",
  "verdict": "wrong",
  "source": "eval",
  "timestamp": "2026-04-09T..."
}
```

**How it grows:**
- Automatically after every eval run (saves wrong pairs with failure category)
- Useful for teaching the model what NOT to do

**Failure categories:**
- `bad_predicate_uri` — SPARQL uses wrong predicate URI
- `missing_join` — query doesn't traverse a needed relationship
- `wrong_filter` — filter condition is malformed
- `wrong_aggregation` — COUNT/AVG/SUM applied incorrectly
- `empty_result` — returns nothing when data exists
- `uri_instead_of_value` — returns entity URIs instead of readable values
- `error` — HTTP error, timeout, or syntax error

## Target Volume

| Stage | Positive Pairs | Negative Pairs | Status |
|---|---|---|---|
| Current | ~114 | 0 | Collecting |
| After Phase 1 (verified ground truth) | ~300 | ~200 | Next |
| After Phase 2 (pipeline fixes) | ~500 | ~300 | Target |
| Fine-tuning ready | 1,000+ | 500+ | Goal |

## How to Grow the Dataset

1. **Run evals on our 8 KGs**: `omnix eval --query-only --cache-questions --fast-judge --kg <name> <csv>`
   Each run adds correct pairs to `finetune_pairs.jsonl` and wrong pairs to
   `finetune_negatives.jsonl`.

2. **Run Spider4SPARQL benchmark**: `python benchmarks/run_spider_benchmark.py --kg <name>`
   Correct answers feed into the example bank and finetune pairs.

3. **Run more eval rounds**: Each round generates 20 questions per KG. With 8 KGs,
   that's 160 question-answer attempts per round. At ~80% accuracy, each round
   adds ~128 positive and ~32 negative pairs (minus dedup).

4. **Add new datasets**: Ingest a new CSV, run eval. New domain = new SPARQL patterns.

## Quality Notes

- Pairs generated with LLM-estimated ground truth (~70% reliable) may have
  incorrect "correct" labels. After Phase 1 (Q2Forge-style execution-verified
  ground truth), the quality improves significantly.
- The ontology field captures the ontology context AT THE TIME of generation.
  If the ontology changes (reingest with different types), old pairs become stale.
  Run a fresh eval round after any reingest to get updated pairs.
- Negative examples are only as good as the failure categorization. The fast judge
  uses programmatic comparison; the LLM judge is more accurate but slower.

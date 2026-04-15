# Naive RAG Baseline — Holdout v2.0

Floor baseline for the v2.0 holdout head-to-head. Proves that vector-retrieval
RAG cannot handle structured multi-hop questions even when given the same
underlying data and the same answer-generation LLM as Omnix.

## Pipeline

1. **Chunking.** Per KG, read every staged CSV / prepped JSON / raw TSV-zip
   table; cap at 800 rows per table and 2000 chunks per KG total. Each row is
   one chunk of the form:
   `[table=<T> row=<id>] col1=v1 | col2=v2 | ...`
2. **Indexing.** Embed chunks with `openai/text-embedding-3-small` via
   OpenRouter. L2-normalize and cache to `/tmp/baseline_rag/<kg>.npz` plus a
   sidecar JSON with chunk text and table metadata.
3. **Retrieval.** Embed each gold question, compute cosine similarity against
   the KG's index (brute-force numpy dot product — no FAISS), take top-10.
4. **Generation.** Prompt `google/gemini-2.5-flash` with the retrieved rows
   and the question, instructing it to emit a scalar or a comma-separated list
   depending on question shape. 3 seeds per question.
5. **Judging.** The same `fast_judge` (T1 / scalar) and `full_set_judge`
   (T2-T4 / list questions with `full_result_count >= 20`) as the primary
   Omnix baseline, applied directly to the free-text answer (no second parser
   LLM).
6. **Aggregation.** Majority-vote over 3 seeds + seed-level Wilson 95% CI,
   matching `scripts/build_baseline_report.py` exactly.

## Files

- `raw_results.json` — same shape as
  `eval_holdout_v2/baseline_run/holdout_eval_multirun_3x.json`, consumable
  by `scripts/build_baseline_report.py`.
- `per_kg/holdout-v2-<kg>.json` — per-KG results with chunk count and
  table inventory.
- `report.md` — generated via `build_baseline_report.py`.

## Reproducing

```bash
cd /path/to/worktree
set -a && source .env && set +a
python scripts/run_holdout_v2_rag_baseline.py
python scripts/build_baseline_report.py \
  --in eval_holdout_v2/baselines/rag/raw_results.json \
  --out eval_holdout_v2/baselines/rag/report.md
```

Flags:
- `--limit-kg holdout-v2-<kg>` — run a single KG
- `--skip-kgs kg1,kg2` — skip specific KGs
- `--seeds 1,2,3`

## Notes and Caveats

- **Row cap.** Ingest caps are deliberate: this is the *naive-RAG floor*, not
  a tuned production RAG. Lifting the cap would not close the gap for T3/T4
  multi-hop questions because cosine retrieval fundamentally cannot compose
  joins or aggregate across tables it did not co-retrieve.
- **No reranker.** The v1 `scripts/rag_baseline.py` used an LLM cross-encoder
  reranker. We intentionally omit it here — the task is to establish the
  floor, not to ship a production RAG stack.
- **No answer parser.** The judge operates on the LLM's free text. For list
  questions the prompt asks for a comma-separated list; for scalars it asks
  for a bare value. `full_set_judge` handles substring containment so
  prose-wrapped answers still score.
- **Patentsview.** Data ships as zipped TSVs up to 2.2 GB; we stream the
  first ~200 rows per table from each zip rather than decompressing fully.
  Scored 0/30 — as expected, since those questions need joined citation
  graphs that row-level vector retrieval cannot reconstruct.

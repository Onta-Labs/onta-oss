# RAG + Cross-Encoder Rerank Baseline

"Strong RAG" counterpoint to the naive RAG baseline in `../rag/`. Built so reviewers
can't dismiss Table 1 as "you only compared against weak RAG."

## Pipeline

1. **Chunking**: every row of every table in a KG becomes one chunk of the form
   `[table=T row=i] col1=v1 | col2=v2 | ...`. Caps: 800 rows/table, 2000 chunks/KG.
2. **First-stage retrieval**: `openai/text-embedding-3-small` via OpenRouter,
   L2-normalized cosine, top-50. (Vector index is SHARED with naive RAG via
   `/tmp/baseline_rag/<kg>.npz` — the two baselines differ only in the rerank stage.)
3. **Cross-encoder rerank**: `BAAI/bge-reranker-v2-m3` scoring `(question, chunk)`
   pairs on CPU via `sentence-transformers.CrossEncoder`. Top-10 by rerank score
   are selected.
4. **Answer**: `google/gemini-3-flash-preview` @ T=0, seeds 1/2/3, majority vote.
5. **Judge**: identical `fast_judge` (T1) / `full_set_judge` (T2-T4) as Omnix
   primary and naive RAG.

## Anti-cheat

- Chunks contain ONLY raw KG CSV rows. No gold questions, no gold SPARQL, no
  expected answers ever enter the retrieval index or the prompt.
- The reranker scores (question, chunk), never (question, other question).
- Gold fields (`expected_answer`, `gold_sparql`, `full_expected_items`) are read
  only in `judge_answer` and result serialization — never in `llm_answer` or
  the retry loop.

## Run command

```
set -a && source /Users/moeen/Desktop/omnix/.env && set +a
PYTHONUNBUFFERED=1 /Users/moeen/Desktop/omnix/.venv/bin/python \
  scripts/run_holdout_v2_rag_rerank_baseline.py \
  --gold-dir eval_holdout_v2/gold \
  --out eval_holdout_v2/baselines/rag_rerank/ \
  --seeds 1,2,3 --concurrency 5 \
  --model google/gemini-3-flash-preview
```

## Headline (see `report.md` for the full breakdown)

- Seed-level: **27.5% [24.7, 30.5]** (249/906)
- Majority-vote: **27.5% [22.8, 32.8]** (83/302)
- Reranker: `BAAI/bge-reranker-v2-m3`
- Wall clock: 584 s (~9.7 min, vector index was cached)
- Cost: ~$1.47 (Gemini 3 Flash Preview, 1.43M in / 22K out)

## Comparison

| System                        | Accuracy        |
|-------------------------------|-----------------|
| Naive RAG                     | 27.8%           |
| **RAG + BGE-v2-m3 rerank**    | **27.5%**       |
| Omnix primary                 | 91.4%           |

The cross-encoder picks different rows than cosine, but it still returns the
same kind of thing: raw string-similar rows. On multi-hop counting / aggregation
questions a better row picker is not the bottleneck — the bottleneck is that
no row set, no matter how well ranked, lets an LLM reliably compose
group-by / join / filter operations. That's the paper's core claim.

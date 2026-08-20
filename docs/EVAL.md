# Evaluating Infona

**Status: live.** One published pin on this tree: **6 / 8** query-accuracy
(75%) on [`examples/trials.csv`](../examples/trials.csv). Two misses are in
the table below — they are the artifact, not a footnote. Same eight questions
as the prior 2/8 and 5/8 pins.

Infona's product claim is **correct, cited, current**: English in, Cypher on
Neo4j, an exact row out. This page is the public eval of that loop. The
harness lives in `infona_client/eval*.py`. There is no `infona eval` CLI.

`/ask` is always-LLM Cypher. Grounding, probes, and few-shots inform the
model; they do not replace it. These scores were **not** juiced with
golden-string Cypher.

A mediocre published number is the honest artifact. Silence would be worse.

## Reproduce (your data or the published sample)

Need: Docker, the `infona` CLI (`npm i -g @infona-ai/cli`),
`OPENROUTER_API_KEY`, and `pandas` (the harness's ground-truth step imports
it). Schema inference and `/ask` both call an LLM.

```bash
git clone https://github.com/infona-ai/infona-oss.git && cd infona-oss
cp .env.example .env          # set OPENROUTER_API_KEY=sk-or-...
./scripts/oss_up.sh           # Neo4j + API + local CLI config
pip install pandas            # ground-truth helper in infona_client.eval_llm

# published sample (16-row oncology CSV)
infona ingest examples/trials.csv --kg eval-public-trials -y
export INFONA_QUERY_MODEL=openai/gpt-oss-120b
export INFONA_EVAL_MODEL=deepseek/deepseek-v4-pro-0813
python scripts/run_public_eval.py \
    --dataset examples/trials.csv \
    --kg eval-public-trials \
    --questions 8 \
    --out docs/eval/public_results.json

# your own CSV — same command, swap the file and kg
infona ingest path/to/your.csv --kg my-kg -y
python scripts/run_public_eval.py --dataset path/to/your.csv --kg my-kg --questions 8
```

The wrapper calls `infona_client.eval.run_full_eval` (question gen → `/ask`
→ LLM judge). It writes a small JSON claim, not a 50 MB dump. Local
iteration artifacts still land in gitignored `eval_reports/`.

## Dataset and models (this pin)

| | |
|---|---|
| **Dataset** | [`examples/trials.csv`](../examples/trials.csv) — 16 rows, 8 sponsors, 11 drugs, 7 indications. Public program names (FLAURA2, KEYNOTE-189, …). Synthetic `TRIAL-*` IDs. No patient data. |
| **KG** | `eval-public-trials` on tenant `default` (654 triples / 54 entities after ingest) |
| **Questions** | 8 generated (2 per tier) |
| **Query model** (`/ask` → Cypher) | `openai/gpt-oss-120b` |
| **Judge + question gen** | `deepseek/deepseek-v4-pro-0813` (reasoning, effort `high`) |
| **Extraction** (`INFONA_EXTRACT_MODEL`) | `deepseek/deepseek-v4-pro-0813` (reasoning, effort `high`). This pin's KG was not re-ingested. |
| **Ran** | 2026-08-19T15:58:38 · 202.2s · tree `1c531e8` (same published 8; V4 Pro reasoning judge) |
| **Artifact** | [`docs/eval/public_results.json`](eval/public_results.json) |

Override either model with the env vars above or `--query-model` /
`--eval-model`. The JSON records whatever actually ran.

## Results by difficulty tier

The **Visible misses** column is the point. Failures stay in the table.

| Tier | Skill | Passed | Asked | Accuracy | Visible misses |
|------|-------|--------|-------|----------|----------------|
| 1 | Count/Lookup | 2 | 2 | 100% | |
| 2 | Filter | 1 | 2 | 50% | Start-year ≥ 2019 returned trial rows, not a count |
| 3 | Join | 2 | 2 | 100% | |
| 4 | Multi-hop | 1 | 2 | 50% | NSCLC Phase-3 average: missing `average_enrollment` column |
| **All** | | **6** | **8** | **75%** | 2 misses (both error) |

Hits (so the miss column is not the whole story):

- T1: “How many clinical trials are there in total?” → 16
- T1: “How many unique sponsors are involved in clinical trials?” → 8
- T2: “How many clinical trials have status 'Active'?” → 4
- T3: “What are the brand names of drugs targeting PD-1?” → Keytruda, Opdivo
- T3: “Which drugs are evaluated in trials sponsored by AstraZeneca?” → judge `correct` (see note)
- T4: “Which sponsor has the highest total enrollment across all their completed trials?” → Bristol Myers Squibb

Tiers (what the harness generates):

1. **Count/Lookup** — `COUNT`, basic entity retrieval
2. **Filter** — `WHERE` / comparison on an attribute
3. **Join** — relationship traversal across types
4. **Multi-hop** — chained joins + aggregation

## Visible misses

Every question the judge did not mark `correct`. Source:
`failures[]` in the artifact.

| Tier | Question | Expected | Got | Verdict |
|------|----------|----------|-----|---------|
| 2 | How many clinical trials started in or after 2019? | 4 | Returned trial rows (start year 2019+) instead of a count | error |
| 4 | What is the average enrollment for Phase 3 trials targeting NSCLC? | 792.88 | Missing column `average_enrollment` | error / empty_result |

The year question is still list-vs-count (`literal_compare` returns rows). NSCLC still misses the measure-leaf name.

AstraZeneca extra brand suffixes (`osimertinib_Tagrisso`, `durvalumab_Imfinzi`) are still in the `/ask` answer. This V4 Pro judge marked that question `correct`; the prior v3.2 judge marked it `partial`. That flip is judge-strictness, not a product fix.

## Ontology quality (not the headline)

The same run scored the inferred ontology **27 / 60**. Treat that figure
as contaminated: the harness fetches `GET /ontology/types` **without a kg
filter**, so on this local store the judge also saw types from other demo
KGs (Book, Chef, …). Scores are in 0–10 this run (no out-of-range
`decomposition = -1`).

Query accuracy above is KG-scoped to `eval-public-trials`. Use that table.

## What this is not

- **Not Eval-MH / spider-bench.** Those holdouts were untracked from this
  tree (`d9c0ab3`). They are not the public claim.
- **Not a golden-string `/ask`.** Production `/ask` stays always-LLM Cypher.
- **Not `infona eval`.** The stranger command is
  `python scripts/run_public_eval.py`.
- **Not a guarantee on your data.** 16 synthetic rows is a smoke-sized
  public pin, not a paper-scale holdout. Re-run on your CSV.

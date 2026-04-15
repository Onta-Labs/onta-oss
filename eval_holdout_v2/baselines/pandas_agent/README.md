# Pandas-Agent Baseline (Holdout v2.0)

Head-to-head external baseline for the v2.0 holdout paper. This is the
**code-execution** baseline — a LangChain-style pandas agent that hands
each KG's CSV tables to an LLM and asks it to write Python that answers
each gold question.

## Setup

- **LLM:** `google/gemini-2.5-flash` via OpenRouter (same model and
  temperature=0 as Omnix).
- **Data:** the same CSV tables that produced each holdout-v2 KG, loaded
  from `benchmarks/datasets/holdout_v2_staged/`,
  `benchmarks/datasets/holdout_v2_prepped/`,
  `eval_holdout_v2/multitable_csv/`, single-CSV `benchmarks/datasets/*.csv`,
  and JSON / TSV.zip files in `benchmarks/datasets/holdout_v2_raw/`.
- **Agent loop:** system prompt + schema block + question -> LLM emits
  `{"code": "..."}` -> code is executed in a `subprocess.run(python -c ...)`
  sandbox with an 8 s timeout, the dict of DataFrames pre-loaded from a
  pickle file. On exec error the stderr is fed back to the LLM and we try
  again, up to 3 attempts. After 3 failures we score the question as
  wrong (counted as `crashes`).
- **Seeds:** `1, 2, 3` (3 independent runs per question, majority vote).
- **Judge:** the same `fast_judge` / `full_set_judge` from
  `scripts/eval_baseline.py` that judges the primary Omnix run, so the
  numbers in this report are directly comparable.

## How to run

```bash
set -a && source .env && set +a
.venv/bin/python scripts/baselines/pandas_agent/run_pandas_agent.py
.venv/bin/python scripts/build_baseline_report.py \
  --in eval_holdout_v2/baselines/pandas_agent/raw_results.json \
  --out eval_holdout_v2/baselines/pandas_agent/report.md
```

`--resume` skips KGs whose `per_kg/<kg>.json` already exists.
`--limit-kg` and `--limit-q` are useful for smoke tests.

## Output layout

```
per_kg/holdout-v2-<kg>.json   one file per KG (list of question results)
raw_results.json              full run payload (compatible with
                              build_baseline_report.py)
report.md                     headline + per-domain/tier/KG tables
README.md                     this file
```

## Why this baseline matters

Code-execution agents are the strongest fair external baseline for the
v2 paper: they have full Turing-completeness, can do joins, aggregations,
filters, anything pandas can. If Omnix beats this on the same data with
the same LLM, the case for graph-native query is iron-clad.

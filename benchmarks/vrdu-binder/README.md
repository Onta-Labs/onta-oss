# Infona binder-bench (spec v11)

Constructed mix of two published VRDU MTL tests. Not a VRDU task. Isolated
from any GraphDelta ontology-skills bench.

Locked rules: [SPEC.md](SPEC.md). Four-arm SD_0 runbook: [EXPERIMENT.md](EXPERIMENT.md).
Say 27B, not 20B or 32B. This README does not contain Bind@type or F1 numbers.

## What is on the slide

- Bind@type accuracy
- Per-corpus official `metric-micro_f1` from predicted-bind dumps

n=2 tax: two types, so chance bind is 50%. Write that next to the accuracy.

## What is a footnote

Table 4 Mixed |D|=200 FormNet Registration 90.51 and LayoutLMv2/FormNet
Ad-buy 46.54/43.23. Those models saw unredacted vis+layout. Not a paired Δ.
STL FormNet Registration 92.12 is Task 1. Leave it off.

Do not put F1_wrong or oracle-type F1 on the slide.

## CI / dry-run (no download, no LLM)

From the repo root:

```bash
PYTHONPATH=benchmarks/vrdu-binder/src pytest benchmarks/vrdu-binder/tests -q
python -m vrdu_binder dry-run --out /tmp/binder-v11-dry
python -m vrdu_binder experiment-dry --out /tmp/binder-v11-exp-dry
```

The dry-run uses a two-type synthetic mix with disjoint keys. It exercises
the freeze. It is not a VRDU score.

## Live data (still not a model sweep)

Splits are small JSON. OCR (`dataset.jsonl.gz`) is tens of MB per corpus.
PDFs are larger. None of that belongs in git.

```bash
python -m vrdu_binder fetch-splits --dest benchmarks/vrdu-binder/data
python -m vrdu_binder fetch-meta --dest benchmarks/vrdu-binder/data
python -m vrdu_binder fetch-ocr --dest benchmarks/vrdu-binder/data
```

`fetch-ocr` writes `*/main/dataset.jsonl.gz`. Decompress if you want a
plain jsonl. Do not vendor the gz or the `pdfs/` trees.

Train-only skills for SD_0 (same command, `--seed 1` or `2`):

```bash
python -m vrdu_binder write-skills --seed 0 \
  --data benchmarks/vrdu-binder/data \
  --out /tmp/binder-skills-sd0.json
```

`KeywordBinder` is a freeze adapter, not a score. `python -m vrdu_binder run`
writes published-split `*-test_predictions.json` and therefore refuses
`--binder keyword` (the default is `llm`). Pointing `--data` at real VRDU
and `--binder keyword` is an error, not a dump. Dry-run fixtures stay on
the `dry-run` command and use `SYNTH-…` dump names.

A published dump needs `--binder llm` and `INFONA_BINDER_API_KEY` or
`TOGETHER_API_KEY`. If both are missing, the command exits 2. It does not
fall back to KeywordBinder. Default chat host is
`https://api.together.xyz/v1`. Override with `INFONA_BINDER_BASE_URL`.

```bash
export INFONA_BINDER_API_KEY=...          # wins if set
# or: export TOGETHER_API_KEY=...
# optional: INFONA_LLM_BASE_URL  INFONA_BINDER_MODEL
python -m vrdu_binder run --seed 0 --corpus registration \
  --data benchmarks/vrdu-binder/data \
  --out /tmp/vrdu-dumps/registration
python -m vrdu_binder run --seed 0 --corpus adbuy \
  --data benchmarks/vrdu-binder/data \
  --out /tmp/vrdu-dumps/adbuy
```

Score with stock toolkit (clone google-research, `cd google_research`):

```bash
python -m vrdu.evaluate \
  --base_dirpath /path/to/vrdu/registration-form \
  --extraction_path /tmp/vrdu-dumps/registration \
  --eval_output_path /tmp/vrdu-dumps/registration.tsv
```

Same for `ad-buy-form`. Do not patch
[google-research/google-research#1882](https://github.com/google-research/google-research/issues/1882).

## Claims this package will not make

This is not a published VRDU task. It does not show Infona≫RAG. It does not
show 8B+Infona≈27B as a prior. The four-arm publish gate is in EXPERIMENT.md
and is not auto-claimed.

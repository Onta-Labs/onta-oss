# Four-arm claim experiment (SD_0)

Locked host: [SPEC.md](SPEC.md) v11. Constructed mix of two published VRDU
MTL tests. Not a VRDU-published task. This file is the runbook. It does
not contain Bind@type or F1 numbers.

## Models

Say **27B** (same family). Do not write "20B" or "32B". Together deprecated
Qwen3-32B. The locked large bare model is Qwen3.5-27B.

| Role | Hugging Face / Together id | URL |
| --- | --- | --- |
| Large bare | `Qwen/Qwen3.5-27B` | https://huggingface.co/Qwen/Qwen3.5-27B |
| Small / FT base | `Qwen/Qwen3.5-0.8B` | https://huggingface.co/Qwen/Qwen3.5-0.8B |

Do not download multi-GB weights from this repo. Default chat host is
Together (`https://api.together.xyz/v1`). Override with
`INFONA_BINDER_BASE_URL` / `INFONA_LLM_BASE_URL` for local vLLM.

## Auth

Prefer `INFONA_BINDER_API_KEY`. If unset, the adapters use `TOGETHER_API_KEY`.
Missing both refuses. No KeywordBinder fallback on official dumps.

## Arms (same seed SD_0, same unmodified test lists)

| id | What the model is | Inference |
| --- | --- | --- |
| `27b_bare` | Qwen3.5-27B, no LoRA | OCR only. No Infona catalog. No skill body. |
| `0.8b_bare` | Qwen3.5-0.8B, no LoRA | Same bare prompts. |
| `0.8b_vanilla_ft` | 0.8B LoRA on train-only bind+extract labels | Bare prompts. No Infona router or skills. |
| `0.8b_ft_infona` | 0.8B LoRA on the compiled train-only slice | Bind@type → exactly one skill extract. |

Arm3 vs arm4 is the inference graph, not just the weights:

- vanilla-FT learns bind ids and flat extract JSON from OCR. At test time it
  still gets the bare prompts.
- FT+Infona trains on the same train filenames, but the bind rows include
  the keys-only catalog and the extract rows include that type's one skill.
  At test time the harness binds one type and extracts with that skill only.

KeywordBinder stays gated off official dumps. Misbind still writes empty
`results[filename]`. Score with stock `python -m vrdu.evaluate --base_dirpath`.
Do not patch google-research/google-research#1882. Valid is unused for
train, model selection, and early stopping.

## Headlines and footnotes

Slide:

- Bind@type accuracy. n=2 types, so chance is 50%. Write that next to the number.
- Per-corpus official `metric-micro_f1` from the predicted-bind dumps.

Footnotes only: F1_wrong, oracle-type F1, Table 4 Mixed |D|=200 FormNet
Registration 90.51 and LayoutLMv2/FormNet Ad-buy 46.54/43.23.

## Publish gate (do not auto-claim)

Write the slide only if **both** hold on the two headlines:

1. arm4 ≈ arm1 (0.8B FT+Infona matches 27B bare)
2. arm4 ≫ arm3 (FT+Infona beats vanilla-FT by a wide margin)

If arm4 ≈ arm3, the story is fine-tune, not Infona. This repo does not
compute those inequalities and does not fill in scores.

Do not claim Infona≫RAG. Do not claim 8B+Infona≈27B. Do not call this a
published VRDU task.

## How to produce the four dumps (Together)

Needs: published split JSON + `dataset.jsonl.gz` (see README fetch) and
`TOGETHER_API_KEY` or `INFONA_BINDER_API_KEY`.

```bash
export PYTHONPATH=benchmarks/vrdu-binder/src
# INFONA_BINDER_API_KEY wins if both are set
export TOGETHER_API_KEY=...   # not committed; Cloud Agents runtime secret is fine

# 1. Train-only skills (arm 4 inference + infona LoRA rows)
python -m vrdu_binder write-skills --seed 0 \
  --data benchmarks/vrdu-binder/data \
  --out /tmp/binder-skills-sd0.json

# 2. LoRA JSONL (train filenames only). Sibling *.together.jsonl is messages-only.
python -m vrdu_binder write-lora-data --recipe vanilla --seed 0 \
  --data benchmarks/vrdu-binder/data \
  --out /tmp/lora/sd0-vanilla.jsonl
python -m vrdu_binder write-lora-data --recipe infona --seed 0 \
  --data benchmarks/vrdu-binder/data \
  --out /tmp/lora/sd0-infona.jsonl
python benchmarks/vrdu-binder/scripts/train_lora.py check \
  --jsonl /tmp/lora/sd0-vanilla.jsonl

# 3. Together LoRA (0.8B). Valid is not a file. No early stopping.
python benchmarks/vrdu-binder/scripts/together_lora.py create \
  --jsonl /tmp/lora/sd0-vanilla.together.jsonl \
  --suffix vrdu-v11-vanilla-sd0
python benchmarks/vrdu-binder/scripts/together_lora.py create \
  --jsonl /tmp/lora/sd0-infona.together.jsonl \
  --suffix vrdu-v11-infona-sd0
# poll with: together_lora.py wait --job ft-...

# 4. Dump one corpus at a time. FT arms pass the Together output model via --model.
python -m vrdu_binder experiment-run --arm 27b_bare --seed 0 \
  --corpus registration --data benchmarks/vrdu-binder/data \
  --out /tmp/arms/27b_bare/registration
python -m vrdu_binder experiment-run --arm 27b_bare --seed 0 \
  --corpus adbuy --data benchmarks/vrdu-binder/data \
  --out /tmp/arms/27b_bare/adbuy

python -m vrdu_binder experiment-run --arm 0.8b_bare --seed 0 \
  --corpus registration --data benchmarks/vrdu-binder/data \
  --out /tmp/arms/0.8b_bare/registration

python -m vrdu_binder experiment-run --arm 0.8b_vanilla_ft --seed 0 \
  --model <together-vanilla-output> \
  --corpus registration --data benchmarks/vrdu-binder/data \
  --out /tmp/arms/0.8b_vanilla_ft/registration

python -m vrdu_binder experiment-run --arm 0.8b_ft_infona --seed 0 \
  --model <together-infona-output> \
  --corpus registration --data benchmarks/vrdu-binder/data \
  --out /tmp/arms/0.8b_ft_infona/registration
```

Together LoRA inference may need a dedicated endpoint
(`https://api-inference.together.ai/v1` plus the endpoint string as `--model`).
Bare 27B / 0.8B use serverless when Together lists a per-token price.

Stock evaluate, one corpus directory per arm:

```bash
# from google_research/
python -m vrdu.evaluate \
  --base_dirpath /path/to/vrdu/registration-form \
  --extraction_path /tmp/arms/27b_bare/registration \
  --eval_output_path /tmp/arms/27b_bare/registration.tsv
```

Repeat for `ad-buy-form` and the other three arms. Bind@type accuracy is
the fraction of test filenames whose predicted type matches corpus
membership, across both dumps.

## Dry path (this PR / CI)

```bash
PYTHONPATH=benchmarks/vrdu-binder/src pytest benchmarks/vrdu-binder/tests -q
python -m vrdu_binder experiment-dry --out /tmp/binder-v11-exp-dry
python -m vrdu_binder write-lora-data --recipe vanilla --fixtures \
  --out /tmp/lora-fix-vanilla.jsonl
```

The dry client is a fixture stub. It is not a VRDU score and not a 27B run.

## Blockers this tree cannot remove

- Together (or another host) for 27B serve and 0.8B LoRA
- `dataset.jsonl.gz` (not vendored)
- `TOGETHER_API_KEY` or `INFONA_BINDER_API_KEY`
- Dedicated-endpoint deploy if Together will not serve a LoRA on serverless
- Human scoring with stock `vrdu.evaluate` after the dumps exist

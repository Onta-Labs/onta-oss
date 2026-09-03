# Four-arm claim experiment (SD_0)

Locked host: [SPEC.md](SPEC.md) v11. Constructed mix of two published VRDU
MTL tests. Not a VRDU-published task. This file is the runbook. It does
not contain Bind@type or F1 numbers.

## Models

Say **32B** (same family). Do not write "20B".

| Role | Hugging Face id | URL |
| --- | --- | --- |
| Large bare | `Qwen/Qwen3-32B` | https://huggingface.co/Qwen/Qwen3-32B |
| Small local / FT base | `Qwen/Qwen3.5-0.8B` | https://huggingface.co/Qwen/Qwen3.5-0.8B |

Do not download multi-GB weights from this repo. Serve them yourself
(vLLM, OpenRouter, or another OpenAI-compatible `base_url`).

## Arms (same seed SD_0, same unmodified test lists)

| id | What the model is | Inference |
| --- | --- | --- |
| `32b_bare` | Qwen3-32B, no LoRA | OCR only. No Infona catalog. No skill body. |
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

1. arm4 ≈ arm1 (0.8B FT+Infona matches 32B bare)
2. arm4 ≫ arm3 (FT+Infona beats vanilla-FT by a wide margin)

If arm4 ≈ arm3, the story is fine-tune, not Infona. This repo does not
compute those inequalities and does not fill in scores.

Do not claim Infona≫RAG. Do not claim 8B+Infona≈27B. Do not call this a
published VRDU task.

## How to produce the four dumps (GPU box)

Needs: published split JSON + `dataset.jsonl.gz` (see README fetch), a
served model, `INFONA_BINDER_API_KEY` (any bearer the server accepts;
local vLLM can use a dummy), and `INFONA_BINDER_BASE_URL` /
`INFONA_LLM_BASE_URL` pointing at that server.

```bash
export PYTHONPATH=benchmarks/vrdu-binder/src
export INFONA_BINDER_API_KEY=local
export INFONA_BINDER_BASE_URL=http://127.0.0.1:8000/v1   # OpenAI-compatible

# 1. Train-only skills (arm 4 inference + infona LoRA rows)
python -m vrdu_binder write-skills --seed 0 \
  --data benchmarks/vrdu-binder/data \
  --out /tmp/binder-skills-sd0.json

# 2. LoRA JSONL (train filenames only)
python -m vrdu_binder write-lora-data --recipe vanilla --seed 0 \
  --data benchmarks/vrdu-binder/data \
  --out /tmp/lora/sd0-vanilla.jsonl
python -m vrdu_binder write-lora-data --recipe infona --seed 0 \
  --data benchmarks/vrdu-binder/data \
  --out /tmp/lora/sd0-infona.jsonl
python benchmarks/vrdu-binder/scripts/train_lora.py check \
  --jsonl /tmp/lora/sd0-vanilla.jsonl
# GPU train is documented in that script. This PR does not run it.

# 3. Serve each model, then dump one corpus at a time
# Arm 1: serve Qwen/Qwen3-32B
export INFONA_BINDER_MODEL=Qwen/Qwen3-32B
python -m vrdu_binder experiment-run --arm 32b_bare --seed 0 \
  --corpus registration --data benchmarks/vrdu-binder/data \
  --out /tmp/arms/32b_bare/registration
python -m vrdu_binder experiment-run --arm 32b_bare --seed 0 \
  --corpus adbuy --data benchmarks/vrdu-binder/data \
  --out /tmp/arms/32b_bare/adbuy

# Arm 2: serve Qwen/Qwen3.5-0.8B
export INFONA_BINDER_MODEL=Qwen/Qwen3.5-0.8B
python -m vrdu_binder experiment-run --arm 0.8b_bare --seed 0 \
  --corpus registration --data benchmarks/vrdu-binder/data \
  --out /tmp/arms/0.8b_bare/registration
# … same for adbuy

# Arm 3: serve the vanilla LoRA adapter (still bare prompts)
export INFONA_BINDER_MODEL=qwen35-0.8b-vanilla-ft
python -m vrdu_binder experiment-run --arm 0.8b_vanilla_ft --seed 0 \
  --corpus registration --data benchmarks/vrdu-binder/data \
  --out /tmp/arms/0.8b_vanilla_ft/registration

# Arm 4: serve the Infona LoRA adapter (bind → one skill)
export INFONA_BINDER_MODEL=qwen35-0.8b-infona-ft
python -m vrdu_binder experiment-run --arm 0.8b_ft_infona --seed 0 \
  --corpus registration --data benchmarks/vrdu-binder/data \
  --out /tmp/arms/0.8b_ft_infona/registration
```

Stock evaluate, one corpus directory per arm:

```bash
# from google_research/
python -m vrdu.evaluate \
  --base_dirpath /path/to/vrdu/registration-form \
  --extraction_path /tmp/arms/32b_bare/registration \
  --eval_output_path /tmp/arms/32b_bare/registration.tsv
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

The dry client is a fixture stub. It is not a VRDU score and not a 32B run.

## Blockers this tree cannot remove

- GPU (32B serve, 0.8B LoRA train)
- `dataset.jsonl.gz` (not vendored)
- A served OpenAI-compatible endpoint and `INFONA_BINDER_API_KEY`
- Human scoring with stock `vrdu.evaluate` after the dumps exist

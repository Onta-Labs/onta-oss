# Mac-local 0.8B SD_0 arms

Together finished the two 0.8B LoRA jobs and the 27B bare dumps. It has
**no dedicated deploy profile** for `Qwen/Qwen3.5-0.8B` or the FT outputs, so
the three small arms have to run on an Apple Silicon Mac. This file is that
runbook. It does **not** invent 0.8B Bind@type or F1 numbers.

All download, serve, and `experiment-run` steps happen **on the Mac**. The
cloud VM cannot reach Mac `localhost`. Do not proxy, tunnel, or point the
harness at a laptop from CI.

Together host + 27B numbers: [EXPERIMENT.md](EXPERIMENT.md). Freeze:
[SPEC.md](SPEC.md).

## Already measured (Together 27B bare, keep these)

Do not re-run `27b_bare` for this table. Fill the three 0.8B rows after the
Mac dumps exist. Leave a cell blank until that dump is scored.

| Arm | Bind@type (n=600, chance 50%) | reg bind | adbuy bind | reg `metric-micro_f1` | adbuy `metric-micro_f1` |
| --- | --- | --- | --- | --- | --- |
| `27b_bare` | 302/600 = 0.5033 | 0.9967 | 0.0100 | 0.010167 | 0.0 |
| `0.8b_bare` | | | | | |
| `0.8b_vanilla_ft` | | | | | |
| `0.8b_ft_infona` | | | | | |

27B almost always emitted `type_0`. That is chance bind on a two-type mix,
not a headline win. n=2 tax: write “chance is 50%” next to Bind@type.

## Prereqs (Mac)

- Apple Silicon Mac (M-series). MLX is the primary serve path.
- Python 3.12+ (harness). A second venv for `mlx-lm` is fine.
- `TOGETHER_API_KEY` in the shell. Used only to **download** the two FT
  archives. Never print, commit, or paste the key.
- This PR branch, not `main`:

```bash
git clone https://github.com/infona-ai/infona-oss.git
cd infona-oss
git fetch origin cursor/vrdu-binder-bench-v11-15ce
git checkout cursor/vrdu-binder-bench-v11-15ce
export PYTHONPATH=benchmarks/vrdu-binder/src
```

- Published VRDU splits / meta / OCR (not in git):

```bash
python -m vrdu_binder fetch-splits --dest benchmarks/vrdu-binder/data
python -m vrdu_binder fetch-meta --dest benchmarks/vrdu-binder/data
python -m vrdu_binder fetch-ocr --dest benchmarks/vrdu-binder/data
# stock evaluate needs uncompressed jsonl next to the gz
gunzip -k benchmarks/vrdu-binder/data/registration-form/main/dataset.jsonl.gz
gunzip -k benchmarks/vrdu-binder/data/ad-buy-form/main/dataset.jsonl.gz
```

- Stock toolkit. Clone google-research, `cd` into that clone, run
  `python -m vrdu.evaluate`. **Do not patch**
  [google-research/google-research#1882](https://github.com/google-research/google-research/issues/1882).

```bash
git clone --depth 1 https://github.com/google-research/google-research.git
# later: cd google-research && python -m vrdu.evaluate --base_dirpath ...
```

- Together CLI for the weight download (`tg` is current; `together` still
  works on older installs):

```bash
pip install together
# confirm without printing secrets:
test -n "$TOGETHER_API_KEY" && echo "TOGETHER_API_KEY is set"
tg --help >/dev/null || together --help >/dev/null
```

- `zstd` to unpack the Together archives: `brew install zstd`

- MLX serve (primary):

```bash
python3 -m venv ~/vrdu-mlx
source ~/vrdu-mlx/bin/activate
pip install -U mlx-lm
```

Need a current `mlx-lm` that loads Qwen3.5. `mlx_lm.server --help` should
show `--chat-template-args` and `--adapter-path`.

## Download

Fill job ids from your own `tg fine-tuning list`; do not commit account ids.

The Together **names are not Hugging Face repos**. Do not
`huggingface-cli download <TOGETHER_USER>/Qwen3.5-0.8B-…`. Download by **fine-tune
job id**. `ml_…` is a Together registry object id, not a Hub id.

| Recipe | Job | Together output name | Registry object |
| --- | --- | --- | --- |
| vanilla | `<VANILLA_FT_JOB_ID>` | `<TOGETHER_USER>/…` | `<VANILLA_ML_ID>` |
| infona | `<INFONA_FT_JOB_ID>` | `<TOGETHER_USER>/…` | `<INFONA_ML_ID>` |

Base (Hub, public): `Qwen/Qwen3.5-0.8B`
(https://huggingface.co/Qwen/Qwen3.5-0.8B).

Prefer **merged** LoRA weights (Together default). Adapter-only is the
optional path below.

```bash
mkdir -p ~/vrdu-models
# current CLI
tg fine-tuning download <VANILLA_FT_JOB_ID> \
  --output-dir ~/vrdu-models/together-vanilla \
  --checkpoint-type merged
tg fine-tuning download <INFONA_FT_JOB_ID> \
  --output-dir ~/vrdu-models/together-infona \
  --checkpoint-type merged

# older together CLI (same jobs)
# together fine-tuning download <VANILLA_FT_JOB_ID> -o ~/vrdu-models/vanilla.tar.zst
# together fine-tuning download <INFONA_FT_JOB_ID> -o ~/vrdu-models/infona.tar.zst
```

If you only have curl (still do not `echo "$TOGETHER_API_KEY"`):

```bash
curl -L \
  -H "Authorization: Bearer ${TOGETHER_API_KEY}" \
  "https://api.together.xyz/v1/finetune/download?ft_id=<VANILLA_FT_JOB_ID>&checkpoint=merged" \
  -o ~/vrdu-models/vanilla.tar.zst
curl -L \
  -H "Authorization: Bearer ${TOGETHER_API_KEY}" \
  "https://api.together.xyz/v1/finetune/download?ft_id=<INFONA_FT_JOB_ID>&checkpoint=merged" \
  -o ~/vrdu-models/infona.tar.zst
```

Unpack (Together ships zstd tarballs):

```bash
# tg --output-dir leaves a .tar.zst in that folder
cd ~/vrdu-models/together-vanilla
zstd -d --keep *.tar.zst
tar -xf *.tar
# expect HF-style config.json + *.safetensors + tokenizer files
cd ~/vrdu-models/together-infona
zstd -d --keep *.tar.zst
tar -xf *.tar
```

Base 0.8B — either let MLX pull the Hub id on first serve, or pin a local
tree:

```bash
# optional local copy
huggingface-cli download Qwen/Qwen3.5-0.8B --local-dir ~/vrdu-models/Qwen3.5-0.8B
```

Optional MLX convert (faster restarts; not required — `mlx_lm.server`
converts a HF directory on first load):

```bash
source ~/vrdu-mlx/bin/activate
mlx_lm.convert --hf-path ~/vrdu-models/Qwen3.5-0.8B \
  --mlx-path ~/vrdu-models/mlx-0.8b-bare
# or, if you did not huggingface-cli download:
# mlx_lm.convert --hf-path Qwen/Qwen3.5-0.8B --mlx-path ~/vrdu-models/mlx-0.8b-bare
mlx_lm.convert --hf-path ~/vrdu-models/together-vanilla \
  --mlx-path ~/vrdu-models/mlx-vanilla
mlx_lm.convert --hf-path ~/vrdu-models/together-infona \
  --mlx-path ~/vrdu-models/mlx-infona
```

If `--hf-path ~/vrdu-models/together-vanilla` fails, pass the **inner**
directory that contains `config.json` (the tarball sometimes nests one
folder). Do not add `-q` unless you accept a 4-bit score that is no longer
matched to the Together 27B precision story. 0.8B bf16 fits on unified
memory.

Adapter-only (if you used `--checkpoint-type adapter` instead of merged):
Together writes PEFT `adapter_model.safetensors`. MLX wants
`adapters.safetensors` + `adapter_config.json`. Prefer merged. If you stay
on adapters, rename/convert into an MLX adapter dir and pass
`--adapter-path` on serve; do not fuse a 4-bit base unless you know the
scale.

## Serve (primary: MLX)

One OpenAI-compatible process at **`http://127.0.0.1:8000/v1`**. One model
per process. Restart between arms. Bind replies must stay type ids, so
disable Qwen3.5 thinking at the server **and** keep the harness
`chat_template_kwargs.enable_thinking=false` (mlx-lm honors both).

`mlx_lm.server` defaults to port **8080**. Pass `--port 8000`.

```bash
source ~/vrdu-mlx/bin/activate

# Terminal A — start exactly one of these, then run that arm.

# 0.8b_bare
mlx_lm.server \
  --model Qwen/Qwen3.5-0.8B \
  --host 127.0.0.1 --port 8000 \
  --max-tokens 1024 \
  --chat-template-args '{"enable_thinking": false}'

# 0.8b_vanilla_ft (merged local dir, or ~/vrdu-models/mlx-vanilla after convert)
mlx_lm.server \
  --model ~/vrdu-models/together-vanilla \
  --host 127.0.0.1 --port 8000 \
  --max-tokens 1024 \
  --chat-template-args '{"enable_thinking": false}'

# 0.8b_ft_infona
mlx_lm.server \
  --model ~/vrdu-models/together-infona \
  --host 127.0.0.1 --port 8000 \
  --max-tokens 1024 \
  --chat-template-args '{"enable_thinking": false}'
```

If you converted, swap `--model` to `~/vrdu-models/mlx-0.8b-bare` /
`mlx-vanilla` / `mlx-infona`. Adapter serve is
`--model Qwen/Qwen3.5-0.8B --adapter-path ~/vrdu-models/mlx-vanilla-adapter`.

Smoke (Mac only). mlx-lm maps the request `model` field: `default_model`
is the CLI `--model`. A **different** string is treated as another HF repo
or path and will try to load it.

```bash
curl -sS http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer local" \
  -d '{
    "model": "default_model",
    "temperature": 0,
    "max_tokens": 16,
    "chat_template_kwargs": {"enable_thinking": false},
    "messages": [
      {"role": "system", "content": "Reply with exactly one id: type_0 or type_1."},
      {"role": "user", "content": "smoke"}
    ]
  }'
```

Expect JSON `choices[0].message.content`. If this fails, fix serve before
the 300-doc dumps.

### Alternatives (not the default)

- **llama.cpp** `llama-server`: convert the merged HF dir to GGUF, then
  `llama-server -m <gguf> --host 127.0.0.1 --port 8000`. Qwen3.5 GGUF
  support is newer and LoRA merge is extra work.
- **Ollama**: only if a Qwen3.5-0.8B tag exists on your machine. LoRA
  needs a merged Modelfile. Same harness URL once `/v1/chat/completions`
  answers.

Stay on MLX unless you already have a working GGUF/Ollama stack for this
exact base.

## Run arms

Harness (repo venv, not necessarily the mlx venv):

```bash
cd /path/to/infona-oss
export PYTHONPATH=benchmarks/vrdu-binder/src
export INFONA_BINDER_API_KEY=local          # any non-empty; not a real secret
export INFONA_BINDER_BASE_URL=http://127.0.0.1:8000/v1
# include /v1. The client posts {base}/chat/completions.
# INFONA_LLM_BASE_URL also works and wins if both are set.
```

`--model` is **required** for the two FT arms (refuses rather than scoring
the base 0.8B). For mlx-lm, pass `default_model` or the **same** path you
gave `mlx_lm.server --model`. Do **not** pass the Together registry name
`<TOGETHER_USER>/Qwen3.5-0.8B-…` at a local server — mlx-lm will try to fetch it
from Hugging Face.

```bash
# 0.8b_bare — server must already be the base model
python -m vrdu_binder experiment-run --arm 0.8b_bare --seed 0 \
  --model default_model \
  --corpus registration --data benchmarks/vrdu-binder/data \
  --out /tmp/arms/0.8b_bare/registration
python -m vrdu_binder experiment-run --arm 0.8b_bare --seed 0 \
  --model default_model \
  --corpus adbuy --data benchmarks/vrdu-binder/data \
  --out /tmp/arms/0.8b_bare/adbuy

# restart mlx_lm.server with vanilla merged weights, then:
python -m vrdu_binder experiment-run --arm 0.8b_vanilla_ft --seed 0 \
  --model default_model \
  --corpus registration --data benchmarks/vrdu-binder/data \
  --out /tmp/arms/0.8b_vanilla_ft/registration
python -m vrdu_binder experiment-run --arm 0.8b_vanilla_ft --seed 0 \
  --model default_model \
  --corpus adbuy --data benchmarks/vrdu-binder/data \
  --out /tmp/arms/0.8b_vanilla_ft/adbuy

# restart with infona merged weights, then:
python -m vrdu_binder experiment-run --arm 0.8b_ft_infona --seed 0 \
  --model default_model \
  --corpus registration --data benchmarks/vrdu-binder/data \
  --out /tmp/arms/0.8b_ft_infona/registration
python -m vrdu_binder experiment-run --arm 0.8b_ft_infona --seed 0 \
  --model default_model \
  --corpus adbuy --data benchmarks/vrdu-binder/data \
  --out /tmp/arms/0.8b_ft_infona/adbuy
```

Each run prints `bind_at_type_accuracy (this corpus only)=… n=300`. Save
stdout. Gold type is corpus membership (`registration` → `type_0`,
`adbuy` → `type_1`). The dump does not store bind ids; empty
`results[filename]` is misbind **or** a failed extract after a correct
bind.

Optional `--concurrency 2` if the Mac stays cool. Default is 1.

`0.8b_ft_infona` is the Infona router (catalog + one skill). The other
two 0.8B arms stay on bare prompts. Skills are compiled from **train**
filenames at run time. Valid is unused.

## Score

From the google-research clone, stock evaluate, **no #1882 patch**.
`--base_dirpath` is the corpus directory that holds `main/dataset.jsonl`
(decompressed) and `main/meta.json`.

```bash
cd /path/to/google-research

python -m vrdu.evaluate \
  --base_dirpath /path/to/infona-oss/benchmarks/vrdu-binder/data/registration-form \
  --extraction_path /tmp/arms/0.8b_bare/registration \
  --eval_output_path /tmp/arms/0.8b_bare/registration.tsv

python -m vrdu.evaluate \
  --base_dirpath /path/to/infona-oss/benchmarks/vrdu-binder/data/ad-buy-form \
  --extraction_path /tmp/arms/0.8b_bare/adbuy \
  --eval_output_path /tmp/arms/0.8b_bare/adbuy.tsv
```

Repeat for `0.8b_vanilla_ft` and `0.8b_ft_infona`. Read
`metric-micro_f1` from each TSV (predicted-bind dump only).

Bind@type across both corpora:

```
overall = (reg_hits + adbuy_hits) / 600
```

`reg_hits` is `round(reg_accuracy * 300)` from that arm’s registration
stdout (or `n * bind_at_type_accuracy` printed there). Same for adbuy.
27B was 299 + 3 = 302.

Put the six new cells next to the 27B row above. Do not invent them.

## Publish gate (do not auto-claim)

Write the slide only if **both** hold on the two headlines (Bind@type and
per-corpus `metric-micro_f1`):

1. `0.8b_ft_infona` ≈ `27b_bare` (small FT+Infona matches 27B bare)
2. `0.8b_ft_infona` ≫ `0.8b_vanilla_ft` (beats vanilla-FT by a wide margin)

If arm4 ≈ arm3, the story is fine-tune, not Infona. This repo does not
compute those inequalities.

n=2 tax: two types, chance bind is 50%. Write that next to Bind@type. Do
not claim Infona≫RAG, 8B+Infona≈27B, or that this is a published VRDU
task.

## What this tree will not do

- Download multi-GB weights into git
- Run the 600-doc Mac jobs from the cloud VM
- Fill 0.8B scores from a stub
- Patch google-research#1882

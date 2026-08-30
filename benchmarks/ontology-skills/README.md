# SLM × Ontology Skills — Capability Compilation Benchmark

**Claim.** Domain intelligence should live in a typed knowledge environment, not in model weights. Infona attaches reusable procedural skills to ontology types and relations, then **deterministically compiles** the skills that apply to the local neighborhood (types, relations, inheritance, provenance). A 1–4B SLM executes only that compiled set. The ontology is a capability router / compiler, not a retriever.

**Success criterion (measure, do not force).** A 2B/4B local model + Infona ontology-bound skills reaching ~90–95% of a frontier model's ontology-task success at materially lower memory, latency, and cost.

This package is **INF-606 + INF-608 + INF-611 scoring + INF-607 executor** (canned + live OpenRouter client). Isolated on purpose: it does not import `infona_client`, and it does not reuse `infona_client.qc`, `infona_client.eval`, or product `infona_client.skills`. Those are a different system. Do not merge them in a drive-by.

## Why this is not WikiSkill

[WikiSkill](https://arxiv.org/abs/2608.27454) (Google Research, 27 Aug 2026) co-evolves skills with a persistent wiki. At inference it **injects the full active skill set** into the system prompt, on purpose, so skill *triggering / retrieval* cannot confound the study (§3.2.1).

That dump-all design is our **condition 3** (flat/full skill set), not the Infona condition.

| | WikiSkill inference | Infona compiler (this benchmark) |
|---|---|---|
| What the SLM sees | Every active skill | Skills compiled from the local type/relation neighborhood |
| Selection | None (full injection) | Deterministic walk: seeds → ancestors → incident relations |
| Inheritance | Not typed | `Entity → Organization → Company → Supplier` accumulates; same `skill_id` on a more specific type shadows |
| Relations | Not a compiler input | `SUPPLIES_TO` contributes temporal / validation procedures |
| Retrieval | Explicitly removed as a confound | Compilation is a function, not search. Condition 9 is RAG over the **same** skill texts — the investor-objection baseline, not Infona. |

WikiSkill's published numbers (average across their five benchmarks, three evolution seeds):

- Qwen-3.5-9B + WikiSkill **47.4** vs Qwen-3.6-27B with no skills **39.4**
- Qwen-3.5-4B **26.2 → 38.5** with WikiSkill
- Skills transfer across models; teacher-evolved skills can beat self-evolved

Those numbers are **WikiSkill's**, on WikiSkill's tasks. They are motivation, not this benchmark's results. This tree contains **no benchmark scores**. Do not invent any.

## Comparison matrix (locked order)

Do **not** run models in this slice. Do **not** start with fine-tuning. Condition 5 is in the matrix so numbering stays stable; the harness refuses it.

| # | Condition | Skill mode | Runnable now |
|---|---|---|---|
| 1 | 4B vanilla | none | yes |
| 2 | 4B + ontology context only | types/relations, no skills | yes |
| 3 | 4B + flat/full skill set | dump-all (WikiSkill-like) | yes |
| 4 | 4B + ontology-routed skills | **primary Infona** | yes |
| 5 | 4B fine-tuned + ontology-routed skills | routed | **blocked** |
| 6 | 9B vanilla | none | yes |
| 7 | 27B or frontier vanilla | none | yes |
| 8 | Strong-teacher-generated skills → 4B executor | routed, teacher provenance | yes |
| 9 | 4B + skills retrieved by embedding similarity | rag | yes |

Primary comparison: **4 vs 1, 2, 3, 7, 9**. Dump-all is not the investor objection; RAG is. Resource story: same success, smaller model / fewer tokens / less VRAM. Do not cook routed to beat RAG.

## Guardrails

- No LLM-as-judge as a primary metric. Gold is a graph delta or other canonical structured object.
- No fake numbers, no placeholder result JSON with invented F1.
- No fine-tune-first. Condition 5 stays blocked until 1–4 and 6–9 have real runs.
- No product imports. `from infona_client…` in this package is a bug.
- No revival of the QC fuzzer, public `/ask` eval, or research-harness as this benchmark.
- Synthetic names only in fixtures (Acme, Globex, Northwind).
- Grow the dataset by appending `fixtures/tasks.jsonl` rows that obey `SPEC.md`. Do not change task families, split ids, metric keys, or matrix order without a schema version bump.

## Layout

```
benchmarks/ontology-skills/
  SPEC.md                         # locked contract — read this before coding slices
  ontology_skills/                # importable package (stdlib only)
  fixtures/ontology.json          # Supplier inheritance fixture
  fixtures/tasks.jsonl            # gold GraphDeltas (~10 tasks per family)
  fixtures/canned_responses.jsonl # dry-run model text (not a scoreboard)
  schemas/run_result.v1.json      # run-log JSON Schema
  tests/                          # compiler, inheritance, scoring, executor
```

Least-invasive home: a new tree under `benchmarks/`, not `infona_client/` and not `packages/` (that directory is npm). The published `infona-client` wheel is unchanged.

## This slice vs later slices

| Slice | Issue | What to do | What not to do |
|---|---|---|---|
| Done | INF-606 | Spec, compiler, fixtures, stub harness | Run models, invent scores, FT |
| Done | INF-608 | ~10 gold GraphDeltas per family, all three splits | Change `RunResult` keys or family ids |
| Done | INF-611 scoring | Deterministic `score_task` / `score_prediction` | LLM-as-judge, fabricated leaderboards |
| Done | INF-607 executor | Prompt builders, GraphDelta parser, canned dry-run, live OpenRouter client | Invent scores, unblock FT, sweep 80 tasks from this CLI |
| Done | RAG baseline | Condition 9 `4b_rag_skills`; prompt v3 strips URI/type leaks | Cook routed to beat RAG; hashing embedder as published baseline |
| Later | INF-611 runs | Run matrix 1–4 and 6–9; plot success vs resources | Unblock condition 5 first; fabricate `hosted_cost_usd` |

## How to run

Compiler tests (no GPU, no keys):

```bash
PYTHONPATH=benchmarks/ontology-skills pytest benchmarks/ontology-skills/tests -q
```

Stub harness (writes JSONL rows with **null** metrics — this is not a result):

```bash
PYTHONPATH=benchmarks/ontology-skills python -m ontology_skills --list-conditions
PYTHONPATH=benchmarks/ontology-skills python -m ontology_skills \
  --condition 4b_ontology_routed --out /tmp/ontology-skills-stub.jsonl
```

Score a predicted GraphDelta locally (no model). Gold-vs-gold is a sanity check, not a published score:

```bash
PYTHONPATH=benchmarks/ontology-skills python -m ontology_skills score \
  --task-id et-001 --predicted pred.json
# or without a task id:
PYTHONPATH=benchmarks/ontology-skills python -m ontology_skills score \
  --family entity_resolution --gold gold.json --predicted pred.json
```

`pred.json` is a GraphDelta object (`adds`, `deletes`, `type_assertions`, `literals`, `merges`, `type_extensions`, `constraint_repairs`). Empty predicted vs non-empty gold scores precision `null`, recall `0`, f1 `0`.

Canned executor (no GPU, no HTTP). Feeds fixture text through prompt → parse → score and writes a run-log row. Resources stay null. This is a loop test, not a model result:

```bash
PYTHONPATH=benchmarks/ontology-skills python -m ontology_skills execute \
  --backend canned --condition 4b_ontology_routed --task-id et-001
```

Live executor POSTs to an OpenAI-compatible `/chat/completions` endpoint (OpenRouter by default) **only when a Bearer key is present**. Missing key → exit 2, no POST. `--task-id` is required; this CLI will not sweep the 80-task set.

```bash
export INFONA_BENCH_API_KEY=...   # or OPENAI_API_KEY / OPENROUTER_API_KEY
PYTHONPATH=benchmarks/ontology-skills python -m ontology_skills execute \
  --backend live --condition 4b_ontology_routed --task-id et-001
```

Prompt template `ontology_skills.prompt.v5` is a short GraphDelta contract: leaf type only; camelCase attrs (`registrationId`, not `vat`); mint an entity URI if the input has none; types/attrs in `type_assertions`/`literals`, not duplicated as `adds`. The hint does not name a gold type or gold URI. Typing and ingest fixtures seed a parent, not the gold leaf (`Company` for `Supplier`; `Organization` for `Customer` / `Company`; `Entity` for `Person` / `Location` / `Product`), so types-only prompts and routed type tags cannot name the answer. Scoring aligns a minted slug to the gold entity (legalName / mention / single-entity), drops adds that only restate those facts, and keeps only leaf types when a prediction also dumps ancestors (Company / Organization / Entity next to Supplier). Gold ops were not changed.

Condition 9 (`4b_rag_skills`) retrieves the same fixture skill bodies by cosine similarity to `json.dumps(task.input)`. k equals the routed compiled skill count for that task. Live embeddings POST `{base}/embeddings` as `openai/text-embedding-3-small` (override `INFONA_BENCH_EMBED_MODEL`) on the same Bearer key as chat. Missing key fails closed. A local hashing embedder is **not** the published RAG baseline (CI uses `MockEmbedder` only). Retrieval scores are logged; they are not compiler provenance.

## OpenRouter env

Default base URL: `https://openrouter.ai/api/v1`. Every live POST also sends `HTTP-Referer: https://infona.ai` and `X-Title: Infona ontology-skills bench`. Request body includes `"usage": {"include": true}` and `"reasoning": {"enabled": false}` (Qwen3 thinks by default; `reasoning.exclude` and `chat_template_kwargs.enable_thinking=false` do **not** stop reasoning-token spend). `hosted_cost_usd` is copied from OpenRouter `usage.cost` when that field is present; it is left null otherwise. Do not invent a price.

| Variable | Alias | Purpose |
|---|---|---|
| `INFONA_BENCH_API_KEY` | `OPENAI_API_KEY` or `OPENROUTER_API_KEY` | Bearer token. Required for `--backend live` to POST |
| `INFONA_BENCH_BASE_URL` | `OPENAI_BASE_URL` | Root URL (default `https://openrouter.ai/api/v1`) |
| `INFONA_BENCH_MODEL` | `OPENAI_MODEL` or `MODEL` | Optional override for a single-model run |
| `INFONA_BENCH_QUANTIZATION` | — | Recorded on the run log (`q4_k_m`, `fp16`, …) |
| `INFONA_BENCH_EMBED_MODEL` | `OPENAI_EMBED_MODEL` | Live RAG only. Default `openai/text-embedding-3-small` |

OpenRouter has **no Qwen 4B** (catalog 2026-08-29: no `*4b*` Qwen id; neither Qwen3-4B nor Qwen3.5-4B). Condition ids stay `4b_*` so the matrix order does not change. The 4B slot’s hosted stand-in is Qwen3-8B; `model.param_count` is recorded as `8B`, not `4B`.

| Conditions | Matrix bucket | Default `model` | Recorded `param_count` |
|---|---|---|---|
| 1–4, 8, 9 | `4b` | `qwen/qwen3-8b` | `8B` |
| 6 | `9b` | `qwen/qwen3.5-9b` | `9B` |
| 7 | `27b_or_frontier` | `qwen/qwen3.5-27b` | `27B` |

CI mocks HTTP. Do not treat canned or mocked-loop metrics as published scores.

Every real run must persist a row matching `schemas/run_result.v1.json` (model, quantization, prompt, context budget, tools, decoding, resources). The headline artifact is **task-success vs inference-resource** (latency, tokens, RAM/VRAM, param count / quant, hosted USD). Protocol: [SPEC.md](SPEC.md).

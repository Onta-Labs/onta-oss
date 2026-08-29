# Capability Compilation Benchmark — spec (v1.2.0)

Status: **locked for INF-606**. Later slices (INF-608 dataset, INF-607 executor, INF-611 runs) implement against this file. Changing families, split ids, matrix order, primary metrics, or run-log keys requires bumping `schema_version` and a spec diff; silent drift is a failed eval.

Companion: [README.md](README.md). Paper: WikiSkill, arXiv:2608.27454.

## 1. Research question

How much model intelligence can be replaced by structured, ontology-grounded procedural knowledge?

Infona moves domain procedure out of weights and onto typed ontology nodes. At runtime a deterministic compiler selects skills from the local neighborhood (types, relations, inheritance, provenance) and exposes **only those skills** to a 1–4B SLM. The ontology is a capability router / compiler. It is not retrieval, not RAG, and not “dump every skill in the prompt.”

Success (measure, do not force): a 2B/4B local model + ontology-bound skills reaching ~90–95% of a frontier model’s ontology-task success at materially lower memory, latency, and cost.

## 2. Isolation

This package is a new eval. It must not:

- import `infona_client` (including `qc`, `eval`, `skills`, `research`)
- import `infona.*`
- call product HTTP routes
- reuse QC scenario fuzzer gold, public `/ask` eval pins, or research-harness types

Product `infona_client.skills` is type-attached markdown for `/ask` agents. That is a different contract (layer shadowing, prompt budget). This benchmark’s compiler is the one specified here.

## 3. WikiSkill contrast (normative for condition 3 vs 4)

WikiSkill §3.2.1: *“the full content of active skills \(S_{k-1}\) is injected directly into the Inference Agent’s system prompt … eliminating skill triggering or retrieval failures as confounding variables.”*

- **Condition 3** reproduces that confound-removal: `compile_flat` emits every enabled skill.
- **Condition 4** (primary) uses `compile_routed`: neighborhood → lineage → incident relations → ordered skill set.
- Infona’s bet is that typed routing beats dump-all on ontology tasks (less prompt, fewer conflicting procedures) *and* still beats vanilla 27B/frontier on the same gold deltas.

Do not “fix” condition 3 by filtering it. The filter is the thing we are measuring.

## 4. Skill representation

A skill is procedural text attached to **exactly one** type or relation.

| Field | Rule |
|---|---|
| `skill_id` | Shadowing key. Unique per `(attached_to, skill_id)`. The same id on a more specific type **replaces** the ancestor. |
| `kind` | `type` or `relation`. Must match `attached_to`. |
| `body` | Non-empty markdown / prose the executor will read. Not code that this package runs. |
| `provenance` | Recorded (`curated`, `teacher`, `executor`, …). **Never a rank or retrieval score.** |
| `enabled` | `false` on a more specific attachment suppresses that `skill_id` from ancestors (does not fall through). |

Types have ordered `parent_ids` (single inheritance in the fixture; multiple is legal). Relations may have `parent_ids` (unused in the v1 fixture). Type ids and relation ids share one namespace and must not collide.

Fixture IRIs:

- types/relations: local ids (`Supplier`, `SUPPLIES_TO`); prefix `https://graph.infona.ai/bench/onto/` when writing graph **ops** (`adds`/`deletes` predicates only)
- `type_assertions.type_id` and `literals.attr` stay short (`Supplier`, `legalName`). Never IRIs.
- entities: `https://graph.infona.ai/bench/ent/{slug}` (author-stable; not a function of the mention)

Do not call product `entity_uri`. Fixture URIs are author-stable.

## 5. Compilation algorithm (normative)

Inputs: `Ontology`, `Neighborhood(type_ids, relation_ids, include_ancestors=True, include_incident_relations=True)`.

1. **Type lineage.** Specific-first DFS from `type_ids` in seed order. Emit the node, then walk `parent_ids` in listed order. Skip already-emitted nodes. Unknown id or cycle → `OntologyError`. If `include_ancestors` is false, lineage is the unique seed tuple.
2. **Specified relations.** Same walk over `relation_ids`.
3. **Incident relations.** If the flag is set, every relation whose `source_type` or `target_type` is in the type lineage, in **alphabetical** `relation_id` order, excluding ids already in the specified walk. Incident relations contribute **relation skills only**. They do not add the far-side type to lineage (EMPLOYS does not compile `Person` skills).
4. **Collect skills.** For each type in lineage, then each relation in the merged relation tuple: take `skills_for(id)` sorted by `(skill_id, version)`. First seen `skill_id` wins. Disabled first-seen ids go to `suppressed_skill_ids` and are not emitted.
5. **Modes.**
   - `none` / ontology-context: empty `skills`, lineage still computed (condition 1, 2, 6, 7).
   - `flat`: all enabled skills sorted by `(kind, attached_to, skill_id)` (condition 3).
   - `routed`: steps 1–4 (conditions 4, 8; condition 5 would use this if unblocked).
   - `rag` is **not** a compile mode in this algorithm. Condition 9 selects enabled skill bodies by embedding cosine against `json.dumps(task.input)`; k is the routed skill count for that task.

**Determinism.** Same `Ontology` (as typed values, not JSON key order) and same `Neighborhood` → identical `CompiledSkillSet` (skill order, lineage, fingerprint). Shuffling the ontology’s skill tuple must not change output. Seed **order** is part of the neighborhood: it may change lineage order; the **set** of skill ids for the same type set must stay equal.

`fingerprint` = SHA-256 of `mode|skill_ids|type_lineage|relation_ids|suppressed_skill_ids`.

## 6. Task families

Every task has `family` in this closed set. Gold is a `GraphDelta` (and only a `GraphDelta`). No free-text answers.

### 6.1 `entity_typing`

- **Input:** a mention / record plus neighborhood seeds.
- **Gold:** `type_assertions` of the **leaf** type (`Supplier`, not `Organization`). Optional identifying `literals`.
- **Success:** exact canonical-op match.
- **Diagnostic:** ancestor-type match is not success.

### 6.2 `property_schema_mapping`

- **Input:** column headers + sample rows.
- **Gold:** `literals` (and type assertions if the row also mints an entity) using ontology attribute names (`legalName`, `registrationId`), not the raw column string.
- **Success:** exact op match. Mapping `VAT Number` → `legalName` is a miss.

### 6.3 `entity_resolution`

- **Input:** two (or more) mentions / URIs.
- **Gold:** `merges` as `(absorbed, survivor)`. Survivor is the URI that already carries the unique key when one exists.
- **Primary metric:** pairwise undirected ER P/R/F1 **and** exact success on the merge set.
- **Pairs are undirected:** `(a,b)` equals `(b,a)` for P/R/F1. Exact success still uses canonical `MERGE absorbed survivor` strings as written in gold — authors must pick a survivor.

### 6.4 `relation_inference`

- **Input:** facts that imply an edge.
- **Gold:** `adds` triples with relation IRIs (`…/onto/SUPPLIES_TO`).
- **Success:** exact triple set. `EMPLOYS` instead of `SUPPLIES_TO` is a miss.

### 6.5 `constraint_violation_repair`

- **Input:** a graph fragment that violates a typed constraint (example: `Person` as `SUPPLIES_TO` source).
- **Gold:** `deletes` and/or `constraint_repairs` tokens. Do **not** gold a type change that launders the violation (Person ↛ Supplier).
- **Primary metrics:** exact op match; `constraint_valid` is true iff the post-repair graph satisfies the fixture constraints listed on the task (v1: “Person must not source SUPPLIES_TO”; “qty ≥ 0”; `registrationId` not on Person; `SUPPLIES_TO` source is Supplier; `EMPLOYS` target is Person). Constraints live on `input.constraints`. A predicted literal whose value is `""` clears that attr on the post-repair graph.

### 6.6 `conflict_resolution`

- **Input:** two claims for the same attr with provenance tags.
- **Gold:** winning `literals` (registry `legalName` kept; nickname → `alias`).
- **Success:** exact literals. Overwriting legalName with the nickname is a miss.

### 6.7 `ontology_extension`

- **Input:** an unseen concept string + short evidence.
- **Gold:** `type_extensions` with `type_id`, `parent_id` in the **known** ontology, `label`.
- **Success:** exact triple `(type_id, parent_id, label)`. A new root (`parent_id` empty / not in ontology) is a miss. Attachment to a wrong parent is a miss.

### 6.8 `multi_step_ingest`

- **Input:** one dirty row + any already-known URI map.
- **Gold:** the composed delta of map → resolve → normalize → mutate (types, literals, adds). Intermediate traces are not scored in v1.
- **Success:** exact composed `GraphDelta`.

## 7. Held-out splits

Closed set:

| `split` | Meaning |
|---|---|
| `known_ontology_unseen_instances` | Fixture types/relations/skills are visible. Instance records are not in any prompt memory. |
| `unseen_ontology_branches` | Gold requires a type (or branch) **not** in the executor’s ontology snapshot; the model must extend. v1 fixture: `ThirdPartyWarehouse` under `Location`. |
| `adversarial_conflicting` | Illegal edges, contradictory literals, negative qty, or other conflicts. |

A task belongs to exactly one split. Dataset growth should keep all three populated. Adversarial is required, not a stretch goal, once INF-608 adds volume; v1 already has two adversarial golds.

Train/val/test cuts for *skill evolution* (WikiSkill-style) are **out of scope for v1**. This spec scores a frozen skill library + compiler. Condition 8 may later swap in teacher-authored skill bodies without changing gold.

## 8. Scoring contract

Primary metrics live on every run row under `metrics` (null in the stub):

| Key | Type | Rule |
|---|---|---|
| `success` | bool | exact canonical-op match **after** score-time prepare (below) |
| `graph_delta_precision` | float \| null | exact-set P over prepared canonical ops |
| `graph_delta_recall` | float \| null | exact-set R |
| `graph_delta_f1` | float \| null | harmonic mean |
| `constraint_valid` | bool \| null | required for `constraint_violation_repair`; null elsewhere is fine |
| `er_precision` / `er_recall` / `er_f1` | float \| null | undirected pairwise merges; required for `entity_resolution` |

Canonical ops (see `GraphDelta.canonical_ops`):

```
ADD<TAB>s<p>o
DEL<TAB>s<p>o
TYPE<TAB>entity<TAB>type_id
LIT<TAB>entity<TAB>attr<TAB>value
MERGE<TAB>absorbed<TAB>survivor
EXTEND_TYPE<TAB>type_id<TAB>parent_id<TAB>label
REPAIR<TAB>token
```

Empty predicted vs non-empty gold: precision null, recall 0, f1 0. Both empty: P=R=F1=1.

`graph_delta_prf` / `task_success` remain exact string match on canonical ops. `score_task` / `score_prediction` first run `prepare_for_score`: drop predicted `adds` whose object restates a `type_assertions.type_id` or `literals.value` on the same subject; if several types are asserted on one entity and some are ancestors of others in the fixture ontology (plus `type_extensions` parent links), keep only the leaves; then rewrite predicted entity URIs onto gold URIs. Alignment order: `legalName` value (case-insensitive); else input `mention` / `record.name` matching a gold `legalName` with one leftover predicted entity; else one predicted entity and one gold entity. Ancestor dumps (Company + Organization + Entity next to Supplier) must not add false-positive TYPE ops. A minted slug that is not the author-stable gold URI can still score type_id / attr / value. Do **not** put the gold URI in the prompt or in `input` to make strings match. `graph_delta_prf(raw_predicted, gold)` without prepare is still a miss on a different slug.

**Forbidden as primary:** LLM-as-judge, BLEU/ROUGE on prose, “helpfulness”, treating an ancestor-only type as success. A later diagnostic judge, if added, must use a different key and must not populate `success`.

Family-level reported score = mean `success` on that family’s test tasks. Overall = mean `success` across all families (macro-family, then micro-task — **v1 reports both**; headline is **macro-family success** so a huge mapping split cannot bury ER).

## 9. Headline artifact

For each runnable condition, plot **macro-family task-success (y)** against inference resources (x), one pane per resource:

- wall latency (ms)
- prompt + completion tokens
- RAM and VRAM (MB)
- model size × quantization (categorical x, or param-count)
- hosted USD / 1k tasks

The interesting overlay is condition 4 (4B routed) vs condition 9 (4B RAG) vs condition 7 (27B/frontier vanilla) vs condition 3 (4B flat). Do not produce this plot until real runs exist. A stub JSONL with null metrics is not a result.

## 10. Comparison matrix

Order is locked in `ontology_skills.conditions.CONDITION_MATRIX`. Ids:

1. `4b_vanilla`
2. `4b_ontology_context`
3. `4b_flat_skills`
4. `4b_ontology_routed` ← primary Infona
5. `4b_ft_ontology_routed` ← `runnable=False` until 1–4 and 6–9 have been run
6. `9b_vanilla`
7. `27b_or_frontier_vanilla`
8. `teacher_skills_4b`
9. `4b_rag_skills` ← cosine top-k over the same enabled fixture skill bodies; k = routed compiled skill count for that task; no ontology walk

Condition 2 injects lineage / relation names as schema text and **no** skill bodies. Condition 8 uses `compile_routed` on a skill library whose `provenance` is `teacher`; gold and neighborhoods stay the same. The v1 fixture has only `curated` bodies — INF-608/611 may add a parallel `fixtures/skills_teacher.json` **without** changing this schema. Condition 9 is retrieval, not compilation. Do not cook routed to beat RAG. Retrieval scores are logged and must not be written as skill provenance.

Harness `compile_for_condition` raises if condition 5 is requested.

## 11. Run log contract

Every executed task writes one JSON object (JSONL). Schema file: `schemas/run_result.v1.json`. `schema_version` is `1.2.0` (added condition 9 / `rag`, `retrieval` block; existing keys unchanged).

Required top-level keys: `schema_version`, `run_id`, `created_at`, `status`, `condition`, `task_id`, `task_family`, `split`, `model`, `prompt`, `context_budget`, `tools`, `decoding`, `resources`, `compiler`, `metrics`, `parse`, `predicted`, `retrieval`, `notes`.

| Block | Must record |
|---|---|
| `model` | `name`, `quantization`, `param_count`, `backend` |
| `prompt` | `template_id`, `sha256` of the exact prompt bytes, `skill_injection` ∈ {`none`, `ontology_context`, `flat`, `routed`, `rag`} |
| `context_budget` | `max_context_tokens`, `max_output_tokens`, `compiled_skill_chars` |
| `tools` | list of tool names exposed to the executor (empty if none) |
| `decoding` | `temperature`, `top_p`, `top_k`, `seed`, `max_new_tokens` |
| `resources` | `latency_ms`, `prompt_tokens`, `completion_tokens`, `ram_mb`, `vram_mb`, `hosted_cost_usd` |
| `compiler` | `mode`, `skill_ids`, `type_lineage`, `relation_ids`, `fingerprint` |
| `retrieval` | `embedder_id`, `k`, `hits` (`skill_id` + cosine). Null fields when the condition is not RAG |
| `parse` | `ok` (bool or null on stubs), `error` (string or null) |
| `predicted` | GraphDelta object actually scored (`adds`, `deletes`, …). Empty on parse failure. |

Stub runs set `status=stub`, all `metrics` null, resource fields null, `model.name=unspecified`. That is not a published score.

`status` for real runs: `ok` | `error` | `blocked`. Errors still write a row.

## 12. Dataset format

- Ontology + skills: `fixtures/ontology.json`
- Tasks: `fixtures/tasks.jsonl`, one object per line
- Required task keys: `task_id`, `family`, `split`, `neighborhood`, `input`, `gold`
- `neighborhood` keys: `type_ids`, `relation_ids`, `include_ancestors`, `include_incident_relations`
- `gold` is a GraphDelta object (same keys as `GraphDelta.to_dict`)
- Do **not** put gold `entity_uri` / `mint_as`, `type_id`, or literals into `input`. URI minting is in-task.

`task_id` unique. Families must stay in the closed set. INF-608 appends lines; it does not introduce a second file format.

v1 ships eight families on the procurement fixture (`Entity → Organization → Company → Supplier`, `SUPPLIES_TO`). **INF-608** fills `fixtures/tasks.jsonl` to **10 tasks per family** (80 total) across all three splits. Append lines; do not introduce a second file format. `scripts/emit_tasks.py` regenerates the JSONL; the JSONL is what the loader reads.

## 13. Executor (INF-607)

The executor may be any 1–4B/9B/27B local or hosted chat model behind an OpenAI-compatible `/v1/chat/completions` endpoint. Default host is OpenRouter (`https://openrouter.ai/api/v1`). `--backend live` POSTs only when `INFONA_BENCH_API_KEY` / `OPENAI_API_KEY` / `OPENROUTER_API_KEY` is set; missing key exits 2 without posting. Live also requires `--task-id` so this CLI cannot sweep the dataset.

It receives:

- the task `input` (never the gold)
- for condition 2: serialized lineage + relation signatures, no skill bodies
- for condition 3: every skill body (flat order)
- for condition 4/8: routed skill bodies in compiler order
- for condition 9: top-k retrieved skill bodies (same corpus, k = routed count)
- for conditions 1/6/7: neither ontology nor skills

It must return a `GraphDelta` JSON object. Parsing failure → empty predicted delta, `status=error`, `success=false`. Do not repair the parse with a second model.

Prompt template id: `ontology_skills.prompt.v5`. Compact GraphDelta contract (do not name a gold type or gold URI in the hint):

- emit only the JSON object: no prose, no code fences, omit empty keys, no extra ops
- `type_id`: leaf type only, short local id, never an IRI
- `attr`: camelCase, never an IRI
- `predicate`: `https://graph.infona.ai/bench/onto/{RELATION_ID}`
- `entity`: `https://graph.infona.ai/bench/ent/{slug}`
- mint an entity URI if the input has none
- types and attrs go in `type_assertions` / `literals`; do not repeat them as `adds`
- `registrationId`, not `vat`

The run log records `prompt.sha256` of the exact prompt bytes.

Dry-run: `python -m ontology_skills execute --backend canned` reads `fixtures/canned_responses.jsonl` (`task_id`, `text`). Metrics on canned rows are loop tests, not published model scores. `resources.*` stay null on canned rows.

Live env: Bearer key as above; `INFONA_BENCH_BASE_URL` (default OpenRouter; alias `OPENAI_BASE_URL`); `INFONA_BENCH_MODEL` optional override (aliases `OPENAI_MODEL` / `MODEL`). OpenRouter catalog 2026-08-29 has no Qwen 4B; the `4b_*` slot (conditions 1–4, 8, 9) defaults to `qwen/qwen3-8b` with recorded `param_count` `8B`. Condition 6: `qwen/qwen3.5-9b`. Condition 7: `qwen/qwen3.5-27b`. Live POST body includes `"reasoning": {"enabled": false}` so thinking cannot consume `max_new_tokens`, `"response_format": {"type": "json_object"}` (listed on `qwen/qwen3-8b`), and `max_tokens` from `DecodingSpec.max_new_tokens` (default 2048; 512 truncated routed et-001). HTTP errors include status and response body. OpenRouter headers: `HTTP-Referer https://infona.ai`, `X-Title Infona ontology-skills bench`. Tokens and `hosted_cost_usd` are copied from the response `usage` object when present (`usage.cost` for USD). Do not fabricate cost. Latency is the client wall-clock of that POST.

Live RAG embeddings: `POST {base}/embeddings` with `INFONA_BENCH_EMBED_MODEL` (default `openai/text-embedding-3-small`) on the same Bearer key. No key → fail closed. A hashing embedder is not the published RAG baseline.

Scoring (INF-611) is deterministic and does not call a model.

## 14. Out of scope

- Fine-tuning (condition 5)
- Skill evolution loops (WikiSkill wiki maintainer / proposer)
- Product ingest / Neo4j writes
- LLM-as-judge
- Invented leaderboards

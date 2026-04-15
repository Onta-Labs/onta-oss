# Pre-Eval Checklist — MUST run before any baseline / cross-LLM dispatch

**Why this exists:** Multiple times during the v2.0 build we ran a full eval, only to discover afterwards that the pipeline was missing a production feature or had a methodology bug. Each restart cost ~$50-200 and 1-3 hours. This checklist exists to catch those issues BEFORE the eval runs.

**Rule**: NO eval dispatch is allowed until every item in §1-3 is checked. §4-5 are recommended but not blocking.

**Owner**: whichever agent is about to run an eval. The agent must paste this checklist into its task report with each item marked ✓ or ✗ before kicking off the run.

---

## §1 — Pipeline parity (the most common bug class)

The eval path for ALL KGs must mirror what production users actually experience. Asymmetry between v1 and multi-table KGs has bitten us 3 separate times.

- [ ] **Routing split is correct**
  - v1 CSV-ingested KGs → production `/ask` endpoint
  - Multi-table-ingested KGs → `ask_client_side` (or production `/ask` if backend has the ontology fallback deployed)
  - Detection logic uses ASK SPARQL on `https://omnix.dev/holdout-v2/{kg}/type/*` (commit `55f811d`)

- [ ] **Both paths use the example bank with anti-cheat**
  - v1 path: production `/ask` accepts `exclude_questions` body parameter ✓
  - Multi-table path: `ask_client_side` retrieves from example bank with same exclude filter
  - **Symmetric**: if production /ask uses examples, the client-side path MUST use them too. Anything else under-represents Cograph's real accuracy.

- [ ] **Both paths have retry-with-error-feedback**
  - v1 path: production `/ask` retry loop in `omnix-oss/omnix/nlp/pipeline.py` (3 attempts)
  - Multi-table path: `ask_client_side` retry loop (commit `d6759e2`)
  - Retry feedback uses ONLY error messages, HTTP status, prior SPARQL — never gold

- [ ] **Both paths apply identical SPARQL repair**
  - FROM-clause regex repair (Pattern A, commit `a2ffac6`)
  - Any other syntax repairs production /ask does (read `omnix-oss/omnix/nlp/pipeline.py::_fix_*` to verify)

- [ ] **Both paths use the same ontology source**
  - v1 path: ontology metadata table OR the runtime fallback
  - Multi-table path: `synthesize_ontology_from_triples` (commit `b98efe7`)
  - Verified equivalent representation (top types, top relationships, cardinalities)

---

## §2 — Anti-cheat (paper credibility hard gate)

Any single failure here invalidates the entire run.

- [ ] **`exclude_questions` is the FULL v2.0 holdout question set** (302 questions as of 2026-04-15)
  - Built by globbing `eval_holdout_v2/gold/*.json` and unioning all `question` fields
  - Passed to BOTH the bank retriever AND the eval runner

- [ ] **Same-KG similarity gate is active** (≥0.75 → blocked, per spec §6.1)
  - Verified in `omnix-oss/omnix/nlp/example_bank.py` constant `SAME_DATASET_MAX_SIM = 0.75`

- [ ] **Cross-KG examples are gated at ≥0.90 similarity** (spec §4.4)
  - Verified in same file: `ANTI_CHEAT_THRESHOLD = 0.90`

- [ ] **`HOLDOUT_V2_KGS` guard is present in bank ingestion**
  - `omnix-oss/omnix/nlp/example_bank.py::populate_from_eval_reports` skips KGs in the holdout list
  - No holdout-v2 KG question or SPARQL has ever entered the bank

- [ ] **No gold field is read inside the retry loop or the prompt builder**
  - `expected_answer`, `gold_sparql`, `full_expected_items` must NEVER be passed to the LLM
  - Grep the eval runner for these strings; verify they only appear in scoring code, not generation code

- [ ] **Judge is deterministic, not LLM-as-judge**
  - T1: `fast_judge` (string match)
  - T2-T4: `full_set_judge` (set membership)
  - No model invocation in the judge path

---

## §3 — Apples-to-apples (cross-system + cross-LLM comparability)

Mismatches here corrupt every comparison table in the paper.

- [ ] **All systems in Table 1 (Cograph vs baselines) use the EXACT SAME LLM model ID**
  - Currently: `google/gemini-3-flash-preview`
  - Verified by grepping each script for the model name
  - Includes Cograph primary, Text-to-SQL, RAG, Pandas-agent

- [ ] **All runs in Table 1 use the SAME gold file directory** (`eval_holdout_v2/gold/`)

- [ ] **All runs in Table 1 use the SAME judge code** (commit hash recorded in each run's report)

- [ ] **All runs in Table 1 use the SAME seed configuration** (`--seeds 1,2,3`)

- [ ] **All runs in Table 1 use the SAME concurrency** (`--concurrency 5`)

- [ ] **Cross-LLM ablation models are all the same architecture class**
  - Either all reasoning OR all non-reasoning (mixing reasoning + direct-output models is comparing apples to oranges)
  - Currently: 3 non-reasoning models (Opus 4.6 default mode, Gemini 3 Flash Preview, Gemini 2.5 Flash Lite)

- [ ] **Cross-LLM mid-tier model is the SAME as Cograph primary** (so Table 1's Cograph column = Table 2's mid column)

- [ ] **All cross-LLM runs use the SAME gold + judge + seeds + concurrency** as Cograph primary

---

## §4 — Sanity checks (recommended; catches stale state)

- [ ] **`gh run list --limit 3 --branch main`** — no in-progress deploys (CLAUDE.md rule)

- [ ] **Neptune `/health`** returns `ok` (not `degraded`)

- [ ] **`scripts/freeze_readiness.py`** — current state matches expectations
  - KG count matches manifest
  - All ingested KGs pass adjacency
  - No KG flagged `floor_failed`

- [ ] **Gold files glob count == manifest ingested KG count** (catches drift)

- [ ] **Each gold file has populated `full_expected_items` and `full_result_count`** (catches the q2forge bug)

- [ ] **`closest_training_kg_table.md` shows ALL ingested KGs as PASS** (no PENDING for ingested KGs)

---

## §5 — Pre-flight smoke test (recommended; cheap)

Before running the FULL eval, run a 3-question smoke test on ONE rich KG to verify the whole pipe is alive:

```bash
set -a && source /Users/moeen/Desktop/omnix/.env && set +a
/Users/moeen/Desktop/omnix/.venv/bin/python scripts/run_holdout_v2_baseline.py \
  --gold-dir eval_holdout_v2/gold \
  --out /tmp/preflight_smoke/ \
  --seeds 1 \
  --concurrency 1 \
  --model <whatever-model-the-real-run-will-use> \
  --limit-kg holdout-v2-ncua-credit-union-call-reports \
  --limit-questions 3
```

(NCUA is the canonical "rich, well-behaved" KG. Should score >80% on a 3-question smoke. If it doesn't, the pipe is broken — DO NOT proceed to the full run.)

---

## §6 — Things we have CAUGHT after the fact (and never want to repeat)

Each entry is something we missed before and are now permanently checking. **Add to this list whenever a new bug is found.**

| # | What | Caught when | Now checked in |
|---|---|---|---|
| 1 | Adjacency check stored full URIs not local names → all multi-table KGs trivially PASS | Wave 6 verification | §1 routing, §2 anti-cheat |
| 2 | q2forge ontology metadata empty for multi-table KGs → 0 questions generated | Wave 7 gold-gen | §1 ontology source |
| 3 | /ask backend serves stale ontology to LLM → 5/7 KGs scored 0% | Wave 9 baseline | §1 routing |
| 4 | Q2Forge T3/T4 prompt asked "any" not "join with both subjects typed" → 0 T3/T4 | Wave 7 gold-gen | (q2forge already patched, monitor) |
| 5 | Routing classified everything as multi-table → v1 KGs regressed | Wave 9b → 9c | §1 routing detection |
| 6 | Pattern A: FROM clause placed inside WHERE → HTTP 500 on 4 questions | Failure analysis | §1 SPARQL repair |
| 7 | Pattern B: judge couldn't strip varname prefixes → 5 false-negatives | Failure analysis | §1 SPARQL repair (judge side) |
| 8 | LLM hallucinated filter values → T4 shortfall on NCUA | Wave 7 gold-gen | (data-realism probe in q2forge) |
| 9 | PatentsView ontology synthesis timed out at 60s → adjacency ERROR | Final consolidation | (timeout raised to 300s) |
| 10 | Gemini version mismatch between Table 1 (2.5) and Table 2 (3-preview) | Wave 11 baseline re-run | §3 apples-to-apples |
| 11 | **Example bank not used by client-side path → 24/26 KGs run without retrieval augmentation** | **2026-04-15 (this checklist)** | **§1 pipeline parity** |
| 12 | GPT-5-mini reasoning latency made cross-LLM hang | Wave 11 cross-LLM | §3 apples-to-apples (reasoning class) |
| 13 | Race condition: same baseline runner dispatched twice → results overwritten | Wave 11 baselines | (orchestration discipline; see §7) |
| 14 | Sub-agents using `run_in_background=true` for the eval runner → script died with parent | Wave 11 baselines | §7 orchestration |

---

## §7 — Orchestration discipline (operator rules, not pipeline checks)

- [ ] **Never re-dispatch a baseline runner without verifying the previous run is dead** — check `ps aux | grep run_holdout_v2_baseline`
- [ ] **Never use `run_in_background=true` for the eval runner inside an agent** — let the agent block synchronously, otherwise the runner dies with the agent
- [ ] **Always archive prior runs to `_pre_<timestamp>_run/` before overwriting** — so we can compare before/after
- [ ] **Never run two cross-LLM agents in parallel against the same `cross_llm_runs/` directory** — they will collide
- [ ] **After dispatching an eval, do NOT dispatch a fix that touches the same code paths until the eval finishes**

---

**Last updated**: 2026-04-15
**Next reviewer**: any operator before the next eval dispatch

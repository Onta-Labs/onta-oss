# Holdout v2.0 — Failure Patterns to Tackle After Ingestion

**Captured:** 2026-04-14 15:16 PDT
**Source baseline:** Gemini 2.5 Flash, 147 questions × 14 KGs, seed-level 80.7% [76.8, 84.1], majority-vote 81.6% [74.6, 87.1]
**Raw data:** `eval_holdout_v2/baseline_run/holdout_eval_multirun_3x.json`

## Headline failure distribution

- **27 questions scored 0/3** (18.4% of the benchmark)
- **2 questions scored 1/3** (seed noise on the margin)
- **61 questions scored 3/3** (solid)
- The remaining 57 questions are 2/3 (majority-vote correct but one seed flipped)

## Pattern 1 — Data-realism drift on free-text filter values (est. 6-8 failures)

**Affected KGs:** doj-enforcement-actions (58.3%), samhsa-n-ssats (81.8%), patentsview (80%)

**Symptom:** Gold SPARQL uses `FILTER(?val = "Fraud")` but the ingested data has `"FRAUD"` or `"fraud - wire"` or `"Conspiracy to Commit Fraud"`. The LLM and the verifier both generate plausible filter values, but the real data doesn't contain them verbatim.

**Root cause:** `q2forge_v2.probe_realistic_attribute_values` only samples up to 30 queries per KG (5 attributes × 6 entity types). For KGs with many string enums (DOJ `ActionType`, `Charge`, `DefendantType`), the sampler skips high-coverage attributes.

**Fix:** Extend the probe to dynamically identify which attributes are enums (cardinality < 200) and sample ALL of them, not just the top 5. Budget: 20 min + re-run affected KGs.

## Pattern 2 — Multi-table URI fallback path regression (est. 4-6 failures)

**Affected KGs:** fema-disaster-declarations-multitable (43.3%), fdic-call-reports (37.5% — compounded by partial ingest)

**Symptom:** The client-side routing path (`run_holdout_v2_baseline.py::ask_client_side`) skips the production `/ask` pipeline's `_fix_attribute_uris` and `_fix_common_sparql_issues` kludges. Those kludges silently repair small URI mismatches on v1 KGs; they never got ported to the multi-table path.

**Root cause:** The v1 `/ask` handler has a multi-attempt retry loop with error feedback. The client-side path is single-shot. If the LLM emits a close-but-wrong predicate URI, v1 retries with the error; multi-table doesn't.

**Fix:** Two options.
- **(a) Backend**: land the /ask ontology fallback inside `omnix-oss/omnix/nlp/pipeline.py::_fetch_ontology`. Requires a deploy (blocked until 5pm by daytime commit hook).
- **(b) Client**: port `_fix_attribute_uris` + retry-with-error-feedback into `ask_client_side`. No deploy needed. ~60 min work.

**Impact:** Probably worth +5-8pp on the headline (fema-multi and FDIC are both ~40% right now; target is ~80%).

## Pattern 3 — FDIC partial-ingest starvation (est. 6-8 failures)

**Affected KGs:** fdic-call-reports (37.5%)

**Symptom:** FDIC has 1,368,743 prepared triples but only ~24k landed in Neptune (1.8%) because of the `/update` rate-limit wedge + the batch-size-1500 connection hang (now fixed — 5000/10000 works). Gold SPARQL references entities that aren't yet in the graph.

**Root cause:** Rate limit contention during wave 6. The background driver (PID was 1489) completed in the morning per the watcher report (99.93% of triples), BUT the current Neptune query returns only 24k — suggesting the graph was dropped or the earlier completion report was measuring something else. Verify first.

**Fix:** Re-run `scripts/ingest_multitable.py` on the FDIC spec with `--batch-size 10000`. Idempotent. ~8 min at new batch size.

**Impact:** +5pp on the headline if FDIC goes to 80%+.

## Pattern 4 — Flat schema tier inflation (est. 4-5 failures)

**Affected KGs:** fema-disaster-declarations flat (55.6%)

**Symptom:** q2forge_v2 generated 3 T3 + 2 T4 questions against a fundamentally flat (1 entity type) CSV schema. Gold SPARQL is syntactically multi-hop but semantically collapses — the "join" is a self-join on the same entity type.

**Root cause:** The data-realism probe AND the T3/T4 prompt fix are both working correctly — but neither checks whether a KG's ontology can *structurally* support multi-hop questions. The flat FEMA KG has only `Disaster` as an entity type, so all joins are `Disaster → Disaster` which is degenerate.

**Fix:** Add a pre-check in `q2forge_v2.generate_for_kg`: if the synthesized ontology has fewer than 3 distinct entity types, **cap the target tiers at T1+T2 only** and log a per-KG `tier_coverage_cap` warning. Document in spec §4.2 as an allowed shortfall path.

**Impact:** Removes ~5 structurally-broken questions from the gold; doesn't raise the number but removes noise.

**Alternative:** Drop fema-flat from v2.0 (already flagged `replace_at_freeze` earlier). Redundant with fema-multitable anyway.

## Pattern 5 — T4 multi-hop prompt ceiling (est. 3-4 failures)

**Affected KGs:** Across all KGs, T4 is 62.1% vs T3 74.1%. A 12pp gap.

**Symptom:** LLM generates T4 SPARQL that's structurally T4 (3 entity types + 2 joins) but frequently picks the wrong *direction* of traversal or misses an intermediate join condition. Example from CMS:
```sparql
SELECT ?state (AVG(?citations) AS ?avg) WHERE {
  ?nh a :NursingHome ; :has_citation ?c ; :state ?state .
  ?c a :Citation ; :severity "G" .
}
GROUP BY ?state
```
This is T3-shaped (2 types). A true T4 would need a third type (e.g., `Ownership`) and a second connecting relationship.

**Root cause:** The T4 prompt template offers 3 T4 paths (3+ entities, nested subquery, property path) but the LLM defaults to the easiest one and often under-delivers on join count.

**Fix:** Add a post-generation classifier check specifically for T4 that REJECTS candidates where `n_types < 3 AND n_joins < 2 AND no_property_path AND no_nested_select`. The current classifier is lenient (accepts any of the 3 paths); a stricter T4 check forces the LLM to regenerate.

**Impact:** +2-3pp on T4 accuracy globally.

## Pattern 6 — Seed noise on 2/3 questions (est. 0 failures, but info)

**Affected KGs:** All KGs, variable

**Symptom:** ~57 questions scored 2/3 — one seed flipped despite T=0. This is the known Gemini Flash non-determinism (±5-10% per run) from memory note `project_llm_nondeterminism`.

**Fix:** None required. Majority voting already handles it. Documented in spec §5.6.

## Tackle-after-ingestion priority order

| Priority | Pattern | Action | Est. impact | Blocked? |
|---|---|---|---|---|
| 1 | #3 FDIC re-ingest | Re-run ingest at batch-size 10000 | +5pp | No |
| 2 | #1 Data-realism extension | Extend probe to cover all enums | +3-4pp | No |
| 3 | #5 T4 strict classifier | Stricter T4 classifier check | +2-3pp | No |
| 4 | #4 Flat schema cap | Add ontology pre-check to q2forge_v2 | ~0pp, -noise | No |
| 5 | #2 Multi-table /ask fallback | Backend deploy OR client-side port | +5-8pp | Deploy |

**Projected headline after all 5 fixes:** ~90% majority-vote (up from 81.6%).

## What this document is NOT

- Not an exhaustive per-question error analysis. The raw failures are in `baseline_run/holdout_eval_multirun_3x.json`.
- Not a replacement for human review. 20% human spot-check is still required per spec §4.5.
- Not the final number. This is the tackle-after-ingestion plan. Ingest first, then fix, then re-eval.

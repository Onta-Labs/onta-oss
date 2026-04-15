# eval_holdout_v2

Status: **DRAFT (pre-freeze)** — version string `2.0-DRAFT`.

This directory holds the Omnix / Cograph Holdout Benchmark v2.0 artifacts. The
definitive specification is `docs/HOLDOUT_V2_SPEC.md`; when the spec and any file
here disagree, the spec wins.

## Directory layout

| Path | Purpose |
|---|---|
| `HOLDOUT_V2_MANIFEST.json` | Canonical manifest (schema per SPEC §7.3). Currently a skeleton; `version` is `2.0-DRAFT` until freeze. |
| `gold/` | Per-KG public-dev gold files (question, SPARQL, full_expected_items). Populated during Phases 1–3. |
| `gold_private/` | Per-KG private-held-out gold files. Populated at Phase 3 freeze via stratified sampling (SPEC §13). Never published. |
| `ingested_snapshots/` | Neptune triple counts per KG at freeze time (`snapshot.json`). |
| `baseline_run/` | Freeze-time 3x eval run output and report. |
| `human_review.jsonl` | Reviewer audit log (SPEC §4.5). One line per reviewed question. |
| `adjacency_report.json` | Per-KG schema-adjacency verdicts (SPEC §3). Filled by `scripts/check_schema_adjacency.py`. |
| `INCIDENTS.md` | Contamination and bug-fix log (SPEC §6.3). |

## Current status

All artifacts in this directory are **DRAFT**. The manifest enumerates the 28
candidate KGs (7 per domain) with placeholder fields; every KG has
`schema_adjacency.verdict: "PENDING"`, `partition: null`, and `n_questions: 0`.
Nothing here is frozen, nothing here is reportable in public artifacts.

## Freeze checklist (SPEC §7.1)

v2.0 is frozen only when **all** of the following are true:

- [ ] All ~28 KGs listed in the manifest are ingested into the `demo-tenant` Neptune graph and queryable.
- [ ] Every KG has >=10 questions (or >=6 for diversity) in the gold file.
- [ ] Every gold SPARQL passes execution verification (no errors, non-empty bindings) on a single atomic Neptune snapshot.
- [ ] Tier distribution is 25% +- 5% per tier per domain, or deviations are documented in the manifest with justification.
- [ ] Schema adjacency check (SPEC §3) passes for every KG.
- [ ] 20% human review (SPEC §4.5) is complete per domain.
- [ ] A baseline eval has been run end-to-end and the report is archived.
- [ ] Stratified public-dev / private-held-out partition assigned per KG (SPEC §13).
- [ ] `manifest_sha256` computed; `version` bumped from `2.0-DRAFT` to `2.0`; `frozen_at` / `frozen_by` filled.

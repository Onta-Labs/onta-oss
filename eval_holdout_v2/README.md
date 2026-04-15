# Holdout v2.0 -- Public Eval Artifacts

Public subset of the Cograph holdout v2 evaluation, published for
reproducibility. These artifacts are referenced throughout the paper.

## Contents

| Directory / file | Description |
|---|---|
| `HOLDOUT_V2_MANIFEST.json` | Frozen manifest (v2.0, SHA `f8f4a7fc...c880dd`) |
| `training_kgs_v9.json` | 36 training KGs with SHA pins |
| `gold/` | 302 public gold questions + SPARQL |
| `baselines/` | pandas_agent, text_to_sql, rag, rag_rerank reports + per-KG CSVs |
| `cross_llm_runs/` | Claude Opus 4.6, Gemini 2.5 Flash Lite, Gemini 3 Flash reports |
| `multitable_specs/` | Multi-table KG schema specifications |
| `closest_training_kg_table.*` | Jaccard adjacency between holdout and training KGs |
| `cross_llm_comparison.*` | Cross-LLM accuracy comparison |
| `PRE_EVAL_CHECKLIST.md` | Reproducibility checklist |
| `FAILURE_PATTERNS.md` | Common failure pattern taxonomy |

## Deliberately held back

- **`gold_private/`** -- held-back gold questions for anti-contamination.
  Not published to prevent benchmark leakage.
- **`baseline_run/`** -- primary Cograph run artifacts (the cross_llm_runs
  already include the primary Opus result).
- Two pandas_agent files >10 MB containing raw LLM outputs were excluded
  to keep the OSS repo lean.

## License

See the repository root [LICENSE](../LICENSE).

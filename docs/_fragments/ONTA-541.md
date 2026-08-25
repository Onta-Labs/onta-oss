**Historical pin (2026-08-19, n=8):** **6 / 8** (75%) on
[`examples/trials.csv`](examples/trials.csv) (16 synthetic oncology rows).
Query model `openai/gpt-oss-120b`, judge `deepseek/deepseek-v4-pro-0813`
(reasoning, effort high). `/ask` is always-LLM Cypher — these scores are
not golden-string shortcuts. Same eight questions as the prior 2/8 pin.
Two misses stay visible. n=8 is too small to sell as the public score.
Going forward: n≥32, generated not hand-picked, same
`run_public_eval.py` (default `--questions 32`). Reproduce:

```bash
infona ingest examples/trials.csv --kg eval-public-trials -y
python scripts/run_public_eval.py --dataset examples/trials.csv --kg eval-public-trials --questions 8
python scripts/run_public_eval.py --dataset examples/trials.csv --kg eval-public-trials --questions 32
```

| Tier | Skill | Passed | Asked | Accuracy | Visible misses |
|------|-------|--------|-------|----------|----------------|
| 1 | Count/Lookup | 2 | 2 | 100% | |
| 2 | Filter | 1 | 2 | 50% | Start-year ≥ 2019 returned rows, not a count |
| 3 | Join | 2 | 2 | 100% | |
| 4 | Multi-hop | 1 | 2 | 50% | NSCLC Phase-3 average used a missing `average_enrollment` column |
| **All** | | **6** | **8** | **75%** | |

Full write-up: [docs/EVAL.md](docs/EVAL.md). Backing JSON:
[docs/eval/public_results.json](docs/eval/public_results.json).

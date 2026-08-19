Published pin: **5 / 8** (62%) on
[`examples/trials.csv`](examples/trials.csv) (16 synthetic oncology rows).
Query model `openai/gpt-oss-120b`, judge `deepseek/deepseek-v3.2`. `/ask`
is always-LLM Cypher — these scores are not golden-string shortcuts. Same
eight questions as the prior 2/8 pin. Three misses stay visible. Reproduce:

```bash
infona ingest examples/trials.csv --kg eval-public-trials -y
python scripts/run_public_eval.py --dataset examples/trials.csv --kg eval-public-trials --questions 8
```

| Tier | Skill | Passed | Asked | Accuracy | Visible misses |
|------|-------|--------|-------|----------|----------------|
| 1 | Count/Lookup | 2 | 2 | 100% | |
| 2 | Filter | 1 | 2 | 50% | Start-year ≥ 2019 returned rows, not a count |
| 3 | Join | 1 | 2 | 50% | AstraZeneca drugs included extra brand-suffixed names |
| 4 | Multi-hop | 1 | 2 | 50% | NSCLC Phase-3 average used a missing `average_enrollment` column |
| **All** | | **5** | **8** | **62%** | |

Full write-up: [docs/EVAL.md](docs/EVAL.md). Backing JSON:
[docs/eval/public_results.json](docs/eval/public_results.json).

Published pin: **2 / 8** (25%) on
[`examples/trials.csv`](examples/trials.csv) (16 synthetic oncology rows).
Query model `openai/gpt-oss-120b`, judge `deepseek/deepseek-v3.2`. `/ask`
is always-LLM Cypher — these scores are not golden-string shortcuts. Six
misses stay visible; three of them are the product failing closed instead
of returning a silent wrong total. Reproduce:

```bash
infona ingest examples/trials.csv --kg eval-public-trials -y
python scripts/run_public_eval.py --dataset examples/trials.csv --kg eval-public-trials --questions 8
```

| Tier | Skill | Passed | Asked | Accuracy | Visible misses |
|------|-------|--------|-------|----------|----------------|
| 1 | Count/Lookup | 1 | 2 | 50% | Unique-sponsor count fail-closed |
| 2 | Filter | 0 | 2 | 0% | Active count returned rows; start-year ≥ 2019 fail-closed |
| 3 | Join | 1 | 2 | 50% | AstraZeneca drugs included extra brand-suffixed names |
| 4 | Multi-hop | 0 | 2 | 0% | NSCLC average fail-closed; top sponsor returned trial IDs |
| **All** | | **2** | **8** | **25%** | |

Full write-up: [docs/EVAL.md](docs/EVAL.md). Backing JSON:
[docs/eval/public_results.json](docs/eval/public_results.json).

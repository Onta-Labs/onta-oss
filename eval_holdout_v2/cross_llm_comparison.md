# Cross-LLM Ablation — Omnix Holdout v2.0

Generated: 2026-04-14T20:40:22Z
Gold: 78 questions across 7 KGs (wave-8)
Routing: v1 KGs through /ask (with body.model override), multi-table KGs through client-side ontology synthesis + direct OpenRouter SPARQL generation
Seeds per question: 3 | Total calls per model: 234

## Headline accuracy (sorted desc)

| Rank | Model | Accuracy [95% CI] | n | Correct | Δ vs primary | Est. cost | Latency p50/p95/p99 (ms) |
|---|---|---|---|---|---|---|---|
| 1 | `google/gemini-2.5-flash` | 80.8% [75.2, 85.3] | 234 | 189 | ±0.0 pp | $0.0684 | 2770 / 23240 / 74943 |
| 2 | `anthropic/claude-sonnet-4.6` | 76.5% [70.7, 81.5] | 234 | 179 | -4.3 pp | $2.9835 | 6239 / 28570 / 46311 |
| 3 | `openai/gpt-5-mini` | 41.0% [34.9, 47.4] | 234 | 96 | -39.7 pp | $1.3689 | 14522 / 49723 / 60185 |

Primary (baseline) model: `google/gemini-2.5-flash`. Cost is estimated at ~2500 in / ~350 out tokens per call (neither /ask nor the client-side path persists per-call token counts today).

## Per-domain breakdown (seed-level)

| Model | finance | healthcare | legal | scientific_public_sector |
|---|---|---|---|---|
| `google/gemini-2.5-flash` | 75.0% (45/60) | 82.5% (52/63) | 80.0% (24/30) | 84.0% (68/81) |
| `anthropic/claude-sonnet-4.6` | 78.3% (47/60) | 90.5% (57/63) | 90.0% (27/30) | 59.3% (48/81) |
| `openai/gpt-5-mini` | 48.3% (29/60) | 41.3% (26/63) | 40.0% (12/30) | 35.8% (29/81) |

**Best model per domain:**

- `finance`: `anthropic/claude-sonnet-4.6`
- `healthcare`: `anthropic/claude-sonnet-4.6`
- `legal`: `anthropic/claude-sonnet-4.6`
- `scientific_public_sector`: `google/gemini-2.5-flash`

## Per-tier breakdown (seed-level)

| Model | T1 | T2 | T3 | T4 |
|---|---|---|---|---|
| `google/gemini-2.5-flash` | 86.5% (83/96) | 79.2% (38/48) | 66.7% (42/63) | 96.3% (26/27) |
| `anthropic/claude-sonnet-4.6` | 81.2% (78/96) | 81.2% (39/48) | 65.1% (41/63) | 77.8% (21/27) |
| `openai/gpt-5-mini` | 79.2% (76/96) | 33.3% (16/48) | 6.3% (4/63) | 0.0% (0/27) |

## Per-question agreement (majority vote)

- All 3 models correct: **31 / 78**
- All 3 models wrong:   **10 / 78**
- 2 of 3 correct (1 wrong):      **28 / 78**
- 1 of 3 correct (split):        **9 / 78**

## Cost summary

- `google/gemini-2.5-flash`: ~$0.0684 (517s wall clock)
- `anthropic/claude-sonnet-4.6`: ~$2.9835 (768s wall clock)
- `openai/gpt-5-mini`: ~$1.3689 (1052s wall clock)
- **Total estimated cost:** ~$4.4208
- **Total wall clock (sequential):** 2337s (38.9 min)

## Mixed-verdict questions (at least one model disagrees)

| KG | Question | `google/gemini-2.5-flash` | `anthropic/claude-sonnet-4.6` | `openai/gpt-5-mini` |
|---|---|---|---|---|
| holdout-v2-cms-nursing-home-compare | Count the number of citations for each nursing home that are related to 'Infe... | OK | OK | X |
| holdout-v2-cms-nursing-home-compare | What are the labels of inspections that had more than 20 total health deficie... | OK | OK | X |
| holdout-v2-cms-nursing-home-compare | What is the average number of total health deficiencies for inspections with ... | OK | OK | X |
| holdout-v2-cms-nursing-home-compare | What is the average total health deficiencies for inspections with more than ... | OK | OK | X |
| holdout-v2-cms-nursing-home-compare | Which nursing homes have inspections with more than 10 total health deficienc... | X | OK | X |
| holdout-v2-fdic-call-reports | What are the different types of BankClass entities in the knowledge graph? | X | OK | X |
| holdout-v2-fema-disaster-declarations | For each County, how many FemaDeclarations have designated it as an area, whe... | OK | OK | X |
| holdout-v2-fema-disaster-declarations | For each FEMA declaration type, what is the average number of designated coun... | OK | OK | X |
| holdout-v2-fema-disaster-declarations | How many FemaDeclarations are associated with each State, for declarations ma... | OK | OK | X |
| holdout-v2-fema-disaster-declarations | How many distinct incident types are recorded in Declarations? | X | X | OK |
| holdout-v2-fema-disaster-declarations | What are the names of all counties? | OK | OK | X |
| holdout-v2-fema-disaster-declarations | What is the average number of counties designated per FemaDeclaration for eac... | OK | OK | X |
| holdout-v2-fema-disaster-declarations-multitable | Count the number of PA Projects for each Damage Category. | OK | X | OK |
| holdout-v2-fema-disaster-declarations-multitable | How many PA Projects are there? | OK | X | X |
| holdout-v2-fema-disaster-declarations-multitable | How many distinct damage categories are there? | OK | X | X |
| holdout-v2-fema-disaster-declarations-multitable | What are the average federal share obligated and total obligated amounts for ... | OK | X | X |
| holdout-v2-fema-disaster-declarations-multitable | What are the names of all the applicants? | OK | X | OK |
| holdout-v2-fema-disaster-declarations-multitable | What are the names of applicants involved in PA Projects that are in an 'Acti... | OK | X | X |
| holdout-v2-fema-disaster-declarations-multitable | What are the names of applicants who have PA Projects in 'Small' size? | OK | X | X |
| holdout-v2-fema-disaster-declarations-multitable | Which counties are located in states that have declared a 'DR' type disaster ... | OK | OK | X |
| holdout-v2-hrsa-hpsa | Count the number of Health Professional Shortage Areas for each County, where... | OK | OK | X |
| holdout-v2-hrsa-hpsa | How many Health Professional Shortage Areas are associated with each State, w... | OK | OK | X |
| holdout-v2-hrsa-hpsa | List the names of all HPSA entities where the component type code is 'UNK'. | OK | OK | X |
| holdout-v2-hrsa-hpsa | What is the average designation population for Health Professional Shortage A... | OK | OK | X |
| holdout-v2-ncua-credit-union-call-reports | How many branches does each credit union have? | OK | OK | X |
| holdout-v2-ncua-credit-union-call-reports | List the credit unions and their associated field of membership descriptions ... | OK | OK | X |
| holdout-v2-ncua-credit-union-call-reports | List the names of credit unions that have a 'TOTAL_ASSETS' metric value great... | OK | OK | X |
| holdout-v2-ncua-credit-union-call-reports | What are the names of credit unions that have a 'Community' field of membersh... | OK | OK | X |
| holdout-v2-ncua-credit-union-call-reports | Which credit unions have branches located in a state with a state FIPS code g... | OK | OK | X |
| holdout-v2-nih-reporter-non-clinical | For each study section, how many projects were reviewed by it that started in... | OK | OK | X |
| holdout-v2-nih-reporter-non-clinical | How many agencies have an abbreviation starting with 'N'? | OK | OK | X |
| holdout-v2-nih-reporter-non-clinical | List the names of investigators who have worked on projects awarded to instit... | OK | OK | X |
| holdout-v2-patentsview | For each patent, count how many outgoing citations it has, considering only u... | X | OK | X |
| holdout-v2-patentsview | How many inventors are listed with a sequence number greater than 10? | OK | OK | X |
| holdout-v2-patentsview | How many utility patents have more than 20 claims? | OK | OK | X |
| holdout-v2-patentsview | List inventors and the number of patents they are associated with, focusing o... | OK | OK | X |
| holdout-v2-patentsview | List the patents that have been cited by the most citations, along with the n... | OK | OK | X |

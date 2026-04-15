# Holdout v2.0 Text-to-SQL Baseline Report

- **Baseline:** text_to_sql (same data, same LLM, same judge as Cograph)
- **Model:** google/gemini-3-flash-preview @ T=0
- **Seeds:** 1, 2, 3  (3 independent SQL generations per question)
- **Questions:** 302  |  **KGs:** 26  |  **Converted:** 26
- **Wall clock:** 330.6s (5.5 min)
- **LLM cost:** $0.598 (1698648 input / 35175 output tokens)

## 1. Headline Accuracy

**Seed-level: 68.5% [65.4, 71.5]**  (621/906)

**Majority-vote (n=302): 68.5% [63.1, 73.5]**

*Cograph primary baseline (same data, same model): 89.4%*

## 2. Per-Domain Accuracy

| Domain | Correct / n | Accuracy (Wilson 95% CI) |
|---|---|---|
| finance | 138/177 | 78.0% [71.3, 83.4] |
| healthcare | 162/246 | 65.9% [59.7, 71.5] |
| legal | 174/252 | 69.0% [63.1, 74.4] |
| scientific_public_sector | 147/231 | 63.6% [57.3, 69.6] |

## 3. Per-Tier Accuracy

| Tier | Correct / n | Accuracy (Wilson 95% CI) |
|---|---|---|
| T1 | 258/312 | 82.7% [78.1, 86.5] |
| T2 | 198/234 | 84.6% [79.4, 88.7] |
| T3 | 81/204 | 39.7% [33.2, 46.6] |
| T4 | 84/156 | 53.8% [46.0, 61.5] |

## 4. Per-KG Accuracy (sorted descending)

| KG | Domain | Correct / n | Accuracy (Wilson 95% CI) |
|---|---|---|---|
| holdout-v2-ofr-financial-stability | finance | 36/39 | 92.3% [79.7, 97.3] |
| holdout-v2-noaa-storm-events | scientific_public_sector | 30/33 | 90.9% [76.4, 96.9] |
| holdout-v2-scdb-supreme-court | legal | 30/33 | 90.9% [76.4, 96.9] |
| holdout-v2-nih-reporter-non-clinical | scientific_public_sector | 21/24 | 87.5% [69.0, 95.7] |
| holdout-v2-cdc-wonder-mortality | healthcare | 33/39 | 84.6% [70.3, 92.8] |
| holdout-v2-pacer-federal-dockets | legal | 33/39 | 84.6% [70.3, 92.8] |
| holdout-v2-cftc-swap-data | finance | 30/36 | 83.3% [68.1, 92.1] |
| holdout-v2-npi-registry | healthcare | 27/33 | 81.8% [65.6, 91.4] |
| holdout-v2-cms-nursing-home-compare | healthcare | 24/30 | 80.0% [62.7, 90.5] |
| holdout-v2-fdic-call-reports | finance | 24/30 | 80.0% [62.7, 90.5] |
| holdout-v2-ftc-consent-decrees | legal | 33/42 | 78.6% [64.1, 88.3] |
| holdout-v2-cdc-fluview | healthcare | 30/39 | 76.9% [61.7, 87.4] |
| holdout-v2-epa-water-quality-portal | scientific_public_sector | 30/39 | 76.9% [61.7, 87.4] |
| holdout-v2-usda-agricultural-statistics | scientific_public_sector | 27/36 | 75.0% [58.9, 86.2] |
| holdout-v2-uspto-trademarks | legal | 27/36 | 75.0% [58.9, 86.2] |
| holdout-v2-doj-enforcement-actions | legal | 24/36 | 66.7% [50.3, 79.8] |
| holdout-v2-fec-enforcement | legal | 24/36 | 66.7% [50.3, 79.8] |
| holdout-v2-ncua-credit-union-call-reports | finance | 24/36 | 66.7% [50.3, 79.8] |
| holdout-v2-sec-edgar-10k | finance | 24/36 | 66.7% [50.3, 79.8] |
| holdout-v2-samhsa-n-ssats | healthcare | 21/33 | 63.6% [46.6, 77.8] |
| holdout-v2-medicare-part-d-pricing | healthcare | 24/39 | 61.5% [45.9, 75.1] |
| holdout-v2-doe-energy-research-grants | scientific_public_sector | 18/36 | 50.0% [34.5, 65.5] |
| holdout-v2-fema-disaster-declarations | scientific_public_sector | 9/27 | 33.3% [18.6, 52.2] |
| holdout-v2-fema-disaster-declarations-multitable | scientific_public_sector | 12/36 | 33.3% [20.2, 49.7] |
| holdout-v2-patentsview | legal | 3/30 | 10.0% [3.5, 25.6] |
| holdout-v2-hrsa-hpsa | healthcare | 3/33 | 9.1% [3.1, 23.6] |

## 5. Per-Question Agreement Across 3 Seeds

| Correct runs | # questions | % |
|---|---|---|
| 3/3 | 207 | 68.5% |
| 2/3 | 0 | 0.0% |
| 1/3 | 0 | 0.0% |
| 0/3 | 95 | 31.5% |

## 6. Runtime and Cost

- Wall clock: **330.6s** (5.51 min)
- Total LLM calls: 906 (plus SQL-error retries)
- Total cost: **$0.598**
- Tokens: 1698648 input / 35175 output

## 7. Conversion Failures

None — all 26 KGs converted to SQLite.

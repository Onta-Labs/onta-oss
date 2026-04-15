# Per-KG closest-training-KG distance table — Omnix Holdout v2.0

Generated: 2026-04-15T07:48:37Z
Source: HOLDOUT_V2_SPEC §8.2 item 13
Training set: 36 v9 KGs (eval_holdout_v2/training_kgs_v9.json, SHA-256 01ff1342...)
Thresholds: hard rule ≥3 types AND ≥2 rels | Jaccard gate ≥0.3

| KG ID | Closest Training KG | Type Overlap | Rel Overlap | Max Jaccard | Verdict |
|---|---|---|---|---|---|
| holdout-v2-hrsa-hpsa | cms-hospital-quality | 6 | 0 | 0.115 | PASS |
| holdout-v2-fema-disaster-declarations | cms-hospital-quality | 3 | 0 | 0.075 | PASS |
| holdout-v2-noaa-storm-events | cms-hospital-infections | 2 | 0 | 0.071 | PASS |
| holdout-v2-npi-registry | cms-nursing-home-providers | 3 | 0 | 0.070 | PASS |
| holdout-v2-fema-disaster-declarations-multitable | cms-hospital-infections | 2 | 0 | 0.065 | PASS |
| holdout-v2-epa-water-quality-portal | cms-nursing-home-providers | 2 | 0 | 0.056 | PASS |
| holdout-v2-nih-reporter-non-clinical | cms-readmissions | 1 | 0 | 0.056 | PASS |
| holdout-v2-usda-agricultural-statistics | cms-readmissions | 1 | 0 | 0.056 | PASS |
| holdout-v2-cms-nursing-home-compare | cms-nursing-home-providers | 2 | 0 | 0.051 | PASS |
| holdout-v2-fdic-call-reports | cms-readmissions | 1 | 0 | 0.050 | PASS |
| holdout-v2-doj-enforcement-actions | universities | 1 | 0 | 0.048 | PASS |
| holdout-v2-ncua-credit-union-call-reports | cms-readmissions | 1 | 0 | 0.048 | PASS |
| holdout-v2-cdc-fluview | coffee-quality | 1 | 0 | 0.043 | PASS |
| holdout-v2-samhsa-n-ssats | cms-readmissions | 1 | 0 | 0.043 | PASS |
| holdout-v2-uspto-trademarks | fda-drug-recalls | 2 | 0 | 0.043 | PASS |
| holdout-v2-cdc-wonder-mortality | cms-hospital-quality | 2 | 0 | 0.042 | PASS |
| holdout-v2-cftc-swap-data | coffee-quality | 1 | 0 | 0.042 | PASS |
| holdout-v2-medicare-part-d-pricing | fda-adverse-events | 1 | 0 | 0.022 | PASS |
| doe-energy-research-grants | — | 0 | 0 | 0.000 | PENDING |
| holdout-v2-doe-energy-research-grants | cfpb-bank-accounts | 0 | 0 | 0.000 | PASS |
| holdout-v2-fec-enforcement | cfpb-bank-accounts | 0 | 0 | 0.000 | PASS |
| holdout-v2-finra-trace-corporate-bonds | — | 0 | 0 | 0.000 | PENDING |
| holdout-v2-ftc-consent-decrees | cfpb-bank-accounts | 0 | 0 | 0.000 | PASS |
| holdout-v2-ofr-financial-stability | cfpb-bank-accounts | 0 | 0 | 0.000 | PASS |
| holdout-v2-pacer-federal-dockets | cfpb-bank-accounts | 0 | 0 | 0.000 | PASS |
| holdout-v2-patentsview | cfpb-bank-accounts | 0 | 0 | 0.000 | PASS |
| holdout-v2-scdb-supreme-court | cfpb-bank-accounts | 0 | 0 | 0.000 | PASS |
| holdout-v2-sec-edgar-10k | cfpb-bank-accounts | 0 | 0 | 0.000 | PASS |
| nsf-awards | — | 0 | 0 | 0.000 | PENDING |

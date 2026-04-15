# Holdout v2.0 Baseline Eval Report

- **Model:** google/gemini-2.5-flash-lite @ T=0
- **Tenant:** demo-tenant
- **Seeds:** 1, 2, 3  (3 independent /ask calls per question)
- **Questions:** 302  |  **KGs:** 26  |  **Total /ask calls:** 906
- **Global exclude_questions size:** 301
- **Wall clock:** 3344.4s (55.7 min)
- **Estimated LLM cost:** ~$1.47 (@ ~2500 in / ~350 out tokens per call, Gemini 2.5 Flash OpenRouter pricing)

## 1. Headline Accuracy

**83.3% [80.8, 85.6]**  (755/906 seed-level correct)

Majority-vote headline (per-question, n=302): **83.4% [78.8, 87.2]**

## 2. Per-Domain Accuracy

| Domain | Correct / n | Accuracy (Wilson 95% CI) |
|---|---|---|
| finance | 147/177 | 83.1% [76.8, 87.9] |
| healthcare | 187/246 | 76.0% [70.3, 80.9] |
| legal | 212/252 | 84.1% [79.1, 88.1] |
| scientific_public_sector | 209/231 | 90.5% [86.0, 93.6] |

## 3. Per-Tier Accuracy

| Tier | Correct / n | Accuracy (Wilson 95% CI) |
|---|---|---|
| T1 | 308/312 | 98.7% [96.8, 99.5] |
| T2 | 184/234 | 78.6% [72.9, 83.4] |
| T3 | 157/204 | 77.0% [70.7, 82.2] |
| T4 | 106/156 | 67.9% [60.3, 74.8] |

## 4. Per-KG Accuracy (sorted descending)

| KG | Domain | Correct / n | Accuracy (Wilson 95% CI) |
|---|---|---|---|
| holdout-v2-nih-reporter-non-clinical | scientific_public_sector | 24/24 | 100.0% [86.2, 100.0] |
| holdout-v2-usda-agricultural-statistics | scientific_public_sector | 36/36 | 100.0% [90.4, 100.0] |
| holdout-v2-ofr-financial-stability | finance | 38/39 | 97.4% [86.8, 99.5] |
| holdout-v2-fema-disaster-declarations | scientific_public_sector | 25/27 | 92.6% [76.6, 97.9] |
| holdout-v2-cdc-wonder-mortality | healthcare | 36/39 | 92.3% [79.7, 97.3] |
| holdout-v2-epa-water-quality-portal | scientific_public_sector | 36/39 | 92.3% [79.7, 97.3] |
| holdout-v2-cftc-swap-data | finance | 33/36 | 91.7% [78.2, 97.1] |
| holdout-v2-doj-enforcement-actions | legal | 33/36 | 91.7% [78.2, 97.1] |
| holdout-v2-scdb-supreme-court | legal | 30/33 | 90.9% [76.4, 96.9] |
| holdout-v2-ftc-consent-decrees | legal | 38/42 | 90.5% [77.9, 96.2] |
| holdout-v2-fema-disaster-declarations-multitable | scientific_public_sector | 32/36 | 88.9% [74.7, 95.6] |
| holdout-v2-uspto-trademarks | legal | 32/36 | 88.9% [74.7, 95.6] |
| holdout-v2-pacer-federal-dockets | legal | 33/39 | 84.6% [70.3, 92.8] |
| holdout-v2-noaa-storm-events | scientific_public_sector | 27/33 | 81.8% [65.6, 91.4] |
| holdout-v2-doe-energy-research-grants | scientific_public_sector | 29/36 | 80.6% [65.0, 90.2] |
| holdout-v2-sec-edgar-10k | finance | 29/36 | 80.6% [65.0, 90.2] |
| holdout-v2-samhsa-n-ssats | healthcare | 26/33 | 78.8% [62.2, 89.3] |
| holdout-v2-hrsa-hpsa | healthcare | 25/33 | 75.8% [59.0, 87.2] |
| holdout-v2-fec-enforcement | legal | 27/36 | 75.0% [58.9, 86.2] |
| holdout-v2-fdic-call-reports | finance | 22/30 | 73.3% [55.6, 85.8] |
| holdout-v2-npi-registry | healthcare | 24/33 | 72.7% [55.8, 84.9] |
| holdout-v2-cdc-fluview | healthcare | 28/39 | 71.8% [56.2, 83.5] |
| holdout-v2-cms-nursing-home-compare | healthcare | 21/30 | 70.0% [52.1, 83.3] |
| holdout-v2-ncua-credit-union-call-reports | finance | 25/36 | 69.4% [53.1, 82.0] |
| holdout-v2-medicare-part-d-pricing | healthcare | 27/39 | 69.2% [53.6, 81.4] |
| holdout-v2-patentsview | legal | 19/30 | 63.3% [45.5, 78.1] |

## 5. Per-Question Agreement Across 3 Seeds

| Correct runs | # questions | % |
|---|---|---|
| 3/3 | 242 | 80.1% |
| 2/3 | 12 | 4.0% |
| 1/3 | 5 | 1.7% |
| 0/3 | 43 | 14.2% |

## 6. Runtime and Cost

- Wall clock: **3344.4s** (55.74 min)
- Total /ask calls: **906**
- Estimated LLM cost: **~$1.47** (Gemini 2.5 Flash; see constants in build_baseline_report.py)

## 7. Systematic Failures (0/3 questions)

**43 / 302 questions failed on all 3 seeds.**

### 7.1 `holdout-v2-cdc-fluview` — q-005 (T2)

**Question:** What is the average number of patients reported in weekly reports where the number of providers is greater than 100?

**Expected answer:** `242669.83`

**Gold SPARQL:**
```sparql
SELECT (AVG(xsd:integer(?numPatients)) AS ?averageNumPatients) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-fluview> WHERE {
  ?report <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/type/WeeklyReport> .
  ?report <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/num_providers> ?numProviders .
  ?report <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/num_patients> ?numPatients .
  FILTER (xsd:integer(?numProviders) > 100) .
}
```

**Seed 1 (error) — answer:** ``

```sparql
SELECT (AVG(?num_patients) AS ?average_patients) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-fluview> WHERE { ?report <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/type/WeeklyReport> . ?report <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/num_providers> ?num_providers . ?report <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/num_patients> ?num_patients . FILTER(xsd:integer(?num_providers) > 100) . }
```

**Seed 2 (error) — answer:** ``

```sparql
SELECT (AVG(?num_patients) AS ?average_patients) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-fluview> WHERE { ?report <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/type/WeeklyReport> . ?report <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/num_providers> ?num_providers . ?report <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/num_patients> ?num_patients . FILTER(xsd:integer(?num_providers) > 100) . }
```

**Seed 3 (error) — answer:** ``

```sparql
SELECT (AVG(?num_patients) AS ?average_patients) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-fluview> WHERE { ?report <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/type/WeeklyReport> . ?report <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/num_patients> ?num_patients . ?report <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/num_providers> ?num_providers . FILTER(xsd:integer(?num_providers) > 100) . FILTER(isLiteral(?num_patients) && REGEX(STR(?num_patients), "^[0-9]+$")) }
```

### 7.2 `holdout-v2-cdc-fluview` — q-008 (T3)

**Question:** Which regions have the most weekly reports in the year 2022?

**Expected answer:** `FL | 52
HHS Region 2 (NJ, NY) | 52
HHS Region 3 (DE, DC, MD, PA, VA, WV) | 52
CT | 52
HHS Region 1 (CT, ME, MA, NH, RI, VT) | 52
HHS Region 10 (AK, ID, OR, WA) | 52
NV | 52
NY | 52
OK | 52
OH | 52`

**Gold SPARQL:**
```sparql
SELECT ?region_name (COUNT(?report) AS ?report_count)
FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-fluview>
WHERE {
  ?report <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/type/WeeklyReport> .
  ?report <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/reported_in_region> ?region .
  ?region <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/type/Region> .
  ?region <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/region_name> ?region_name .
  ?report <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/year> "2022" .
}
GROUP BY ?region_name
ORDER BY DESC(?report_count)
LIMIT 10
```

**Seed 1 (wrong) — answer:** `regionName: IN, reportCount: 52`

```sparql
SELECT ?regionName (COUNT(?report) AS ?reportCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-fluview> WHERE { ?report <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/reported_in_region> ?region . ?region <http://www.w3.org/2000/01/rdf-schema#label> ?regionName . ?report <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/year> "2022" . } GROUP BY ?regionName ORDER BY DESC(?reportCount) LIMIT 1
```

**Seed 2 (error) — answer:** `ERROR_ConnectError_[Errno 8] nodename nor servname provided, or not known`

```sparql
SELECT ?regionName (COUNT(?report) AS ?reportCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-fluview> WHERE { ?report <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/type/WeeklyReport> ; <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/reported_in_region> ?region . ?region <http://www.w3.org/2000/01/rdf-schema#label> ?regionName . ?report <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/year> "2022" . } GROUP BY ?regionName ORDER BY DESC(?reportCount) LIMIT 1
```

**Seed 3 (wrong) — answer:** `regionName: IN, reportCount: 52`

```sparql
SELECT ?regionName (COUNT(?report) AS ?reportCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-fluview> WHERE { ?report <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/reported_in_region> ?region . ?region <http://www.w3.org/2000/01/rdf-schema#label> ?regionName . ?report <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/year> "2022" . } GROUP BY ?regionName ORDER BY DESC(?reportCount) LIMIT 1
```

### 7.3 `holdout-v2-cdc-fluview` — q-013 (T4)

**Question:** What are the labels of regions that reported weekly reports in the year 2022, and what is the count of distinct age groups associated with those reports?

**Expected answer:** `HHS Region 3 (DE, DC, MD, PA, VA, WV) | 5
HHS Region 7 (IA, KS, MO, NE) | 5
HHS Region 6 (AR, LA, NM, OK, TX) | 5
HHS Region 4 (AL, FL, GA, KY, MS, NC, SC, TN) | 5
HHS Region 2 (NJ, NY) | 5
HHS Region 5 (IL, IN, MI, MN, OH, WI) | 5
HHS Region 8 (CO, MT, ND, SD, UT, WY) | 5
National | 5
HHS Region 9 (AZ, CA, HI, NV) | 5
HHS Region 1 (CT, ME, MA, NH, RI, VT) | 5
... (11 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?regionLabel (COUNT(DISTINCT ?ageGroup) AS ?ageGroupCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-fluview> WHERE {
  ?report <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/type/WeeklyReport> .
  ?report <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/year> "2022" .
  ?report <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/reported_in_region> ?region .
  ?region <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/type/Region> .
  ?region <http://www.w3.org/2000/01/rdf-schema#label> ?regionLabel .
  ?report <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/age_breakdown_for_report> ?ageGroup .
  ?ageGroup <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/type/AgeGroup> .
} GROUP BY ?regionLabel
ORDER BY DESC(?ageGroupCount)
```

**Seed 1 (error) — answer:** `HTTP_500`

```sparql
SELECT DISTINCT ?regionLabel ?ageGroupCount FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-fluview> WHERE { ?report <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/year> "2022" . ?report <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/reported_in_region> ?region . ?region <http://www.w3.org/2000/01/rdf-schema#label> ?regionLabel . ?report <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/age_breakdown_for_report> ?ageGroup . WITH ?report AS ?reportForAgeCount { SELECT (COUNT(DISTINCT ?ageGroup) AS ?ageGroupCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-fluview> WHERE { ?reportForAgeCount <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/year> "2022" . ?reportForAgeCount <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/age_breakdown_for_report> ?ageGroup } } }
```

**Seed 2 (error) — answer:** `HTTP_500`

```sparql
SELECT DISTINCT ?regionLabel ?ageGroupCount FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-fluview> WHERE { ?report <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/year> "2022" . ?report <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/reported_in_region> ?region . ?region <http://www.w3.org/2000/01/rdf-schema#label> ?regionLabel . ?report <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/age_breakdown_for_report> ?ageGroup . WITH ?report AS ?reportForAgeCount { SELECT (COUNT(DISTINCT ?ageGroup) AS ?ageGroupCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-fluview> WHERE { ?reportForAgeCount <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/year> "2022" . ?reportForAgeCount <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/age_breakdown_for_report> ?ageGroup } } }
```

**Seed 3 (error) — answer:** `HTTP_500`

```sparql
SELECT DISTINCT ?regionLabel ?ageGroupCount FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-fluview> WHERE { ?report <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/year> "2022" . ?report <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/reported_in_region> ?region . ?region <http://www.w3.org/2000/01/rdf-schema#label> ?regionLabel . ?report <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/age_breakdown_for_report> ?ageGroup . WITH ?report AS ?reportForAgeCount { SELECT (COUNT(DISTINCT ?ageGroup) AS ?ageGroupCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-fluview> WHERE { ?reportForAgeCount <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/year> "2022" . ?reportForAgeCount <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/age_breakdown_for_report> ?ageGroup } } }
```

### 7.4 `holdout-v2-cdc-wonder-mortality` — q-006 (T2)

**Question:** What is the average number of deaths recorded in mortality records where the label contains '2010'?

**Expected answer:** `15107.64`

**Gold SPARQL:**
```sparql
SELECT (AVG(xsd:integer(?deaths)) AS ?averageDeaths) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-wonder-mortality> WHERE {
  ?record <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/type/MortalityRecord> .
  ?record <http://www.w3.org/2000/01/rdf-schema#label> ?label .
  ?record <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/pred/deaths> ?deaths .
  FILTER (CONTAINS(STR(?label), "2010"))
}
```

**Seed 1 (error) — answer:** ``

```sparql
SELECT (AVG(?deaths) AS ?averageDeaths) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-wonder-mortality> WHERE { ?record <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/type/MortalityRecord> . ?record <http://www.w3.org/2000/01/rdf-schema#label> ?label . FILTER(CONTAINS(LCASE(?label), "2010")) . ?record <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/pred/deaths> ?deaths . FILTER(isLiteral(?deaths)) . FILTER(xsd:integer(?deaths) > 0) }
```

**Seed 2 (error) — answer:** ``

```sparql
SELECT (AVG(?deaths) AS ?averageDeaths) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-wonder-mortality> WHERE { ?record <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/type/MortalityRecord> . ?record <http://www.w3.org/2000/01/rdf-schema#label> ?label . FILTER(CONTAINS(LCASE(?label), "2010")) . ?record <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/pred/deaths> ?deaths . FILTER(isLiteral(?deaths)) . FILTER(xsd:integer(?deaths) > 0) }
```

**Seed 3 (error) — answer:** ``

```sparql
SELECT (AVG(?deaths) AS ?averageDeaths) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-wonder-mortality> WHERE { ?record <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/type/MortalityRecord> . ?record <http://www.w3.org/2000/01/rdf-schema#label> ?label . FILTER(CONTAINS(LCASE(?label), "2010")) . ?record <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/pred/deaths> ?deaths . FILTER(isLiteral(?deaths)) . FILTER(xsd:integer(?deaths) > 0) }
```

### 7.5 `holdout-v2-cftc-swap-data` — q-007 (T3)

**Question:** How many swap positions are associated with each contract name?

**Expected answer:** `CALIF CARBON ALLOWANCE V2024 | 68
MARINE .5% FOB USGC/BRENT 1st | 53
STEEL-HRC | 53
GULF # 6 FUEL OIL CRACK | 53
TX REC CRS V26 FRONT HALF | 53
CG Mainline Basis | 53
USGC HSFO (PLATTS) | 53
LITHIUM HYDROXIDE | 53
GULF JET NY HEAT OIL SPR | 53
ALUMINIUM EURO PREM DUTY-PAID | 53`

**Gold SPARQL:**
```sparql
SELECT ?contractName (COUNT(?swapPosition) AS ?numberOfSwapPositions) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cftc-swap-data> WHERE {
  ?swapPosition <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/type/SwapPosition> .
  ?swapPosition <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/pred/position_for_contract> ?contract .
  ?contract <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/type/Contract> .
  ?contract <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/pred/contract_name> ?contractName .
} GROUP BY ?contractName ORDER BY DESC(?numberOfSwapPositions) LIMIT 10
```

**Seed 1 (wrong) — answer:** `contractName: WHEAT-SRW, positionCount: 53
contractName: NAT GAS ICE PEN, positionCount: 53
contractName: PG&E CITYGATE FIN BASIS, positionCount: 53
contractName: NWP ROCKIES FIN BASIS, positionCount: 53
contractName: AECO FIN BASIS, positionCount: 53
contractName: CHICAGO FIN BASIS, positionCount: `

```sparql
SELECT DISTINCT ?contractName (COUNT(?position) AS ?positionCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cftc-swap-data> WHERE { ?position <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/type/SwapPosition> . ?position <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/pred/position_for_contract> ?contract . ?contract <http://www.w3.org/2000/01/rdf-schema#label> ?contractName . } GROUP BY ?contractName
```

**Seed 2 (wrong) — answer:** `contractName: WHEAT-SRW, positionCount: 53
contractName: TX REC CRS V29 BACK HALF, positionCount: 19
contractName: MARINE FUEL OIL 0.5% FOB USGC, positionCount: 47
contractName: USGC HSFO (PLATTS), positionCount: 53
contractName: FUEL OIL-3% USGC/3.5% FOB RDAM, positionCount: 53
contractName: USGC H`

```sparql
SELECT DISTINCT ?contractName (COUNT(?position) AS ?positionCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cftc-swap-data> WHERE { ?position <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/type/SwapPosition> . ?position <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/pred/position_for_contract> ?contract . ?contract <http://www.w3.org/2000/01/rdf-schema#label> ?contractName . } GROUP BY ?contractName
```

**Seed 3 (wrong) — answer:** `contractName: WHEAT-SRW, positionCount: 53
contractName: TX REC CRS V29 BACK HALF, positionCount: 19
contractName: MARINE FUEL OIL 0.5% FOB USGC, positionCount: 47
contractName: FUEL OIL-3% USGC/3.5% FOB RDAM, positionCount: 53
contractName: USGC HSFO-PLATTS/BRENT 1ST LN, positionCount: 53
contractN`

```sparql
SELECT DISTINCT ?contractName (COUNT(?position) AS ?positionCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cftc-swap-data> WHERE { ?position <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/type/SwapPosition> . ?position <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/pred/position_for_contract> ?contract . ?contract <http://www.w3.org/2000/01/rdf-schema#label> ?contractName . } GROUP BY ?contractName
```

### 7.6 `holdout-v2-cms-nursing-home-compare` — q-006 (T2)

**Question:** What is the average total health deficiencies for inspections with more than 20 total health deficiencies?

**Expected answer:** `29.86`

**Gold SPARQL:**
```sparql
SELECT (AVG(xsd:integer(?totalHealthDeficiencies)) AS ?averageDeficiencies) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cms-nursing-home-compare> WHERE {
  ?inspection <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/Inspection> .
  ?inspection <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/total_health_deficiencies> ?totalHealthDeficiencies .
  FILTER (xsd:integer(?totalHealthDeficiencies) > 20)
}
```

**Seed 1 (error) — answer:** ``

```sparql
SELECT (AVG(?totalHealthDeficiencies) AS ?averageTotalHealthDeficiencies) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cms-nursing-home-compare> WHERE { ?inspection <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/Inspection> . ?inspection <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/total_health_deficiencies> ?totalHealthDeficiencies . FILTER (xsd:integer(?totalHealthDeficiencies) > 20) }
```

**Seed 2 (error) — answer:** ``

```sparql
SELECT (AVG(?totalHealthDeficiencies) AS ?averageTotalHealthDeficiencies) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cms-nursing-home-compare> WHERE { ?inspection <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/Inspection> . ?inspection <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/total_health_deficiencies> ?totalHealthDeficiencies . FILTER (xsd:integer(?totalHealthDeficiencies) > 20) }
```

**Seed 3 (error) — answer:** ``

```sparql
SELECT (AVG(?totalHealthDeficiencies) AS ?averageTotalHealthDeficiencies) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cms-nursing-home-compare> WHERE { ?inspection <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/Inspection> . ?inspection <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/total_health_deficiencies> ?totalHealthDeficiencies . FILTER (xsd:integer(?totalHealthDeficiencies) > 20) }
```

### 7.7 `holdout-v2-cms-nursing-home-compare` — q-007 (T2)

**Question:** What is the average number of total health deficiencies for inspections with more than 20 deficiencies?

**Expected answer:** `29.86`

**Gold SPARQL:**
```sparql
SELECT (AVG(xsd:integer(?totalHealthDeficiencies)) AS ?averageDeficiencies) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cms-nursing-home-compare> WHERE {
  ?inspection <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/Inspection> .
  ?inspection <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/total_health_deficiencies> ?totalHealthDeficiencies .
  FILTER (xsd:integer(?totalHealthDeficiencies) > 20)
}
```

**Seed 1 (error) — answer:** ``

```sparql
SELECT (AVG(?totalHealthDeficiencies) AS ?averageTotalHealthDeficiencies) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cms-nursing-home-compare> WHERE { ?inspection <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/Inspection> . ?inspection <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/total_health_deficiencies> ?totalHealthDeficiencies . FILTER (xsd:integer(?totalHealthDeficiencies) > 20) }
```

**Seed 2 (error) — answer:** ``

```sparql
SELECT (AVG(?totalHealthDeficiencies) AS ?averageTotalHealthDeficiencies) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cms-nursing-home-compare> WHERE { ?inspection <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/Inspection> . ?inspection <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/total_health_deficiencies> ?totalHealthDeficiencies . FILTER (xsd:integer(?totalHealthDeficiencies) > 20) }
```

**Seed 3 (error) — answer:** ``

```sparql
SELECT (AVG(?totalHealthDeficiencies) AS ?averageTotalHealthDeficiencies) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cms-nursing-home-compare> WHERE { ?inspection <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/Inspection> . ?inspection <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/total_health_deficiencies> ?totalHealthDeficiencies . FILTER (xsd:integer(?totalHealthDeficiencies) > 20) }
```

### 7.8 `holdout-v2-cms-nursing-home-compare` — q-008 (T3)

**Question:** How many penalties of type 'Fine' does each nursing home have?

**Expected answer:** `https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/NursingHome/055866 | 1
https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/NursingHome/055992 | 4
https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/NursingHome/055894 | 5
https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/NursingHome/055916 | 1
https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/NursingHome/055899 | 2
https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/NursingHome/055932 | 4
https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/NursingHome/055952 | 2
https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/NursingHome/055954 | 1
https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/NursingHome/055922 | 1
https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/NursingHome/075192 | 1
... (1768 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?nursingHome (COUNT(?penalty) AS ?numberOfFines) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cms-nursing-home-compare> WHERE {
  ?nursingHome <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/NursingHome> .
  ?nursingHome <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/has_penalty> ?penalty .
  ?penalty <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/Penalty> .
  ?penalty <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/penalty_type> "Fine" .
} GROUP BY ?nursingHome
```

**Seed 1 (wrong) — answer:** `providerName: GRANT CUESTA SUB-ACUTE AND REHABILITATION CENTER, penaltyCount: 1
providerName: KEARNY MESA CONVALESCENT AND NURSING HOME, penaltyCount: 2
providerName: BRIGHTON POST ACUTE, penaltyCount: 1
providerName: VACAVILLE RANCH POST ACUTE, penaltyCount: 7
providerName: SAYLOR LANE HEALTHCARE C`

```sparql
SELECT ?providerName (COUNT(?penalty) AS ?penaltyCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cms-nursing-home-compare> WHERE { ?nh <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/NursingHome> . ?nh <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/provider_name> ?providerName . ?penalty <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/penalty_against_facility> ?nh . ?penalty <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/penalty_type> "Fine" . } GROUP BY ?providerName
```

**Seed 2 (wrong) — answer:** `providerName: GRANT CUESTA SUB-ACUTE AND REHABILITATION CENTER, penaltyCount: 1
providerName: KEARNY MESA CONVALESCENT AND NURSING HOME, penaltyCount: 2
providerName: BRIGHTON POST ACUTE, penaltyCount: 1
providerName: VACAVILLE RANCH POST ACUTE, penaltyCount: 7
providerName: SAYLOR LANE HEALTHCARE C`

```sparql
SELECT ?providerName (COUNT(?penalty) AS ?penaltyCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cms-nursing-home-compare> WHERE { ?nh <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/NursingHome> . ?nh <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/provider_name> ?providerName . ?penalty <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/penalty_against_facility> ?nh . ?penalty <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/penalty_type> "Fine" . } GROUP BY ?providerName
```

**Seed 3 (wrong) — answer:** `providerName: DIAMONDBACK HEALTHCARE CENTER, penaltyCount: 2
providerName: THE WOODS, A NIGHTINGALE COMMUNITY, penaltyCount: 1
providerName: HERITAGE SQUARE HEALTHCARE CENTER, penaltyCount: 1
providerName: RECTOR NURSING AND REHAB, penaltyCount: 1
providerName: THREE RIVERS HEALTH AND REHABILITATION`

```sparql
SELECT ?providerName (COUNT(?penalty) AS ?penaltyCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cms-nursing-home-compare> WHERE { ?nh <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/NursingHome> . ?nh <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/provider_name> ?providerName . ?penalty <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/penalty_against_facility> ?nh . ?penalty <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/penalty_type> "Fine" . } GROUP BY ?providerName
```

### 7.9 `holdout-v2-doe-energy-research-grants` — q-012 (T4)

**Question:** What are the average number of subjects per grant for grants published in 2020?

**Expected answer:** `3`

**Gold SPARQL:**
```sparql
SELECT (AVG(?subjectCount) AS ?averageSubjectsPerGrant) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doe-energy-research-grants> WHERE {
  ?grant <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/type/Grant> .
  ?grant <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/grant_in_year> ?year .
  ?year <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/type/Year> .
  ?year <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/year> "2020" .
  {
    SELECT ?grant (COUNT(DISTINCT ?subject) AS ?subjectCount) WHERE {
      ?grant <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/grant_has_subject> ?subject .
      ?subject <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/type/Subject> .
    } GROUP BY ?grant
  }
}
```

**Seed 1 (error) — answer:** `HTTP_500`

```sparql
SELECT (AVG(?subjectCount) AS ?averageSubjectsPerGrant) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doe-energy-research-grants> WHERE { ?grant <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/type/Grant> . ?grant <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/grant_in_year>/<https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/year> "2020" . { SELECT ?grant (COUNT(?subject) AS ?subjectCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doe-energy-research-grants> WHERE { ?grant <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/grant_has_subject> ?subject . } GROUP BY ?grant } }
```

**Seed 2 (error) — answer:** `HTTP_500`

```sparql
SELECT (AVG(?subjectCount) AS ?averageSubjectsPerGrant) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doe-energy-research-grants> WHERE { ?grant <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/type/Grant> . ?grant <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/grant_in_year>/<https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/year> "2020" . ?grant <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/grant_has_subject> ?subject . GROUP BY ?grant . BIND(COUNT(?subject) AS ?subjectCount) }
```

**Seed 3 (error) — answer:** `HTTP_500`

```sparql
SELECT (AVG(?subjectCount) AS ?averageSubjectsPerGrant) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doe-energy-research-grants> WHERE { ?grant <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/type/Grant> . ?grant <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/grant_in_year>/<https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/year> "2020" . ?grant <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/grant_has_subject> ?subject . GROUP BY ?grant . BIND(COUNT(?subject) AS ?subjectCount) }
```

### 7.10 `holdout-v2-doj-enforcement-actions` — q-010 (T4)

**Question:** What is the average total payment for actions taken against companies that are US public companies, grouped by the crime type?

**Expected answer:** `Fraud - Tax | 7.8236806E8
Fraud - General | 3.149544E8
Antitrust | 1.36317152E8
False Statements | 230200.0
Fraud - Accounting | 3.5925E7
Food | 1.69E7
Gambling | 1.0741209E7
Environmental | 8155654.0
Act to Prevent Pollution from Ships | 5200000.0
Controlled Substances / Drugs / Meth Act | 2.26520016E8
... (24 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?crimeTypeName (AVG(xsd:float(?totalPayment)) AS ?averageTotalPayment) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doj-enforcement-actions> WHERE { 
  ?action <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/type/Action> .
  ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_by_company> ?company .
  ?company <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/type/Company> .
  ?company <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/company_is_us_public_co> "True" .
  ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_total_payment> ?totalPayment .
  ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_for_crime> ?crimeType .
  ?crimeType <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/type/CrimeType> .
  ?crimeType <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/crimetype_name> ?crimeTypeName .
} GROUP BY ?crimeTypeName
```

**Seed 1 (error) — answer:** `No results found.`

```sparql
SELECT ?crime_type_name (AVG(?total_payment) AS ?average_total_payment) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doj-enforcement-actions> WHERE { ?action <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/type/Action> . ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_total_payment> ?action_total_payment_str . BIND(xsd:float(?action_total_payment_str) AS ?total_payment) . ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_by_company> ?company . ?company <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/company_is_us_public_co> "true" . ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_for_crime> ?crime_type . ?crime_type <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/crimetype_name> ?crime_type_name . FILTER(STR(?action_total_payment_str) != "") } GROUP BY ?crime_type_name
```

**Seed 2 (error) — answer:** `No results found.`

```sparql
SELECT ?crime_type_name (AVG(?total_payment) AS ?average_total_payment) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doj-enforcement-actions> WHERE { ?action <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/type/Action> . ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_total_payment> ?action_total_payment_str . BIND(xsd:float(?action_total_payment_str) AS ?total_payment) . ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_by_company> ?company . ?company <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/company_is_us_public_co> "true" . ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_for_crime> ?crime_type . ?crime_type <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/crimetype_name> ?crime_type_name . FILTER(STR(?action_total_payment_str) != "") } GROUP BY ?crime_type_name
```

**Seed 3 (error) — answer:** `No results found.`

```sparql
SELECT ?crime_type_name (AVG(?total_payment) AS ?average_total_payment) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doj-enforcement-actions> WHERE { ?action <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/type/Action> . ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_total_payment> ?action_total_payment_str . BIND(xsd:float(?action_total_payment_str) AS ?total_payment) . ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_by_company> ?company . ?company <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/company_is_us_public_co> "true" . ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_for_crime> ?crime_type . ?crime_type <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/crimetype_name> ?crime_type_name . FILTER(STR(?action_total_payment_str) != "") } GROUP BY ?crime_type_name
```

### 7.11 `holdout-v2-epa-water-quality-portal` — q-011 (T4)

**Question:** What is the average number of samples collected by organizations that operate stations in the 'Imperial' county?

**Expected answer:** `California State Water Resources Control Board | 246
Cocopah Tribe of Arizona (Tribal) | 14
Quechan Tribe of the Fort Yuma Indian Reservation, California &amp; Arizona (Tribal) | 12`

**Gold SPARQL:**
```sparql
SELECT ?organization_name (AVG(?sample_count) AS ?average_samples) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-epa-water-quality-portal> WHERE { 
  ?organization <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/Organization> .
  ?organization <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/organization_name> ?organization_name .
  ?station <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/Station> .
  ?station <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/station_operated_by> ?organization .
  ?station <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/station_in_county> ?county .
  ?county <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/County> .
  ?county <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/county_name> "Imperial" .
  { 
    SELECT ?organization (COUNT(?sample) AS ?sample_count) WHERE {
      ?sample <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/Sample> .
      ?sample <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/sample_by_organization> ?organization .
      ?organization <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/Organization> .
    } GROUP BY ?organization
  }
} GROUP BY ?organization_name
```

**Seed 1 (error) — answer:** `HTTP_500`

```sparql
SELECT (AVG(?sampleCount) AS ?averageSampleCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-epa-water-quality-portal> WHERE { ?county <http://www.w3.org/2000/01/rdf-schema#label> "Imperial" . ?station <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/station_in_county> ?county . ?organization <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/station_operated_by> ?station . { SELECT ?organization (COUNT(DISTINCT ?sample) AS ?sampleCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-epa-water-quality-portal> WHERE { ?sample <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/sample_by_organization> ?organization } GROUP BY ?organization } }
```

**Seed 2 (error) — answer:** `HTTP_500`

```sparql
SELECT (AVG(?sampleCount) AS ?averageSampleCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-epa-water-quality-portal> WHERE { ?county <http://www.w3.org/2000/01/rdf-schema#label> "Imperial" . ?station <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/station_in_county> ?county . ?organization <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/station_operated_by> ?station . { SELECT ?organization (COUNT(DISTINCT ?sample) AS ?sampleCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-epa-water-quality-portal> WHERE { ?sample <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/sample_by_organization> ?organization } GROUP BY ?organization } }
```

**Seed 3 (error) — answer:** `HTTP_500`

```sparql
SELECT (AVG(?sampleCount) AS ?averageSampleCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-epa-water-quality-portal> WHERE { ?county <http://www.w3.org/2000/01/rdf-schema#label> "Imperial" . ?station <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/station_in_county> ?county . ?organization <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/station_operated_by> ?station . { SELECT ?organization (COUNT(DISTINCT ?sample) AS ?sampleCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-epa-water-quality-portal> WHERE { ?sample <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/sample_by_organization> ?organization } GROUP BY ?organization } }
```

### 7.12 `holdout-v2-fdic-call-reports` — q-005 (T3)

**Question:** How many financial metrics are associated with each call report for the report date '20240630'?

**Expected answer:** `18094_20240630 | 19
18194_20240630 | 19
18145_20240630 | 19
18059_20240630 | 19
17933_20240630 | 19
18164_20240630 | 19
17943_20240630 | 19
17922_20240630 | 19
17937_20240630 | 19
17950_20240630 | 19
... (4608 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?callReportId (COUNT(?financialMetric) AS ?metricCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fdic-call-reports> WHERE {
  ?callReport <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/type/CallReport> .
  ?callReport <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/callreport_has_metric> ?financialMetric .
  ?financialMetric <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/type/FinancialMetric> .
  ?callReport <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/callreport_repdte> "20240630" .
  ?callReport <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/callreport_id> ?callReportId .
} GROUP BY ?callReportId
```

**Seed 1 (wrong) — answer:** `callReportLabel: 2024-Q2, metric_count: 87450`

```sparql
SELECT ?callReportLabel (COUNT(DISTINCT ?metric) AS ?metric_count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fdic-call-reports> WHERE { ?callReport <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/type/CallReport> . ?callReport <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/callreport_repdte> "20240630" . ?callReport <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/callreport_has_metric> ?metric . ?callReport <http://www.w3.org/2000/01/rdf-schema#label> ?callReportLabel } GROUP BY ?callReportLabel
```

**Seed 2 (wrong) — answer:** `callReportLabel: 2024-Q2, metric_count: 87450`

```sparql
SELECT ?callReportLabel (COUNT(DISTINCT ?metric) AS ?metric_count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fdic-call-reports> WHERE { ?callReport <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/type/CallReport> . ?callReport <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/callreport_repdte> "20240630" . ?callReport <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/callreport_has_metric> ?metric . ?callReport <http://www.w3.org/2000/01/rdf-schema#label> ?callReportLabel } GROUP BY ?callReportLabel
```

**Seed 3 (wrong) — answer:** `callReportLabel: 2024-Q2, metric_count: 87450`

```sparql
SELECT ?callReportLabel (COUNT(DISTINCT ?metric) AS ?metric_count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fdic-call-reports> WHERE { ?callReport <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/type/CallReport> . ?callReport <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/callreport_repdte> "20240630" . ?callReport <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/callreport_has_metric> ?metric . ?callReport <http://www.w3.org/2000/01/rdf-schema#label> ?callReportLabel } GROUP BY ?callReportLabel
```

### 7.13 `holdout-v2-fdic-call-reports` — q-006 (T3)

**Question:** What are the names of banks headquartered in the state of 'CA'?

**Expected answer:** `Pacific Coast Bankers' Bank, Westamerica Bank, Royal Business Bank, HCN Bank, American Plus Bank, N.A., Beneficial State Bank, River City Bank, State Bank of India (California), Banc of California, Hanmi Bank ... (119 total)`

**Gold SPARQL:**
```sparql
SELECT ?bankName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fdic-call-reports> WHERE {
  ?bank <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/type/Bank> .
  ?bank <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/bank_headquartered_in_state> ?state .
  ?state <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/type/State> .
  ?state <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/state_stalp> "CA" .
  ?bank <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/bank_name> ?bankName .
}
```

**Seed 1 (wrong) — answer:** `bank_name: First-Citizens Bank & Trust Company
bank_name: The Fidelity Bank
bank_name: Enterprise Bank of South Carolina
bank_name: Farmers and Merchants Bank of Long Beach
bank_name: Mechanics & Farmers Bank
bank_name: Bank of America California, National Association
bank_name: C3bank, National Ass`

```sparql
SELECT DISTINCT ?bank_name FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fdic-call-reports> WHERE { ?bank <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/type/Bank> . ?bank <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/bank_headquartered_in_state> ?state . ?state <http://www.w3.org/2000/01/rdf-schema#label> ?state_name . FILTER(CONTAINS(LCASE(?state_name), "ca")) . ?bank <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/bank_name> ?bank_name }
```

**Seed 2 (wrong) — answer:** `bank_name: Farmers and Merchants Bank of Long Beach
bank_name: Mechanics & Farmers Bank
bank_name: Farmers & Merchants Bank of Central California
bank_name: East West Bank
bank_name: Home Federal Savings and Loan Association
bank_name: CBL State Savings Bank
bank_name: Dedicated Community Bank
bank_`

```sparql
SELECT DISTINCT ?bank_name FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fdic-call-reports> WHERE { ?bank <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/type/Bank> . ?bank <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/bank_headquartered_in_state> ?state . ?state <http://www.w3.org/2000/01/rdf-schema#label> ?state_name . FILTER(CONTAINS(LCASE(?state_name), "ca")) . ?bank <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/bank_name> ?bank_name }
```

**Seed 3 (wrong) — answer:** `bank_name: Arthur State Bank
bank_name: Bank of York
bank_name: Bank of Greeleyville
bank_name: First-Citizens Bank & Trust Company
bank_name: The Fidelity Bank
bank_name: Enterprise Bank of South Carolina
bank_name: Oak Valley Community Bank
bank_name: Uwharrie Bank
bank_name: Roxboro Savings Bank,`

```sparql
SELECT DISTINCT ?bank_name FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fdic-call-reports> WHERE { ?bank <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/type/Bank> . ?bank <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/bank_headquartered_in_state> ?state . ?state <http://www.w3.org/2000/01/rdf-schema#label> ?state_name . FILTER(CONTAINS(LCASE(?state_name), "ca")) . ?bank <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/bank_name> ?bank_name }
```

### 7.14 `holdout-v2-fec-enforcement` — q-005 (T2)

**Question:** How many dispositions have a penalty greater than 50000 USD?

**Expected answer:** `12`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?disposition) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fec-enforcement> WHERE {
  ?disposition <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/type/Disposition> .
  ?disposition <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/disposition_penalty_usd> ?penalty .
  FILTER(xsd:float(?penalty) > 50000.0)
}
```

**Seed 1 (wrong) — answer:** `0`

```sparql
SELECT (COUNT(DISTINCT ?disposition) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fec-enforcement> WHERE { ?disposition <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/type/Disposition> . ?disposition <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/disposition_penalty_usd> ?penalty . FILTER (xsd:integer(?penalty) > 50000) }
```

**Seed 2 (wrong) — answer:** `0`

```sparql
SELECT (COUNT(DISTINCT ?disposition) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fec-enforcement> WHERE { ?disposition <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/type/Disposition> . ?disposition <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/disposition_penalty_usd> ?penalty . FILTER (xsd:integer(?penalty) > 50000) }
```

**Seed 3 (wrong) — answer:** `0`

```sparql
SELECT (COUNT(DISTINCT ?disposition) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fec-enforcement> WHERE { ?disposition <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/type/Disposition> . ?disposition <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/disposition_penalty_usd> ?penalty . FILTER (xsd:integer(?penalty) > 50000) }
```

### 7.15 `holdout-v2-fec-enforcement` — q-008 (T3)

**Question:** What are the names of participants involved in cases that have more than 1 respondent?

**Expected answer:** `Taylor, Carson, Spies, Charles "Charlie" R., Gibson, Benjamin J., Clark, Justin, Lilienthal, Elizabeth, Mitchell, Emmett "Bucky", Tim Sheehy for Montana, ECN Capital Corp., Crate, Bradley T., Make America Great Again PAC (f/k/a Donald J. Trump for President, Inc.) ... (569 total)`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?participantName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fec-enforcement> WHERE {
  ?case <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/type/Case> .
  ?case <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/case_has_participant> ?participant .
  ?participant <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/type/Participant> .
  ?participant <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/participant_name> ?participantName .
  ?case <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/case_n_respondents> ?numRespondents .
  FILTER (xsd:integer(?numRespondents) > 1)
}
```

**Seed 1 (wrong) — answer:** `participant_name: Perini & Hoerger
participant_name: CatholicVote.org
participant_name: Surry County Republican Executive Committee
participant_name: New San Diego
participant_name: The Republicans of Northeast Ohio
participant_name: No Labels
participant_name: Salazar for Congress
participant_name:`

```sparql
SELECT DISTINCT ?participant_name FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fec-enforcement> WHERE { ?case <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/type/Case> . ?case <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/case_has_respondent> ?respondent . ?case <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/case_has_participant> ?participant . ?participant <http://www.w3.org/2000/01/rdf-schema#label> ?participant_name . FILTER (EXISTS { ?case <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/case_has_respondent> ?respondent2 . FILTER NOT EXISTS { ?case <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/case_has_respondent> ?respondent3 . FILTER (?respondent3 != ?respondent) } }) }
```

**Seed 2 (wrong) — answer:** `participant_name: Perini & Hoerger
participant_name: CatholicVote.org
participant_name: Surry County Republican Executive Committee
participant_name: New San Diego
participant_name: The Republicans of Northeast Ohio
participant_name: No Labels
participant_name: Salazar for Congress
participant_name:`

```sparql
SELECT DISTINCT ?participant_name FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fec-enforcement> WHERE { ?case <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/type/Case> . ?case <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/case_has_respondent> ?respondent . ?case <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/case_has_participant> ?participant . ?participant <http://www.w3.org/2000/01/rdf-schema#label> ?participant_name . FILTER (EXISTS { ?case <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/case_has_respondent> ?respondent2 . FILTER NOT EXISTS { ?case <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/case_has_respondent> ?respondent3 . FILTER (?respondent3 != ?respondent) } }) }
```

**Seed 3 (wrong) — answer:** `participant_name: Perini & Hoerger
participant_name: CatholicVote.org
participant_name: Surry County Republican Executive Committee
participant_name: New San Diego
participant_name: The Republicans of Northeast Ohio
participant_name: No Labels
participant_name: Salazar for Congress
participant_name:`

```sparql
SELECT DISTINCT ?participant_name FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fec-enforcement> WHERE { ?case <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/type/Case> . ?case <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/case_has_respondent> ?respondent . ?case <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/case_has_participant> ?participant . ?participant <http://www.w3.org/2000/01/rdf-schema#label> ?participant_name . FILTER (EXISTS { ?case <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/case_has_respondent> ?respondent2 . FILTER NOT EXISTS { ?case <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/case_has_respondent> ?respondent3 . FILTER (?respondent3 != ?respondent) } } ) }
```

### 7.16 `holdout-v2-fec-enforcement` — q-010 (T4)

**Question:** What are the names of participants who are involved in cases that have dispositions with a penalty greater than 5000 USD?

**Expected answer:** `Calspan Corporation, Black Voters Matter Action PAC, 1199 SEIU United Healthcare Workers East Federal Political Action Fund, Sheila Jackson Lee for Congress, Meier, David, Sauer, Peter, Franklin, Kamau, Schaub, Helen, Grays, Karen Y., Lippes Mathias LLP ... (48 total)`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?participantName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fec-enforcement> WHERE {
  ?participant <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/type/Participant> .
  ?participant <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/participant_name> ?participantName .
  ?case <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/type/Case> .
  ?case <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/case_has_participant> ?participant .
  ?case <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/case_has_disposition> ?disposition .
  ?disposition <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/type/Disposition> .
  ?disposition <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/disposition_penalty_usd> ?penalty .
  FILTER (xsd:float(?penalty) > 5000.0)
}
```

**Seed 1 (wrong) — answer:** `participant_name: PA Lawyer Fund
participant_name: John James for Senate, Inc.
participant_name: OkieWay
participant_name: Crate, Bradley T.
participant_name: Hawach, Sid
participant_name: Martin, Steven
participant_name: Calspan Corporation
participant_name: 1199 SEIU United Healthcare Workers East`

```sparql
SELECT DISTINCT ?participant_name FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fec-enforcement> WHERE { ?disposition <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/disposition_penalty_usd> ?penalty . FILTER (STR(?penalty) != "" && STR(?penalty) != "N/A" && STR(?penalty) != "null" && CONTAINS(LCASE(STR(?penalty)), "5000")) . ?disposition <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/disposition_against_respondent> ?respondent . ?respondent <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/respondent_involved_in_case> ?case . ?case <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/case_has_participant> ?participant . ?participant <http://www.w3.org/2000/01/rdf-schema#label> ?participant_name }
```

**Seed 2 (wrong) — answer:** `participant_name: Calspan Corporation
participant_name: 1199 SEIU United Healthcare Workers East Federal Political Action Fund
participant_name: Meier, David
participant_name: Sauer, Peter
participant_name: Schaub, Helen
participant_name: Lippes Mathias LLP
participant_name: Trister, Ross, Schadler `

```sparql
SELECT DISTINCT ?participant_name FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fec-enforcement> WHERE { ?disposition <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/disposition_penalty_usd> ?penalty . FILTER (STR(?penalty) != "" && STR(?penalty) != "N/A" && STR(?penalty) != "null" && CONTAINS(LCASE(STR(?penalty)), "5000")) . ?disposition <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/disposition_against_respondent> ?respondent . ?respondent <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/respondent_involved_in_case> ?case . ?case <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/case_has_participant> ?participant . ?participant <http://www.w3.org/2000/01/rdf-schema#label> ?participant_name }
```

**Seed 3 (wrong) — answer:** `participant_name: PA Lawyer Fund
participant_name: John James for Senate, Inc.
participant_name: OkieWay
participant_name: Crate, Bradley T.
participant_name: Hawach, Sid
participant_name: Martin, Steven
participant_name: Calspan Corporation
participant_name: 1199 SEIU United Healthcare Workers East`

```sparql
SELECT DISTINCT ?participant_name FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fec-enforcement> WHERE { ?disposition <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/disposition_penalty_usd> ?penalty . FILTER (STR(?penalty) != "" && STR(?penalty) != "N/A" && STR(?penalty) != "null" && CONTAINS(LCASE(STR(?penalty)), "5000")) . ?disposition <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/disposition_against_respondent> ?respondent . ?respondent <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/respondent_involved_in_case> ?case . ?case <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/case_has_participant> ?participant . ?participant <http://www.w3.org/2000/01/rdf-schema#label> ?participant_name }
```

### 7.17 `holdout-v2-fema-disaster-declarations-multitable` — q-007 (T2)

**Question:** Find the total obligated amount for PA Projects where the total obligated is less than 1000.

**Expected answer:** `-2288936.8`

**Gold SPARQL:**
```sparql
SELECT (SUM(xsd:float(?totalObligated)) AS ?sumTotalObligated) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations-multitable> WHERE {
  ?project <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/PAProject> .
  ?project <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/pred/totalobligated> ?totalObligated .
  FILTER (xsd:float(?totalObligated) < 1000)
}
```

**Seed 1 (wrong) — answer:** `totalObligated: 776.42
totalObligated: 932.48
totalObligated: 963.33
totalObligated: 429.19
totalObligated: 996.09
totalObligated: 883.97
totalObligated: 803.03
totalObligated: 856.28
totalObligated: 976.74
totalObligated: 807.73
totalObligated: 799.01
totalObligated: 589.47
totalObligated: 783.9
to`

```sparql
SELECT ?totalObligated FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations-multitable> WHERE { ?paProject <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/PAProject> . ?paProject <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/pred/totalobligated> ?totalObligated . FILTER (xsd:decimal(?totalObligated) < 1000) }
```

**Seed 2 (wrong) — answer:** `totalObligated: 925
totalObligated: 675
totalObligated: 900
totalObligated: 900
totalObligated: 750
totalObligated: 915
totalObligated: 994
totalObligated: 945
totalObligated: 750
totalObligated: 750
totalObligated: 750
totalObligated: 375
totalObligated: 905
totalObligated: 350
totalObligated: 750`

```sparql
SELECT ?totalObligated FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations-multitable> WHERE { ?paProject <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/PAProject> . ?paProject <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/pred/totalobligated> ?totalObligated . FILTER (xsd:integer(?totalObligated) < 1000) }
```

**Seed 3 (wrong) — answer:** `totalObligated: 925
totalObligated: 900
totalObligated: 900
totalObligated: 750
totalObligated: 675
totalObligated: 915
totalObligated: 994
totalObligated: 945
totalObligated: 750
totalObligated: 750
totalObligated: 750
totalObligated: 375
totalObligated: 905
totalObligated: 350
totalObligated: 750`

```sparql
SELECT ?totalObligated FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations-multitable> WHERE { ?paProject <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/PAProject> . ?paProject <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/pred/totalobligated> ?totalObligated . FILTER (xsd:integer(?totalObligated) < 1000) }
```

### 7.18 `holdout-v2-ftc-consent-decrees` — q-014 (T4)

**Question:** What is the average number of distinct enforcement types associated with matters in each fiscal year?

**Expected answer:** `1996 | 4
1997 | 6
1998 | 4
1999 | 3
2000 | 3
2001 | 4
2002 | 4
2003 | 3
2004 | 6
2005 | 3
... (24 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?fiscalYearLabel (AVG(?distinctEnforcementTypes) AS ?averageDistinctEnforcementTypes) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ftc-consent-decrees> WHERE {
  ?fiscalYear <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/type/FiscalYear> .
  ?fiscalYear <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/fy_value> ?fiscalYearLabel .
  {
    SELECT ?fiscalYear (COUNT(DISTINCT ?enforcementType) AS ?distinctEnforcementTypes) WHERE {
      ?fiscalYear <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/type/FiscalYear> .
      ?fiscalYear <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/fy_includes_matter> ?matter .
      ?matter <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/type/Matter> .
      ?matter <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/matter_has_enforcement_type> ?enforcementType .
      ?enforcementType <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/type/EnforcementType> .
    } GROUP BY ?fiscalYear
  }
} GROUP BY ?fiscalYearLabel
ORDER BY ?fiscalYearLabel
```

**Seed 1 (error) — answer:** `HTTP_500`

```sparql
SELECT ?fyLabel (AVG(?distinctEnforcementTypes) AS ?averageDistinctEnforcementTypes) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ftc-consent-decrees> WHERE { ?fy <http://www.w3.org/2000/01/rdf-schema#label> ?fyLabel . { SELECT ?fy (COUNT(DISTINCT ?enforcementType) AS ?distinctEnforcementTypes) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ftc-consent-decrees> WHERE { ?matter <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/matter_in_fy> ?fy . ?matter <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/matter_has_enforcement_type> ?enforcementType . } GROUP BY ?fy } } GROUP BY ?fyLabel
```

**Seed 2 (error) — answer:** `HTTP_500`

```sparql
SELECT ?fyLabel (AVG(?distinctEnforcementTypes) AS ?averageDistinctEnforcementTypes) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ftc-consent-decrees> WHERE { ?fy <http://www.w3.org/2000/01/rdf-schema#label> ?fyLabel . { SELECT ?fy (COUNT(DISTINCT ?enforcementType) AS ?distinctEnforcementTypes) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ftc-consent-decrees> WHERE { ?matter <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/matter_in_fy> ?fy . ?matter <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/matter_has_enforcement_type> ?enforcementType . } GROUP BY ?fy } } GROUP BY ?fyLabel
```

**Seed 3 (error) — answer:** `HTTP_500`

```sparql
SELECT ?fyLabel (AVG(?distinctEnforcementTypes) AS ?averageDistinctEnforcementTypes) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ftc-consent-decrees> WHERE { ?fy <http://www.w3.org/2000/01/rdf-schema#label> ?fyLabel . { SELECT ?fy (COUNT(DISTINCT ?enforcementType) AS ?distinctEnforcementTypes) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ftc-consent-decrees> WHERE { ?matter <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/matter_in_fy> ?fy . ?matter <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/matter_has_enforcement_type> ?enforcementType . } GROUP BY ?fy } } GROUP BY ?fyLabel
```

### 7.19 `holdout-v2-hrsa-hpsa` — q-004 (T1)

**Question:** How many unique zip codes are recorded?

**Expected answer:** `423`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?zipCode) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-hrsa-hpsa> WHERE { ?zipCode <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/ZipCode> . }
```

**Seed 1 (wrong) — answer:** `250`

```sparql
SELECT (COUNT(DISTINCT ?zipCodeName) AS ?uniqueZipCodeCount)  FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-hrsa-hpsa>
WHERE { ?hpsa <https://omnix.dev/onto/hpsa_postal_code> ?zipCode . ?zipCode <https://omnix.dev/types/ZipCode/attrs/name> ?zipCodeName . }
```

**Seed 2 (wrong) — answer:** `250`

```sparql
SELECT (COUNT(DISTINCT ?zipCode) AS ?uniqueZipCodeCount)  FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-hrsa-hpsa>
WHERE { ?hpsa <https://omnix.dev/onto/hpsa_postal_code> ?zipCode . }
```

**Seed 3 (wrong) — answer:** `250`

```sparql
SELECT (COUNT(DISTINCT ?zipCodeName) AS ?uniqueZipCodeCount)

FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-hrsa-hpsa>
WHERE {
  ?hpsa <https://omnix.dev/onto/hpsa_postal_code> ?zipCode .
  ?zipCode <https://omnix.dev/types/ZipCode/attrs/name> ?zipCodeName .
}
```

### 7.20 `holdout-v2-hrsa-hpsa` — q-006 (T2)

**Question:** What is the average designation population for Health Professional Shortage Areas that are designated as 'Federally Qualified Health Center'?

**Expected answer:** `128860.07`

**Gold SPARQL:**
```sparql
SELECT (AVG(?population) AS ?averageDesignationPopulation) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-hrsa-hpsa> WHERE {
  ?hpsa <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/HealthProfessionalShortageArea> .
  ?hpsa <https://omnix.dev/types/HealthProfessionalShortageArea/attrs/designation_type> ?designationType .
  ?hpsa <https://omnix.dev/types/HealthProfessionalShortageArea/attrs/hpsa_designation_population> ?population .
  FILTER(?designationType = "Federally Qualified Health Center")
}
```

**Seed 1 (wrong) — answer:** `0`

```sparql
SELECT (AVG(?designation_population) AS ?averageDesignationPopulation)  FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-hrsa-hpsa>
WHERE { ?hpsa <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/HealthProfessionalShortageArea> . ?hpsa <https://omnix.dev/types/Hpsa/attrs/designation_population_type_description> "Federally Qualified Health Center" . ?hpsa <https://omnix.dev/types/HealthProfessionalShortageArea/attrs/hpsa_designation_population> ?designation_population . }
```

**Seed 2 (wrong) — answer:** `0`

```sparql
SELECT (AVG(?designation_population) AS ?averageDesignationPopulation)  FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-hrsa-hpsa>
WHERE { ?hpsa <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/HealthProfessionalShortageArea> . ?hpsa <https://omnix.dev/types/Hpsa/attrs/designation_population_type_description> "Federally Qualified Health Center" . ?hpsa <https://omnix.dev/types/HealthProfessionalShortageArea/attrs/hpsa_designation_population> ?designation_population . }
```

**Seed 3 (wrong) — answer:** `0`

```sparql
SELECT (AVG(?designation_population) AS ?averageDesignationPopulation)  FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-hrsa-hpsa>
WHERE { ?hpsa <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/HealthProfessionalShortageArea> . ?hpsa <https://omnix.dev/types/Hpsa/attrs/designation_population_type_description> "Federally Qualified Health Center" . ?hpsa <https://omnix.dev/types/HealthProfessionalShortageArea/attrs/hpsa_designation_population> ?designation_population . }
```

### 7.21 `holdout-v2-medicare-part-d-pricing` — q-005 (T2)

**Question:** How many spending records have a total spending in 2023 greater than 1,000,000?

**Expected answer:** `4584`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?spendingRecord) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing> WHERE {
  ?spendingRecord <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/SpendingRecord> .
  ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/total_spending_2023> ?totalSpending .
  FILTER(xsd:float(?totalSpending) > 1000000)
}
```

**Seed 1 (wrong) — answer:** `51`

```sparql
SELECT (COUNT(DISTINCT ?spendingRecord) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing> WHERE { ?spendingRecord <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/SpendingRecord> . ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/total_spending_2023> ?totalSpending . FILTER (xsd:integer(?totalSpending) > 1000000) }
```

**Seed 2 (wrong) — answer:** `51`

```sparql
SELECT (COUNT(DISTINCT ?spendingRecord) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing> WHERE { ?spendingRecord <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/SpendingRecord> . ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/total_spending_2023> ?totalSpending . FILTER (xsd:integer(?totalSpending) > 1000000) }
```

**Seed 3 (wrong) — answer:** `51`

```sparql
SELECT (COUNT(DISTINCT ?spendingRecord) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing> WHERE { ?spendingRecord <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/SpendingRecord> . ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/total_spending_2023> ?totalSpending . FILTER (xsd:integer(?totalSpending) > 1000000) }
```

### 7.22 `holdout-v2-medicare-part-d-pricing` — q-006 (T2)

**Question:** What is the average total dosage units for spending records where the average spend per unit in 2023 is less than 5?

**Expected answer:** `15581633`

**Gold SPARQL:**
```sparql
SELECT (AVG(xsd:float(?totalDosageUnits)) AS ?averageTotalDosageUnits) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing> WHERE {
  ?spendingRecord <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/SpendingRecord> .
  ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/avg_spend_per_unit_2023> ?avgSpendPerUnit .
  ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/total_dosage_units_2023> ?totalDosageUnits .
  FILTER(xsd:float(?avgSpendPerUnit) < 5)
}
```

**Seed 1 (error) — answer:** ``

```sparql
SELECT (AVG(?total_dosage_units_2023) AS ?avg_total_dosage_units_2023) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing> WHERE { ?spending_record <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/SpendingRecord> . ?spending_record <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/avg_spend_per_unit_2023> ?avg_spend_per_unit_2023 . FILTER (xsd:float(?avg_spend_per_unit_2023) < 5.0) ?spending_record <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/total_dosage_units_2023> ?total_dosage_units_2023 }
```

**Seed 2 (error) — answer:** ``

```sparql
SELECT (AVG(?total_dosage_units_2023) AS ?avg_total_dosage_units_2023) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing> WHERE { ?spending_record <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/SpendingRecord> . ?spending_record <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/avg_spend_per_unit_2023> ?avg_spend_per_unit_2023 . FILTER (xsd:float(?avg_spend_per_unit_2023) < 5.0) ?spending_record <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/total_dosage_units_2023> ?total_dosage_units_2023 }
```

**Seed 3 (error) — answer:** ``

```sparql
SELECT (AVG(?total_dosage_units_2023) AS ?avg_total_dosage_units_2023) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing> WHERE { ?spending_record <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/SpendingRecord> . ?spending_record <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/avg_spend_per_unit_2023> ?avg_spend_per_unit_2023 . FILTER (xsd:float(?avg_spend_per_unit_2023) < 5.0) ?spending_record <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/total_dosage_units_2023> ?total_dosage_units_2023 }
```

### 7.23 `holdout-v2-medicare-part-d-pricing` — q-010 (T3)

**Question:** For each manufacturer, how many spending records are associated with them where the average spend per claim in 2023 was greater than 100?

**Expected answer:** `Amneal Pharmace | 122
Teva USA | 97
Mylan | 95
Dr.Reddy'S Lab | 80
Northstar Rx Ll | 80
Sandoz | 75
Apotex Corp | 73
Aurobindo Pharm | 73
Lupin Pharmaceu | 69
Camber Pharmace | 68
... (736 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?manufacturerName (COUNT(?spendingRecord) AS ?numberOfSpendingRecords) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing> WHERE {
  ?manufacturer <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/Manufacturer> .
  ?manufacturer <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/manufacturer_name> ?manufacturerName .
  ?manufacturer <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/manufacturer_has_spending> ?spendingRecord .
  ?spendingRecord <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/SpendingRecord> .
  ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/avg_spend_per_claim_2023> ?avgSpendPerClaim .
  FILTER (xsd:float(?avgSpendPerClaim) > 100) .
} GROUP BY ?manufacturerName ORDER BY DESC(?numberOfSpendingRecords)
```

**Seed 1 (error) — answer:** `No results found.`

```sparql
SELECT ?manufacturerName (COUNT(DISTINCT ?spendingRecord) AS ?spendingRecordCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing> WHERE { ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/avg_spend_per_claim_2023> ?avgSpendPerClaim . FILTER (xsd:float(?avgSpendPerClaim) > 100) ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/manufacturer_has_spending> ?manufacturer . ?manufacturer <http://www.w3.org/2000/01/rdf-schema#label> ?manufacturerName . } GROUP BY ?manufacturerName
```

**Seed 2 (error) — answer:** `No results found.`

```sparql
SELECT ?manufacturerName (COUNT(DISTINCT ?spendingRecord) AS ?spendingRecordCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing> WHERE { ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/avg_spend_per_claim_2023> ?avgSpendPerClaim . FILTER (xsd:float(?avgSpendPerClaim) > 100) ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/manufacturer_has_spending> ?manufacturer . ?manufacturer <http://www.w3.org/2000/01/rdf-schema#label> ?manufacturerName . } GROUP BY ?manufacturerName
```

**Seed 3 (error) — answer:** `No results found.`

```sparql
SELECT ?manufacturerName (COUNT(DISTINCT ?spendingRecord) AS ?spendingRecordCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing> WHERE { ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/avg_spend_per_claim_2023> ?avgSpendPerClaim . FILTER (xsd:float(?avgSpendPerClaim) > 100) ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/manufacturer_has_spending> ?manufacturer . ?manufacturer <http://www.w3.org/2000/01/rdf-schema#label> ?manufacturerName . } GROUP BY ?manufacturerName
```

### 7.24 `holdout-v2-medicare-part-d-pricing` — q-012 (T4)

**Question:** For each manufacturer, what is the total number of distinct drugs they produce that have an associated spending record with a total spending in 2023 greater than 100000?

**Expected answer:** `Somerset Therap | 12
Oceanside Pharm | 41
Fosun Pharma Us | 6
Apotex Corp* | 5
Thea Pharma Inc | 6
Sun Pharma Glob* | 1
Ranbaxy/Sun Pha | 8
Westminster Pha | 27
Bionpharma Inc. | 34
Mylan/Archis Ph | 4
... (736 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?manufacturerName (COUNT(DISTINCT ?drug) AS ?numberOfDrugs) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing> WHERE {
  ?manufacturer <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/Manufacturer> .
  ?manufacturer <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/manufacturer_name> ?manufacturerName .
  ?manufacturer <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/manufacturer_has_spending> ?spendingRecord .
  ?spendingRecord <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/SpendingRecord> .
  ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/spending_for_drug> ?drug .
  ?drug <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/Drug> .
  ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/total_spending_2023> ?totalSpending .
  FILTER(xsd:float(?totalSpending) > 100000) .
} GROUP BY ?manufacturerName
```

**Seed 1 (wrong) — answer:** `manufacturerName: Camber Pharmace, drugName: Sertraline HCl
manufacturerName: Camber Pharmace, drugName: Torsemide
manufacturerName: Camber Pharmace, drugName: Solifenacin Succinate
manufacturerName: Camber Pharmace, drugName: Tenofovir Disoproxil Fumarate
manufacturerName: Camber Pharmace, drugName`

```sparql
SELECT DISTINCT ?manufacturerName ?drugName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing> WHERE { ?manufacturer <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/Manufacturer> . ?manufacturer <http://www.w3.org/2000/01/rdf-schema#label> ?manufacturerName . ?manufacturer <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/manufacturer_has_spending> ?spendingRecord . ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/total_spending_2023> ?totalSpending . FILTER (xsd:float(?totalSpending) > 100000) . ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/spending_for_drug> ?drug . ?drug <http://www.w3.org/2000/01/rdf-schema#label> ?drugName . }
```

**Seed 2 (wrong) — answer:** `manufacturerName: Epic Pharma LLC, drugName: Sotalol Af
manufacturerName: Biocon Pharma I, drugName: Tacrolimus*
manufacturerName: Epic Pharma LLC, drugName: Sulfadiazine
manufacturerName: Teva Parenteral, drugName: Treprostinil
manufacturerName: Teva Parenteral, drugName: Triamcinolone Acetonide*
m`

```sparql
SELECT DISTINCT ?manufacturerName ?drugName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing> WHERE { ?manufacturer <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/Manufacturer> . ?manufacturer <http://www.w3.org/2000/01/rdf-schema#label> ?manufacturerName . ?manufacturer <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/manufacturer_has_spending> ?spendingRecord . ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/total_spending_2023> ?totalSpending . FILTER (xsd:float(?totalSpending) > 100000) . ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/spending_for_drug> ?drug . ?drug <http://www.w3.org/2000/01/rdf-schema#label> ?drugName . }
```

**Seed 3 (wrong) — answer:** `manufacturerName: Oceanside Pharm, drugName: Ethacrynic Acid
manufacturerName: Tagi Pharma, drugName: Cholestyramine
manufacturerName: Tagi Pharma, drugName: Gabapentin
manufacturerName: Fresenius-Novap, drugName: Enoxaparin Sodium
manufacturerName: Jubilant Cadist, drugName: Prochlorperazine Maleat`

```sparql
SELECT DISTINCT ?manufacturerName ?drugName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing> WHERE { ?manufacturer <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/Manufacturer> . ?manufacturer <http://www.w3.org/2000/01/rdf-schema#label> ?manufacturerName . ?manufacturer <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/manufacturer_has_spending> ?spendingRecord . ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/total_spending_2023> ?totalSpending . FILTER (xsd:float(?totalSpending) > 100000) . ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/spending_for_drug> ?drug . ?drug <http://www.w3.org/2000/01/rdf-schema#label> ?drugName . }
```

### 7.25 `holdout-v2-ncua-credit-union-call-reports` — q-007 (T2)

**Question:** Find the maximum metric value among all financial metrics that have 'Total' in their name.

**Expected answer:** `177693540000`

**Gold SPARQL:**
```sparql
SELECT (MAX(xsd:float(?metricValue)) AS ?maxMetricValue) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ncua-credit-union-call-reports> WHERE { ?financialMetric <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/FinancialMetric> . ?financialMetric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_name> ?metricName . ?financialMetric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_value> ?metricValue . FILTER(CONTAINS(LCASE(STR(?metricName)), 'total')) }
```

**Seed 1 (wrong) — answer:** `99955388`

```sparql
SELECT (MAX(?metric_value) AS ?max_metric_value) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ncua-credit-union-call-reports> WHERE { ?financialMetric <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/FinancialMetric> . ?financialMetric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_name> ?metric_name . FILTER (CONTAINS(LCASE(?metric_name), "total")) . ?financialMetric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_value> ?metric_value . FILTER(isLiteral(?metric_value)) }
```

**Seed 2 (wrong) — answer:** `99955388`

```sparql
SELECT (MAX(?metric_value) AS ?max_metric_value) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ncua-credit-union-call-reports> WHERE { ?financialMetric <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/FinancialMetric> . ?financialMetric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_name> ?metric_name . FILTER(CONTAINS(LCASE(?metric_name), "total")) . ?financialMetric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_value> ?metric_value . FILTER(REGEX(?metric_value, "^[0-9]+", "i")) }
```

**Seed 3 (wrong) — answer:** `99955388`

```sparql
SELECT (MAX(?metric_value) AS ?max_metric_value) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ncua-credit-union-call-reports> WHERE { ?financialMetric <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/FinancialMetric> . ?financialMetric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_name> ?metric_name . FILTER(CONTAINS(LCASE(?metric_name), "total")) . ?financialMetric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_value> ?metric_value . FILTER(REGEX(?metric_value, "^[0-9]+", "i")) }
```

### 7.26 `holdout-v2-ncua-credit-union-call-reports` — q-011 (T4)

**Question:** Find the names of credit unions that have at least one branch in a state where the state name contains 'New', and for which the 'Total Assets' metric is greater than 1000000.

**Expected answer:** `THE SUMMIT, PALISADES, FOX, A C M G, COBBLESTONE COUNTRY, UKRAINIAN NATIONAL, BETHPAGE, TEACHERS, SING SING EMPLOYEES, HERITAGE FINANCIAL ... (496 total)`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?creditUnionName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ncua-credit-union-call-reports> WHERE {
  ?creditUnion <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/CreditUnion> .
  ?creditUnion <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/creditunion_cu_name> ?creditUnionName .
  ?creditUnion <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/has_branch> ?branch .
  ?branch <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/Branch> .
  ?branch <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/branch_located_in_state> ?state .
  ?state <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/State> .
  ?state <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/state_name> ?stateName .
  FILTER (CONTAINS(LCASE(STR(?stateName)), "new")) .
  ?financialMetric <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/FinancialMetric> .
  ?financialMetric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_for_creditunion> ?creditUnion .
  ?financialMetric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_name> "Total Assets" .
  ?financialMetric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_value> ?totalAssetsValue .
  FILTER (xsd:float(?totalAssetsValue) > 1000000)
}
```

**Seed 1 (error) — answer:** `No results found.`

```sparql
SELECT DISTINCT ?cuName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ncua-credit-union-call-reports> WHERE { ?creditUnion <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/CreditUnion> . ?creditUnion <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/has_branch> ?branch . ?branch <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/branch_located_in_state> ?state . ?state <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/state_name> ?stateName . FILTER(CONTAINS(LCASE(?stateName), "new")) . ?creditUnion <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/has_call_report> ?callReport . ?callReport <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/callreport_has_metric> ?metric . ?metric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_code> "Total Assets" . ?metric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_value> ?metricValue . FILTER(xsd:integer(?metricValue) > 1000000) . ?creditUnion <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/creditunion_cu_name> ?cuName }
```

**Seed 2 (error) — answer:** `No results found.`

```sparql
SELECT DISTINCT ?cuName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ncua-credit-union-call-reports> WHERE { ?creditUnion <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/CreditUnion> . ?creditUnion <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/creditunion_cu_name> ?cuName . ?creditUnion <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/has_branch> ?branch . ?branch <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/branch_located_in_state> ?state . ?state <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/state_name> ?stateName . FILTER(CONTAINS(LCASE(?stateName), "new")) . ?creditUnion <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/has_call_report> ?callReport . ?callReport <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/callreport_has_metric> ?metric . ?metric <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/FinancialMetric> . ?metric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_code> "Total Assets" . ?metric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_value> ?metricValue . FILTER(xsd:integer(?metricValue) > 1000000) }
```

**Seed 3 (error) — answer:** `No results found.`

```sparql
SELECT DISTINCT ?cuName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ncua-credit-union-call-reports> WHERE { ?creditUnion <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/CreditUnion> . ?creditUnion <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/creditunion_cu_name> ?cuName . ?creditUnion <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/has_branch> ?branch . ?branch <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/branch_located_in_state> ?state . ?state <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/state_name> ?stateName . FILTER(CONTAINS(LCASE(?stateName), "new")) . ?creditUnion <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/has_call_report> ?callReport . ?callReport <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/callreport_has_metric> ?metric . ?metric <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/FinancialMetric> . ?metric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_code> "Total Assets" . ?metric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_value> ?metricValue . FILTER(xsd:integer(?metricValue) > 1000000) }
```

### 7.27 `holdout-v2-ncua-credit-union-call-reports` — q-012 (T4)

**Question:** Which credit unions have a 'Net Worth Ratio' greater than 0.05 in any of their call reports, and what are their names?

**Expected answer:** `VERMONT, ONENEBRASKA, WHITE COUNTY, ARKANSAS TEACHERS, U.P. EMPLOYEES, LION, ALLEGACY, ARKANSAS FARM BUREAU, PIONEER VALLEY, BP ... (4457 total)`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?creditUnionName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ncua-credit-union-call-reports> WHERE {
  ?creditUnion <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/CreditUnion> .
  ?creditUnion <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/creditunion_cu_name> ?creditUnionName .
  ?creditUnion <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/has_call_report> ?callReport .
  ?callReport <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/CallReport> .
  ?callReport <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/callreport_has_metric> ?financialMetric .
  ?financialMetric <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/FinancialMetric> .
  ?financialMetric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_name> "Net Worth Ratio" .
  ?financialMetric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_value> ?metricValue .
  FILTER (xsd:float(?metricValue) > 0.05)
}
```

**Seed 1 (error) — answer:** `No results found.`

```sparql
SELECT DISTINCT ?creditUnionName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ncua-credit-union-call-reports> WHERE { ?creditUnion <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/CreditUnion> . ?creditUnion <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/has_call_report> ?callReport . ?callReport <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/callreport_has_metric> ?metric . ?metric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_code> "Net Worth Ratio" . ?metric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_value> ?metricValue . FILTER (xsd:float(?metricValue) > 0.05) . ?creditUnion <http://www.w3.org/2000/01/rdf-schema#label> ?creditUnionName }
```

**Seed 2 (error) — answer:** `No results found.`

```sparql
SELECT DISTINCT ?creditUnionName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ncua-credit-union-call-reports> WHERE { ?creditUnion <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/CreditUnion> . ?creditUnion <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/has_call_report> ?callReport . ?callReport <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/callreport_has_metric> ?metric . ?metric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_code> "Net Worth Ratio" . ?metric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_value> ?metricValue . FILTER (xsd:float(?metricValue) > 0.05) . ?creditUnion <http://www.w3.org/2000/01/rdf-schema#label> ?creditUnionName }
```

**Seed 3 (error) — answer:** `No results found.`

```sparql
SELECT DISTINCT ?creditUnionName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ncua-credit-union-call-reports> WHERE { ?creditUnion <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/CreditUnion> . ?creditUnion <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/has_call_report> ?callReport . ?callReport <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/callreport_has_metric> ?metric . ?metric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_code> "Net Worth Ratio" . ?metric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_value> ?metricValue . FILTER (xsd:float(?metricValue) > 0.05) . ?creditUnion <http://www.w3.org/2000/01/rdf-schema#label> ?creditUnionName }
```

### 7.28 `holdout-v2-noaa-storm-events` — q-007 (T2)

**Question:** How many storm events had more than 5 direct deaths?

**Expected answer:** `7`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?stormEvent) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-noaa-storm-events> WHERE {
  ?stormEvent <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/type/StormEvent> .
  ?stormEvent <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/pred/deaths_direct> ?deaths_direct .
  FILTER (xsd:integer(?deaths_direct) > 5)
}
```

**Seed 1 (wrong) — answer:** `0`

```sparql
SELECT (COUNT(DISTINCT ?event) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-noaa-storm-events> WHERE { ?event <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/type/StormEvent> . ?event <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/pred/deaths_direct> ?deaths . FILTER(?deaths > 5) }
```

**Seed 2 (wrong) — answer:** `0`

```sparql
SELECT (COUNT(DISTINCT ?event) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-noaa-storm-events> WHERE { ?event <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/type/StormEvent> . ?event <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/pred/deaths_direct> ?deaths . FILTER(?deaths > 5) }
```

**Seed 3 (wrong) — answer:** `0`

```sparql
SELECT (COUNT(DISTINCT ?event) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-noaa-storm-events> WHERE { ?event <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/type/StormEvent> . ?event <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/pred/deaths_direct> ?deaths . FILTER(?deaths > 5) }
```

### 7.29 `holdout-v2-noaa-storm-events` — q-008 (T2)

**Question:** What is the total property damage from storm events where the source is 'Newspaper'?

**Expected answer:** `3590010`

**Gold SPARQL:**
```sparql
SELECT (SUM(xsd:integer(?damage_property)) AS ?total_damage) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-noaa-storm-events> WHERE {
  ?stormEvent <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/type/StormEvent> .
  ?stormEvent <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/pred/source> ?source .
  ?stormEvent <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/pred/damage_property> ?damage_property .
  FILTER (?source = "Newspaper")
}
```

**Seed 1 (error) — answer:** ``

```sparql
SELECT (SUM(?damage_property) AS ?total_damage_property) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-noaa-storm-events> WHERE { ?storm_event <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/type/StormEvent> . ?storm_event <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/pred/source> 'Newspaper' . ?storm_event <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/pred/damage_property> ?damage_property . }
```

**Seed 2 (error) — answer:** ``

```sparql
SELECT (SUM(?damage_property) AS ?total_property_damage) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-noaa-storm-events> WHERE { ?storm_event <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/type/StormEvent> . ?storm_event <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/pred/source> "Newspaper" . ?storm_event <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/pred/damage_property> ?damage_property . }
```

**Seed 3 (error) — answer:** ``

```sparql
SELECT (SUM(?damage_property) AS ?total_damage_property) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-noaa-storm-events> WHERE { ?storm_event <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/type/StormEvent> . ?storm_event <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/pred/source> 'Newspaper' . ?storm_event <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/pred/damage_property> ?damage_property . }
```

### 7.30 `holdout-v2-npi-registry` — q-009 (T3)

**Question:** How many providers are associated with each practice location?

**Expected answer:** `https://omnix.dev/holdout-v2/holdout-v2-npi-registry/PracticeLocation/1598934572__loc | 1
https://omnix.dev/holdout-v2/holdout-v2-npi-registry/PracticeLocation/1760114839__loc | 1
https://omnix.dev/holdout-v2/holdout-v2-npi-registry/PracticeLocation/1366647794__loc | 1
https://omnix.dev/holdout-v2/holdout-v2-npi-registry/PracticeLocation/1811596877__loc | 1
https://omnix.dev/holdout-v2/holdout-v2-npi-registry/PracticeLocation/1003576810__loc | 1
https://omnix.dev/holdout-v2/holdout-v2-npi-registry/PracticeLocation/1295447886__loc | 1
https://omnix.dev/holdout-v2/holdout-v2-npi-registry/PracticeLocation/1043451859__loc | 1
https://omnix.dev/holdout-v2/holdout-v2-npi-registry/PracticeLocation/1922158385__loc | 1
https://omnix.dev/holdout-v2/holdout-v2-npi-registry/PracticeLocation/1669775771__loc | 1
https://omnix.dev/holdout-v2/holdout-v2-npi-registry/PracticeLocation/1992830467__loc | 1`

**Gold SPARQL:**
```sparql
SELECT ?practiceLocation (COUNT(DISTINCT ?provider) AS ?providerCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-npi-registry> WHERE {
  ?provider <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/type/Provider> .
  ?provider <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/provider_has_practice_location> ?practiceLocation .
  ?practiceLocation <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/type/PracticeLocation> .
} GROUP BY ?practiceLocation ORDER BY DESC(?providerCount) LIMIT 10
```

**Seed 1 (wrong) — answer:** `practiceLocationName: 54 HOSPITAL DR, providerCount: 150
practiceLocationName: 17199 HAPPY HOLLOW RD, providerCount: 1
practiceLocationName: 902 W MAIN ST, providerCount: 1
practiceLocationName: 1116 S. OAK ST, providerCount: 1
practiceLocationName: 621 REDWOOD DR, providerCount: 1
practiceLocationN`

```sparql
SELECT ?practiceLocationName (COUNT(DISTINCT ?provider) AS ?providerCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-npi-registry> WHERE { ?provider <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/provider_has_practice_location> ?practiceLocation . ?practiceLocation <http://www.w3.org/2000/01/rdf-schema#label> ?practiceLocationName . } GROUP BY ?practiceLocationName
```

**Seed 2 (wrong) — answer:** `practiceLocationName: 3536 KUHNE RD, providerCount: 2
practiceLocationName: 318 OAK ST, providerCount: 1
practiceLocationName: 701 N OAK ST, providerCount: 5
practiceLocationName: 1601 1800 WEIN ST., providerCount: 1
practiceLocationName: 1075 NICHOLS RD, providerCount: 13
practiceLocationName: 985 `

```sparql
SELECT ?practiceLocationName (COUNT(DISTINCT ?provider) AS ?providerCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-npi-registry> WHERE { ?provider <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/provider_has_practice_location> ?practiceLocation . ?practiceLocation <http://www.w3.org/2000/01/rdf-schema#label> ?practiceLocationName . } GROUP BY ?practiceLocationName
```

**Seed 3 (wrong) — answer:** `practiceLocationName: 3536 KUHNE RD, providerCount: 2
practiceLocationName: 318 OAK ST, providerCount: 1
practiceLocationName: 701 N OAK ST, providerCount: 5
practiceLocationName: 1601 1800 WEIN ST., providerCount: 1
practiceLocationName: 1075 NICHOLS RD, providerCount: 13
practiceLocationName: 985 `

```sparql
SELECT ?practiceLocationName (COUNT(DISTINCT ?provider) AS ?providerCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-npi-registry> WHERE { ?provider <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/provider_has_practice_location> ?practiceLocation . ?practiceLocation <http://www.w3.org/2000/01/rdf-schema#label> ?practiceLocationName . } GROUP BY ?practiceLocationName
```

### 7.31 `holdout-v2-npi-registry` — q-010 (T3)

**Question:** Which cities host the most practice locations, and how many do they host?

**Expected answer:** `https://omnix.dev/holdout-v2/holdout-v2-npi-registry/City/KY__louisa | 972
https://omnix.dev/holdout-v2/holdout-v2-npi-registry/City/IA__ankeny | 455
https://omnix.dev/holdout-v2/holdout-v2-npi-registry/City/IA__ames | 415
https://omnix.dev/holdout-v2/holdout-v2-npi-registry/City/MO__osage_beach | 397
https://omnix.dev/holdout-v2/holdout-v2-npi-registry/City/OH__dublin | 355
https://omnix.dev/holdout-v2/holdout-v2-npi-registry/City/OH__westerville | 324
https://omnix.dev/holdout-v2/holdout-v2-npi-registry/City/OH__columbus | 299
https://omnix.dev/holdout-v2/holdout-v2-npi-registry/City/CA__rancho_cucamonga | 273
https://omnix.dev/holdout-v2/holdout-v2-npi-registry/City/KY__paintsville | 248
https://omnix.dev/holdout-v2/holdout-v2-npi-registry/City/CA__pomona | 218`

**Gold SPARQL:**
```sparql
SELECT ?city (COUNT(DISTINCT ?practiceLocation) AS ?practiceLocationCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-npi-registry> WHERE {
  ?city <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/type/City> .
  ?city <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/city_hosts_practice_location> ?practiceLocation .
  ?practiceLocation <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/type/PracticeLocation> .
} GROUP BY ?city ORDER BY DESC(?practiceLocationCount) LIMIT 10
```

**Seed 1 (wrong) — answer:** `cityName: Louisa, practiceLocationCount: 972`

```sparql
SELECT ?cityName (COUNT(?practiceLocation) AS ?practiceLocationCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-npi-registry> WHERE { ?city <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/type/City> . ?city <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/city_name> ?cityName . ?city <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/city_hosts_practice_location> ?practiceLocation . } GROUP BY ?cityName ORDER BY DESC(?practiceLocationCount) LIMIT 1
```

**Seed 2 (wrong) — answer:** `cityName: Louisa, practiceLocationCount: 972`

```sparql
SELECT ?cityName (COUNT(?practiceLocation) AS ?practiceLocationCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-npi-registry> WHERE { ?city <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/type/City> . ?city <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/city_name> ?cityName . ?city <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/city_hosts_practice_location> ?practiceLocation . } GROUP BY ?cityName ORDER BY DESC(?practiceLocationCount) LIMIT 1
```

**Seed 3 (wrong) — answer:** `cityName: Louisa, practiceLocationCount: 972`

```sparql
SELECT ?cityName (COUNT(?practiceLocation) AS ?practiceLocationCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-npi-registry> WHERE { ?city <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/type/City> . ?city <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/city_name> ?cityName . ?city <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/city_hosts_practice_location> ?practiceLocation . } GROUP BY ?cityName ORDER BY DESC(?practiceLocationCount) LIMIT 1
```

### 7.32 `holdout-v2-npi-registry` — q-011 (T3)

**Question:** How many providers are there for each specialty, specifically for specialties with 'Unknown' in their name?

**Expected answer:** `https://omnix.dev/holdout-v2/holdout-v2-npi-registry/Specialty/208VP0000X | 29
https://omnix.dev/holdout-v2/holdout-v2-npi-registry/Specialty/2086S0129X | 6
https://omnix.dev/holdout-v2/holdout-v2-npi-registry/Specialty/2085R0001X | 6
https://omnix.dev/holdout-v2/holdout-v2-npi-registry/Specialty/213ES0131X | 2
https://omnix.dev/holdout-v2/holdout-v2-npi-registry/Specialty/222Z00000X | 1
https://omnix.dev/holdout-v2/holdout-v2-npi-registry/Specialty/2084N0600X | 1
https://omnix.dev/holdout-v2/holdout-v2-npi-registry/Specialty/2085N0904X | 1`

**Gold SPARQL:**
```sparql
SELECT ?specialty (COUNT(DISTINCT ?provider) AS ?providerCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-npi-registry> WHERE {
  ?provider <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/type/Provider> .
  ?provider <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/provider_has_specialty> ?specialty .
  ?specialty <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/type/Specialty> .
  ?specialty <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/specialty_name> ?specialtyName .
  FILTER (CONTAINS(LCASE(?specialtyName), 'unknown'))
} GROUP BY ?specialty ORDER BY DESC(?providerCount) LIMIT 10
```

**Seed 1 (wrong) — answer:** `specialtyName: Unknown, providerCount: 46`

```sparql
SELECT ?specialtyName (COUNT(DISTINCT ?provider) AS ?providerCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-npi-registry> WHERE { ?provider <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/provider_has_specialty> ?specialty . ?specialty <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/specialty_name> ?specialtyName . FILTER(CONTAINS(LCASE(?specialtyName), "unknown")) } GROUP BY ?specialtyName
```

**Seed 2 (wrong) — answer:** `specialtyName: Unknown, providerCount: 46`

```sparql
SELECT ?specialtyName (COUNT(DISTINCT ?provider) AS ?providerCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-npi-registry> WHERE { ?provider <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/provider_has_specialty> ?specialty . ?specialty <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/specialty_name> ?specialtyName . FILTER(CONTAINS(LCASE(?specialtyName), "unknown")) } GROUP BY ?specialtyName
```

**Seed 3 (wrong) — answer:** `specialtyName: Unknown, providerCount: 46`

```sparql
SELECT ?specialtyName (COUNT(DISTINCT ?provider) AS ?providerCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-npi-registry> WHERE { ?provider <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/provider_has_specialty> ?specialty . ?specialty <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/specialty_name> ?specialtyName . FILTER(CONTAINS(LCASE(?specialtyName), "unknown")) } GROUP BY ?specialtyName
```

### 7.33 `holdout-v2-pacer-federal-dockets` — q-012 (T4)

**Question:** For each court, what is the average number of dockets that have a 'Civil Miscellaneous Case' cause, grouped by the year they were filed?

**Expected answer:** `District Court, District of Columbia | 2026 | 1
District Court, E.D. Missouri | 2026 | 1`

**Gold SPARQL:**
```sparql
SELECT ?courtName ?yearValue (COUNT(?docket) AS ?numberOfDockets) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-pacer-federal-dockets> WHERE {
  ?court <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/Court> .
  ?court <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/court_full_name> ?courtName .
  ?docket <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/Docket> .
  ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_filed_in_court> ?court .
  ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_cause> "Civil Miscellaneous Case" .
  ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_filed_in_year> ?year .
  ?year <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/Year> .
  ?year <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/year_value> ?yearValue .
} GROUP BY ?courtName ?yearValue
ORDER BY ?courtName ?yearValue
```

**Seed 1 (error) — answer:** `HTTP_500`

```sparql
SELECT ?courtName ?yearValue (AVG(?docketCount) AS ?averageDockets) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-pacer-federal-dockets> WHERE { ?court <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/Court> . ?court <http://www.w3.org/2000/01/rdf-schema#label> ?courtName . ?court <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/court_has_docket> ?docket . ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_cause> "Civil Miscellaneous Case" . ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_filed_in_year> ?year . ?year <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/year_value> ?yearValue . { SELECT ?court ?yearValue (COUNT(DISTINCT ?docket) AS ?docketCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-pacer-federal-dockets> WHERE { ?court <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/Court> . ?court <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/court_has_docket> ?docket . ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_cause> "Civil Miscellaneous Case" . ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_filed_in_year> ?year . ?year <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/year_value> ?yearValue } GROUP BY ?court ?yearValue } } GROUP BY ?courtName ?yearValue ORDER BY ?courtName ?yearValue
```

**Seed 2 (error) — answer:** `HTTP_500`

```sparql
SELECT ?courtName ?yearValue (AVG(?docketCount) AS ?averageDockets) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-pacer-federal-dockets> WHERE { { SELECT ?court ?yearValue (COUNT(DISTINCT ?docket) AS ?docketCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-pacer-federal-dockets> WHERE { ?court <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/Court> . ?court <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/court_has_docket> ?docket . ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_cause> "Civil Miscellaneous Case" . ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_filed_in_year> ?year . ?year <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/year_value> ?yearValue . } GROUP BY ?court ?yearValue } ?court <http://www.w3.org/2000/01/rdf-schema#label> ?courtName . } GROUP BY ?courtName ?yearValue ORDER BY ?courtName ?yearValue
```

**Seed 3 (error) — answer:** `HTTP_500`

```sparql
SELECT ?courtName ?yearValue (AVG(?docketCount) AS ?averageDockets) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-pacer-federal-dockets> WHERE { ?court <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/Court> . ?court <http://www.w3.org/2000/01/rdf-schema#label> ?courtName . ?court <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/court_has_docket> ?docket . ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_cause> "Civil Miscellaneous Case" . ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_filed_in_year> ?year . ?year <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/year_value> ?yearValue . { SELECT ?court ?yearValue (COUNT(DISTINCT ?docket) AS ?docketCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-pacer-federal-dockets> WHERE { ?court <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/Court> . ?court <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/court_has_docket> ?docket . ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_cause> "Civil Miscellaneous Case" . ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_filed_in_year> ?year . ?year <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/year_value> ?yearValue } GROUP BY ?court ?yearValue } } GROUP BY ?courtName ?yearValue ORDER BY ?courtName ?yearValue
```

### 7.34 `holdout-v2-pacer-federal-dockets` — q-013 (T4)

**Question:** What are the names of judges who have been assigned to dockets in courts with 'District' in their full name, and how many such dockets has each judge been assigned to?

**Expected answer:** `Unassigned Judge | 30
M CASEY RODGERS | 19
Aaron Christian Peterson | 15
Aleta A. Trauger | 15
Lisa G. Wood | 14
Unassigned | 12
Nancy J. Rosenstengel | 11
Stephanie M. Rose | 11
Jason A. Robertson | 11
James Wesley Hendrix | 11`

**Gold SPARQL:**
```sparql
SELECT ?judgeName (COUNT(DISTINCT ?docket) AS ?docketCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-pacer-federal-dockets> WHERE { 
  ?judge <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/Judge> .
  ?judge <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/judge_name> ?judgeName .
  ?docket <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/Docket> .
  ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_assigned_to_judge> ?judge .
  ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_filed_in_court> ?court .
  ?court <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/Court> .
  ?court <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/court_full_name> ?courtFullName .
  FILTER(CONTAINS(LCASE(STR(?courtFullName)), "district"))
} GROUP BY ?judgeName
ORDER BY DESC(?docketCount)
LIMIT 10
```

**Seed 1 (wrong) — answer:** `judgeName: Sparkle L. Sooknanan, docketCount: 2
judgeName: Ana C. Reyes, docketCount: 2
judgeName: Beryl A. Howell, docketCount: 2
judgeName: Colleen Kollar-Kotelly, docketCount: 1
judgeName: Edmund G LaCour, Jr, docketCount: 4
judgeName: Staci G Cornelius, docketCount: 1
judgeName: Annemarie Carney`

```sparql
SELECT DISTINCT ?judgeName (COUNT(DISTINCT ?docket) AS ?docketCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-pacer-federal-dockets> WHERE { ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_assigned_to_judge> ?judge . ?judge <http://www.w3.org/2000/01/rdf-schema#label> ?judgeName . ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_filed_in_court> ?court . ?court <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/court_full_name> ?courtFullName . FILTER (CONTAINS(LCASE(?courtFullName), "district")) . } GROUP BY ?judgeName
```

**Seed 2 (wrong) — answer:** `judgeName: Edmund G LaCour, Jr, docketCount: 4
judgeName: Staci G Cornelius, docketCount: 1
judgeName: Annemarie Carney Axon, docketCount: 3
judgeName: Liles C Burke, docketCount: 2
judgeName: Anna M Manasco, docketCount: 2
judgeName: John H England, III, docketCount: 3
judgeName: C Lynwood Smith, J`

```sparql
SELECT DISTINCT ?judgeName (COUNT(DISTINCT ?docket) AS ?docketCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-pacer-federal-dockets> WHERE { ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_assigned_to_judge> ?judge . ?judge <http://www.w3.org/2000/01/rdf-schema#label> ?judgeName . ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_filed_in_court> ?court . ?court <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/court_full_name> ?courtFullName . FILTER (CONTAINS(LCASE(?courtFullName), "district")) . } GROUP BY ?judgeName
```

**Seed 3 (wrong) — answer:** `judgeName: Frances M. Tydingco-Gatewood, docketCount: 5
judgeName: Troy L. Nunley, docketCount: 4
judgeName: Dale A. Drozd, docketCount: 4
judgeName: Corey L Maze, docketCount: 3
judgeName: Edmund G LaCour, Jr, docketCount: 4
judgeName: Kirk E. Sherriff, docketCount: 7
judgeName: Sean C. Riordan, do`

```sparql
SELECT ?judgeName (COUNT(DISTINCT ?docket) AS ?docketCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-pacer-federal-dockets> WHERE { ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_assigned_to_judge> ?judge . ?judge <http://www.w3.org/2000/01/rdf-schema#label> ?judgeName . ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_filed_in_court> ?court . ?court <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/court_full_name> ?courtFullName . FILTER (CONTAINS(LCASE(?courtFullName), "district")) . } GROUP BY ?judgeName
```

### 7.35 `holdout-v2-patentsview` — q-005 (T2)

**Question:** How many utility patents have more than 20 claims?

**Expected answer:** `635`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?patent) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview> WHERE { ?patent <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Patent> . ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/patent_type> "utility" . ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/num_claims> ?numClaims . FILTER(xsd:integer(?numClaims) > 20) }
```

**Seed 1 (wrong) — answer:** `0`

```sparql
SELECT (COUNT(DISTINCT ?patent) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview> WHERE { ?patent <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Patent> . ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/patent_type> "Utility" . ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/num_claims> ?num_claims . FILTER(xsd:integer(?num_claims) > 20) . }
```

**Seed 2 (wrong) — answer:** `0`

```sparql
SELECT (COUNT(DISTINCT ?patent) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview> WHERE { ?patent <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Patent> . ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/patent_type> "Utility" . ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/num_claims> ?num_claims . FILTER(xsd:integer(?num_claims) > 20) . }
```

**Seed 3 (wrong) — answer:** `0`

```sparql
SELECT (COUNT(DISTINCT ?patent) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview> WHERE { ?patent <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Patent> . ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/patent_type> "Utility" . ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/num_claims> ?num_claims . FILTER(xsd:integer(?num_claims) > 20) . }
```

### 7.36 `holdout-v2-patentsview` — q-007 (T2)

**Question:** Find all assignees whose organization name contains the word 'Technology'.

**Expected answer:** `ba3ecc52-2929-4d0e-9203-20b91cdb6630 | Beijing E-Town Semiconductor Technology Co., Ltd.
f7cf7c89-c39f-421c-a1e5-e3d403782dc7 | Industrial Technology Research Institute
6276fb79-21ef-420c-8037-6e586cc49e56 | Ormazabal Corporate Technology, A.I.E.
09cc6f08-128f-4298-ba1a-9539b8e192e6 | Praxair Technology, Inc.
557784be-231b-4aef-a9f4-740b36729a1b | Ojai Retinal Technology, LLC
0f9a639e-9224-423e-9f5f-0b64121f154b | Sensor Electronic Technology, Inc.
eb2ebb85-8503-414d-9730-aed37c7ab928 | Japan Science and Technology Agency
b815f470-f656-4e72-b06e-71fd2d04f22c | Fu Da Tong Technology Co., Ltd.
1149ebbb-6368-440e-ae2f-0f1303eb4bcb | Ningbo Defeng Power Technology Co., Ltd.
845130de-72da-4cbb-b225-374b1c819d91 | Commonwealth Of Australia represented by Department of Defence, Defence Science and Technology Organisation
... (67 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?assigneeId ?orgName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview> WHERE { ?assignee <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Assignee> . ?assignee <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/assignee_id> ?assigneeId . ?assignee <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/disambig_assignee_organization> ?orgName . FILTER(CONTAINS(?orgName, "Technology")) }
```

**Seed 1 (wrong) — answer:** `assigneeName: AGENCY FOR SCIENCE, TECHNOLOGY AND RESEARCH
assigneeName: BAIJIE TENG TECHNOLOGY CORPORATION
assigneeName: HEFEI BOE OPTOELECTRONICS TECHNOLOGY CO., LTD.
assigneeName: SHENZHEN CHINA STAR OPROELECTRONICS SEMICONDUCTOR DISPLAY TECHNOLOGY CO., LTD.
assigneeName: GE ENERGY POWER CONVERSIO`

```sparql
SELECT DISTINCT ?assigneeName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview> WHERE { ?assignee <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Assignee> . ?assignee <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/disambig_assignee_organization> ?assigneeName . FILTER(CONTAINS(LCASE(?assigneeName), 'technology')) }
```

**Seed 2 (wrong) — answer:** `assigneeName: AGENCY FOR SCIENCE, TECHNOLOGY AND RESEARCH
assigneeName: BAIJIE TENG TECHNOLOGY CORPORATION
assigneeName: HEFEI BOE OPTOELECTRONICS TECHNOLOGY CO., LTD.
assigneeName: SHENZHEN CHINA STAR OPROELECTRONICS SEMICONDUCTOR DISPLAY TECHNOLOGY CO., LTD.
assigneeName: GE ENERGY POWER CONVERSIO`

```sparql
SELECT DISTINCT ?assigneeName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview> WHERE { ?assignee <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Assignee> . ?assignee <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/disambig_assignee_organization> ?assigneeName . FILTER(CONTAINS(LCASE(?assigneeName), 'technology')) }
```

**Seed 3 (wrong) — answer:** `assigneeName: AGENCY FOR SCIENCE, TECHNOLOGY AND RESEARCH
assigneeName: BAIJIE TENG TECHNOLOGY CORPORATION
assigneeName: HEFEI BOE OPTOELECTRONICS TECHNOLOGY CO., LTD.
assigneeName: SHENZHEN CHINA STAR OPROELECTRONICS SEMICONDUCTOR DISPLAY TECHNOLOGY CO., LTD.
assigneeName: THINKON NEW TECHNOLOGY JA`

```sparql
SELECT DISTINCT ?assigneeName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview> WHERE { ?assignee <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Assignee> . ?assignee <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/disambig_assignee_organization> ?assigneeName . FILTER(CONTAINS(LCASE(?assigneeName), 'technology')) }
```

### 7.37 `holdout-v2-patentsview` — q-008 (T3)

**Question:** For each patent, count how many outgoing citations it has, considering only utility patents filed after 2000.

**Expected answer:** `https://omnix.dev/holdout-v2/holdout-v2-patentsview/Patent/10542974 | 4770
https://omnix.dev/holdout-v2/holdout-v2-patentsview/Patent/10542991 | 1962
https://omnix.dev/holdout-v2/holdout-v2-patentsview/Patent/10555769 | 1879
https://omnix.dev/holdout-v2/holdout-v2-patentsview/Patent/10524872 | 1849
https://omnix.dev/holdout-v2/holdout-v2-patentsview/Patent/10542967 | 1574
https://omnix.dev/holdout-v2/holdout-v2-patentsview/Patent/10525236 | 800
https://omnix.dev/holdout-v2/holdout-v2-patentsview/Patent/10556090 | 614
https://omnix.dev/holdout-v2/holdout-v2-patentsview/Patent/10553241 | 611
https://omnix.dev/holdout-v2/holdout-v2-patentsview/Patent/10543064 | 602
https://omnix.dev/holdout-v2/holdout-v2-patentsview/Patent/10591877 | 585`

**Gold SPARQL:**
```sparql
SELECT ?patent (COUNT(DISTINCT ?citation) AS ?citationCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview> WHERE {
  ?patent <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Patent> .
  ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/has_outgoing_citation> ?citation .
  ?citation <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Citation> .
  ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/patent_type> "utility" .
  ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/patent_date> ?date .
  FILTER(?date > "2000-01-01")
} GROUP BY ?patent ORDER BY DESC(?citationCount) LIMIT 10
```

**Seed 1 (error) — answer:** `No results found.`

```sparql
SELECT ?patent_title (COUNT(?citation) AS ?outgoing_citation_count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview> WHERE { ?patent <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Patent> . ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/patent_type> 'Utility' . ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/patent_date> ?patent_date . FILTER (STR(?patent_date) > "2000") . ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/has_outgoing_citation> ?citation . ?patent <http://www.w3.org/2000/01/rdf-schema#label> ?patent_title . } GROUP BY ?patent_title
```

**Seed 2 (error) — answer:** `No results found.`

```sparql
SELECT ?patent_title (COUNT(?citation) AS ?outgoing_citation_count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview> WHERE { ?patent <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Patent> . ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/patent_type> 'Utility' . ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/patent_date> ?patent_date . FILTER (STR(?patent_date) > "2000") . ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/has_outgoing_citation> ?citation . ?patent <http://www.w3.org/2000/01/rdf-schema#label> ?patent_title . } GROUP BY ?patent_title
```

**Seed 3 (error) — answer:** `No results found.`

```sparql
SELECT ?patent_title (COUNT(?citation) AS ?outgoing_citation_count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview> WHERE { ?patent <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Patent> . ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/patent_type> 'Utility' . ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/patent_date> ?patent_date . FILTER (STR(?patent_date) > "2000") . ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/has_outgoing_citation> ?citation . ?patent <http://www.w3.org/2000/01/rdf-schema#label> ?patent_title . } GROUP BY ?patent_title
```

### 7.38 `holdout-v2-samhsa-n-ssats` — q-008 (T3)

**Question:** List the names of treatment facilities that accept 'Medicaid' as a payment type and serve 'Adolescents'.

**Expected answer:** `11229, 11266, 11263, 11273, 11277, 11279, 11332, 11334, 11278, 11264 ... (5864 total)`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?facility_name FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-samhsa-n-ssats> WHERE {
  ?facility <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/TreatmentFacility> .
  ?facility <http://www.w3.org/2000/01/rdf-schema#label> ?facility_name .
  ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/accepts_payment> ?payment_type .
  ?payment_type <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/PaymentType> .
  ?payment_type <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/payment_name> "Medicaid" .
  ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/serves_client_type> ?client_type .
  ?client_type <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/ClientType> .
  ?client_type <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/client_type_name> "Adolescents" .
}
```

**Seed 1 (wrong) — answer:** `facilityName: 11236
facilityName: 11237
facilityName: 11239
facilityName: 11254
facilityName: 11258
facilityName: 11240
facilityName: 11251
facilityName: 11257
facilityName: 11241
facilityName: 11242
facilityName: 11252
facilityName: 11259
facilityName: 11260
facilityName: 11256
facilityName: 11247
`

```sparql
SELECT DISTINCT ?facilityName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-samhsa-n-ssats> WHERE { ?facility <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/TreatmentFacility> . ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/accepts_payment> ?paymentType . ?paymentType <http://www.w3.org/2000/01/rdf-schema#label> ?paymentName . FILTER(CONTAINS(LCASE(?paymentName), 'medicaid')) . ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/serves_client_type> ?clientType . ?clientType <http://www.w3.org/2000/01/rdf-schema#label> ?clientTypeName . FILTER(CONTAINS(LCASE(?clientTypeName), 'adolescents')) . ?facility <http://www.w3.org/2000/01/rdf-schema#label> ?facilityName . }
```

**Seed 2 (wrong) — answer:** `facilityName: 11655
facilityName: 11656
facilityName: 11657
facilityName: 11658
facilityName: 11659
facilityName: 11663
facilityName: 11665
facilityName: 11669
facilityName: 11660
facilityName: 11661
facilityName: 11691
facilityName: 11692
facilityName: 11630
facilityName: 11631
facilityName: 11633
`

```sparql
SELECT DISTINCT ?facilityName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-samhsa-n-ssats> WHERE { ?facility <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/TreatmentFacility> . ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/accepts_payment> ?paymentType . ?paymentType <http://www.w3.org/2000/01/rdf-schema#label> ?paymentName . FILTER(CONTAINS(LCASE(?paymentName), 'medicaid')) . ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/serves_client_type> ?clientType . ?clientType <http://www.w3.org/2000/01/rdf-schema#label> ?clientTypeName . FILTER(CONTAINS(LCASE(?clientTypeName), 'adolescents')) . ?facility <http://www.w3.org/2000/01/rdf-schema#label> ?facilityName . }
```

**Seed 3 (wrong) — answer:** `facilityName: 11428
facilityName: 11426
facilityName: 11422
facilityName: 11425
facilityName: 11409
facilityName: 11424
facilityName: 11385
facilityName: 11418
facilityName: 11378
facilityName: 11415
facilityName: 11411
facilityName: 11419
facilityName: 11416
facilityName: 11420
facilityName: 11414
`

```sparql
SELECT DISTINCT ?facilityName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-samhsa-n-ssats> WHERE { ?facility <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/TreatmentFacility> . ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/accepts_payment> ?paymentType . ?paymentType <http://www.w3.org/2000/01/rdf-schema#label> ?paymentName . FILTER(CONTAINS(LCASE(?paymentName), 'medicaid')) . ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/serves_client_type> ?clientType . ?clientType <http://www.w3.org/2000/01/rdf-schema#label> ?clientTypeName . FILTER(CONTAINS(LCASE(?clientTypeName), 'adolescents')) . ?facility <http://www.w3.org/2000/01/rdf-schema#label> ?facilityName . }
```

### 7.39 `holdout-v2-samhsa-n-ssats` — q-010 (T4)

**Question:** What is the average number of distinct services offered by treatment facilities in each state?

**Expected answer:** `VA | 4.93574297188755020080
DE | 5.93877551020408163265
OH | 5.14216478190630048465
OK | 5.33846153846153846154
NV | 5.73394495412844036697
NY | 6.07270693512304250559
PA | 5.10264900662251655629
RI | 5.60317460317460317460
OR | 5.21457489878542510121
PR | 5.01980198019801980198
... (50 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?state_name (AVG(?num_services) AS ?average_services_per_facility) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-samhsa-n-ssats> WHERE { 
  ?state <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/State> .
  ?state <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/state_name> ?state_name .
  ?state <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/state_has_facility> ?facility .
  ?facility <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/TreatmentFacility> .
  {
    SELECT ?facility (COUNT(DISTINCT ?service) AS ?num_services) WHERE {
      ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/offers_service> ?service .
      ?service <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/Service> .
    } GROUP BY ?facility
  }
} GROUP BY ?state_name
```

**Seed 1 (wrong) — answer:** `stateName: MD, distinctServiceCount: 84
stateName: MA, distinctServiceCount: 84
stateName: NE, distinctServiceCount: 74
stateName: NJ, distinctServiceCount: 85
stateName: MT, distinctServiceCount: 77
stateName: MS, distinctServiceCount: 80
stateName: NM, distinctServiceCount: 79
stateName: NH, disti`

```sparql
SELECT ?stateName (COUNT(DISTINCT ?service) AS ?distinctServiceCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-samhsa-n-ssats> WHERE { ?facility <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/TreatmentFacility> . ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/located_in_state>/<http://www.w3.org/2000/01/rdf-schema#label> ?stateName . ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/offers_service> ?service . } GROUP BY ?stateName
```

**Seed 2 (wrong) — answer:** `stateName: MD, distinctServiceCount: 84
stateName: MA, distinctServiceCount: 84
stateName: NH, distinctServiceCount: 80
stateName: NE, distinctServiceCount: 74
stateName: NJ, distinctServiceCount: 85
stateName: NM, distinctServiceCount: 79
stateName: MS, distinctServiceCount: 80
stateName: MT, disti`

```sparql
SELECT ?stateName (COUNT(DISTINCT ?service) AS ?distinctServiceCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-samhsa-n-ssats> WHERE { ?facility <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/TreatmentFacility> . ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/located_in_state>/<http://www.w3.org/2000/01/rdf-schema#label> ?stateName . ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/offers_service> ?service . } GROUP BY ?stateName
```

**Seed 3 (wrong) — answer:** `stateName: MD, distinctServiceCount: 84
stateName: MA, distinctServiceCount: 84
stateName: NH, distinctServiceCount: 80
stateName: NE, distinctServiceCount: 74
stateName: NJ, distinctServiceCount: 85
stateName: NM, distinctServiceCount: 79
stateName: MS, distinctServiceCount: 80
stateName: NC, disti`

```sparql
SELECT ?stateName (COUNT(DISTINCT ?service) AS ?distinctServiceCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-samhsa-n-ssats> WHERE { ?facility <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/TreatmentFacility> . ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/located_in_state>/<http://www.w3.org/2000/01/rdf-schema#label> ?stateName . ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/offers_service> ?service . } GROUP BY ?stateName
```

### 7.40 `holdout-v2-scdb-supreme-court` — q-006 (T2)

**Question:** How many terms occurred after the year 1990?

**Expected answer:** `34`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?term_entity) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-scdb-supreme-court> WHERE {
  ?term_entity <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-scdb-supreme-court/type/Term> .
  ?term_entity <https://omnix.dev/holdout-v2/holdout-v2-scdb-supreme-court/pred/term> ?term_year .
  FILTER(xsd:integer(?term_year) > 1990)
}
```

**Seed 1 (wrong) — answer:** `0`

```sparql
SELECT (COUNT(DISTINCT ?term) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-scdb-supreme-court> WHERE { ?term <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-scdb-supreme-court/type/Term> . ?term <https://omnix.dev/holdout-v2/holdout-v2-scdb-supreme-court/pred/term> ?year . FILTER(?year > 1990) }
```

**Seed 2 (wrong) — answer:** `0`

```sparql
SELECT (COUNT(DISTINCT ?term) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-scdb-supreme-court> WHERE { ?term <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-scdb-supreme-court/type/Term> . ?term <https://omnix.dev/holdout-v2/holdout-v2-scdb-supreme-court/pred/term> ?year . FILTER(?year > 1990) }
```

**Seed 3 (wrong) — answer:** `0`

```sparql
SELECT (COUNT(DISTINCT ?term) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-scdb-supreme-court> WHERE { ?term <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-scdb-supreme-court/type/Term> . ?term <https://omnix.dev/holdout-v2/holdout-v2-scdb-supreme-court/pred/term> ?year . FILTER(?year > 1990) }
```

### 7.41 `holdout-v2-sec-edgar-10k` — q-009 (T3)

**Question:** Which filers have filed the most 10-K filings?

**Expected answer:** `CitroTech Inc. | 11
FEDERAL HOME LOAN MORTGAGE CORP | 3
SOUTHERN CO | 3
Public Storage | 2
Xerox Holdings Corp | 2
SolarWindow Technologies, Inc. | 2
Soluna Holdings, Inc | 2
FIFTH THIRD BANCORP | 2
FEDERAL AGRICULTURAL MORTGAGE CORP | 2
EPR PROPERTIES | 2`

**Gold SPARQL:**
```sparql
SELECT ?filerName (COUNT(?filing) AS ?filingCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-sec-edgar-10k> WHERE {
  ?filer <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/type/Filer> .
  ?filer <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filer_name> ?filerName .
  ?filer <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/has_filing> ?filing .
  ?filing <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/type/Filing> .
  ?filing <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filing_form_type> "10-K" .
} GROUP BY ?filerName ORDER BY DESC(?filingCount) LIMIT 10
```

**Seed 1 (wrong) — answer:** `filerName: CitroTech Inc., filingCount: 11`

```sparql
SELECT ?filerName (COUNT(?filing) AS ?filingCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-sec-edgar-10k> WHERE { ?filing <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/type/Filing> . ?filing <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filed_by> ?filer . ?filer <http://www.w3.org/2000/01/rdf-schema#label> ?filerName . FILTER(CONTAINS(?filing_form_type, "10-K")) . ?filing <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filing_form_type> ?filing_form_type . } GROUP BY ?filerName ORDER BY DESC(?filingCount) LIMIT 1
```

**Seed 2 (wrong) — answer:** `filerName: CitroTech Inc., filingCount: 11`

```sparql
SELECT ?filerName (COUNT(?filing) AS ?filingCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-sec-edgar-10k> WHERE { ?filing <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/type/Filing> . ?filing <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filed_by> ?filer . ?filer <http://www.w3.org/2000/01/rdf-schema#label> ?filerName . FILTER(CONTAINS(?filing_form_type, "10-K")) . ?filing <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filing_form_type> ?filing_form_type . } GROUP BY ?filerName ORDER BY DESC(?filingCount) LIMIT 1
```

**Seed 3 (wrong) — answer:** `filerName: CitroTech Inc., filingCount: 11`

```sparql
SELECT ?filerName (COUNT(?filing) AS ?filingCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-sec-edgar-10k> WHERE { ?filing <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/type/Filing> . ?filing <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filed_by> ?filer . ?filer <http://www.w3.org/2000/01/rdf-schema#label> ?filerName . FILTER(CONTAINS(?filing_form_type, "10-K")) . ?filing <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filing_form_type> ?filing_form_type . } GROUP BY ?filerName ORDER BY DESC(?filingCount) LIMIT 1
```

### 7.42 `holdout-v2-sec-edgar-10k` — q-011 (T4)

**Question:** Find the names of filers who have filed at least one '10-K' form and are incorporated in a state with 'A' in its name, along with the count of such filings.

**Expected answer:** `ENBRIDGE INC | 2
PROG Holdings, Inc. | 1
HERBALIFE LTD. | 1
NOVAGOLD RESOURCES INC | 1
HERITAGE COMMERCE CORP | 1
Community West Bancshares | 1
Triton International Ltd | 1
SOUTHERN CALIFORNIA GAS CO | 1
NSTAR ELECTRIC CO | 1
DYNEX CAPITAL INC | 1`

**Gold SPARQL:**
```sparql
SELECT ?filer_name (COUNT(DISTINCT ?filing) AS ?num_10k_filings) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-sec-edgar-10k> WHERE {
  ?filer <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/type/Filer> .
  ?filer <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filer_name> ?filer_name .
  ?filer <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/has_filing> ?filing .
  ?filing <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/type/Filing> .
  ?filing <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filing_form_type> "10-K" .
  ?filer <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filer_incorporated_in> ?state_of_incorporation .
  ?state_of_incorporation <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/type/StateOfIncorporation> .
  ?state_of_incorporation <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/state_name> ?state_name .
  FILTER (CONTAINS(LCASE(STR(?state_name)), "a"))
} GROUP BY ?filer_name
HAVING (COUNT(DISTINCT ?filing) > 0)
ORDER BY DESC(?num_10k_filings)
LIMIT 10
```

**Seed 1 (error) — answer:** `HTTP_500`

```sparql
SELECT DISTINCT ?filerName ?filingCount FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-sec-edgar-10k> WHERE { ?filer <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/type/Filer> . ?filer <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filer_name> ?filerName . ?filer <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filer_incorporated_in> ?state . ?state <http://www.w3.org/2000/01/rdf-schema#label> ?stateName . FILTER(CONTAINS(LCASE(?stateName), "a")) . ?filing <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filed_by> ?filer . ?filing <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filing_form_type> "10-K" . { SELECT ?filer (COUNT(?filing) AS ?filingCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-sec-edgar-10k> WHERE { ?filing <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filed_by> ?filer . ?filing <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filing_form_type> "10-K" } } }
```

**Seed 2 (error) — answer:** `HTTP_500`

```sparql
SELECT DISTINCT ?filerName ?filingCount FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-sec-edgar-10k> WHERE { ?filer <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/type/Filer> . ?filer <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filer_name> ?filerName . ?filer <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filer_incorporated_in> ?state . ?state <http://www.w3.org/2000/01/rdf-schema#label> ?stateName . FILTER(CONTAINS(LCASE(?stateName), "a")) . ?filing <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filed_by> ?filer . ?filing <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filing_form_type> "10-K" . { SELECT ?filer (COUNT(?filing) AS ?filingCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-sec-edgar-10k> WHERE { ?filing <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filed_by> ?filer . ?filing <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filing_form_type> "10-K" } } }
```

**Seed 3 (error) — answer:** `HTTP_500`

```sparql
SELECT DISTINCT ?filerName ?filingCount FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-sec-edgar-10k> WHERE { ?filer <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/type/Filer> . ?filer <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filer_name> ?filerName . ?filer <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filer_incorporated_in> ?state . ?state <http://www.w3.org/2000/01/rdf-schema#label> ?stateName . FILTER(CONTAINS(LCASE(?stateName), "a")) . ?filing <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filed_by> ?filer . ?filing <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filing_form_type> "10-K" . { SELECT ?filer (COUNT(?filing) AS ?filingCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-sec-edgar-10k> WHERE { ?filing <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filed_by> ?filer . ?filing <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filing_form_type> "10-K" } } }
```

### 7.43 `holdout-v2-uspto-trademarks` — q-007 (T2)

**Question:** Count the number of NiceClasses with a class code greater than '030'.

**Expected answer:** `15`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?niceClass) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-uspto-trademarks> WHERE {
  ?niceClass <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/NiceClass> .
  ?niceClass <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/class_code> ?classCode .
  FILTER(xsd:integer(?classCode) > 30)
}
```

**Seed 1 (wrong) — answer:** `16`

```sparql
SELECT (COUNT(DISTINCT ?niceClass) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-uspto-trademarks> WHERE { ?niceClass <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/NiceClass> . ?niceClass <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/class_code> ?classCode . FILTER (?classCode > "030") }
```

**Seed 2 (wrong) — answer:** `16`

```sparql
SELECT (COUNT(DISTINCT ?niceClass) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-uspto-trademarks> WHERE { ?niceClass <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/NiceClass> . ?niceClass <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/class_code> ?classCode . FILTER (?classCode > "030") }
```

**Seed 3 (wrong) — answer:** `16`

```sparql
SELECT (COUNT(DISTINCT ?niceClass) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-uspto-trademarks> WHERE { ?niceClass <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/NiceClass> . ?niceClass <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/class_code> ?classCode . FILTER(?classCode > "030") }
```

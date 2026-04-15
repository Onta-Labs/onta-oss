# Holdout v2.0 Baseline Eval Report

- **Model:** google/gemini-3-flash-preview @ T=0
- **Tenant:** pandas-sandbox
- **Seeds:** 1, 2, 3  (3 independent /ask calls per question)
- **Questions:** 302  |  **KGs:** 26  |  **Total /ask calls:** 906
- **Global exclude_questions size:** 0
- **Wall clock:** 1643.5s (27.4 min)
- **Estimated LLM cost:** ~$1.47 (@ ~2500 in / ~350 out tokens per call, Gemini 2.5 Flash OpenRouter pricing)

## 1. Headline Accuracy

**76.0% [73.2, 78.7]**  (689/906 seed-level correct)

Majority-vote headline (per-question, n=302): **77.2% [72.1, 81.5]**

## 2. Per-Domain Accuracy

| Domain | Correct / n | Accuracy (Wilson 95% CI) |
|---|---|---|
| finance | 148/177 | 83.6% [77.5, 88.3] |
| healthcare | 182/246 | 74.0% [68.2, 79.1] |
| legal | 183/252 | 72.6% [66.8, 77.8] |
| scientific_public_sector | 176/231 | 76.2% [70.3, 81.2] |

## 3. Per-Tier Accuracy

| Tier | Correct / n | Accuracy (Wilson 95% CI) |
|---|---|---|
| T1 | 246/312 | 78.8% [74.0, 83.0] |
| T2 | 183/234 | 78.2% [72.5, 83.0] |
| T3 | 151/204 | 74.0% [67.6, 79.6] |
| T4 | 109/156 | 69.9% [62.3, 76.5] |

## 4. Per-KG Accuracy (sorted descending)

| KG | Domain | Correct / n | Accuracy (Wilson 95% CI) |
|---|---|---|---|
| holdout-v2-cdc-fluview | healthcare | 39/39 | 100.0% [91.0, 100.0] |
| holdout-v2-nih-reporter-non-clinical | scientific_public_sector | 24/24 | 100.0% [86.2, 100.0] |
| holdout-v2-noaa-storm-events | scientific_public_sector | 33/33 | 100.0% [89.6, 100.0] |
| holdout-v2-scdb-supreme-court | legal | 33/33 | 100.0% [89.6, 100.0] |
| holdout-v2-ofr-financial-stability | finance | 36/39 | 92.3% [79.7, 97.3] |
| holdout-v2-fec-enforcement | legal | 33/36 | 91.7% [78.2, 97.1] |
| holdout-v2-sec-edgar-10k | finance | 33/36 | 91.7% [78.2, 97.1] |
| holdout-v2-npi-registry | healthcare | 30/33 | 90.9% [76.4, 96.9] |
| holdout-v2-epa-water-quality-portal | scientific_public_sector | 35/39 | 89.7% [76.4, 95.9] |
| holdout-v2-usda-agricultural-statistics | scientific_public_sector | 32/36 | 88.9% [74.7, 95.6] |
| holdout-v2-cms-nursing-home-compare | healthcare | 26/30 | 86.7% [70.3, 94.7] |
| holdout-v2-medicare-part-d-pricing | healthcare | 33/39 | 84.6% [70.3, 92.8] |
| holdout-v2-doe-energy-research-grants | scientific_public_sector | 30/36 | 83.3% [68.1, 92.1] |
| holdout-v2-ftc-consent-decrees | legal | 35/42 | 83.3% [69.4, 91.7] |
| holdout-v2-uspto-trademarks | legal | 30/36 | 83.3% [68.1, 92.1] |
| holdout-v2-fdic-call-reports | finance | 24/30 | 80.0% [62.7, 90.5] |
| holdout-v2-cftc-swap-data | finance | 28/36 | 77.8% [61.9, 88.3] |
| holdout-v2-ncua-credit-union-call-reports | finance | 27/36 | 75.0% [58.9, 86.2] |
| holdout-v2-samhsa-n-ssats | healthcare | 24/33 | 72.7% [55.8, 84.9] |
| holdout-v2-pacer-federal-dockets | legal | 28/39 | 71.8% [56.2, 83.5] |
| holdout-v2-doj-enforcement-actions | legal | 21/36 | 58.3% [42.2, 72.9] |
| holdout-v2-cdc-wonder-mortality | healthcare | 21/39 | 53.8% [38.6, 68.4] |
| holdout-v2-fema-disaster-declarations-multitable | scientific_public_sector | 15/36 | 41.7% [27.1, 57.8] |
| holdout-v2-hrsa-hpsa | healthcare | 9/33 | 27.3% [15.1, 44.2] |
| holdout-v2-fema-disaster-declarations | scientific_public_sector | 7/27 | 25.9% [13.2, 44.7] |
| holdout-v2-patentsview | legal | 3/30 | 10.0% [3.5, 25.6] |

## 5. Per-Question Agreement Across 3 Seeds

| Correct runs | # questions | % |
|---|---|---|
| 3/3 | 225 | 74.5% |
| 2/3 | 6 | 2.0% |
| 1/3 | 2 | 0.7% |
| 0/3 | 69 | 22.8% |

## 6. Runtime and Cost

- Wall clock: **1643.5s** (27.39 min)
- Total /ask calls: **906**
- Estimated LLM cost: **~$1.47** (Gemini 2.5 Flash; see constants in build_baseline_report.py)

## 7. Systematic Failures (0/3 questions)

**69 / 302 questions failed on all 3 seeds.**

### 7.1 `holdout-v2-cdc-wonder-mortality` — q-001 (T1)

**Question:** How many distinct mortality records are there?

**Expected answer:** `10868`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?mortalityRecord) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-wonder-mortality> WHERE {
  ?mortalityRecord <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/type/MortalityRecord> .
}
```

**Seed 1 (error) — answer:** `ERROR_llm_HTTPStatusError`

```sparql
(none)
```

**Seed 2 (error) — answer:** `ERROR_llm_HTTPStatusError`

```sparql
(none)
```

**Seed 3 (error) — answer:** `ERROR_llm_HTTPStatusError`

```sparql
(none)
```

### 7.2 `holdout-v2-cdc-wonder-mortality` — q-002 (T1)

**Question:** List all the available years in the dataset.

**Expected answer:** `2016, 2013, 2005, 2004, 2010, 2001, 2015, 2007, 2009, 2006 ... (19 total)`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?yearLabel FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-wonder-mortality> WHERE {
  ?year <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/type/Year> .
  ?year <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/pred/year> ?yearLabel .
}
```

**Seed 1 (error) — answer:** `ERROR_llm_HTTPStatusError`

```sparql
(none)
```

**Seed 2 (error) — answer:** `ERROR_llm_HTTPStatusError`

```sparql
(none)
```

**Seed 3 (error) — answer:** `ERROR_llm_HTTPStatusError`

```sparql
(none)
```

### 7.3 `holdout-v2-cdc-wonder-mortality` — q-003 (T1)

**Question:** What are the names of all the causes of death recorded?

**Expected answer:** `Alzheimer's disease, Stroke, Suicide, Unintentional injuries, All causes, CLRD, Heart disease, Influenza and pneumonia, Kidney disease, Cancer ... (11 total)`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?causeName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-wonder-mortality> WHERE {
  ?cause <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/type/Cause> .
  ?cause <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/pred/cause_name> ?causeName .
}
```

**Seed 1 (error) — answer:** `ERROR_llm_HTTPStatusError`

```sparql
(none)
```

**Seed 2 (error) — answer:** `ERROR_llm_HTTPStatusError`

```sparql
(none)
```

**Seed 3 (error) — answer:** `ERROR_llm_HTTPStatusError`

```sparql
(none)
```

### 7.4 `holdout-v2-cdc-wonder-mortality` — q-004 (T1)

**Question:** How many distinct states are represented in the data?

**Expected answer:** `52`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?state) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-wonder-mortality> WHERE {
  ?state <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/type/State> .
}
```

**Seed 1 (error) — answer:** `ERROR_llm_HTTPStatusError`

```sparql
(none)
```

**Seed 2 (error) — answer:** `ERROR_llm_HTTPStatusError`

```sparql
(none)
```

**Seed 3 (error) — answer:** `ERROR_llm_HTTPStatusError`

```sparql
(none)
```

### 7.5 `holdout-v2-cdc-wonder-mortality` — q-005 (T2)

**Question:** How many mortality records have an age-adjusted rate greater than 100?

**Expected answer:** `2965`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?record) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-wonder-mortality> WHERE {
  ?record <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/type/MortalityRecord> .
  ?record <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/pred/age_adjusted_rate> ?rate .
  FILTER (xsd:float(?rate) > 100)
}
```

**Seed 1 (error) — answer:** `ERROR_llm_HTTPStatusError`

```sparql
(none)
```

**Seed 2 (error) — answer:** `ERROR_llm_HTTPStatusError`

```sparql
(none)
```

**Seed 3 (error) — answer:** `ERROR_llm_HTTPStatusError`

```sparql
(none)
```

### 7.6 `holdout-v2-cdc-wonder-mortality` — q-006 (T2)

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

**Seed 1 (error) — answer:** `ERROR_llm_HTTPStatusError`

```sparql
(none)
```

**Seed 2 (error) — answer:** `ERROR_llm_HTTPStatusError`

```sparql
(none)
```

**Seed 3 (error) — answer:** `ERROR_llm_HTTPStatusError`

```sparql
(none)
```

### 7.7 `holdout-v2-cftc-swap-data` — q-006 (T2)

**Question:** What are the labels of contracts with 'CHICAGO' in their exchange name?

**Expected answer:** `SOYBEAN MEAL, USD Malaysian Crude Palm Oil C, ROUGH RICE, BUTTER (CASH SETTLED), MILK, Class III, NON FAT DRY MILK, CME MILK IV, DRY WHEY, LEAN HOGS, LIVE CATTLE ... (20 total)`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?contractLabel FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cftc-swap-data> WHERE {
  ?contract <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/type/Contract> .
  ?contract <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/pred/contract_exchange> ?exchangeName .
  ?contract <http://www.w3.org/2000/01/rdf-schema#label> ?contractLabel .
  FILTER (CONTAINS(LCASE(STR(?exchangeName)), "chicago"))
}
```

**Seed 1 (error) — answer:** ``

```sparql
(none)
```

**Seed 2 (error) — answer:** ``

```sparql
(none)
```

**Seed 3 (error) — answer:** `ERROR_llm_HTTPStatusError`

```sparql
(none)
```

### 7.8 `holdout-v2-cftc-swap-data` — q-007 (T3)

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

**Seed 1 (error) — answer:** `ERROR_llm_HTTPStatusError`

```sparql
(none)
```

**Seed 2 (error) — answer:** `ERROR_llm_HTTPStatusError`

```sparql
(none)
```

**Seed 3 (error) — answer:** `ERROR_llm_HTTPStatusError`

```sparql
(none)
```

### 7.9 `holdout-v2-cms-nursing-home-compare` — q-005 (T2)

**Question:** What are the labels of inspections that had more than 20 total health deficiencies?

**Expected answer:** `2025-03-19, 2023-08-10, 2024-07-02, 2023-01-26, 2024-09-10, 2023-09-25, 2025-03-27, 2024-12-12, 2023-10-16, 2025-05-22 ... (794 total)`

**Gold SPARQL:**
```sparql
SELECT ?label FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cms-nursing-home-compare> WHERE {
  ?inspection <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/Inspection> .
  ?inspection <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/total_health_deficiencies> ?deficiencies .
  FILTER (xsd:integer(?deficiencies) > 20) .
  ?inspection <http://www.w3.org/2000/01/rdf-schema#label> ?label .
}
```

**Seed 1 (wrong) — answer:** `015133-183
015463-652
025025-693
025025-694
025036-720
025036-721
035016-741
035093-798
035094-801
035099-813
035099-814
035134-884
035140-896
035146-911
035205-981
035217-994
035232-1001
035242-1015
035242-1016
035255-1042
035255-1043
035276-1084
045143-1175
045326-1464
045326-1465
045327-1467
0453`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `015133-183
015463-652
025025-693
025025-694
025036-720
025036-721
035016-741
035093-798
035094-801
035099-813
035099-814
035134-884
035140-896
035146-911
035205-981
035217-994
035232-1001
035242-1015
035242-1016
035255-1042
035255-1043
035276-1084
045143-1175
045326-1464
045326-1465
045327-1467
0453`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `015133-183
015463-652
025025-693
025025-694
025036-720
025036-721
035016-741
035093-798
035094-801
035099-813
035099-814
035134-884
035140-896
035146-911
035205-981
035217-994
035232-1001
035242-1015
035242-1016
035255-1042
035255-1043
035276-1084
045143-1175
045326-1464
045326-1465
045327-1467
0453`

```sparql
(none)
```

### 7.10 `holdout-v2-doe-energy-research-grants` — q-002 (T1)

**Question:** List all the names of the authors.

**Expected answer:** `Delgado, Hernan [Argonne National Laboratory (ANL), Argonne, IL (United States)], Diaz, Jasey [Energetics, Inc., Columbia, MD (United States)], Forrest, David [Nexight, Silver Spring, MD (United States)], Guy, Logan [Energetics, Inc., Columbia, MD (United States)], Hasanbeigi, Ali [Global Efficiency Intelligence, LLC, St Petersburg, FL (United States)], Kamath, Dipti [Oak Ridge National Laboratory (ORNL), Oak Ridge, TN (United States)] (ORCID:0000000278739994), Liddell, Heather [Energetics, Inc., Columbia, MD (United States)], Lim, Tae [Lawrence Berkeley National Laboratory (LBNL), Berkeley, CA (United States)], Ma, Seungwook [Energetics, Inc., Columbia, MD (United States)], Okeke, Ikenna J. [Oak Ridge National Laboratory (ORNL), Oak Ridge, TN (United States)] ... (16924 total)`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?author_name FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doe-energy-research-grants> WHERE {
  ?author <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/type/Author> .
  ?author <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/author_name> ?author_name .
}
```

**Seed 1 (wrong) — answer:** `Walker, William C [ORNL] (ORCID:0000000271261019)
Hagmüller, Alex Walter (ORCID:0009000321820067)
Unich, Lorenzo [Univ. of Naples Federico II (Italy)] (ORCID:000900054936722X)
So, Wai-Man [Lawrence Livermore National Laboratory]
Maheras, Steven J. [Pacific Northwest National Laboratory (PNNL), Richl`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Walker, William C [ORNL] (ORCID:0000000271261019)
Hagmüller, Alex Walter (ORCID:0009000321820067)
Unich, Lorenzo [Univ. of Naples Federico II (Italy)] (ORCID:000900054936722X)
So, Wai-Man [Lawrence Livermore National Laboratory]
Maheras, Steven J. [Pacific Northwest National Laboratory (PNNL), Richl`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Walker, William C [ORNL] (ORCID:0000000271261019)
Hagmüller, Alex Walter (ORCID:0009000321820067)
Unich, Lorenzo [Univ. of Naples Federico II (Italy)] (ORCID:000900054936722X)
So, Wai-Man [Lawrence Livermore National Laboratory]
Maheras, Steven J. [Pacific Northwest National Laboratory (PNNL), Richl`

```sparql
(none)
```

### 7.11 `holdout-v2-doe-energy-research-grants` — q-011 (T4)

**Question:** Find the names of research organizations that performed grants sponsored by 'DOE' and published in 2018.

**Expected answer:** `Oak Ridge National Laboratory (ORNL), Oak Ridge, TN (United States), Lawrence Livermore National Laboratory (LLNL), Livermore, CA (United States), Pacific Northwest National Laboratory (PNNL), Richland, WA (United States), Idaho National Laboratory (INL), Idaho Falls, ID (United States), Univ. of Nevada, Reno, NV (United States), Desert Research Inst. (DRI), Las Vegas, NV (United States), Michigan State Univ., East Lansing, MI (United States), Desert Research Institute, Nevada University, Reno, NV (United States), Desert Research Institute, Reno, NV (United States), Nevada National Security Site (NNSS), Mercury, NV (United States) ... (13 total)`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?researchOrgName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doe-energy-research-grants> WHERE {
  ?grant <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/type/Grant> .
  ?grant <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/grant_performed_by> ?researchOrg .
  ?researchOrg <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/type/ResearchOrg> .
  ?researchOrg <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/research_org_name> ?researchOrgName .
  ?grant <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/grant_sponsored_by> ?sponsorOrg .
  ?sponsorOrg <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/type/SponsorOrg> .
  ?sponsorOrg <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/sponsor_name> ?sponsorName .
  FILTER (CONTAINS(LCASE(STR(?sponsorName)), "doe")) .
  ?grant <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/grant_in_year> ?year .
  ?year <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/type/Year> .
  ?year <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/year> "2018" .
}
```

**Seed 1 (error) — answer:** ``

```sparql
(none)
```

**Seed 2 (error) — answer:** ``

```sparql
(none)
```

**Seed 3 (error) — answer:** ``

```sparql
(none)
```

### 7.12 `holdout-v2-doj-enforcement-actions` — q-008 (T3)

**Question:** How many actions are associated with each company that is a US public company?

**Expected answer:** `Overseas Shipholding Group, Inc. | 6
Monsanto Co. | 5
UBS AG | 4
Tyson Foods, Inc. | 3
Deutsche Bank AG | 3
Wal-Mart Stores, Inc. | 3
Barclays Bank PLC | 2
Eli Lilly & Co. | 2
Credit Suisse AG | 2
Nash-Finch Co. Inc. | 2
... (329 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?company_name (COUNT(?action) AS ?action_count)
FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doj-enforcement-actions>
WHERE {
  ?company <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/type/Company> .
  ?company <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/company_has_action> ?action .
  ?action <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/type/Action> .
  ?company <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/company_is_us_public_co> "True" .
  ?company <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/company_name> ?company_name .
}
GROUP BY ?company_name
ORDER BY DESC(?action_count)
```

**Seed 1 (error) — answer:** `SANDBOX___PANDAS_AGENT_ERROR__ KeyError 'company_id'`

```sparql
(none)
```

**Seed 2 (error) — answer:** `SANDBOX___PANDAS_AGENT_ERROR__ KeyError 'company_id'`

```sparql
(none)
```

**Seed 3 (error) — answer:** `SANDBOX___PANDAS_AGENT_ERROR__ KeyError 'company_id'`

```sparql
(none)
```

### 7.13 `holdout-v2-doj-enforcement-actions` — q-009 (T3)

**Question:** Find the number of actions for each jurisdiction where the action had a probation period of more than 12 months.

**Expected answer:** `Florida - Southern District | 115
USDOJ - Tax Division | 77
USDOJ - Criminal Division - Fraud Section | 75
Missouri - Eastern District | 60
California - Central District | 57
California - Southern District | 52
New York - Southern District | 51
Louisiana - Eastern District | 50
New Jersey | 49
California - Northern District | 49
... (303 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?jurisdiction_name (COUNT(?action) AS ?action_count)
FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doj-enforcement-actions>
WHERE {
  ?action <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/type/Action> .
  ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_in_jurisdiction> ?jurisdiction .
  ?jurisdiction <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/type/Jurisdiction> .
  ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_probation_months> ?probation_months_str .
  FILTER (xsd:integer(?probation_months_str) > 12) .
  ?jurisdiction <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/jurisdiction_name> ?jurisdiction_name .
}
GROUP BY ?jurisdiction_name
ORDER BY DESC(?action_count)
```

**Seed 1 (error) — answer:** `SANDBOX___PANDAS_AGENT_ERROR__ KeyError 'jurisdiction_id'`

```sparql
(none)
```

**Seed 2 (error) — answer:** `SANDBOX___PANDAS_AGENT_ERROR__ KeyError 'jurisdiction_id'`

```sparql
(none)
```

**Seed 3 (error) — answer:** `SANDBOX___PANDAS_AGENT_ERROR__ KeyError 'jurisdiction_id'`

```sparql
(none)
```

### 7.14 `holdout-v2-doj-enforcement-actions` — q-010 (T4)

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

**Seed 1 (error) — answer:** `SANDBOX___PANDAS_AGENT_ERROR__ KeyError 'company_id'`

```sparql
(none)
```

**Seed 2 (error) — answer:** `SANDBOX___PANDAS_AGENT_ERROR__ KeyError 'company_id'`

```sparql
(none)
```

**Seed 3 (error) — answer:** `SANDBOX___PANDAS_AGENT_ERROR__ KeyError 'company_id'`

```sparql
(none)
```

### 7.15 `holdout-v2-doj-enforcement-actions` — q-011 (T4)

**Question:** For each year, what is the total number of actions where the company involved is a financial institution?

**Expected answer:** `1999 | 1
2000 | 1
2002 | 2
2003 | 6
2004 | 8
2005 | 8
2006 | 9
2007 | 9
2008 | 7
2009 | 9
... (26 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?yearValue (COUNT(?action) AS ?numberOfActions) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doj-enforcement-actions> WHERE {
  ?action <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/type/Action> .
  ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_by_company> ?company .
  ?company <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/type/Company> .
  ?company <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/company_is_financial_institution> "True" .
  ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_in_year> ?year .
  ?year <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/type/Year> .
  ?year <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/year_value> ?yearValue .
} GROUP BY ?yearValue ORDER BY ?yearValue
```

**Seed 1 (error) — answer:** `SANDBOX___PANDAS_AGENT_ERROR__ KeyError 'company_id'`

```sparql
(none)
```

**Seed 2 (error) — answer:** `SANDBOX___PANDAS_AGENT_ERROR__ KeyError 'company_id'`

```sparql
(none)
```

**Seed 3 (error) — answer:** `SANDBOX___PANDAS_AGENT_ERROR__ KeyError 'company_id'`

```sparql
(none)
```

### 7.16 `holdout-v2-doj-enforcement-actions` — q-012 (T4)

**Question:** What is the average total payment for actions associated with companies that are US public companies, grouped by the crime type?

**Expected answer:** `Fraud - Tax | 7.82368E8
Fraud - Securities | 4.9096128E8
Bank Secrecy Act | 4.8110608E8
Fraud - General | 3.37999808E8
Fraud - Health Care | 2.98837536E8
Controlled Substances / Drugs / Meth Act | 2.83150016E8
Bribery | 1.83853408E8
Antitrust | 1.65527984E8
FDCA / Pharma | 1.46727216E8
Import / Export | 1.3097652E8`

**Gold SPARQL:**
```sparql
SELECT ?crimeTypeName (AVG(xsd:float(?totalPayment)) AS ?averageTotalPayment) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doj-enforcement-actions> WHERE {
  ?action <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/type/Action> .
  ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_by_company> ?company .
  ?company <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/type/Company> .
  ?company <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/company_is_us_public_co> "True" .
  ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_for_crime> ?crimeType .
  ?crimeType <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/type/CrimeType> .
  ?crimeType <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/crimetype_name> ?crimeTypeName .
  ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_total_payment> ?totalPayment .
  FILTER (xsd:float(?totalPayment) > 0)
} GROUP BY ?crimeTypeName
ORDER BY DESC(?averageTotalPayment)
LIMIT 10
```

**Seed 1 (error) — answer:** `SANDBOX___PANDAS_AGENT_ERROR__ KeyError 'company_id'`

```sparql
(none)
```

**Seed 2 (error) — answer:** `SANDBOX___PANDAS_AGENT_ERROR__ KeyError 'company_id'`

```sparql
(none)
```

**Seed 3 (error) — answer:** `SANDBOX___PANDAS_AGENT_ERROR__ KeyError 'company_id'`

```sparql
(none)
```

### 7.17 `holdout-v2-epa-water-quality-portal` — q-011 (T4)

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

**Seed 1 (wrong) — answer:** `90.66666666666667`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `90.66666666666667`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `90.66666666666667`

```sparql
(none)
```

### 7.18 `holdout-v2-fdic-call-reports` — q-005 (T3)

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

**Seed 1 (wrong) — answer:** `10004_20240630 | 19
10011_20240630 | 19
10012_20240630 | 19
10015_20240630 | 19
10044_20240630 | 19
10055_20240630 | 19
10057_20240630 | 19
1005_20240630 | 19
10072_20240630 | 19
10074_20240630 | 19
10075_20240630 | 19
10077_20240630 | 19
1008_20240630 | 19
10101_20240630 | 19
10111_20240630 | 19
10`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `10004_20240630 | 19
10011_20240630 | 19
10012_20240630 | 19
10015_20240630 | 19
10044_20240630 | 19
10055_20240630 | 19
10057_20240630 | 19
1005_20240630 | 19
10072_20240630 | 19
10074_20240630 | 19
10075_20240630 | 19
10077_20240630 | 19
1008_20240630 | 19
10101_20240630 | 19
10111_20240630 | 19
10`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `10004_20240630 | 19
10011_20240630 | 19
10012_20240630 | 19
10015_20240630 | 19
10044_20240630 | 19
10055_20240630 | 19
10057_20240630 | 19
1005_20240630 | 19
10072_20240630 | 19
10074_20240630 | 19
10075_20240630 | 19
10077_20240630 | 19
1008_20240630 | 19
10101_20240630 | 19
10111_20240630 | 19
10`

```sparql
(none)
```

### 7.19 `holdout-v2-fdic-call-reports` — q-009 (T4)

**Question:** For each state, how many banks headquartered there have at least one financial metric with a value greater than 1000?

**Expected answer:** `Texas | 346
Illinois | 328
Minnesota | 226
Iowa | 221
Missouri | 193
Kansas | 186
Oklahoma | 172
Ohio | 154
Wisconsin | 152
Nebraska | 139
... (55 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?stateName (COUNT(DISTINCT ?bank) AS ?numberOfBanks) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fdic-call-reports> WHERE {
  ?state <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/type/State> .
  ?state <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/state_name> ?stateName .
  ?bank <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/type/Bank> .
  ?bank <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/bank_headquartered_in_state> ?state .
  ?metric <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/type/FinancialMetric> .
  ?metric <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/metric_for_bank> ?bank .
  ?metric <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/metric_value> ?metricValue .
  FILTER (xsd:float(?metricValue) > 1000)
} GROUP BY ?stateName
ORDER BY DESC(?numberOfBanks)
```

**Seed 1 (wrong) — answer:** `AK | 5
AL | 92
AR | 78
AZ | 12
CA | 118
CO | 65
CT | 28
DC | 4
DE | 16
FL | 80
FM | 1
GA | 127
GU | 2
HI | 6
IA | 230
ID | 10
IL | 329
IN | 89
KS | 188
KY | 120
LA | 106
MA | 91
MD | 28
ME | 22
MI | 73
MN | 227
MO | 196
MS | 57
MT | 36
NC | 38
ND | 61
NE | 141
NH | 17
NJ | 48
NM | 29
NV | 15
NY | 11`

```sparql
(none)
```

**Seed 2 (error) — answer:** `SANDBOX___PANDAS_AGENT_ERROR__ SyntaxError unterminated string literal (detected at line 2) (<string>, line 2)`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `AK | 5
AL | 92
AR | 78
AZ | 12
CA | 118
CO | 65
CT | 28
DC | 4
DE | 16
FL | 80
FM | 1
GA | 127
GU | 2
HI | 6
IA | 230
ID | 10
IL | 329
IN | 89
KS | 188
KY | 120
LA | 106
MA | 91
MD | 28
ME | 22
MI | 73
MN | 227
MO | 196
MS | 57
MT | 36
NC | 38
ND | 61
NE | 141
NH | 17
NJ | 48
NM | 29
NV | 15
NY | 11`

```sparql
(none)
```

### 7.20 `holdout-v2-fec-enforcement` — q-011 (T4)

**Question:** For each case, what is the average penalty of its dispositions, considering only dispositions that cite a statute?

**Expected answer:** `Hamilton County Republican Party | 0.0
The Republicans of Northeast Ohio | 0.0
New San Diego | 0.0
McCorkle for Colorado | 0.0
Support American Leaders PAC | 0.0
Joanna Weiss for Congress | 0.0
The Joshua Super PAC | 0.0
Evan Low for Congress | 0.0
Hawaii Republican Party | 0.0
X Corp. | 0.0
... (130 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?caseName (AVG(xsd:float(?penalty)) AS ?averagePenalty) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fec-enforcement> WHERE {
  ?case <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/type/Case> .
  ?case <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/case_name> ?caseName .
  ?case <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/case_has_disposition> ?disposition .
  ?disposition <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/type/Disposition> .
  ?disposition <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/disposition_penalty_usd> ?penalty .
  ?disposition <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/disposition_cites_statute> ?statute .
  ?statute <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/type/CitationStatute> .
} GROUP BY ?caseName
```

**Seed 1 (wrong) — answer:** `mur_8130 | 0.0
mur_8131 | 0.0
mur_8132 | 0.0
mur_8133 | 0.0
mur_8134 | 0.0
mur_8137 | 0.0
mur_8138 | 0.0
mur_8139 | 0.0
mur_8140 | 0.0
mur_8142 | 0.0
mur_8143 | 19000.0
mur_8144 | 0.0
mur_8145 | 0.0
mur_8146 | 0.0
mur_8148 | 0.0
mur_8149 | 0.0
mur_8150 | 0.0
mur_8153 | 0.0
mur_8155 | 0.0
mur_8158 | `

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `mur_8130 | 0.0
mur_8131 | 0.0
mur_8132 | 0.0
mur_8133 | 0.0
mur_8134 | 0.0
mur_8137 | 0.0
mur_8138 | 0.0
mur_8139 | 0.0
mur_8140 | 0.0
mur_8142 | 0.0
mur_8143 | 19000.0
mur_8144 | 0.0
mur_8145 | 0.0
mur_8146 | 0.0
mur_8148 | 0.0
mur_8149 | 0.0
mur_8150 | 0.0
mur_8153 | 0.0
mur_8155 | 0.0
mur_8158 | `

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `mur_8130 | 0.0
mur_8131 | 0.0
mur_8132 | 0.0
mur_8133 | 0.0
mur_8134 | 0.0
mur_8137 | 0.0
mur_8138 | 0.0
mur_8139 | 0.0
mur_8140 | 0.0
mur_8142 | 0.0
mur_8143 | 19000.0
mur_8144 | 0.0
mur_8145 | 0.0
mur_8146 | 0.0
mur_8148 | 0.0
mur_8149 | 0.0
mur_8150 | 0.0
mur_8153 | 0.0
mur_8155 | 0.0
mur_8158 | `

```sparql
(none)
```

### 7.21 `holdout-v2-fema-disaster-declarations` — q-001 (T1)

**Question:** How many FemaDeclarations are there?

**Expected answer:** `724`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?femaDeclaration) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations> WHERE { ?femaDeclaration <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/FemaDeclaration> . }
```

**Seed 1 (wrong) — answer:** `5173`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `5173`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `5173`

```sparql
(none)
```

### 7.22 `holdout-v2-fema-disaster-declarations` — q-003 (T1)

**Question:** How many distinct incident types are recorded in Declarations?

**Expected answer:** `21`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?incidentType) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations> WHERE { ?declaration <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/Declaration> . ?declaration <https://omnix.dev/types/Declaration/attrs/type> ?incidentType . }
```

**Seed 1 (wrong) — answer:** `27`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `27`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `27`

```sparql
(none)
```

### 7.23 `holdout-v2-fema-disaster-declarations` — q-004 (T1)

**Question:** What are the names of all counties?

**Expected answer:** `Bedford, Choctaw, Claiborne (County), Issaquena (County), Jefferson Davis (County), Leake (County), Leflore (County), Tate (County), Walthall (County), George (County) ... (2339 total)`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?countyName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations> WHERE { ?county <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/County> . ?county <https://omnix.dev/types/County/attrs/name> ?countyName . }
```

**Seed 1 (wrong) — answer:** `Washington (County)
Jefferson (County)
Deschutes (County)
Resighini Rancheria (Indian Reservation)
Autauga (County)
Barbour (County)
Blount (County)
Bullock (County)
Butler (County)
Baker (County)
Malheur (County)
Grant (County)
Morrow (County)
Umatilla (County)
Harney (County)
Gilliam (County)
Whee`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Washington (County)
Jefferson (County)
Deschutes (County)
Resighini Rancheria (Indian Reservation)
Autauga (County)
Barbour (County)
Blount (County)
Bullock (County)
Butler (County)
Baker (County)
Malheur (County)
Grant (County)
Morrow (County)
Umatilla (County)
Harney (County)
Gilliam (County)
Whee`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Washington (County)
Jefferson (County)
Deschutes (County)
Resighini Rancheria (Indian Reservation)
Autauga (County)
Barbour (County)
Blount (County)
Bullock (County)
Butler (County)
Baker (County)
Malheur (County)
Grant (County)
Morrow (County)
Umatilla (County)
Harney (County)
Gilliam (County)
Whee`

```sparql
(none)
```

### 7.24 `holdout-v2-fema-disaster-declarations` — q-005 (T3)

**Question:** How many FemaDeclarations are associated with each State, for declarations made in fiscal year 2020?

**Expected answer:** `CA | 27
OR | 17
WA | 11
AZ | 8
NV | 6
MT | 5
FL | 5
MS | 4
OK | 4
AL | 4
... (33 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?stateName (COUNT(DISTINCT ?femaDeclaration) AS ?declarationCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations> WHERE {
  ?femaDeclaration <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/FemaDeclaration> .
  ?femaDeclaration <https://omnix.dev/onto/state> ?state .
  ?state <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/State> .
  ?state <https://omnix.dev/types/State/attrs/name> ?stateName .
  ?femaDeclaration <https://omnix.dev/types/FemaDeclaration/attrs/fy_declared> "2020" .
} GROUP BY ?stateName ORDER BY DESC(?declarationCount)
```

**Seed 1 (wrong) — answer:** `AK | 2
AL | 8
AR | 5
AS | 2
AZ | 9
CA | 29
CO | 5
CT | 3
DC | 2
DE | 2
FL | 11
GA | 2
GU | 2
HI | 5
IA | 5
ID | 2
IL | 2
IN | 2
KS | 5
KY | 3
LA | 7
MA | 4
MD | 2
ME | 3
MI | 4
MN | 4
MO | 3
MP | 4
MS | 9
MT | 8
NC | 5
ND | 5
NE | 6
NH | 2
NJ | 2
NM | 13
NV | 7
NY | 5
OH | 2
OK | 20
OR | 21
PA | 2
P`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `AK | 2
AL | 8
AR | 5
AS | 2
AZ | 9
CA | 29
CO | 5
CT | 3
DC | 2
DE | 2
FL | 11
GA | 2
GU | 2
HI | 5
IA | 5
ID | 2
IL | 2
IN | 2
KS | 5
KY | 3
LA | 7
MA | 4
MD | 2
ME | 3
MI | 4
MN | 4
MO | 3
MP | 4
MS | 9
MT | 8
NC | 5
ND | 5
NE | 6
NH | 2
NJ | 2
NM | 13
NV | 7
NY | 5
OH | 2
OK | 20
OR | 21
PA | 2
P`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `AK | 2
AL | 8
AR | 5
AS | 2
AZ | 9
CA | 29
CO | 5
CT | 3
DC | 2
DE | 2
FL | 11
GA | 2
GU | 2
HI | 5
IA | 5
ID | 2
IL | 2
IN | 2
KS | 5
KY | 3
LA | 7
MA | 4
MD | 2
ME | 3
MI | 4
MN | 4
MO | 3
MP | 4
MS | 9
MT | 8
NC | 5
ND | 5
NE | 6
NH | 2
NJ | 2
NM | 13
NV | 7
NY | 5
OH | 2
OK | 20
OR | 21
PA | 2
P`

```sparql
(none)
```

### 7.25 `holdout-v2-fema-disaster-declarations` — q-007 (T3)

**Question:** Which Regions have the most FemaDeclarations where the HM program was declared?

**Expected answer:** `9 | 156
10 | 133
6 | 76
8 | 76
4 | 69
7 | 29
1 | 19
2 | 14
3 | 13
5 | 9`

**Gold SPARQL:**
```sparql
SELECT ?regionName (COUNT(DISTINCT ?femaDeclaration) AS ?declarationCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations> WHERE {
  ?femaDeclaration <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/FemaDeclaration> .
  ?femaDeclaration <https://omnix.dev/onto/region> ?region .
  ?region <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/Region> .
  ?region <https://omnix.dev/types/Region/attrs/name> ?regionName .
  ?femaDeclaration <https://omnix.dev/types/FemaDeclaration/attrs/hm_program_declared> true .
} GROUP BY ?regionName ORDER BY DESC(?declarationCount)
```

**Seed 1 (wrong) — answer:** `4 | 7483
6 | 5704
7 | 4526
5 | 2881
3 | 2282
8 | 2216
2 | 1487
1 | 1274
10 | 1112
9 | 1056`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `4 | 7483
6 | 5704
7 | 4526
5 | 2881
3 | 2282
8 | 2216
2 | 1487
1 | 1274
10 | 1112
9 | 1056`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `4 | 7483
6 | 5704
7 | 4526
5 | 2881
3 | 2282
8 | 2216
2 | 1487
1 | 1274
10 | 1112
9 | 1056`

```sparql
(none)
```

### 7.26 `holdout-v2-fema-disaster-declarations` — q-009 (T4)

**Question:** For each FEMA declaration type, what is the average number of designated counties involved?

**Expected answer:** `DR | 19.125
EM | 11.83333333333333333333
FM | 1.26720647773279352227`

**Gold SPARQL:**
```sparql
SELECT ?declaration_type (AVG(?countyCount) AS ?averageCountyCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations> WHERE {
  ?femaDeclaration <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/FemaDeclaration> .
  ?femaDeclaration <https://omnix.dev/types/FemaDeclaration/attrs/declaration_type> ?declaration_type .
  {
    SELECT ?femaDeclaration (COUNT(DISTINCT ?county) AS ?countyCount) WHERE {
      ?femaDeclaration <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/FemaDeclaration> .
      ?femaDeclaration <https://omnix.dev/onto/designated_area> ?county .
      ?county <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/County> .
    } GROUP BY ?femaDeclaration
  }
} GROUP BY ?declaration_type ORDER BY DESC(?averageCountyCount)
```

**Seed 1 (wrong) — answer:** `DR | 15.58255773871079
EM | 32.631007751937986
FM | 1.281499692685925`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `DR | 15.58255773871079
EM | 32.631007751937986
FM | 1.281499692685925`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `DR | 15.58255773871079
EM | 32.631007751937986
FM | 1.281499692685925`

```sparql
(none)
```

### 7.27 `holdout-v2-fema-disaster-declarations-multitable` — q-001 (T1)

**Question:** How many Public Assistance Projects are there?

**Expected answer:** `20000`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?paProject) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations-multitable> WHERE { ?paProject <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/PAProject> . }
```

**Seed 1 (wrong) — answer:** `50000`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `50000`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `50000`

```sparql
(none)
```

### 7.28 `holdout-v2-fema-disaster-declarations-multitable` — q-002 (T1)

**Question:** List all the names of the applicants.

**Expected answer:** `CAT Z - Management Costs, (PW# 67) PUBLIC BUILDINGS AND FACILITIES, (PW# 71) EMERGENCY PROTECTIVE MEASURES, (PW# 73) EMERGENCY PROTECTIVE MEASURES, (PW# 119) PUBLIC BUILDINGS AND FACILITIES, Vaccine Storage, (PW# 35) PUBLIC BUILDINGS AND FACILITIES, (PW# 14) EMERGENCY PROTECTIVE MEASURES, (PW# 11) PUBLIC BUILDINGS AND FACILITIES, DR4416 - City of Stamford - Roads ... (2157 total)`

**Gold SPARQL:**
```sparql
SELECT ?applicantName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations-multitable> WHERE { ?applicant <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/Applicant> . ?applicant <http://www.w3.org/2000/01/rdf-schema#label> ?applicantName . }
```

**Seed 1 (wrong) — answer:** `(PW# 1) IMMEDIATE NEEDS FUNDING
(PW# 5) Not Provided
(PW# 7) Not Provided
(PW# 8) Not Provided
(PW# 10) Not Provided
(PW# 16) Not Provided
(PW# 25) L
(PW# 27) Not Provided
(PW# 40) Not Provided
(PW# 41) Not Provided
(PW# 49) Not Provided
(PW# 58) Not Provided
(PW# 1587) 30081 Building 33 - Nolan Hal`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `(PW# 1) IMMEDIATE NEEDS FUNDING
(PW# 5) Not Provided
(PW# 7) Not Provided
(PW# 8) Not Provided
(PW# 10) Not Provided
(PW# 16) Not Provided
(PW# 25) L
(PW# 27) Not Provided
(PW# 40) Not Provided
(PW# 41) Not Provided
(PW# 49) Not Provided
(PW# 58) Not Provided
(PW# 1587) 30081 Building 33 - Nolan Hal`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `(PW# 1) IMMEDIATE NEEDS FUNDING
(PW# 5) Not Provided
(PW# 7) Not Provided
(PW# 8) Not Provided
(PW# 10) Not Provided
(PW# 16) Not Provided
(PW# 25) L
(PW# 27) Not Provided
(PW# 40) Not Provided
(PW# 41) Not Provided
(PW# 49) Not Provided
(PW# 58) Not Provided
(PW# 1587) 30081 Building 33 - Nolan Hal`

```sparql
(none)
```

### 7.29 `holdout-v2-fema-disaster-declarations-multitable` — q-004 (T1)

**Question:** How many distinct damage categories are there?

**Expected answer:** `9`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?damageCategory) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations-multitable> WHERE { ?damageCategory <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/DamageCategory> . }
```

**Seed 1 (wrong) — answer:** `12`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `12`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `12`

```sparql
(none)
```

### 7.30 `holdout-v2-fema-disaster-declarations-multitable` — q-005 (T2)

**Question:** How many PA Projects have a project amount greater than 5000?

**Expected answer:** `3109`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?project) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations-multitable> WHERE {
  ?project <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/PAProject> .
  ?project <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/pred/projectamount> ?amount .
  FILTER (xsd:integer(?amount) > 5000)
}
```

**Seed 1 (wrong) — answer:** `37788`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `37788`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `37788`

```sparql
(none)
```

### 7.31 `holdout-v2-fema-disaster-declarations-multitable` — q-006 (T2)

**Question:** List the labels of all Applicants whose application title contains 'School'.

**Expected answer:** `DOC CERT - Food Service Delivery Truck and School Buses, LaMarque Elementary School - Contents, (PW# 224) DEBRIS REMOVAL - SCHOOL GROUNDS AND ATHLETIC FIELDS, (PW# 478) SCHOOL PARKING LOT SURFACES AND BASE, Huffman Middle School Tractor Shed - Equipment, DR4798TX-Fort Bend Independent School District Debris Removal, Building D - Preschool and Worship Center, High School Football Field Lighting, Elementary School, Highschool Building ... (34 total)`

**Gold SPARQL:**
```sparql
SELECT ?label FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations-multitable> WHERE {
  ?applicant <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/Applicant> .
  ?applicant <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/pred/application_title> ?title .
  ?applicant <http://www.w3.org/2000/01/rdf-schema#label> ?label .
  FILTER (CONTAINS(LCASE(STR(?title)), "school"))
}
```

**Seed 1 (wrong) — answer:** `167-U8YNL-00
369-10F28-00
067-00E11-00
277-1988C-00
381-049BC-00
201-U54QL-00
457-U5TZT-00
355-11E54-00
199-104F2-00
199-14C07-00
201-UHZ3T-00
201-02CC6-00
201-U9R6G-00
201-02A7D-00
355-UZQ01-00
355-0ECFD-00
245-U866V-00
469-03500-00
361-UCRP5-00
167-02F3B-00
089-04955-00
291-UTVUV-00
071-0E614-00
0`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `167-U8YNL-00
369-10F28-00
067-00E11-00
277-1988C-00
381-049BC-00
201-U54QL-00
457-U5TZT-00
355-11E54-00
199-104F2-00
199-14C07-00
201-UHZ3T-00
201-02CC6-00
201-U9R6G-00
201-02A7D-00
355-UZQ01-00
355-0ECFD-00
245-U866V-00
469-03500-00
361-UCRP5-00
167-02F3B-00
089-04955-00
291-UTVUV-00
071-0E614-00
0`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `167-U8YNL-00
369-10F28-00
067-00E11-00
277-1988C-00
381-049BC-00
201-U54QL-00
457-U5TZT-00
355-11E54-00
199-104F2-00
199-14C07-00
201-UHZ3T-00
201-02CC6-00
201-U9R6G-00
201-02A7D-00
355-UZQ01-00
355-0ECFD-00
245-U866V-00
469-03500-00
361-UCRP5-00
167-02F3B-00
089-04955-00
291-UTVUV-00
071-0E614-00
0`

```sparql
(none)
```

### 7.32 `holdout-v2-fema-disaster-declarations-multitable` — q-007 (T2)

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

**Seed 1 (wrong) — answer:** `-3257358.3900000006`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `-3257358.3900000006`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `-3257358.3900000006`

```sparql
(none)
```

### 7.33 `holdout-v2-fema-disaster-declarations-multitable` — q-009 (T3)

**Question:** What are the total obligated amounts for PA Projects in each State?

**Expected answer:** `TX | 1.87847619E10`

**Gold SPARQL:**
```sparql
SELECT ?stateName (SUM(xsd:float(?totalObligated)) AS ?totalObligatedAmount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations-multitable> WHERE {
  ?paProject <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/PAProject> .
  ?paProject <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/pred/pa_project_in_state> ?state .
  ?state <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/State> .
  ?state <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/pred/state_name> ?stateName .
  ?paProject <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/pred/totalobligated> ?totalObligated .
  FILTER (xsd:float(?totalObligated) > 0)
} GROUP BY ?stateName ORDER BY DESC(?totalObligatedAmount) LIMIT 10
```

**Seed 1 (wrong) — answer:** `CA | 20391551245.74
TX | 20703099894.32`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `CA | 20391551245.74
TX | 20703099894.32`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `CA | 20391551245.74
TX | 20703099894.32`

```sparql
(none)
```

### 7.34 `holdout-v2-ftc-consent-decrees` — q-012 (T4)

**Question:** For each industry, how many matters are associated with it, and what is the average fiscal year of those matters?

**Expected answer:** `Manufacturing - Consumer Goods (non Food & Bev.) | 22 | 2006.22727272727272727273
Health Care - Medical Equipment/Devices | 27 | 2008.33333333333333333333
Manufacturing - Industrial Goods | 52 | 2005.71153846153846153846
Health Care - Prescription Drugs | 90 | 2008.62222222222222222222
Health Care - Hospitals/Clinics | 36 | 2011.47222222222222222222
Retail - Grocery/Supermarkets | 24 | 2004.41666666666666666667
Health Care - Retail/Pharmacies | 12 | 2003.41666666666666666667
Energy - Natural Gas | 22 | 2003.90909090909090909091
Defense - Ammunitions | 2 | 2012.5
Defense - Equipment and Engineering Services | 9 | 2000.66666666666666666667
... (34 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?industryLabel (COUNT(DISTINCT ?matter) AS ?matterCount) (AVG(xsd:integer(?fyValue)) AS ?averageFiscalYear) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ftc-consent-decrees> WHERE {
  ?industry <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/type/Industry> .
  ?industry <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/industry_label> ?industryLabel .
  ?matter <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/type/Matter> .
  ?matter <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/matter_in_industry> ?industry .
  ?matter <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/matter_in_fy> ?fiscalYear .
  ?fiscalYear <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/type/FiscalYear> .
  ?fiscalYear <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/fy_value> ?fyValue .
} GROUP BY ?industryLabel
```

**Seed 1 (wrong) — answer:** `["Defense - Ammunitions", 2, 2012.5]
["Defense - Equipment and Engineering Services", 9, 2000.6666666666667]
["Defense - Other", 1, 1996.0]
["Defense - Satellite", 1, 1996.0]
["Energy - Natural Gas", 22, 2003.909090909091]
["Energy - Other", 7, 2012.4285714285713]
["Energy - Petroleum", 25, 2005.6]
`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `["Defense - Ammunitions", 2, 2012.5]
["Defense - Equipment and Engineering Services", 9, 2000.6666666666667]
["Defense - Other", 1, 1996.0]
["Defense - Satellite", 1, 1996.0]
["Energy - Natural Gas", 22, 2003.909090909091]
["Energy - Other", 7, 2012.4285714285713]
["Energy - Petroleum", 25, 2005.6]
`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `["Defense - Ammunitions", 2, 2012.5]
["Defense - Equipment and Engineering Services", 9, 2000.6666666666667]
["Defense - Other", 1, 1996.0]
["Defense - Satellite", 1, 1996.0]
["Energy - Natural Gas", 22, 2003.909090909091]
["Energy - Other", 7, 2012.4285714285713]
["Energy - Petroleum", 25, 2005.6]
`

```sparql
(none)
```

### 7.35 `holdout-v2-ftc-consent-decrees` — q-014 (T4)

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

**Seed 1 (wrong) — answer:** `4.041666666666667`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `4.041666666666667`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `4.041666666666667`

```sparql
(none)
```

### 7.36 `holdout-v2-hrsa-hpsa` — q-001 (T1)

**Question:** How many Health Professional Shortage Areas are there?

**Expected answer:** `724`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?hpsa) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-hrsa-hpsa> WHERE { ?hpsa <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/HealthProfessionalShortageArea> . }
```

**Seed 1 (wrong) — answer:** `17538`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `17538`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `17538`

```sparql
(none)
```

### 7.37 `holdout-v2-hrsa-hpsa` — q-002 (T1)

**Question:** List all the names of organizations.

**Expected answer:** `020670, 020730, 020700, 022030, 021040, 025310, 025320, 02E00103, 021400, 022090 ... (82 total)`

**Gold SPARQL:**
```sparql
SELECT ?organizationName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-hrsa-hpsa> WHERE { ?organization <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/Organization> . ?organization <https://omnix.dev/types/Organization/attrs/name> ?organizationName . }
```

**Seed 1 (wrong) — answer:** `Republic of the Marshall Islands
GOVERNMENT OF GUAM- DEPARTMENT OF ADMINISTRATION
Southern Guam
LI - Guam
Guam
Guam Department of Correction
Guam Youth Correctional Facility
Kosrae Department of Health Services
CHUUK STATE DEPARTMENT OF HEALTH SERVICES
Pohnpei Community Health Center
YAP STATE DEPAR`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Republic of the Marshall Islands
GOVERNMENT OF GUAM- DEPARTMENT OF ADMINISTRATION
Southern Guam
LI - Guam
Guam
Guam Department of Correction
Guam Youth Correctional Facility
Kosrae Department of Health Services
CHUUK STATE DEPARTMENT OF HEALTH SERVICES
Pohnpei Community Health Center
YAP STATE DEPAR`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Republic of the Marshall Islands
GOVERNMENT OF GUAM- DEPARTMENT OF ADMINISTRATION
Southern Guam
LI - Guam
Guam
Guam Department of Correction
Guam Youth Correctional Facility
Kosrae Department of Health Services
CHUUK STATE DEPARTMENT OF HEALTH SERVICES
Pohnpei Community Health Center
YAP STATE DEPAR`

```sparql
(none)
```

### 7.38 `holdout-v2-hrsa-hpsa` — q-003 (T1)

**Question:** What are the names of all the components?

**Expected answer:** `1859, 9620, 1862, 1013, 1014, 1012.01, MARSHFIELD MEDICAL CENTER PARK FALLS CLINIC, Census Tract 1015, Census Tract 215, MARSHFIELD CLINIC CHETEK CENTER ... (2197 total)`

**Gold SPARQL:**
```sparql
SELECT ?componentName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-hrsa-hpsa> WHERE { ?component <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/Component> . ?component <https://omnix.dev/types/Component/attrs/name> ?componentName . }
```

**Seed 1 (wrong) — answer:** `Rongrik
Likiep
Rongelap
Maloelap
Ujae
Majuro
Ailinginae
Arno
Ujelang
Jemo
Utrik
Kwajalein
Wotho
Aur
Wotje
Bikar
Lae
Bikini
Lib
Ebon
Namorik
Enewetak
Namu
Mejit
Toke
Jabat
Jaluit
Mili
Erikub
Ailinglaplap
Ailuk
GOVERNMENT OF GUAM- DEPARTMENT OF ADMINISTRATION
Guam
Guam Department of Correction
Guam Yo`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Rongrik
Likiep
Rongelap
Maloelap
Ujae
Majuro
Ailinginae
Arno
Ujelang
Jemo
Utrik
Kwajalein
Wotho
Aur
Wotje
Bikar
Lae
Bikini
Lib
Ebon
Namorik
Enewetak
Namu
Mejit
Toke
Jabat
Jaluit
Mili
Erikub
Ailinglaplap
Ailuk
GOVERNMENT OF GUAM- DEPARTMENT OF ADMINISTRATION
Guam
Guam Department of Correction
Guam Yo`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Rongrik
Likiep
Rongelap
Maloelap
Ujae
Majuro
Ailinginae
Arno
Ujelang
Jemo
Utrik
Kwajalein
Wotho
Aur
Wotje
Bikar
Lae
Bikini
Lib
Ebon
Namorik
Enewetak
Namu
Mejit
Toke
Jabat
Jaluit
Mili
Erikub
Ailinglaplap
Ailuk
GOVERNMENT OF GUAM- DEPARTMENT OF ADMINISTRATION
Guam
Guam Department of Correction
Guam Yo`

```sparql
(none)
```

### 7.39 `holdout-v2-hrsa-hpsa` — q-004 (T1)

**Question:** How many unique zip codes are recorded?

**Expected answer:** `423`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?zipCode) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-hrsa-hpsa> WHERE { ?zipCode <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/ZipCode> . }
```

**Seed 1 (wrong) — answer:** `4752`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `4752`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `4752`

```sparql
(none)
```

### 7.40 `holdout-v2-hrsa-hpsa` — q-005 (T2)

**Question:** How many Health Professional Shortage Areas have a common state FIPS code of '66'?

**Expected answer:** `7`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?hpsa) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-hrsa-hpsa> WHERE {
  ?hpsa <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/HealthProfessionalShortageArea> .
  ?hpsa <https://omnix.dev/types/HealthProfessionalShortageArea/attrs/common_state_fips_code> ?fipsCode .
  FILTER(?fipsCode = "66")
}
```

**Seed 1 (wrong) — answer:** `8`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `8`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `8`

```sparql
(none)
```

### 7.41 `holdout-v2-hrsa-hpsa` — q-007 (T2)

**Question:** List the names of all HPSA entities where the component type code is 'UNK'.

**Expected answer:** `CF-Green Bay Correctional Institution, ST THOMAS EAST END MEDICAL CENTER CORPORATION, FREDERIKSTED HEALTH CARE INC, COWBOY MEDICAL GROUP PC, Powell Health Care Coalition, UPLAND HILLS HEALTH CLINIC  MONTFORT, FREDERIC CLINIC, LAFARGE MEDICAL CLINIC, WEBSTER HEALTH CENTER, BLAND CLINIC VERNON MEMORIAL HOSPITAL ... (305 total)`

**Gold SPARQL:**
```sparql
SELECT ?hpsaName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-hrsa-hpsa> WHERE {
  ?hpsa <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/Hpsa> .
  ?hpsa <https://omnix.dev/types/Hpsa/attrs/component_type_code> ?componentTypeCode .
  ?hpsa <https://omnix.dev/types/Hpsa/attrs/name> ?hpsaName .
  FILTER(?componentTypeCode = "UNK")
}
```

**Seed 1 (wrong) — answer:** `GOVERNMENT OF GUAM- DEPARTMENT OF ADMINISTRATION
Guam Department of Correction
Guam Youth Correctional Facility
Kosrae Department of Health Services
CHUUK STATE DEPARTMENT OF HEALTH SERVICES
Pohnpei Community Health Center
YAP STATE DEPARTMENT OF HEALTH SERVICES
Kosrae Community Health Center
DEPART`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `GOVERNMENT OF GUAM- DEPARTMENT OF ADMINISTRATION
Guam Department of Correction
Guam Youth Correctional Facility
Kosrae Department of Health Services
CHUUK STATE DEPARTMENT OF HEALTH SERVICES
Pohnpei Community Health Center
YAP STATE DEPARTMENT OF HEALTH SERVICES
Kosrae Community Health Center
DEPART`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `GOVERNMENT OF GUAM- DEPARTMENT OF ADMINISTRATION
Guam Department of Correction
Guam Youth Correctional Facility
Kosrae Department of Health Services
CHUUK STATE DEPARTMENT OF HEALTH SERVICES
Pohnpei Community Health Center
YAP STATE DEPARTMENT OF HEALTH SERVICES
Kosrae Community Health Center
DEPART`

```sparql
(none)
```

### 7.42 `holdout-v2-hrsa-hpsa` — q-008 (T2)

**Question:** How many HPSA entities have a degree of shortage greater than '3'?

**Expected answer:** `424`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?hpsa) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-hrsa-hpsa> WHERE {
  ?hpsa <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/Hpsa> .
  ?hpsa <https://omnix.dev/types/Hpsa/attrs/degree_of_shortage> ?degreeOfShortage .
  FILTER(STR(?degreeOfShortage) > "3")
}
```

**Seed 1 (wrong) — answer:** `69560`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `481`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `69560`

```sparql
(none)
```

### 7.43 `holdout-v2-hrsa-hpsa` — q-010 (T3)

**Question:** Count the number of Health Professional Shortage Areas for each County, where the HPSA has a 'Designated' status.

**Expected answer:** `Milwaukee County | 11
Grant County | 10
Juneau County | 8
Fremont County | 6
Monroe County | 5
Jefferson County | 5
Guam | 4
Barron County | 4
Laramie County | 4
Sawyer County | 4
... (193 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?countyName (COUNT(?hpsa) AS ?hpsaCount)
FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-hrsa-hpsa>
WHERE {
  ?hpsa <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/HealthProfessionalShortageArea> .
  ?hpsa <https://omnix.dev/onto/common_county_name> ?county .
  ?county <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/County> .
  ?county <https://omnix.dev/types/County/attrs/name> ?countyName .
  ?hpsa <https://omnix.dev/types/HealthProfessionalShortageArea/attrs/hpsa_status> "Designated" .
}
GROUP BY ?countyName
ORDER BY DESC(?hpsaCount)
```

**Seed 1 (wrong) — answer:** `Abbeville | 3
Acadia | 1
Accomack | 2
Ada | 4
Adair | 8
Adams | 81
Addison | 1
Adjuntas | 1
Aguas Buenas | 1
Aiken | 2
Ailinginae | 1
Ailinglaplap | 1
Ailuk | 1
Aimeliik | 1
Airai | 1
Aitkin | 51
Alachua | 5
Alamance | 1
Alameda | 13
Alamosa | 1
Albany | 2
Alcona | 1
Alcorn | 1
Aleutians East | 7
Al`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Abbeville County, SC | 3
Acadia Parish, LA | 1
Accomack County, VA | 2
Ada County, ID | 4
Adair County, IA | 1
Adair County, KY | 1
Adair County, MO | 4
Adair County, OK | 2
Adams County, CO | 58
Adams County, IA | 2
Adams County, ID | 1
Adams County, IL | 7
Adams County, IN | 5
Adams County, MS | 1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Abbeville County, SC | 3
Acadia Parish, LA | 1
Accomack County, VA | 2
Ada County, ID | 4
Adair County, IA | 1
Adair County, KY | 1
Adair County, MO | 4
Adair County, OK | 2
Adams County, CO | 58
Adams County, IA | 2
Adams County, ID | 1
Adams County, IL | 7
Adams County, IN | 5
Adams County, MS | 1`

```sparql
(none)
```

### 7.44 `holdout-v2-medicare-part-d-pricing` — q-002 (T1)

**Question:** List all available drug names.

**Expected answer:** `Daytrana, Deferasirox, Daypro, Daybue, Daysee, Depakote ER, Depakote Sprinkle, Dentagel, Depakote, Depo-Estradiol ... (3474 total)`

**Gold SPARQL:**
```sparql
SELECT ?drugName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing> WHERE {
  ?drug <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/Drug> .
  ?drug <http://www.w3.org/2000/01/rdf-schema#label> ?drugName .
}
```

**Seed 1 (wrong) — answer:** `1st Tier Unifine Pentips
1st Tier Unifine Pentips Plus
Abacavir
Abacavir-Lamivudine
Abilify
Abilify Asimtufii
Abilify Maintena
Abilify Mycite
Abiraterone Acetate
Abraxane
Abrysvo
Absorica
Absorica Ld
Acamprosate Calcium
Acanya
Acarbose
Accolate
Accutane
Acebutolol HCl
Acetamin-Caff-Dihydrocodeine
Ac`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `1st Tier Unifine Pentips
1st Tier Unifine Pentips Plus
Abacavir
Abacavir-Lamivudine
Abilify
Abilify Asimtufii
Abilify Maintena
Abilify Mycite
Abiraterone Acetate
Abraxane
Abrysvo
Absorica
Absorica Ld
Acamprosate Calcium
Acanya
Acarbose
Accolate
Accutane
Acebutolol HCl
Acetamin-Caff-Dihydrocodeine
Ac`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `1st Tier Unifine Pentips
1st Tier Unifine Pentips Plus
Abacavir
Abacavir-Lamivudine
Abilify
Abilify Asimtufii
Abilify Maintena
Abilify Mycite
Abiraterone Acetate
Abraxane
Abrysvo
Absorica
Absorica Ld
Acamprosate Calcium
Acanya
Acarbose
Accolate
Accutane
Acebutolol HCl
Acetamin-Caff-Dihydrocodeine
Ac`

```sparql
(none)
```

### 7.45 `holdout-v2-medicare-part-d-pricing` — q-006 (T2)

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

**Seed 1 (wrong) — answer:** `16432931.552137323`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `16432931.552137323`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `16432931.552137323`

```sparql
(none)
```

### 7.46 `holdout-v2-ncua-credit-union-call-reports` — q-001 (T1)

**Question:** How many distinct financial metrics are recorded in the system?

**Expected answer:** `46255`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?financialMetric) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ncua-credit-union-call-reports> WHERE { ?financialMetric <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/FinancialMetric> . }
```

**Seed 1 (wrong) — answer:** `10`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `10`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `10`

```sparql
(none)
```

### 7.47 `holdout-v2-ncua-credit-union-call-reports` — q-002 (T1)

**Question:** List all the names of the credit unions.

**Expected answer:** `KONE EMPLOYEES, CENTRAL CREDIT UNION OF ILLINOIS, PEORIA POSTAL EMPLOYEES, BEACON COMMUNITY, PEORIA CITY EMPLOYEES, VALLEY, COMMUNITY CREDIT UNION OF FLORIDA, TEXANS, ALABAMA, COMBINED EMPLOYEES ... (4631 total)`

**Gold SPARQL:**
```sparql
SELECT ?creditUnionName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ncua-credit-union-call-reports> WHERE { ?creditUnion <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/CreditUnion> . ?creditUnion <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/creditunion_cu_name> ?creditUnionName . }
```

**Seed 1 (wrong) — answer:** `MORRIS SHEPPARD TEXARKANA
THE NEW ORLEANS FIREMEN'S
FRANKLIN TRUST
EFCU FINANCIAL
WOODMEN
NEW HAVEN TEACHERS
WATERBURY CONNECTICUT TEACHERS
FARMERS
CENTRIS
FD COMMUNITY
FARGO PUBLIC SCHOOLS
EAGLE EXPRESS
BROADVIEW
MOTION
LEVO
SERVICE FIRST
CAMPUS
FIRST ATLANTIC
POST GAZETTE
NIAGARA FALLS TEACHERS
KE`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `MORRIS SHEPPARD TEXARKANA
THE NEW ORLEANS FIREMEN'S
FRANKLIN TRUST
EFCU FINANCIAL
WOODMEN
NEW HAVEN TEACHERS
WATERBURY CONNECTICUT TEACHERS
FARMERS
CENTRIS
FD COMMUNITY
FARGO PUBLIC SCHOOLS
EAGLE EXPRESS
BROADVIEW
MOTION
LEVO
SERVICE FIRST
CAMPUS
FIRST ATLANTIC
POST GAZETTE
NIAGARA FALLS TEACHERS
KE`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `MORRIS SHEPPARD TEXARKANA
THE NEW ORLEANS FIREMEN'S
FRANKLIN TRUST
EFCU FINANCIAL
WOODMEN
NEW HAVEN TEACHERS
WATERBURY CONNECTICUT TEACHERS
FARMERS
CENTRIS
FD COMMUNITY
FARGO PUBLIC SCHOOLS
EAGLE EXPRESS
BROADVIEW
MOTION
LEVO
SERVICE FIRST
CAMPUS
FIRST ATLANTIC
POST GAZETTE
NIAGARA FALLS TEACHERS
KE`

```sparql
(none)
```

### 7.48 `holdout-v2-ncua-credit-union-call-reports` — q-009 (T3)

**Question:** How many branches does each credit union have?

**Expected answer:** `NAVY FEDERAL CREDIT UNION | 363
STATE EMPLOYEES' | 286
AMERICA FIRST | 125
FIRST COMMUNITY | 113
MOUNTAIN AMERICA | 105
VYSTAR | 99
SUNCOAST | 79
GLOBAL | 78
SELF-HELP | 76
LAKE MICHIGAN | 75
... (4464 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?creditUnionName (COUNT(?branch) AS ?numberOfBranches) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ncua-credit-union-call-reports> WHERE {
  ?creditUnion <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/CreditUnion> .
  ?creditUnion <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/creditunion_cu_name> ?creditUnionName .
  ?creditUnion <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/has_branch> ?branch .
  ?branch <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/Branch> .
} GROUP BY ?creditUnionName ORDER BY DESC(?numberOfBranches)
```

**Seed 1 (wrong) — answer:** `MORRIS SHEPPARD TEXARKANA | 1
THE NEW ORLEANS FIREMEN'S | 10
FRANKLIN TRUST | 2
EFCU FINANCIAL | 9
WOODMEN | 1
NEW HAVEN TEACHERS | 1
WATERBURY CONNECTICUT TEACHERS | 2
FARMERS | 1
CENTRIS | 15
FD COMMUNITY | 3
FARGO PUBLIC SCHOOLS | 2
EAGLE EXPRESS | 1
BROADVIEW | 66
MOTION | 6
LEVO | 5
SERVICE FIR`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `1 | 1
6 | 10
12 | 2
13 | 9
16 | 1
19 | 1
22 | 2
26 | 1
28 | 15
42 | 3
47 | 2
48 | 1
51 | 66
53 | 6
60 | 5
77 | 3
79 | 9
82 | 5
90 | 2
98 | 1
107 | 2
108 | 1
110 | 2
119 | 1
120 | 2
132 | 1
135 | 3
138 | 1
146 | 1
147 | 1
150 | 5
151 | 23
153 | 1
159 | 25
162 | 6
167 | 13
168 | 2
169 | 2
176 | 9
187 `

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `MORRIS SHEPPARD TEXARKANA | 1
THE NEW ORLEANS FIREMEN'S | 10
FRANKLIN TRUST | 2
EFCU FINANCIAL | 9
WOODMEN | 1
NEW HAVEN TEACHERS | 1
WATERBURY CONNECTICUT TEACHERS | 2
FARMERS | 1
CENTRIS | 15
FD COMMUNITY | 3
FARGO PUBLIC SCHOOLS | 2
EAGLE EXPRESS | 1
BROADVIEW | 66
MOTION | 6
LEVO | 5
SERVICE FIR`

```sparql
(none)
```

### 7.49 `holdout-v2-npi-registry` — q-011 (T3)

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

**Seed 1 (wrong) — answer:** `Unknown | 46`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Unknown | 46`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Unknown | 46`

```sparql
(none)
```

### 7.50 `holdout-v2-ofr-financial-stability` — q-012 (T4)

**Question:** For each year, what is the average value of indicator readings that occurred on a 'Friday'?

**Expected answer:** `2000 | 1.0443207
2001 | 1.2513739
2002 | 1.2831702
2003 | 0.4425135
2004 | -0.68760645
2005 | -1.2797388
2006 | -1.2572224
2007 | -0.5077654
2008 | 3.9917865
2009 | 2.1762724
... (27 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?year (AVG(xsd:float(?readingValue)) AS ?averageReadingValue) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ofr-financial-stability> WHERE {
  ?yearEntity <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/type/Year> .
  ?yearEntity <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/year> ?year .
  ?yearEntity <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/year_contains_month> ?month .
  ?month <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/type/Month> .
  ?month <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/month_contains_period> ?period .
  ?period <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/type/Period> .
  ?period <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/day_of_week> "Friday" .
  ?period <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/period_has_reading> ?reading .
  ?reading <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/type/IndicatorReading> .
  ?reading <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/value> ?readingValue .
} GROUP BY ?year
ORDER BY ?year
```

**Seed 1 (wrong) — answer:** `2000.0 | 1.0443202614379086
2001.0 | 1.2513733333333334
2002.0 | 1.283169934640523
2003.0 | 0.4425133333333333
2004.0 | -0.6876066666666667
2005.0 | -1.2797385620915032
2006.0 | -1.257222222222222
2007.0 | -0.5077647058823529
2008.0 | 3.991786666666667
2009.0 | 2.1762721088435373
2010.0 | 0.37934666`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `2000.0 | 1.0443202614379086
2001.0 | 1.2513733333333334
2002.0 | 1.283169934640523
2003.0 | 0.4425133333333333
2004.0 | -0.6876066666666667
2005.0 | -1.2797385620915032
2006.0 | -1.257222222222222
2007.0 | -0.5077647058823529
2008.0 | 3.991786666666667
2009.0 | 2.1762721088435373
2010.0 | 0.37934666`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `2000.0 | 1.0443202614379086
2001.0 | 1.2513733333333334
2002.0 | 1.283169934640523
2003.0 | 0.4425133333333333
2004.0 | -0.6876066666666667
2005.0 | -1.2797385620915032
2006.0 | -1.257222222222222
2007.0 | -0.5077647058823529
2008.0 | 3.991786666666667
2009.0 | 2.1762721088435373
2010.0 | 0.37934666`

```sparql
(none)
```

### 7.51 `holdout-v2-pacer-federal-dockets` — q-005 (T2)

**Question:** List the docket numbers for dockets that are not blocked.

**Expected answer:** `5:26-cv-05076, 4:26-cv-04028, 4:26-cv-00356, 4:26-cv-00355, 5:26-cv-01770, 2:26-cv-03969, 3:26-cv-00115, 4:26-cv-00358, 4:26-cr-00077, 4:26-crcor-00072 ... (3877 total)`

**Gold SPARQL:**
```sparql
SELECT ?docket_number FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-pacer-federal-dockets> WHERE {
  ?docket <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/Docket> .
  ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_blocked> ?blocked_status .
  FILTER(?blocked_status = "false") .
  ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_number> ?docket_number .
}
```

**Seed 1 (wrong) — answer:** `1:26-cv-01267
Civil Action No. 2025-1012
1:26-cv-01266
1:26-cv-01265
1:26-cv-01264
1:26-cv-01263
1:26-cv-01262
1:26-cv-01260
1:26-mc-00045
1:26-cv-01261
1:26-cv-01257
Civil Action No. 2025-0547
1:25-cr-00222
1:26-cv-01251
1:26-cv-01249
1:26-cv-01223
1:26-cv-01250
1:26-cv-01248
1:26-cv-01247
1:26-cv-`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `1:26-cv-01267
Civil Action No. 2025-1012
1:26-cv-01266
1:26-cv-01265
1:26-cv-01264
1:26-cv-01263
1:26-cv-01262
1:26-cv-01260
1:26-mc-00045
1:26-cv-01261
1:26-cv-01257
Civil Action No. 2025-0547
1:25-cr-00222
1:26-cv-01251
1:26-cv-01249
1:26-cv-01223
1:26-cv-01250
1:26-cv-01248
1:26-cv-01247
1:26-cv-`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `1:26-cv-01267
Civil Action No. 2025-1012
1:26-cv-01266
1:26-cv-01265
1:26-cv-01264
1:26-cv-01263
1:26-cv-01262
1:26-cv-01260
1:26-mc-00045
1:26-cv-01261
1:26-cv-01257
Civil Action No. 2025-0547
1:25-cr-00222
1:26-cv-01251
1:26-cv-01249
1:26-cv-01223
1:26-cv-01250
1:26-cv-01248
1:26-cv-01247
1:26-cv-`

```sparql
(none)
```

### 7.52 `holdout-v2-pacer-federal-dockets` — q-008 (T3)

**Question:** Which courts have the most dockets with 'United States v.' in their case name?

**Expected answer:** `District Court, E.D. North Carolina | 38
District Court, D. Wyoming | 38
District Court, D. Arizona | 37
District Court, D. New Mexico | 35
District Court, D. Utah | 34
District Court, D. Guam | 32
District Court, S.D. California | 30
District Court, N.D. Oklahoma | 29
District Court, W.D. Texas | 29
District Court, D. South Dakota | 28`

**Gold SPARQL:**
```sparql
SELECT ?court_name (COUNT(?docket) AS ?docket_count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-pacer-federal-dockets> WHERE {
  ?docket <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/Docket> .
  ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_filed_in_court> ?court .
  ?court <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/Court> .
  ?court <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/court_full_name> ?court_name .
  ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_case_name> ?case_name .
  FILTER (CONTAINS(?case_name, "United States v."))
} GROUP BY ?court_name ORDER BY DESC(?docket_count) LIMIT 10
```

**Seed 1 (wrong) — answer:** `District Court, E.D. North Carolina
District Court, D. Wyoming`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `District Court, E.D. North Carolina
District Court, D. Wyoming`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `District Court, E.D. North Carolina
District Court, D. Wyoming`

```sparql
(none)
```

### 7.53 `holdout-v2-pacer-federal-dockets` — q-009 (T3)

**Question:** List the judges and the number of dockets they were assigned to that have a 'Civil Rights Act' cause.

**Expected answer:** `Kristi K. Dubose | 2
William M. Conley | 2
D. Edward Snow | 2
R. Austin Huffaker Jr. | 1
Colin Stirling Bruce | 1
Gregory F. VanTatenhove | 1
John A. Woodcock Jr. | 1
LANCE E. WALKER | 1
Daniel D. Crabtree | 1
Brett H. Ludwig | 1`

**Gold SPARQL:**
```sparql
SELECT ?judge_name (COUNT(?docket) AS ?docket_count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-pacer-federal-dockets> WHERE {
  ?docket <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/Docket> .
  ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_assigned_to_judge> ?judge .
  ?judge <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/Judge> .
  ?judge <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/judge_name> ?judge_name .
  ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_cause> ?cause .
  FILTER (CONTAINS(?cause, "Civil Rights Act"))
} GROUP BY ?judge_name ORDER BY DESC(?docket_count) LIMIT 10
```

**Seed 1 (error) — answer:** `SANDBOX___PANDAS_AGENT_ERROR__ SyntaxError unterminated string literal (detected at line 2) (<string>, line 2)`

```sparql
(none)
```

**Seed 2 (error) — answer:** ``

```sparql
(none)
```

**Seed 3 (error) — answer:** `SANDBOX___PANDAS_AGENT_ERROR__ SyntaxError unterminated string literal (detected at line 2) (<string>, line 2)`

```sparql
(none)
```

### 7.54 `holdout-v2-patentsview` — q-001 (T1)

**Question:** How many citations are there in the database?

**Expected answer:** `117660`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?x) AS ?n) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview> WHERE { ?x <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Citation> . }
```

**Seed 1 (wrong) — answer:** `100000`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `100000`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `100000`

```sparql
(none)
```

### 7.55 `holdout-v2-patentsview` — q-002 (T1)

**Question:** How many patents are in the database?

**Expected answer:** `92836`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?x) AS ?n) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview> WHERE { ?x <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Patent> . }
```

**Seed 1 (wrong) — answer:** `100000`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `100000`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `100000`

```sparql
(none)
```

### 7.56 `holdout-v2-patentsview` — q-003 (T1)

**Question:** How many unique CPC classes are defined in the system?

**Expected answer:** `25447`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?x) AS ?n) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview> WHERE { ?x <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/CPCClass> . }
```

**Seed 1 (wrong) — answer:** `127`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `127`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `127`

```sparql
(none)
```

### 7.57 `holdout-v2-patentsview` — q-004 (T1)

**Question:** How many inventors are listed in the database?

**Expected answer:** `14039`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?x) AS ?n) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview> WHERE { ?x <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Inventor> . }
```

**Seed 1 (wrong) — answer:** `92436`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `92436`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `92436`

```sparql
(none)
```

### 7.58 `holdout-v2-patentsview` — q-005 (T2)

**Question:** How many utility patents have more than 20 claims?

**Expected answer:** `635`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?patent) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview> WHERE { ?patent <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Patent> . ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/patent_type> "utility" . ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/num_claims> ?numClaims . FILTER(xsd:integer(?numClaims) > 20) }
```

**Seed 1 (wrong) — answer:** `15901`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `15901`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `15901`

```sparql
(none)
```

### 7.59 `holdout-v2-patentsview` — q-006 (T2)

**Question:** How many inventors are listed with a sequence number greater than 10?

**Expected answer:** `77`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?inventor) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview> WHERE { ?inventor <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Inventor> . ?inventor <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/inventor_sequence> ?seq . FILTER(xsd:integer(?seq) > 10) }
```

**Seed 1 (wrong) — answer:** `490`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `490`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `490`

```sparql
(none)
```

### 7.60 `holdout-v2-patentsview` — q-007 (T2)

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

**Seed 1 (wrong) — answer:** `Cisco Technology, Inc.
Wyse Technology L.L.C.
Montage Technology, Inc.
Innovative Micro Technology
Trax Technology Solutions Pte Ltd.
Navcom Technology, Inc.
Silergy Semiconductor Technology (Hangzhou) Ltd.
Massachusetts Institute of Technology
Lutron Technology Company LLC
Micron Technology, Inc.
D`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Cisco Technology, Inc.
Wyse Technology L.L.C.
Montage Technology, Inc.
Innovative Micro Technology
Trax Technology Solutions Pte Ltd.
Navcom Technology, Inc.
Silergy Semiconductor Technology (Hangzhou) Ltd.
Massachusetts Institute of Technology
Lutron Technology Company LLC
Micron Technology, Inc.
D`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Cisco Technology, Inc.
Wyse Technology L.L.C.
Montage Technology, Inc.
Innovative Micro Technology
Trax Technology Solutions Pte Ltd.
Navcom Technology, Inc.
Silergy Semiconductor Technology (Hangzhou) Ltd.
Massachusetts Institute of Technology
Lutron Technology Company LLC
Micron Technology, Inc.
D`

```sparql
(none)
```

### 7.61 `holdout-v2-patentsview` — q-009 (T3)

**Question:** List inventors and the number of patents they are associated with, focusing only on inventors whose last name contains 'Smith'.

**Expected answer:** `https://omnix.dev/holdout-v2/holdout-v2-patentsview/Inventor/fl%3Ami_ln%3Asmith-108 | 1
https://omnix.dev/holdout-v2/holdout-v2-patentsview/Inventor/fl%3Aja_ln%3Asmith-72 | 1
https://omnix.dev/holdout-v2/holdout-v2-patentsview/Inventor/fl%3Atr_ln%3Asmith-11 | 1
https://omnix.dev/holdout-v2/holdout-v2-patentsview/Inventor/fl%3Ada_ln%3Asmith-102 | 1
https://omnix.dev/holdout-v2/holdout-v2-patentsview/Inventor/fl%3Ada_ln%3Asmith-256 | 1
https://omnix.dev/holdout-v2/holdout-v2-patentsview/Inventor/fl%3Ama_ln%3Asmith-175 | 1
https://omnix.dev/holdout-v2/holdout-v2-patentsview/Inventor/fl%3Abr_ln%3Asmith-149 | 1
https://omnix.dev/holdout-v2/holdout-v2-patentsview/Inventor/fl%3Ach_ln%3Asmith-58 | 1
https://omnix.dev/holdout-v2/holdout-v2-patentsview/Inventor/fl%3Aro_ln%3Asmith-78 | 1
https://omnix.dev/holdout-v2/holdout-v2-patentsview/Inventor/fl%3Ary_ln%3Asmith-15 | 1`

**Gold SPARQL:**
```sparql
SELECT ?inventor (COUNT(DISTINCT ?patent) AS ?patentCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview> WHERE {
  ?inventor <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Inventor> .
  ?patent <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Patent> .
  ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/has_inventor> ?inventor .
  ?inventor <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/disambig_inventor_name_last> ?lastName .
  FILTER(CONTAINS(?lastName, "Smith"))
} GROUP BY ?inventor ORDER BY DESC(?patentCount) LIMIT 10
```

**Seed 1 (wrong) — answer:** `Adrian Edward Smith | 1
Alan Smith | 1
Alexander B. Smith | 1
Amanda Smith | 1
Amos B. Smith, III | 1
Andrew L. Smith | 1
Andrew P. Smith | 2
Andrew T Smith | 1
Anthony Smith | 1
Austin Lee Smith | 1
Barry C. Smith | 1
Barry S. Smith | 1
Benjamin A. Smith | 2
Benjamin P. Smith | 1
Benjamin Smith | 2`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Adrian Edward Smith | 1
Alan Smith | 1
Alexander B. Smith | 1
Amanda Smith | 1
Amos B. Smith, III | 1
Andrew L. Smith | 1
Andrew P. Smith | 2
Andrew T Smith | 1
Anthony Smith | 1
Austin Lee Smith | 1
Barry C. Smith | 1
Barry S. Smith | 1
Benjamin A. Smith | 2
Benjamin P. Smith | 1
Benjamin Smith | 2`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Adrian Edward Smith | 1
Alan Smith | 1
Alexander B. Smith | 1
Amanda Smith | 1
Amos B. Smith, III | 1
Andrew L. Smith | 1
Andrew P. Smith | 2
Andrew T Smith | 1
Anthony Smith | 1
Austin Lee Smith | 1
Barry C. Smith | 1
Barry S. Smith | 1
Benjamin A. Smith | 2
Benjamin P. Smith | 1
Benjamin Smith | 2`

```sparql
(none)
```

### 7.62 `holdout-v2-patentsview` — q-010 (T3)

**Question:** List the patents that have been cited by the most citations, along with the number of citations each has received, considering only patents that have at least one citation.

**Expected answer:** `https://omnix.dev/holdout-v2/holdout-v2-patentsview/Patent/10542991 | Surgical stapling system comprising a jaw attachment lockout | 955
https://omnix.dev/holdout-v2/holdout-v2-patentsview/Patent/10542974 | Surgical instrument including a control system | 611
https://omnix.dev/holdout-v2/holdout-v2-patentsview/Patent/10555769 | Flexible circuits for electrosurgical instrument | 351
https://omnix.dev/holdout-v2/holdout-v2-patentsview/Patent/10531929 | Control of robotic arm motion based on sensed load on cutting tool | 246
https://omnix.dev/holdout-v2/holdout-v2-patentsview/Patent/10574705 | Data processing and scanning systems for generating and populating a data inventory | 224
https://omnix.dev/holdout-v2/holdout-v2-patentsview/Patent/D874034 | Vehicle rear taillamp | 182
https://omnix.dev/holdout-v2/holdout-v2-patentsview/Patent/10599155 | Autonomous vehicle operation feature monitoring and evaluation of effectiveness | 132
https://omnix.dev/holdout-v2/holdout-v2-patentsview/Patent/10579028 | Systems and methods for monitoring building health | 125
https://omnix.dev/holdout-v2/holdout-v2-patentsview/Patent/10555318 | Guided wave communication system with resource allocation and methods for use therewith | 124
https://omnix.dev/holdout-v2/holdout-v2-patentsview/Patent/10552058 | Techniques for delegating data processing to a cooperative memory controller | 113`

**Gold SPARQL:**
```sparql
SELECT ?patent ?title (COUNT(DISTINCT ?citation) AS ?citationCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview> WHERE {
  ?citation <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Citation> .
  ?citation <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/citation_targets_patent> ?patent .
  ?patent <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Patent> .
  ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/patent_title> ?title .
}
GROUP BY ?patent ?title
HAVING (COUNT(DISTINCT ?citation) >= 1)
ORDER BY DESC(?citationCount)
LIMIT 10
```

**Seed 1 (wrong) — answer:** `7297977 | 13
7674650 | 13`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `7297977 | 13
7674650 | 13`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `7297977 | 13
7674650 | 13`

```sparql
(none)
```

### 7.63 `holdout-v2-samhsa-n-ssats` — q-008 (T3)

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

**Seed 1 (wrong) — answer:** `1
7
10
14
17
20
21
22
23
27
28
29
30
35
36
37
38
39
40
41
42
45
46
52
54
56
57
58
59
60
61
73
81
86
87
89
90
91
92
93
94
97
98
99
101
103
106
119
121
129
132
138
139
144
152
154
156
166
172
177
181
182
183
184
188
192
193
194
196
207
208
209
211
221
224
229
237
239
240
241
250
252
255
257
262
263
26`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `1
7
10
14
17
20
21
22
23
27
28
29
30
35
36
37
38
39
40
41
42
45
46
52
54
56
57
58
59
60
61
73
81
86
87
89
90
91
92
93
94
97
98
99
101
103
106
119
121
129
132
138
139
144
152
154
156
166
172
177
181
182
183
184
188
192
193
194
196
207
208
209
211
221
224
229
237
239
240
241
250
252
255
257
262
263
26`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `1
7
10
14
17
20
21
22
23
27
28
29
30
35
36
37
38
39
40
41
42
45
46
52
54
56
57
58
59
60
61
73
81
86
87
89
90
91
92
93
94
97
98
99
101
103
106
119
121
129
132
138
139
144
152
154
156
166
172
177
181
182
183
184
188
192
193
194
196
207
208
209
211
221
224
229
237
239
240
241
250
252
255
257
262
263
26`

```sparql
(none)
```

### 7.64 `holdout-v2-samhsa-n-ssats` — q-009 (T3)

**Question:** List the names of treatment facilities that accept 'Medicaid' as a payment type and serve 'Adolescents' as a client type.

**Expected answer:** `11692, 11592, 11618, 11621, 11622, 11624, 11631, 11633, 11636, 11688 ... (5864 total)`

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

**Seed 1 (wrong) — answer:** `1
7
10
14
17
20
21
22
23
27
28
29
30
35
36
37
38
39
40
41
42
45
46
52
54
56
57
58
59
60
61
73
81
86
87
89
90
91
92
93
94
97
98
99
101
103
106
119
121
129
132
138
139
144
152
154
156
166
172
177
181
182
183
184
188
192
193
194
196
207
208
209
211
221
224
229
237
239
240
241
250
252
255
257
262
263
26`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `1
7
10
14
17
20
21
22
23
27
28
29
30
35
36
37
38
39
40
41
42
45
46
52
54
56
57
58
59
60
61
73
81
86
87
89
90
91
92
93
94
97
98
99
101
103
106
119
121
129
132
138
139
144
152
154
156
166
172
177
181
182
183
184
188
192
193
194
196
207
208
209
211
221
224
229
237
239
240
241
250
252
255
257
262
263
26`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `1
7
10
14
17
20
21
22
23
27
28
29
30
35
36
37
38
39
40
41
42
45
46
52
54
56
57
58
59
60
61
73
81
86
87
89
90
91
92
93
94
97
98
99
101
103
106
119
121
129
132
138
139
144
152
154
156
166
172
177
181
182
183
184
188
192
193
194
196
207
208
209
211
221
224
229
237
239
240
241
250
252
255
257
262
263
26`

```sparql
(none)
```

### 7.65 `holdout-v2-samhsa-n-ssats` — q-010 (T4)

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

**Seed 1 (wrong) — answer:** `AK | 5.161904761904762
AL | 4.658064516129032
AR | 4.654970760233918
AZ | 5.427293064876958
CA | 5.368937644341801
CO | 4.745547073791348
CT | 6.066666666666666
DC | 5.785714285714286
DE | 5.938775510204081
FL | 5.582865168539326
GA | 4.968390804597701
HI | 3.770186335403727
IA | 4.52972972972973
ID`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `AK | 5.161904761904762
AL | 4.658064516129032
AR | 4.654970760233918
AZ | 5.427293064876958
CA | 5.368937644341801
CO | 4.745547073791348
CT | 6.066666666666666
DC | 5.785714285714286
DE | 5.938775510204081
FL | 5.582865168539326
GA | 4.968390804597701
HI | 3.770186335403727
IA | 4.52972972972973
ID`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `AK | 5.161904761904762
AL | 4.658064516129032
AR | 4.654970760233918
AZ | 5.427293064876958
CA | 5.368937644341801
CO | 4.745547073791348
CT | 6.066666666666666
DC | 5.785714285714286
DE | 5.938775510204081
FL | 5.582865168539326
GA | 4.968390804597701
HI | 3.770186335403727
IA | 4.52972972972973
ID`

```sparql
(none)
```

### 7.66 `holdout-v2-sec-edgar-10k` — q-005 (T2)

**Question:** How many filings have 'FORM 10-K' in their primary document description?

**Expected answer:** `100`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?filing) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-sec-edgar-10k> WHERE {
  ?filing <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/type/Filing> .
  ?filing <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filing_primary_doc_desc> ?desc .
  FILTER(CONTAINS(LCASE(STR(?desc)), "form 10-k"))
}
```

**Seed 1 (wrong) — answer:** `94`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `94`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `94`

```sparql
(none)
```

### 7.67 `holdout-v2-usda-agricultural-statistics` — q-010 (T4)

**Question:** What is the average value of observations for each commodity in the year 2020?

**Expected answer:** `SOYBEANS | 1384268.5
CAULIFLOWER | 301.97623
VEGETABLE TOTALS | 38.41781
PEAS | 5577.006
BEANS | 1835.6147
CELERY | 71.32136
PUMPKINS | 2069.6313
LETTUCE | 3382.5762
CARROTS | 391.0734
ONIONS | 8096.544
... (21 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?commodityLabel (AVG(xsd:float(?observationValue)) AS ?averageObservationValue) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-usda-agricultural-statistics> WHERE {
  ?observation <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-usda-agricultural-statistics/type/Observation> .
  ?observation <https://omnix.dev/holdout-v2/holdout-v2-usda-agricultural-statistics/pred/observation_of_commodity> ?commodity .
  ?commodity <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-usda-agricultural-statistics/type/Commodity> .
  ?commodity <http://www.w3.org/2000/01/rdf-schema#label> ?commodityLabel .
  ?observation <https://omnix.dev/holdout-v2/holdout-v2-usda-agricultural-statistics/pred/observation_in_year> ?year .
  ?year <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-usda-agricultural-statistics/type/Year> .
  ?year <https://omnix.dev/holdout-v2/holdout-v2-usda-agricultural-statistics/pred/year> "2020" .
  ?observation <https://omnix.dev/holdout-v2/holdout-v2-usda-agricultural-statistics/pred/value> ?observationValue .
} GROUP BY ?commodityLabel
```

**Seed 1 (wrong) — answer:** `ASPARAGUS | 1.4116666666666664
BEANS | 1835.6147575757577
BROCCOLI | 91.87734615384616
CABBAGE | 81.92413043478261
CARROTS | 391.0734
CAULIFLOWER | 301.9762352941176
CELERY | 71.32136363636364
CUCUMBERS | 1908.239
GARLIC | 6150.1900000000005
LETTUCE | 3382.576765625
MELONS | 3765.639190476191
ONIONS`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `ASPARAGUS | 1.4116666666666664
BEANS | 1835.6147575757577
BROCCOLI | 91.87734615384616
CABBAGE | 81.92413043478261
CARROTS | 391.0734
CAULIFLOWER | 301.9762352941176
CELERY | 71.32136363636364
CUCUMBERS | 1908.239
GARLIC | 6150.1900000000005
LETTUCE | 3382.576765625
MELONS | 3765.639190476191
ONIONS`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `ASPARAGUS | 1.4116666666666664
BEANS | 1835.6147575757577
BROCCOLI | 91.87734615384616
CABBAGE | 81.92413043478261
CARROTS | 391.0734
CAULIFLOWER | 301.9762352941176
CELERY | 71.32136363636364
CUCUMBERS | 1908.239
GARLIC | 6150.1900000000005
LETTUCE | 3382.576765625
MELONS | 3765.639190476191
ONIONS`

```sparql
(none)
```

### 7.68 `holdout-v2-uspto-trademarks` — q-007 (T2)

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
(none)
```

**Seed 2 (wrong) — answer:** `16`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `16`

```sparql
(none)
```

### 7.69 `holdout-v2-uspto-trademarks` — q-012 (T4)

**Question:** What is the average number of goods and services covered by trademarks owned by each owner?

**Expected answer:** `THE SEVENEX GROUP, LLC | 2
ASOLO S.P.A. | 2
American Century Home Fabrics, Inc. | 6
Barnes, Jan | 1
Frames & Mats R Us Inc. | 1
Koenig, Allen | 1
Caprio, Ray Vincent | 1
ABB ASEA BROWN BOVERI LTD | 1
Greene, Daniel J. | 1
ByTheZip.com Directory, Inc. | 1
... (368 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?ownerName (AVG(?numGoodsServices) AS ?averageGoodsServices) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-uspto-trademarks> WHERE {
  ?owner <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/Owner> .
  ?owner <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/owner_name> ?ownerName .
  {
    SELECT ?owner (COUNT(DISTINCT ?goodsServices) AS ?numGoodsServices) WHERE {
      ?owner <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/owner_owns_trademark> ?trademark .
      ?trademark <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/Trademark> .
      ?trademark <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/trademark_covers_goods> ?goodsServices .
      ?goodsServices <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/GoodsServices> .
    } GROUP BY ?owner
  }
} GROUP BY ?ownerName
```

**Seed 1 (wrong) — answer:** `1.6558265582655827`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `1.6558265582655827`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `1.6558265582655827`

```sparql
(none)
```

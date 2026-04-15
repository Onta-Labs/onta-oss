# Holdout v2.0 Baseline Eval Report

- **Model:** google/gemini-3-flash-preview @ T=0
- **Tenant:** demo-tenant
- **Seeds:** 1, 2, 3  (3 independent /ask calls per question)
- **Questions:** 302  |  **KGs:** 26  |  **Total /ask calls:** 906
- **Global exclude_questions size:** 0
- **Wall clock:** 170.9s (2.8 min)
- **Estimated LLM cost:** ~$1.47 (@ ~2500 in / ~350 out tokens per call, Gemini 2.5 Flash OpenRouter pricing)

## 1. Headline Accuracy

**27.7% [24.9, 30.7]**  (251/906 seed-level correct)

Majority-vote headline (per-question, n=302): **27.8% [23.1, 33.1]**

## 2. Per-Domain Accuracy

| Domain | Correct / n | Accuracy (Wilson 95% CI) |
|---|---|---|
| finance | 56/177 | 31.6% [25.2, 38.8] |
| healthcare | 72/246 | 29.3% [23.9, 35.2] |
| legal | 72/252 | 28.6% [23.3, 34.4] |
| scientific_public_sector | 51/231 | 22.1% [17.2, 27.9] |

## 3. Per-Tier Accuracy

| Tier | Correct / n | Accuracy (Wilson 95% CI) |
|---|---|---|
| T1 | 128/312 | 41.0% [35.7, 46.6] |
| T2 | 72/234 | 30.8% [25.2, 37.0] |
| T3 | 39/204 | 19.1% [14.3, 25.1] |
| T4 | 12/156 | 7.7% [4.5, 13.0] |

## 4. Per-KG Accuracy (sorted descending)

| KG | Domain | Correct / n | Accuracy (Wilson 95% CI) |
|---|---|---|---|
| holdout-v2-scdb-supreme-court | legal | 18/33 | 54.5% [38.0, 70.2] |
| holdout-v2-cdc-fluview | healthcare | 21/39 | 53.8% [38.6, 68.4] |
| holdout-v2-ofr-financial-stability | finance | 21/39 | 53.8% [38.6, 68.4] |
| holdout-v2-usda-agricultural-statistics | scientific_public_sector | 18/36 | 50.0% [34.5, 65.5] |
| holdout-v2-npi-registry | healthcare | 15/33 | 45.5% [29.8, 62.0] |
| holdout-v2-cftc-swap-data | finance | 15/36 | 41.7% [27.1, 57.8] |
| holdout-v2-samhsa-n-ssats | healthcare | 12/33 | 36.4% [22.2, 53.4] |
| holdout-v2-fec-enforcement | legal | 12/36 | 33.3% [20.2, 49.7] |
| holdout-v2-uspto-trademarks | legal | 12/36 | 33.3% [20.2, 49.7] |
| holdout-v2-cdc-wonder-mortality | healthcare | 12/39 | 30.8% [18.6, 46.4] |
| holdout-v2-epa-water-quality-portal | scientific_public_sector | 12/39 | 30.8% [18.6, 46.4] |
| holdout-v2-ftc-consent-decrees | legal | 12/42 | 28.6% [17.2, 43.6] |
| holdout-v2-doj-enforcement-actions | legal | 9/36 | 25.0% [13.8, 41.1] |
| holdout-v2-nih-reporter-non-clinical | scientific_public_sector | 6/24 | 25.0% [12.0, 44.9] |
| holdout-v2-sec-edgar-10k | finance | 9/36 | 25.0% [13.8, 41.1] |
| holdout-v2-pacer-federal-dockets | legal | 9/39 | 23.1% [12.6, 38.3] |
| holdout-v2-fema-disaster-declarations | scientific_public_sector | 6/27 | 22.2% [10.6, 40.8] |
| holdout-v2-cms-nursing-home-compare | healthcare | 6/30 | 20.0% [9.5, 37.3] |
| holdout-v2-fdic-call-reports | finance | 6/30 | 20.0% [9.5, 37.3] |
| holdout-v2-noaa-storm-events | scientific_public_sector | 6/33 | 18.2% [8.6, 34.4] |
| holdout-v2-ncua-credit-union-call-reports | finance | 5/36 | 13.9% [6.1, 28.7] |
| holdout-v2-hrsa-hpsa | healthcare | 3/33 | 9.1% [3.1, 23.6] |
| holdout-v2-fema-disaster-declarations-multitable | scientific_public_sector | 3/36 | 8.3% [2.9, 21.8] |
| holdout-v2-medicare-part-d-pricing | healthcare | 3/39 | 7.7% [2.7, 20.3] |
| holdout-v2-doe-energy-research-grants | scientific_public_sector | 0/36 | 0.0% [0.0, 9.6] |
| holdout-v2-patentsview | legal | 0/30 | 0.0% [0.0, 11.4] |

## 5. Per-Question Agreement Across 3 Seeds

| Correct runs | # questions | % |
|---|---|---|
| 3/3 | 83 | 27.5% |
| 2/3 | 1 | 0.3% |
| 1/3 | 0 | 0.0% |
| 0/3 | 218 | 72.2% |

## 6. Runtime and Cost

- Wall clock: **170.9s** (2.85 min)
- Total /ask calls: **906**
- Estimated LLM cost: **~$1.47** (Gemini 2.5 Flash; see constants in build_baseline_report.py)

## 7. Systematic Failures (0/3 questions)

**218 / 302 questions failed on all 3 seeds.**

### 7.1 `holdout-v2-cdc-fluview` — q-001 (T1)

**Question:** How many weekly reports are there in total?

**Expected answer:** `18122`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?report) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-fluview> WHERE { ?report <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/type/WeeklyReport> . }
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

### 7.2 `holdout-v2-cdc-fluview` — q-003 (T1)

**Question:** How many distinct age groups are recorded?

**Expected answer:** `6`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?age_group) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-fluview> WHERE { ?age_group <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/type/AgeGroup> . }
```

**Seed 1 (wrong) — answer:** `1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `1`

```sparql
(none)
```

### 7.3 `holdout-v2-cdc-fluview` — q-007 (T2)

**Question:** How many weekly reports have an epiweek that contains '2022'?

**Expected answer:** `3284`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?report) AS ?reportCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-fluview> WHERE {
  ?report <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/type/WeeklyReport> .
  ?report <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/epiweek> ?epiweek .
  FILTER (CONTAINS(STR(?epiweek), "2022")) .
}
```

**Seed 1 (wrong) — answer:** `0`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `0`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `0`

```sparql
(none)
```

### 7.4 `holdout-v2-cdc-fluview` — q-008 (T3)

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

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

### 7.5 `holdout-v2-cdc-fluview` — q-011 (T4)

**Question:** What are the region types and the number of distinct seasons associated with regions of that type?

**Expected answer:** `HHS Region | 6
State | 6
National | 6`

**Gold SPARQL:**
```sparql
SELECT ?regionTypeName (COUNT(DISTINCT ?season) AS ?numberOfSeasons) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-fluview> WHERE {
  ?regionType <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/type/RegionType> .
  ?regionType <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/region_type_name> ?regionTypeName .
  ?regionType <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/region_type_includes> ?region .
  ?region <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/type/Region> .
  ?region <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/region_has_report> ?report .
  ?report <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/type/WeeklyReport> .
  ?report <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/pred/in_season> ?season .
  ?season <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-fluview/type/Season> .
} GROUP BY ?regionTypeName ORDER BY DESC(?numberOfSeasons)
```

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

### 7.6 `holdout-v2-cdc-fluview` — q-013 (T4)

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

**Seed 1 (wrong) — answer:** `Insufficient rows to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Insufficient rows to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Insufficient rows to compute the exact answer.`

```sparql
(none)
```

### 7.7 `holdout-v2-cdc-wonder-mortality` — q-001 (T1)

**Question:** How many distinct mortality records are there?

**Expected answer:** `10868`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?mortalityRecord) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-wonder-mortality> WHERE {
  ?mortalityRecord <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/type/MortalityRecord> .
}
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

### 7.8 `holdout-v2-cdc-wonder-mortality` — q-003 (T1)

**Question:** What are the names of all the causes of death recorded?

**Expected answer:** `Alzheimer's disease, Stroke, Suicide, Unintentional injuries, All causes, CLRD, Heart disease, Influenza and pneumonia, Kidney disease, Cancer ... (11 total)`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?causeName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-wonder-mortality> WHERE {
  ?cause <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/type/Cause> .
  ?cause <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/pred/cause_name> ?causeName .
}
```

**Seed 1 (wrong) — answer:** `all_causes`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `all_causes`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `all_causes`

```sparql
(none)
```

### 7.9 `holdout-v2-cdc-wonder-mortality` — q-004 (T1)

**Question:** How many distinct states are represented in the data?

**Expected answer:** `52`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?state) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-wonder-mortality> WHERE {
  ?state <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/type/State> .
}
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

### 7.10 `holdout-v2-cdc-wonder-mortality` — q-005 (T2)

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

**Seed 1 (wrong) — answer:** `7`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `7`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `7`

```sparql
(none)
```

### 7.11 `holdout-v2-cdc-wonder-mortality` — q-006 (T2)

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

**Seed 1 (wrong) — answer:** `326117.4`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `326117.4`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `326117.4`

```sparql
(none)
```

### 7.12 `holdout-v2-cdc-wonder-mortality` — q-009 (T3)

**Question:** How many mortality records are associated with each state?

**Expected answer:** `Hawaii | 209
Michigan | 209
Maryland | 209
Massachusetts | 209
Maine | 209
Kentucky | 209
Louisiana | 209
New Mexico | 209
New Jersey | 209
New Hampshire | 209
... (52 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?state_name (COUNT(?record) AS ?record_count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-wonder-mortality> WHERE {
  ?record <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/type/MortalityRecord> .
  ?record <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/pred/record_in_state> ?state .
  ?state <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/type/State> .
  ?state <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/pred/state_name> ?state_name .
} GROUP BY ?state_name ORDER BY DESC(?record_count)
```

**Seed 1 (wrong) — answer:** `NE: 1, DE: 1, US: 2, IA: 1, MI: 1, WI: 1, IN: 1, MD: 1, AR: 1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `NE: 1, DE: 1, US: 2, IA: 1, MI: 1, WI: 1, IN: 1, MD: 1, AR: 1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `NE: 1, DE: 1, US: 2, IA: 1, MI: 1, WI: 1, IN: 1, MD: 1, AR: 1`

```sparql
(none)
```

### 7.13 `holdout-v2-cdc-wonder-mortality` — q-011 (T4)

**Question:** Which regions have mortality records for 'Alzheimer's disease' in '2016'?

**Expected answer:** `National, Midwest, West, South, Northeast`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?region_name FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-wonder-mortality> WHERE {
  ?mortality_record <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/type/MortalityRecord> .
  ?mortality_record <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/pred/record_for_cause> ?cause .
  ?cause <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/type/Cause> .
  ?cause <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/pred/cause_name> "Alzheimer's disease" .
  ?mortality_record <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/pred/record_in_year> ?year .
  ?year <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/type/Year> .
  ?year <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/pred/year> "2016" .
  ?mortality_record <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/pred/record_in_region> ?region .
  ?region <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/type/Region> .
  ?region <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/pred/region_name> ?region_name .
}
```

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

### 7.14 `holdout-v2-cdc-wonder-mortality` — q-012 (T4)

**Question:** Count the number of distinct causes associated with mortality records in states that are part of the 'Midwest' region.

**Expected answer:** `11`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?cause) AS ?distinct_causes_count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-wonder-mortality> WHERE {
  ?mortality_record <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/type/MortalityRecord> .
  ?mortality_record <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/pred/record_in_state> ?state .
  ?state <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/type/State> .
  ?state <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/pred/state_in_region> ?region .
  ?region <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/type/Region> .
  ?region <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/pred/region_name> "Midwest" .
  ?mortality_record <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/pred/record_for_cause> ?cause .
  ?cause <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/type/Cause> .
}
```

**Seed 1 (wrong) — answer:** `1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `1`

```sparql
(none)
```

### 7.15 `holdout-v2-cdc-wonder-mortality` — q-013 (T4)

**Question:** List the cause categories that include causes with mortality records in 'Washington' state for the year '2010'.

**Expected answer:** `Injury / External Cause, Chronic Disease, All Causes (Roll-up), Infectious Disease`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?cause_category_name FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cdc-wonder-mortality> WHERE {
  ?mortality_record <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/type/MortalityRecord> .
  ?mortality_record <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/pred/record_in_state> ?state .
  ?state <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/type/State> .
  ?state <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/pred/state_name> "Washington" .
  ?mortality_record <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/pred/record_in_year> ?year .
  ?year <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/type/Year> .
  ?year <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/pred/year> "2010" .
  ?mortality_record <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/pred/record_for_cause> ?cause .
  ?cause <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/type/Cause> .
  ?cause <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/pred/cause_in_category> ?cause_category .
  ?cause_category <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/type/CauseCategory> .
  ?cause_category <https://omnix.dev/holdout-v2/holdout-v2-cdc-wonder-mortality/pred/cause_category_name> ?cause_category_name .
}
```

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Insufficient rows to compute the exact answer.`

```sparql
(none)
```

### 7.16 `holdout-v2-cftc-swap-data` — q-001 (T1)

**Question:** How many swap positions are recorded in the dataset?

**Expected answer:** `13601`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?swapPosition) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cftc-swap-data> WHERE {
  ?swapPosition <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/type/SwapPosition> .
}
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

### 7.17 `holdout-v2-cftc-swap-data` — q-004 (T1)

**Question:** How many distinct report dates are present in the dataset?

**Expected answer:** `53`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?reportDate) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cftc-swap-data> WHERE {
  ?rd <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/type/ReportDate> .
  ?rd <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/pred/report_date> ?reportDate .
}
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

### 7.18 `holdout-v2-cftc-swap-data` — q-005 (T2)

**Question:** How many swap positions have an open interest greater than 100000?

**Expected answer:** `3993`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?swapPosition) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cftc-swap-data> WHERE {
  ?swapPosition <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/type/SwapPosition> .
  ?swapPosition <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/pred/position_open_interest> ?openInterest .
  FILTER (xsd:integer(?openInterest) > 100000)
}
```

**Seed 1 (wrong) — answer:** `6`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `6`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `6`

```sparql
(none)
```

### 7.19 `holdout-v2-cftc-swap-data` — q-007 (T3)

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

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer. (The dataset contains contract codes but does not provide the corresponding contract names).`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer. (The dataset contains contract codes but does not provide the corresponding contract names).`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer. (The dataset contains contract codes but does not provide the corresponding contract names).`

```sparql
(none)
```

### 7.20 `holdout-v2-cftc-swap-data` — q-010 (T4)

**Question:** What are the contract names and their corresponding asset classes for contracts traded on the 'CHICAGO BOARD OF TRADE'?

**Expected answer:** `WHEAT-SRW | Agriculture
WHEAT-HRW | Agriculture
CORN | Agriculture
SOYBEAN OIL | Financial
SOYBEAN MEAL | Agriculture
OATS | Energy
SOYBEANS | Energy
MINI SOYBEANS | Energy
ROUGH RICE | Metals`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?contract_name ?asset_class FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cftc-swap-data> WHERE {
  ?contract <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/type/Contract> .
  ?contract <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/pred/contract_name> ?contract_name .
  ?contract <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/pred/contract_on_exchange> ?exchange .
  ?exchange <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/type/Exchange> .
  ?exchange <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/pred/exchange_name> "CHICAGO BOARD OF TRADE" .
  ?contract <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/pred/contract_in_asset_class> ?assetClass .
  ?assetClass <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/type/AssetClass> .
  ?assetClass <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/pred/asset_class> ?asset_class .
}
```

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer. (The dataset contains contract names but does not provide a column for asset classes).`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer. (The dataset contains contract names but does not provide a column for asset classes).`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer. (The dataset contains contract names but does not provide a column for asset classes).`

```sparql
(none)
```

### 7.21 `holdout-v2-cftc-swap-data` — q-011 (T4)

**Question:** For each report year, what is the average 'position_open_interest' for swap positions associated with contracts that have a 'contract_commodity_code' of '001'?

**Expected answer:** `2024 | 241405.19`

**Gold SPARQL:**
```sparql
SELECT ?report_year (AVG(xsd:float(?open_interest)) AS ?average_open_interest) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cftc-swap-data> WHERE {
  ?position <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/type/SwapPosition> .
  ?position <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/pred/position_open_interest> ?open_interest .
  ?position <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/pred/position_for_contract> ?contract .
  ?contract <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/type/Contract> .
  ?contract <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/pred/contract_commodity_code> "001" .
  ?position <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/pred/position_on_date> ?reportDate .
  ?reportDate <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/type/ReportDate> .
  ?reportDate <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/pred/report_year> ?report_year .
} GROUP BY ?report_year
ORDER BY ?report_year
```

**Seed 1 (wrong) — answer:** `Insufficient rows to compute the exact answer. The provided rows contain `position_contract_code` but do not include the `contract_commodity_code` field required to filter the data.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Insufficient rows to compute the exact answer. The provided context contains 'position_contract_code' but does not contain the 'contract_commodity_code' field required to filter the data.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Insufficient rows to compute the exact answer. The provided rows contain `position_contract_code` but do not include the `contract_commodity_code` field required to filter the data.`

```sparql
(none)
```

### 7.22 `holdout-v2-cftc-swap-data` — q-012 (T4)

**Question:** Find the contract names for contracts that have swap positions with a 'position_swap_long' greater than 100000 and are associated with an asset class of 'Energy'.

**Expected answer:** `SOYBEANS`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?contract_name FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cftc-swap-data> WHERE {
  ?contract <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/type/Contract> .
  ?contract <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/pred/contract_name> ?contract_name .
  ?contract <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/pred/contract_has_position> ?position .
  ?position <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/type/SwapPosition> .
  ?position <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/pred/position_swap_long> ?swap_long .
  FILTER (xsd:float(?swap_long) > 100000) .
  ?contract <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/pred/contract_in_asset_class> ?assetClass .
  ?assetClass <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/type/AssetClass> .
  ?assetClass <https://omnix.dev/holdout-v2/holdout-v2-cftc-swap-data/pred/asset_class> "Energy" .
}
```

**Seed 1 (wrong) — answer:** `Insufficient rows to compute the exact answer. The provided rows do not contain an 'asset_class' column or contract names.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Insufficient rows to compute the exact answer. The provided rows do not contain an 'asset_class' column or contract names.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Insufficient rows to compute the exact answer. The provided rows do not contain an 'asset_class' column or contract names.`

```sparql
(none)
```

### 7.23 `holdout-v2-cms-nursing-home-compare` — q-001 (T1)

**Question:** How many nursing homes are there?

**Expected answer:** `14703`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?nursingHome) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cms-nursing-home-compare> WHERE {
  ?nursingHome <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/NursingHome> .
}
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

### 7.24 `holdout-v2-cms-nursing-home-compare` — q-004 (T1)

**Question:** How many unique inspection cycles have been recorded?

**Expected answer:** `3`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?inspectionCycle) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cms-nursing-home-compare> WHERE {
  ?inspection <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/Inspection> .
  ?inspection <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/inspection_cycle> ?inspectionCycle .
}
```

**Seed 1 (wrong) — answer:** `2`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `2`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `2`

```sparql
(none)
```

### 7.25 `holdout-v2-cms-nursing-home-compare` — q-005 (T2)

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

**Seed 1 (wrong) — answer:** `None of the provided rows have more than 20 total health deficiencies.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `None of the provided rows have more than 20 total health deficiencies.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `None of the provided rows have more than 20 total health deficiencies.`

```sparql
(none)
```

### 7.26 `holdout-v2-cms-nursing-home-compare` — q-006 (T2)

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

**Seed 1 (wrong) — answer:** `33`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `33`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `33`

```sparql
(none)
```

### 7.27 `holdout-v2-cms-nursing-home-compare` — q-007 (T2)

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

**Seed 1 (wrong) — answer:** `33`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `33`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `33`

```sparql
(none)
```

### 7.28 `holdout-v2-cms-nursing-home-compare` — q-008 (T3)

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

**Seed 1 (wrong) — answer:** `015075: 1, 015076: 2, 015467: 1, 045384: 1, 015468: 1, 035217: 1, 015175: 1, 015206: 1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `015075: 1, 015076: 2, 015467: 1, 045384: 1, 015468: 1, 035217: 1, 015175: 1, 015206: 1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `015075: 1, 015076: 2, 015467: 1, 045384: 1, 015468: 1, 035217: 1, 015175: 1, 015206: 1`

```sparql
(none)
```

### 7.29 `holdout-v2-cms-nursing-home-compare` — q-009 (T3)

**Question:** Which nursing homes have inspections with more than 10 total health deficiencies?

**Expected answer:** `ATTALLA CENTER FOR REHABILITATION AND NURSING, DECATUR HEALTH & REHAB CENTER, DIVERSICARE OF BESSEMER, SELF SKILLED NURSING & REHAB, BIRMINGHAM NURSING AND REHABILITATION CTR LLC, ARABELLA HEALTH AND WELLNESS OF MONTGOMERY, REGENCY HEALTH CARE AND REHABILITATION CENTER, GLENWOOD CENTER, COOSA VALLEY HEALTH AND REHAB, WILDFLOWER COURT ... (1048 total)`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?nursingHomeLabel FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cms-nursing-home-compare> WHERE {
  ?nursingHome <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/NursingHome> .
  ?nursingHome <http://www.w3.org/2000/01/rdf-schema#label> ?nursingHomeLabel .
  ?nursingHome <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/has_inspection> ?inspection .
  ?inspection <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/Inspection> .
  ?inspection <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/total_health_deficiencies> ?deficiencies .
  FILTER (xsd:integer(?deficiencies) > 10) .
}
```

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

### 7.30 `holdout-v2-cms-nursing-home-compare` — q-010 (T3)

**Question:** Count the number of citations for each nursing home that are related to 'Infection Control Deficiencies'.

**Expected answer:** `DIVERSICARE OF LANETT | 1
LAFAYETTE EXTENDED CARE | 1
MOUNDVILLE HEALTH AND REHABILITATION, LLC | 2
JACKSON HEALTH CARE FACILITY | 1
MCGUFFEY HEALTH & REHABILITATION CENTER | 2
SELF SKILLED NURSING & REHAB | 3
TWIN OAKS REHABILITATION AND HEALTHCARE CENTER | 2
DIVERSICARE OF BESSEMER | 2
TALLADEGA HEALTHCARE CENTER, INC | 2
SYLACAUGA HEALTH AND REHAB SERVICES | 2
... (185 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?nursingHomeLabel (COUNT(?citation) AS ?numberOfCitations) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cms-nursing-home-compare> WHERE {
  ?nursingHome <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/NursingHome> .
  ?nursingHome <http://www.w3.org/2000/01/rdf-schema#label> ?nursingHomeLabel .
  ?nursingHome <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/has_citation> ?citation .
  ?citation <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/Citation> .
  ?citation <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/deficiency_category> "Infection Control Deficiencies" .
} GROUP BY ?nursingHomeLabel
```

**Seed 1 (wrong) — answer:** `015028: 2, 015024: 2, 015031: 1, 015066: 1, 015065: 1, 015044: 1, 015050: 1, 015016: 1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `015028: 2, 015024: 2, 015031: 1, 015066: 1, 015065: 1, 015044: 1, 015050: 1, 015016: 1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `015028: 2, 015024: 2, 015031: 1, 015066: 1, 015065: 1, 015044: 1, 015050: 1, 015016: 1`

```sparql
(none)
```

### 7.31 `holdout-v2-doe-energy-research-grants` — q-001 (T1)

**Question:** How many grants are there in total?

**Expected answer:** `5000`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?grant) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doe-energy-research-grants> WHERE {
  ?grant <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/type/Grant> .
}
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

### 7.32 `holdout-v2-doe-energy-research-grants` — q-002 (T1)

**Question:** List all the names of the authors.

**Expected answer:** `Delgado, Hernan [Argonne National Laboratory (ANL), Argonne, IL (United States)], Diaz, Jasey [Energetics, Inc., Columbia, MD (United States)], Forrest, David [Nexight, Silver Spring, MD (United States)], Guy, Logan [Energetics, Inc., Columbia, MD (United States)], Hasanbeigi, Ali [Global Efficiency Intelligence, LLC, St Petersburg, FL (United States)], Kamath, Dipti [Oak Ridge National Laboratory (ORNL), Oak Ridge, TN (United States)] (ORCID:0000000278739994), Liddell, Heather [Energetics, Inc., Columbia, MD (United States)], Lim, Tae [Lawrence Berkeley National Laboratory (LBNL), Berkeley, CA (United States)], Ma, Seungwook [Energetics, Inc., Columbia, MD (United States)], Okeke, Ikenna J. [Oak Ridge National Laboratory (ORNL), Oak Ridge, TN (United States)] ... (16924 total)`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?author_name FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doe-energy-research-grants> WHERE {
  ?author <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/type/Author> .
  ?author <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/author_name> ?author_name .
}
```

**Seed 1 (wrong) — answer:** `Britt, David [Univ. of California (United States)], Zinn, Alfred, jain, Sourabh [The University of Texas at Austin], Blakemore, James [Univ. of Kansas, Lawrence, KS (United States)], Schmookler, Barack [Univ. of Houston, TX (United States)], Power, Philip [Univ. of California (United States)], Caput`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Britt, David [Univ. of California (United States)], Zinn, Alfred, jain, Sourabh [The University of Texas at Austin], Blakemore, James [Univ. of Kansas, Lawrence, KS (United States)], Schmookler, Barack [Univ. of Houston, TX (United States)], Power, Philip [Univ. of California (United States)], Caput`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Britt, David [Univ. of California (United States)], Zinn, Alfred, jain, Sourabh [The University of Texas at Austin], Blakemore, James [Univ. of Kansas, Lawrence, KS (United States)], Schmookler, Barack [Univ. of Houston, TX (United States)], Power, Philip [Univ. of California (United States)], Caput`

```sparql
(none)
```

### 7.33 `holdout-v2-doe-energy-research-grants` — q-003 (T1)

**Question:** How many distinct research organizations are mentioned?

**Expected answer:** `1454`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?research_org) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doe-energy-research-grants> WHERE {
  ?research_org <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/type/ResearchOrg> .
}
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

### 7.34 `holdout-v2-doe-energy-research-grants` — q-004 (T1)

**Question:** What are the different years in which grants were made?

**Expected answer:** `2024, 2016, 2013, 2015, 2018, 2020, 2017, 2014, 2019, 2021 ... (14 total)`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?year_value FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doe-energy-research-grants> WHERE {
  ?year <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/type/Year> .
  ?year <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/year> ?year_value .
}
```

**Seed 1 (wrong) — answer:** `the rows are insufficient to compute the exact answer`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `the rows are insufficient to compute the exact answer`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `the rows are insufficient to compute the exact answer`

```sparql
(none)
```

### 7.35 `holdout-v2-doe-energy-research-grants` — q-005 (T2)

**Question:** List the titles of grants published in the year 2018.

**Expected answer:** `Nevada National Security Site Radiological Control Manual, Approaches and Recommendations to Address Regulatory Gaps Identified in INL/EXT-15-36945, Evaluation of Buildings 23-531, -532, -535, and -536 in Block 17, Mercury, Area 23, Nevada National Security Site, Nye County, Nevada, Evaluation of Dell Frenzi Park, Mercury, Area 23, Nevada National Security Site, Nye County, Nevada (Cultural Resources Inventory Report), Evaluation of 1950s-Era Architectural Resources in Blocks 10, 11, and 17, Mercury, Area 23, Nevada National Security Site, Nye County, Nevada, LLNL Livermore Site Spill Prevention, Control, and Countermeasure (SPCC) Plan June 2018, Determining Unabated Airborne Radionuclide Emissions Monitoring Requirements Using Inventory-Based Methods, GALE 3.1 Verification Report, Lawrence Livermore National Laboratory Site 300 Spill Prevention, Control, and Countermeasures Plan, Control of Interface- and Mesoscopic Structure in High Performance Organic Solar Cells: Towards a Predictive Device Paradigm [and earlier project titles]. Final Technical Report for DE-FG02-98ER45737 ... (15 total)`

**Gold SPARQL:**
```sparql
SELECT ?title FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doe-energy-research-grants> WHERE {
  ?grant <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/type/Grant> .
  ?grant <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/publication_date> ?date .
  ?grant <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/title> ?title .
  FILTER(CONTAINS(STR(?date), "2018"))
}
```

**Seed 1 (wrong) — answer:** `Insufficient rows to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Insufficient rows to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Insufficient rows to compute the exact answer.`

```sparql
(none)
```

### 7.36 `holdout-v2-doe-energy-research-grants` — q-006 (T2)

**Question:** How many authors have names containing 'Smith'?

**Expected answer:** `91`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?author) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doe-energy-research-grants> WHERE {
  ?author <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/type/Author> .
  ?author <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/author_name> ?name .
  FILTER(CONTAINS(LCASE(STR(?name)), "smith"))
}
```

**Seed 1 (wrong) — answer:** `0`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `0`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `0`

```sparql
(none)
```

### 7.37 `holdout-v2-doe-energy-research-grants` — q-007 (T2)

**Question:** Find the report numbers of grants whose titles include the word 'energy'.

**Expected answer:** `DOE-OSU-AR0000794-1, DOE-MTU--0008800, DE--IE0000119, DOE-Shawnee--10782, LLNL--TR-2012039, ORNL/TM-2025/4030, LLNL--TR-2012382, None, NREL/TP--6A20-94426, NREL/TP-6A20-92819 ... (576 total)`

**Gold SPARQL:**
```sparql
SELECT ?reportNumber FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doe-energy-research-grants> WHERE {
  ?grant <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/type/Grant> .
  ?grant <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/title> ?title .
  ?grant <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/report_number> ?reportNumber .
  FILTER(CONTAINS(LCASE(STR(?title)), "energy"))
}
```

**Seed 1 (wrong) — answer:** `ORNL/TM-2025/4268, NLR/PR-6A20-99736, DOE/EE-2898, DOE-FIU-FE0031651`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `ORNL/TM-2025/4268, NLR/PR-6A20-99736, DOE/EE-2898, DOE-FIU-FE0031651`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `ORNL/TM-2025/4268, NLR/PR-6A20-99736, DOE/EE-2898, DOE-FIU-FE0031651`

```sparql
(none)
```

### 7.38 `holdout-v2-doe-energy-research-grants` — q-008 (T3)

**Question:** How many grants were sponsored by each sponsor organization?

**Expected answer:** `USDOE National Nuclear Security Administration (NNSA) | 934
USDOE | 519
USDOE Office of Nuclear Energy (NE) | 267
USDOE Office of Science (SC) | 224
USDOE Office of Energy Efficiency and Renewable Energy (EERE), Energy Efficiency Office. Building Technologies Office | 219
USDOE Laboratory Directed Research and Development (LDRD) Program | 199
USDOE Office of Energy Efficiency and Renewable Energy (EERE) | 197
USDOE Office of Energy Efficiency and Renewable Energy (EERE), Renewable Power Office. Solar Energy Technologies Office | 185
USDOE Office of Environmental Management (EM) | 177
USDOE Office of Science (SC), Basic Energy Sciences (BES) | 160`

**Gold SPARQL:**
```sparql
SELECT ?sponsor_name (COUNT(DISTINCT ?grant) AS ?grant_count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doe-energy-research-grants> WHERE {
  ?grant <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/type/Grant> .
  ?grant <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/grant_sponsored_by> ?sponsor_org .
  ?sponsor_org <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/type/SponsorOrg> .
  ?sponsor_org <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/sponsor_name> ?sponsor_name .
} GROUP BY ?sponsor_name ORDER BY DESC(?grant_count) LIMIT 10
```

**Seed 1 (wrong) — answer:** `65a86ff2fe77: 2, c8ec005dc636: 2, 81a410f64d99: 1, 84d963e203fa: 1, 4d1e667bd795: 1, 6f97bb7d823f: 1, ee3676e5f360: 1, cfe64a186844: 1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `65a86ff2fe77: 2, c8ec005dc636: 2, 81a410f64d99: 1, 84d963e203fa: 1, 4d1e667bd795: 1, 6f97bb7d823f: 1, ee3676e5f360: 1, cfe64a186844: 1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `65a86ff2fe77: 2, c8ec005dc636: 2, 81a410f64d99: 1, 84d963e203fa: 1, 4d1e667bd795: 1, 6f97bb7d823f: 1, ee3676e5f360: 1, cfe64a186844: 1`

```sparql
(none)
```

### 7.39 `holdout-v2-doe-energy-research-grants` — q-009 (T3)

**Question:** List the authors and the number of grants they are associated with for grants that are 'Technical Report' product type.

**Expected answer:** `Horsey, Ry [National Renewable Energy Laboratory (NREL), Golden, CO (United States)] (ORCID:0000000286674385) | 150
Adams, Lauren [National Renewable Energy Laboratory (NREL), Golden, CO (United States)] | 149
Praprost, Marlena [National Renewable Energy Laboratory (NREL), Golden, CO (United States)] (ORCID:0000000174638427) | 81
Parker, Andrew [National Renewable Energy Laboratory (NREL), Golden, CO (United States)] (ORCID:0000000254905134) | 79
Fontanini, Anthony [National Renewable Energy Laboratory (NREL), Golden, CO (United States)] (ORCID:0000000259219635) | 77
CaraDonna, Chris [National Renewable Energy Laboratory (NREL), Golden, CO (United States)] | 77
Van Sant, Amy [National Renewable Energy Laboratory (NREL), Golden, CO (United States)] (ORCID:0000000224973056) | 77
Klun, Lauren [National Renewable Energy Laboratory (NREL), Golden, CO (United States)] | 76
Dahlhausen, Matthew [National Renewable Energy Laboratory (NREL), Golden, CO (United States)] (ORCID:0000000313362003) | 76
Kuang, Wenyi [National Renewable Energy Laboratory (NREL), Golden, CO (United States)] | 76`

**Gold SPARQL:**
```sparql
SELECT ?author_name (COUNT(DISTINCT ?grant) AS ?grant_count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doe-energy-research-grants> WHERE {
  ?grant <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/type/Grant> .
  ?grant <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/grant_has_author> ?author .
  ?author <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/type/Author> .
  ?author <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/author_name> ?author_name .
  ?grant <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/product_type> "Technical Report" .
} GROUP BY ?author_name ORDER BY DESC(?grant_count) LIMIT 10
```

**Seed 1 (wrong) — answer:** `Insufficient rows to compute the exact answer. The provided GrantAuthor rows do not match the osti_id values of the Technical Reports in the context.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Insufficient rows to compute the exact answer. The provided context contains 8 'Technical Report' grants but only 2 GrantAuthor rows, and the osti_ids in the GrantAuthor rows (2375417, 2375009) do not match the osti_ids of the listed Technical Reports.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Insufficient rows to compute the exact answer. The provided context contains 8 'Technical Report' grants but only 2 GrantAuthor rows, and the osti_ids in the GrantAuthor rows (2375417, 2375009) do not match the osti_ids of the listed Technical Reports.`

```sparql
(none)
```

### 7.40 `holdout-v2-doe-energy-research-grants` — q-010 (T4)

**Question:** What are the titles of grants that were published in 2016 and have at least two authors?

**Expected answer:** `Girls In STEM White Coat Ceremony 2017, Technology Assessments of High Performance Envelope with Optimized Lighting, Solar Control, and Daylighting, Data Validation Package, July 2016 Groundwater Sampling at the Shirley Basin South, Wyoming, Disposal Site November 2016`

**Gold SPARQL:**
```sparql
SELECT ?grantTitle FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doe-energy-research-grants> WHERE {
  ?grant <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/type/Grant> .
  ?grant <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/title> ?grantTitle .
  ?grant <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/grant_in_year> ?year .
  ?year <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/type/Year> .
  ?year <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/year> "2016" .
  {
    SELECT ?grant (COUNT(DISTINCT ?author) AS ?authorCount) WHERE {
      ?grant <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/pred/grant_has_author> ?author .
      ?author <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doe-energy-research-grants/type/Author> .
    } GROUP BY ?grant
    HAVING (?authorCount >= 2)
  }
}
```

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

### 7.41 `holdout-v2-doe-energy-research-grants` — q-011 (T4)

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

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

### 7.42 `holdout-v2-doe-energy-research-grants` — q-012 (T4)

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

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

### 7.43 `holdout-v2-doj-enforcement-actions` — q-001 (T1)

**Question:** How many distinct companies are there?

**Expected answer:** `4935`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?company) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doj-enforcement-actions> WHERE { ?company <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/type/Company> . }
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

### 7.44 `holdout-v2-doj-enforcement-actions` — q-002 (T1)

**Question:** List all available crime types.

**Expected answer:** `Bank Secrecy Act, Antitrust, FCPA, Environmental, FDCA / Pharma, Controlled Substances / Drugs / Meth Act, Act to Prevent Pollution from Ships, Bribery, Fraud - Health Care, Fraud - Securities ... (24 total)`

**Gold SPARQL:**
```sparql
SELECT ?crimetype_name FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doj-enforcement-actions> WHERE { ?crimetype <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/type/CrimeType> . ?crimetype <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/crimetype_name> ?crimetype_name . }
```

**Seed 1 (wrong) — answer:** `Obstruction of Justice`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Obstruction of Justice`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Obstruction of Justice`

```sparql
(none)
```

### 7.45 `holdout-v2-doj-enforcement-actions` — q-004 (T1)

**Question:** How many distinct enforcement actions are recorded?

**Expected answer:** `5070`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?action) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doj-enforcement-actions> WHERE { ?action <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/type/Action> . }
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

### 7.46 `holdout-v2-doj-enforcement-actions` — q-006 (T2)

**Question:** How many actions have a probation period of more than 30 months?

**Expected answer:** `1696`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?action) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doj-enforcement-actions> WHERE {
  ?action <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/type/Action> .
  ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_probation_months> ?probationMonths .
  FILTER(xsd:integer(?probationMonths) > 30)
}
```

**Seed 1 (wrong) — answer:** `0`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `0`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `0`

```sparql
(none)
```

### 7.47 `holdout-v2-doj-enforcement-actions` — q-008 (T3)

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

**Seed 1 (wrong) — answer:** `0`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `0`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `0`

```sparql
(none)
```

### 7.48 `holdout-v2-doj-enforcement-actions` — q-009 (T3)

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

**Seed 1 (wrong) — answer:** `I am sorry, but the provided rows are insufficient to compute the exact answer. None of the actions in the retrieved context have a probation period of more than 12 months (all listed actions have `action_probation_months=0`).`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `I am sorry, but the provided rows are insufficient to compute the exact answer. None of the actions in the retrieved context have a probation period of more than 12 months (all listed actions have `action_probation_months=0`).`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `0`

```sparql
(none)
```

### 7.49 `holdout-v2-doj-enforcement-actions` — q-010 (T4)

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

**Seed 1 (wrong) — answer:** `insufficient rows`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `insufficient rows`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `insufficient rows`

```sparql
(none)
```

### 7.50 `holdout-v2-doj-enforcement-actions` — q-011 (T4)

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

**Seed 1 (wrong) — answer:** `The provided rows do not contain a column identifying whether a company is a financial institution. Therefore, the rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The provided rows do not contain a column identifying whether a company is a financial institution. Therefore, the rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The provided rows do not contain a column identifying whether a company is a financial institution. Therefore, the rows are insufficient to compute the exact answer.`

```sparql
(none)
```

### 7.51 `holdout-v2-doj-enforcement-actions` — q-012 (T4)

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

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer. (The provided context does not contain information to identify which companies are US public companies).`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Insufficient rows to determine which companies are US public companies.`

```sparql
(none)
```

### 7.52 `holdout-v2-epa-water-quality-portal` — q-001 (T1)

**Question:** How many distinct organizations are there?

**Expected answer:** `34`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?organization) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-epa-water-quality-portal> WHERE {
  ?organization <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/Organization> .
}
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

### 7.53 `holdout-v2-epa-water-quality-portal` — q-004 (T1)

**Question:** How many unique HUC8 codes are recorded?

**Expected answer:** `44`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?huc8_code) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-epa-water-quality-portal> WHERE {
  ?huc8 <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/HUC8> .
  ?huc8 <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/huc8_code> ?huc8_code .
}
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

### 7.54 `holdout-v2-epa-water-quality-portal` — q-005 (T2)

**Question:** What are the labels of organizations whose name contains 'National Park Service'?

**Expected answer:** `National Park Service Water Resources Division`

**Gold SPARQL:**
```sparql
SELECT ?label FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-epa-water-quality-portal> WHERE {
  ?organization <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/Organization> .
  ?organization <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/organization_name> ?name .
  ?organization <http://www.w3.org/2000/01/rdf-schema#label> ?label .
  FILTER(CONTAINS(LCASE(STR(?name)), "national park service"))
}
```

**Seed 1 (wrong) — answer:** `11NPSWRD_WQX`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `11NPSWRD_WQX`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `11NPSWRD_WQX`

```sparql
(none)
```

### 7.55 `holdout-v2-epa-water-quality-portal` — q-007 (T2)

**Question:** How many stations have an elevation greater than 1000?

**Expected answer:** `3`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?station) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-epa-water-quality-portal> WHERE {
  ?station <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/Station> .
  ?station <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/station_elevation> ?elevation_str .
  BIND(xsd:integer(?elevation_str) AS ?elevation) .
  FILTER(?elevation > 1000)
}
```

**Seed 1 (wrong) — answer:** `1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `1`

```sparql
(none)
```

### 7.56 `holdout-v2-epa-water-quality-portal` — q-008 (T3)

**Question:** How many samples were collected by each organization?

**Expected answer:** `Hoopa Valley Tribe (Tribal) | 1218
Tolowa Dee-ni Nation (Smith River Rancheria), California (Tribal) | 870
Big Valley Band of Pomo Indians of the Big Valley Rancheria, California (Tribal) | 700
National Park Service Water Resources Division | 359
California State Water Resources Control Board | 246
Cher-Ae Heights Indian Community of the Trinidad Rancheria (Tribal) | 228
Coyote Valley Band of Pomo Indians of California (Tribal) | 225
Yurok Tribe of the Yurok Reservation, California (Tribal) | 137
BLUELAKERANCHERIA_WQX (Tribal) | 96
Habematolel Pomo of Upper Lake, California (Tribal) | 82
... (34 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?organizationName (COUNT(?sample) AS ?sampleCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-epa-water-quality-portal> WHERE {
  ?sample <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/Sample> .
  ?sample <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/sample_by_organization> ?organization .
  ?organization <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/Organization> .
  ?organization <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/organization_name> ?organizationName .
} GROUP BY ?organizationName ORDER BY DESC(?sampleCount)
```

**Seed 1 (wrong) — answer:** `CA_BVR: 8, ADVENTURESCIENTISTS: 1, 11NPSWRD_WQX: 1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `CA_BVR: 8, ADVENTURESCIENTISTS: 1, 11NPSWRD_WQX: 1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `CA_BVR: 8, ADVENTURESCIENTISTS: 1, 11NPSWRD_WQX: 1`

```sparql
(none)
```

### 7.57 `holdout-v2-epa-water-quality-portal` — q-010 (T3)

**Question:** How many samples were collected at each station?

**Expected answer:** `CDR and Nutrients at Saints Rest Bar | 822
CDR at Red Rock | 396
Smith River Middle Fork | 106
Rowdy Creek 1 | 106
Lopez Creek 2 | 102
Rowdy Creek 2 | 98
Smith River 2 | 98
Gilbert Creek 3 | 96
Smith River 3A | 94
Lopez Creek 1 | 86`

**Gold SPARQL:**
```sparql
SELECT ?station_name (COUNT(?sample) AS ?sample_count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-epa-water-quality-portal> WHERE {
  ?station <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/Station> .
  ?station <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/station_has_sample> ?sample .
  ?sample <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/Sample> .
  ?station <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/station_name> ?station_name .
} GROUP BY ?station_name ORDER BY DESC(?sample_count) LIMIT 10
```

**Seed 1 (wrong) — answer:** `SRR_WQX-SR-2: 4, SRR_WQX-RC-2: 1, SRR_WQX-RC-1: 3, NALMS-F254866: 1, SRR_WQX-GC-2: 1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `SRR_WQX-SR-2: 5, SRR_WQX-RC-2: 1, SRR_WQX-RC-1: 3, NALMS-F254866: 1, SRR_WQX-GC-2: 1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `SRR_WQX-SR-2: 5, SRR_WQX-RC-2: 1, SRR_WQX-RC-1: 3, NALMS-F254866: 1, SRR_WQX-GC-2: 1`

```sparql
(none)
```

### 7.58 `holdout-v2-epa-water-quality-portal` — q-011 (T4)

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

**Seed 1 (wrong) — answer:** `Insufficient rows to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

### 7.59 `holdout-v2-epa-water-quality-portal` — q-012 (T4)

**Question:** Find the names of organizations that have collected samples of 'Water' media and operate stations with a 'WGS84' horizontal datum.

**Expected answer:** `Karuk Tribe (Tribal), National Park Service Water Resources Division, Augustine Band of Cahuilla Indians (Tribal), Bishop Paiute Tribe, Adventure Scientists (Volunteer)*, Pauma Band of Luiseno Mission Indians of the Pauma &amp; Yuima Reservation, California (Tribal), California State Water Resources Control Board, Pechanga Band of Luiseno Mission Indians of the Pechanga Reservation, California (Tribal), Tolowa Dee-ni Nation (Smith River Rancheria), California (Tribal), Middletown Rancheria of Pomo Indians of California (Tribal) ... (12 total)`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?organization_name FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-epa-water-quality-portal> WHERE { 
  ?organization <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/Organization> .
  ?organization <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/organization_name> ?organization_name .
  ?sample <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/Sample> .
  ?sample <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/sample_by_organization> ?organization .
  ?sample <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/sample_media> "Water" .
  ?station <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/Station> .
  ?station <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/station_operated_by> ?organization .
  ?station <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/station_horizontal_datum> "WGS84" .
}
```

**Seed 1 (wrong) — answer:** `11NPSWRD_WQX`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `11NPSWRD_WQX`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `11NPSWRD_WQX`

```sparql
(none)
```

### 7.60 `holdout-v2-epa-water-quality-portal` — q-013 (T4)

**Question:** List the names of water body types associated with stations that have samples with a 'Field Msr/Obs' activity type.

**Expected answer:** `River/Stream, Other-Surface Water, Reservoir, River/Stream Intermittent, River/Stream Perennial, River/stream Effluent-Dominated, Estuary, Seep, Well, Pond ... (21 total)`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?waterbodytype_name FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-epa-water-quality-portal> WHERE { 
  ?waterbodytype <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/WaterBodyType> .
  ?waterbodytype <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/waterbodytype_name> ?waterbodytype_name .
  ?station <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/Station> .
  ?station <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/station_waterbody_type> ?waterbodytype .
  ?sample <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/Sample> .
  ?sample <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/sample_at_station> ?station .
  ?sample <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/sample_activity_type> "Field Msr/Obs" .
}
```

**Seed 1 (wrong) — answer:** `Insufficient rows to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Insufficient rows to compute the exact answer. The provided rows do not contain a column for water body type.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Insufficient rows to compute the exact answer. The provided rows do not contain a column for water body type.`

```sparql
(none)
```

### 7.61 `holdout-v2-fdic-call-reports` — q-001 (T1)

**Question:** How many financial metrics are recorded in the system?

**Expected answer:** `87409`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?financialMetric) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fdic-call-reports> WHERE {
  ?financialMetric <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/type/FinancialMetric> .
}
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

### 7.62 `holdout-v2-fdic-call-reports` — q-002 (T1)

**Question:** List all the available bank classes.

**Expected answer:** `State-chartered Federal Reserve member bank, State-chartered Federal Reserve nonmember bank, Savings bank (state-chartered), Savings association, Insured U.S. branch of a foreign-chartered bank, National commercial bank`

**Gold SPARQL:**
```sparql
SELECT ?bankClassLabel FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fdic-call-reports> WHERE {
  ?bankClass <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/type/BankClass> .
  ?bankClass <http://www.w3.org/2000/01/rdf-schema#label> ?bankClassLabel .
}
```

**Seed 1 (wrong) — answer:** `SM, NM, SB, N, SA, OI`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `SM, NM, SB, N, SA, OI`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `SM, NM, SB, N, SA, OI`

```sparql
(none)
```

### 7.63 `holdout-v2-fdic-call-reports` — q-003 (T1)

**Question:** How many unique call reports are there?

**Expected answer:** `4609`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?callReport) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fdic-call-reports> WHERE {
  ?callReport <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/type/CallReport> .
}
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

### 7.64 `holdout-v2-fdic-call-reports` — q-005 (T3)

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

**Seed 1 (wrong) — answer:** `Insufficient rows to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Insufficient rows to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Insufficient rows to compute the exact answer.`

```sparql
(none)
```

### 7.65 `holdout-v2-fdic-call-reports` — q-006 (T3)

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

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

### 7.66 `holdout-v2-fdic-call-reports` — q-008 (T4)

**Question:** What are the names of banks that filed call reports in Q2 2024 and are classified as 'commercial' charter category?

**Expected answer:** `RSNB Bank, West Michigan Community Bank, Legacy Bank & Trust Company, One Community Bank, Bank of Easton, Coastal Heritage Bank, Charles River Bank, Reading Co-operative Bank, United Texas Bank, The Bankers Bank ... (3288 total)`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?bankName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fdic-call-reports> WHERE {
  ?bank <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/type/Bank> .
  ?bank <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/bank_name> ?bankName .
  ?bank <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/has_call_report> ?callReport .
  ?callReport <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/type/CallReport> .
  ?callReport <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/callreport_fiscal_quarter> "Q2" .
  ?callReport <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/callreport_fiscal_year> "2024" .
  ?bank <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/bank_classified_as> ?bankClass .
  ?bankClass <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/type/BankClass> .
  ?bankClass <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/bankclass_charter_category> "commercial" .
}
```

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

### 7.67 `holdout-v2-fdic-call-reports` — q-009 (T4)

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

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer. The provided context contains financial metric values (ranging from approximately 8.49 to 15.34) but does not include the state headquarters for the banks.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer. The provided context contains financial metric values (ranging from approximately 8.49 to 15.34) but does not include the state headquarters for the banks.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer. The provided context contains financial metric values (ranging from approximately 8.49 to 15.34) but does not include the state headquarters for the banks.`

```sparql
(none)
```

### 7.68 `holdout-v2-fdic-call-reports` — q-010 (T4)

**Question:** List the bank names and their primary regulators for banks that have a 'Tier 1 Risk-Based Capital Ratio' metric reported on '20240630'.

**Expected answer:** `First Bank | FRB
Equitable Bank | FRB
Progressive Ozark Bank | FRB
Dime Community Bank | FRB
Orrstown Bank | FRB
Miners Exchange Bank | FRB
Plumas Bank | FRB
Fall River Five Cents Savings Bank | FRB
Commerce Bank & Trust | FRB
Charles Schwab Bank, SSB | FRB
... (3564 total rows)`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?bankName ?primaryRegulator FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fdic-call-reports> WHERE {
  ?bank <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/type/Bank> .
  ?bank <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/bank_name> ?bankName .
  ?bank <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/bank_classified_as> ?bankClass .
  ?bankClass <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/type/BankClass> .
  ?bankClass <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/bankclass_primary_regulator> ?primaryRegulator .
  ?metric <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/type/FinancialMetric> .
  ?metric <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/metric_for_bank> ?bank .
  ?metric <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/metric_name> "Tier 1 Risk-Based Capital Ratio" .
  ?metric <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/metric_repdte> "20240630" .
}
```

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer. The provided context contains financial metrics but does not include bank names or primary regulator information.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer. The provided context contains financial metrics but does not include bank names or primary regulator information.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer. The provided context contains financial metrics but does not include bank names or primary regulator information.`

```sparql
(none)
```

### 7.69 `holdout-v2-fec-enforcement` — q-001 (T1)

**Question:** How many participants are there?

**Expected answer:** `738`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?participant) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fec-enforcement> WHERE {
  ?participant <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/type/Participant> .
}
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

### 7.70 `holdout-v2-fec-enforcement` — q-004 (T1)

**Question:** How many unique citation statutes are recorded?

**Expected answer:** `235`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?statute) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fec-enforcement> WHERE {
  ?statute <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/type/CitationStatute> .
}
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

### 7.71 `holdout-v2-fec-enforcement` — q-005 (T2)

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
(none)
```

**Seed 2 (wrong) — answer:** `0`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `0`

```sparql
(none)
```

### 7.72 `holdout-v2-fec-enforcement` — q-007 (T3)

**Question:** How many dispositions are associated with each respondent, and what are the names of those respondents?

**Expected answer:** `Crate, Bradley T. | 11
Boles, Jason D. | 6
Datwyler, Thomas Charles | 6
Never Surrender Inc. | 4
ActBlue | 4
Gilmer, George | 4
Harris for President | 4
Spencer, Keana | 4
Kilgore, Paul | 4
Lisker, Lisa | 4`

**Gold SPARQL:**
```sparql
SELECT ?respondentName (COUNT(DISTINCT ?disposition) AS ?numberOfDispositions) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fec-enforcement> WHERE {
  ?disposition <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/type/Disposition> .
  ?disposition <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/disposition_against_respondent> ?respondent .
  ?respondent <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/type/Respondent> .
  ?respondent <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/respondent_name> ?respondentName .
} GROUP BY ?respondentName ORDER BY DESC(?numberOfDispositions) LIMIT 10
```

**Seed 1 (wrong) — answer:** `Unknown Respondents (8289): 1, X Corp.: 1, Manuel, Jacqueline: 1, Collins, Paula: 1, Alsobrooks, Angela D.: 1, GWEN PAC: 1, Curtis, Elizabeth: 1, Washington Post, The: 1, Never Surrender Inc.: 2`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Unknown Respondents (8289): 1, X Corp.: 1, Manuel, Jacqueline: 1, Collins, Paula: 1, Alsobrooks, Angela D.: 1, GWEN PAC: 1, Curtis, Elizabeth: 1, Washington Post, The: 1, Never Surrender Inc.: 2`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Unknown Respondents (8289): 1, X Corp.: 1, Manuel, Jacqueline: 1, Collins, Paula: 1, Alsobrooks, Angela D.: 1, GWEN PAC: 1, Curtis, Elizabeth: 1, Washington Post, The: 1, Never Surrender Inc.: 2`

```sparql
(none)
```

### 7.73 `holdout-v2-fec-enforcement` — q-008 (T3)

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

**Seed 1 (wrong) — answer:** `X Corp., Dhillon Law Group Inc.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `X Corp., Dhillon Law Group Inc.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `X Corp., Dhillon Law Group Inc.`

```sparql
(none)
```

### 7.74 `holdout-v2-fec-enforcement` — q-009 (T3)

**Question:** List the citation titles and the outcomes of dispositions that cite them, where the disposition outcome is 'Dismissed-Other'.

**Expected answer:** `11 | Dismissed-Other
52 | Dismissed-Other
2 | Dismissed-Other`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?citationTitle ?dispositionOutcome FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fec-enforcement> WHERE {
  ?disposition <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/type/Disposition> .
  ?disposition <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/disposition_cites_statute> ?citationStatute .
  ?citationStatute <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/type/CitationStatute> .
  ?citationStatute <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/citation_title> ?citationTitle .
  ?disposition <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/disposition_outcome> ?dispositionOutcome .
  FILTER (?dispositionOutcome = "Dismissed-Other")
}
```

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

### 7.75 `holdout-v2-fec-enforcement` — q-011 (T4)

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

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer. None of the provided rows contain information about cited statutes.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer. None of the provided rows contain information about cited statutes.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer. None of the provided rows contain information about cited statutes.`

```sparql
(none)
```

### 7.76 `holdout-v2-fec-enforcement` — q-012 (T4)

**Question:** What is the average penalty for dispositions that cite a statute, grouped by the case they belong to?

**Expected answer:** `Oklahoma Leadership Council | 174000.0
John James for Senate, Inc. | 95000.0
1199 SEIU United Healthcare Workers East Federal Political Action Fund | 85000.0
Democratic Executive Committee of Florida | 70000.0
Citizens for Waters | 68000.0
The Moderate PAC, Inc. | 58000.0
Michigan Republican Party | 43500.0
Straight Talk Politics PAC | 37700.0
Sheila Jackson Lee for Congress | 33000.0
PA Lawyer Fund | 25000.0`

**Gold SPARQL:**
```sparql
SELECT ?case_name (AVG(xsd:float(?penalty)) AS ?average_penalty) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fec-enforcement> WHERE {
  ?case <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/type/Case> .
  ?case <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/case_name> ?case_name .
  ?case <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/case_has_disposition> ?disposition .
  ?disposition <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/type/Disposition> .
  ?disposition <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/disposition_cites_statute> ?statute .
  ?statute <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/type/CitationStatute> .
  ?disposition <https://omnix.dev/holdout-v2/holdout-v2-fec-enforcement/pred/disposition_penalty_usd> ?penalty .
  FILTER(xsd:float(?penalty) > 0)
}
GROUP BY ?case_name
ORDER BY DESC(?average_penalty)
LIMIT 10
```

**Seed 1 (wrong) — answer:** `Insufficient rows to compute the exact answer. (The provided rows do not contain information about cited statutes).`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Insufficient rows to compute the exact answer. (The provided rows do not contain information about cited statutes).`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Insufficient rows to compute the exact answer. (The provided rows do not contain information about cited statutes).`

```sparql
(none)
```

### 7.77 `holdout-v2-fema-disaster-declarations` — q-001 (T1)

**Question:** How many FemaDeclarations are there?

**Expected answer:** `724`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?femaDeclaration) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations> WHERE { ?femaDeclaration <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/FemaDeclaration> . }
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

### 7.78 `holdout-v2-fema-disaster-declarations` — q-002 (T1)

**Question:** List all available regions.

**Expected answer:** `9, 6, 8, 4, 3, 5, 2, 1, 7, 10`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?regionName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations> WHERE { ?region <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/Region> . ?region <https://omnix.dev/types/Region/attrs/name> ?regionName . }
```

**Seed 1 (wrong) — answer:** `7, 10, 4, 8`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `7, 10, 4, 8`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `7, 10, 4, 8`

```sparql
(none)
```

### 7.79 `holdout-v2-fema-disaster-declarations` — q-003 (T1)

**Question:** How many distinct incident types are recorded in Declarations?

**Expected answer:** `21`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?incidentType) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations> WHERE { ?declaration <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/Declaration> . ?declaration <https://omnix.dev/types/Declaration/attrs/type> ?incidentType . }
```

**Seed 1 (wrong) — answer:** `1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `1`

```sparql
(none)
```

### 7.80 `holdout-v2-fema-disaster-declarations` — q-005 (T3)

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

**Seed 1 (wrong) — answer:** `OK: 2`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `OK: 2`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `OK: 2`

```sparql
(none)
```

### 7.81 `holdout-v2-fema-disaster-declarations` — q-006 (T3)

**Question:** For each County, how many FemaDeclarations have designated it as an area, where the declaration type is 'DR'?

**Expected answer:** `Lincoln (County) | 30
Jackson (County) | 30
Clay (County) | 26
Jefferson (County) | 25
Washington (County) | 25
Franklin (County) | 24
Marion (County) | 21
Madison (County) | 18
Grant (County) | 17
Lee (County) | 17
... (1994 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?countyName (COUNT(DISTINCT ?femaDeclaration) AS ?declarationCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations> WHERE {
  ?femaDeclaration <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/FemaDeclaration> .
  ?femaDeclaration <https://omnix.dev/onto/designated_area> ?county .
  ?county <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/County> .
  ?county <https://omnix.dev/types/County/attrs/name> ?countyName .
  ?femaDeclaration <https://omnix.dev/types/FemaDeclaration/attrs/declaration_type> "DR" .
} GROUP BY ?countyName ORDER BY DESC(?declarationCount)
```

**Seed 1 (wrong) — answer:** `Douglas (County): 1, Decatur (County): 1, Fannin (County): 1, Fayette (County): 1, Marion (County): 1, Montgomery (County): 1, Towns (County): 1, Troup (County): 1, Union (County): 1, Webster (County): 1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Douglas (County): 1, Decatur (County): 1, Fannin (County): 1, Fayette (County): 1, Marion (County): 1, Montgomery (County): 1, Towns (County): 1, Troup (County): 1, Union (County): 1, Webster (County): 1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Douglas (County): 1, Decatur (County): 1, Fannin (County): 1, Fayette (County): 1, Marion (County): 1, Montgomery (County): 1, Towns (County): 1, Troup (County): 1, Union (County): 1, Webster (County): 1`

```sparql
(none)
```

### 7.82 `holdout-v2-fema-disaster-declarations` — q-008 (T4)

**Question:** What is the average number of counties designated per FemaDeclaration for each incident type?

**Expected answer:** `Severe Storm | 12.77027027027027027027
Hurricane | 27.66666666666666666667
Fire | 1.41732283464566929134
Flood | 12.56756756756756756757
Winter Storm | 9.2
Snowstorm | 10.125
Earthquake | 2.66666666666666666667
Dam/Levee Break | 5
Biological | 48.27272727272727272727
Tornado | 10
... (18 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?incident_type (AVG(?county_count) AS ?average_counties) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations> WHERE {
  ?fema_declaration <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/FemaDeclaration> .
  ?fema_declaration <https://omnix.dev/types/FemaDeclaration/attrs/incident_type> ?incident_type .
  {
    SELECT ?fema_declaration (COUNT(DISTINCT ?county) AS ?county_count) WHERE {
      ?fema_declaration <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/FemaDeclaration> .
      ?fema_declaration <https://omnix.dev/onto/designated_area> ?county .
      ?county <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/County> .
    } GROUP BY ?fema_declaration
  }
} GROUP BY ?incident_type
```

**Seed 1 (wrong) — answer:** `Fire, 2.5`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Fire, 2.5`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Fire, 2.5`

```sparql
(none)
```

### 7.83 `holdout-v2-fema-disaster-declarations` — q-009 (T4)

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

**Seed 1 (wrong) — answer:** `DR, 2.5
FM, 1.25`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `DR, 2.5
FM, 1.25`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `DR, 2.5
FM, 1.25`

```sparql
(none)
```

### 7.84 `holdout-v2-fema-disaster-declarations-multitable` — q-001 (T1)

**Question:** How many Public Assistance Projects are there?

**Expected answer:** `20000`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?paProject) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations-multitable> WHERE { ?paProject <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/PAProject> . }
```

**Seed 1 (wrong) — answer:** `2`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `2`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `2`

```sparql
(none)
```

### 7.85 `holdout-v2-fema-disaster-declarations-multitable` — q-002 (T1)

**Question:** List all the names of the applicants.

**Expected answer:** `CAT Z - Management Costs, (PW# 67) PUBLIC BUILDINGS AND FACILITIES, (PW# 71) EMERGENCY PROTECTIVE MEASURES, (PW# 73) EMERGENCY PROTECTIVE MEASURES, (PW# 119) PUBLIC BUILDINGS AND FACILITIES, Vaccine Storage, (PW# 35) PUBLIC BUILDINGS AND FACILITIES, (PW# 14) EMERGENCY PROTECTIVE MEASURES, (PW# 11) PUBLIC BUILDINGS AND FACILITIES, DR4416 - City of Stamford - Roads ... (2157 total)`

**Gold SPARQL:**
```sparql
SELECT ?applicantName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations-multitable> WHERE { ?applicant <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/Applicant> . ?applicant <http://www.w3.org/2000/01/rdf-schema#label> ?applicantName . }
```

**Seed 1 (wrong) — answer:** `039-UHFLZ-00, 000-49334-00, 000-72468-00, 000-81002-00`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `039-UHFLZ-00, 000-49334-00, 000-72468-00, 000-81002-00`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `039-UHFLZ-00, 000-49334-00, 000-72468-00, 000-81002-00`

```sparql
(none)
```

### 7.86 `holdout-v2-fema-disaster-declarations-multitable` — q-003 (T1)

**Question:** What are the different incident types recorded?

**Expected answer:** `Winter Storm, Coastal Storm, Freezing, Fire, Severe Storm, Tornado, Hurricane, Biological, Severe Ice Storm, Flood ... (12 total)`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?incidentTypeLabel FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations-multitable> WHERE { ?incidentType <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/IncidentType> . ?incidentType <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/pred/incident_type> ?incidentTypeLabel . }
```

**Seed 1 (wrong) — answer:** `Fire, Biological, Flood`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Fire, Biological, Flood`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Fire, Biological, Flood`

```sparql
(none)
```

### 7.87 `holdout-v2-fema-disaster-declarations-multitable` — q-004 (T1)

**Question:** How many distinct damage categories are there?

**Expected answer:** `9`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?damageCategory) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations-multitable> WHERE { ?damageCategory <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/DamageCategory> . }
```

**Seed 1 (wrong) — answer:** `5`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `5`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `5`

```sparql
(none)
```

### 7.88 `holdout-v2-fema-disaster-declarations-multitable` — q-005 (T2)

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

**Seed 1 (wrong) — answer:** `6`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `6`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `6`

```sparql
(none)
```

### 7.89 `holdout-v2-fema-disaster-declarations-multitable` — q-006 (T2)

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

**Seed 1 (wrong) — answer:** `Tahoe Truckee Unified School District`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Tahoe Truckee Unified School District`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Tahoe Truckee Unified School District`

```sparql
(none)
```

### 7.90 `holdout-v2-fema-disaster-declarations-multitable` — q-007 (T2)

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

**Seed 1 (wrong) — answer:** `The rows provided are insufficient to compute the exact answer. None of the provided rows have a totalObligated amount less than 1000.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Insufficient rows to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Insufficient rows to compute the exact answer.`

```sparql
(none)
```

### 7.91 `holdout-v2-fema-disaster-declarations-multitable` — q-008 (T3)

**Question:** How many PA Projects are associated with each Applicant?

**Expected answer:** `CVOID-19 | 713
(PW# 3267) EMERGENCY PROTECTIVE MEASURES | 386
Public Health - CTG Computer Task Group | 370
(PW# 3) DEBRIS REMOVAL | 178
(PW# 2289) DEBRIS REMOVAL | 164
DR4485 (Cat B Disaster Costs) | 160
Metro Health BCFS Cost Share | 159
Emergency Protective Measures (Law Enforcement Division) | 151
(PW# 7) EMERGENCY PROTECTIVE MEASURES | 141
COVID19 Emergency Response | 137`

**Gold SPARQL:**
```sparql
SELECT ?applicantLabel (COUNT(?paProject) AS ?numberOfPAProjects) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations-multitable> WHERE {
  ?paProject <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/PAProject> .
  ?paProject <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/pred/pa_project_has_applicant> ?applicant .
  ?applicant <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/Applicant> .
  ?applicant <http://www.w3.org/2000/01/rdf-schema#label> ?applicantLabel .
} GROUP BY ?applicantLabel ORDER BY DESC(?numberOfPAProjects) LIMIT 10
```

**Seed 1 (wrong) — answer:** `213-14224-00: 1, 181-67496-00: 1, 000-72468-00: 1, 147-99147-00: 1, 000-68188-00: 2, 000-81002-00: 1, 000-US6DK-00: 1, 000-49334-00: 1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `213-14224-00: 1, 181-67496-00: 1, 000-72468-00: 1, 147-99147-00: 1, 000-68188-00: 2, 000-81002-00: 1, 000-US6DK-00: 1, 000-49334-00: 1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `213-14224-00: 1, 181-67496-00: 1, 000-72468-00: 1, 147-99147-00: 1, 000-68188-00: 2, 000-81002-00: 1, 000-US6DK-00: 1, 000-49334-00: 1`

```sparql
(none)
```

### 7.92 `holdout-v2-fema-disaster-declarations-multitable` — q-009 (T3)

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

**Seed 1 (wrong) — answer:** `CA: 1988948.19, TX: 960728.19`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `CA: 1988948.19, TX: 960728.19`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `CA: 1988947.79, TX: 960728.19`

```sparql
(none)
```

### 7.93 `holdout-v2-fema-disaster-declarations-multitable` — q-010 (T3)

**Question:** List the incident types and the number of disasters declared for each type.

**Expected answer:** `Fire | 268
Flood | 43
Hurricane | 25
Severe Storm | 20
Tornado | 15
Severe Ice Storm | 3
Biological | 3
Other | 3
Coastal Storm | 2
Freezing | 2`

**Gold SPARQL:**
```sparql
SELECT ?incidentTypeLabel (COUNT(?disaster) AS ?numberOfDisasters) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations-multitable> WHERE {
  ?disaster <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/Disaster> .
  ?disaster <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/pred/has_incident_type> ?incidentType .
  ?incidentType <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/IncidentType> .
  ?incidentType <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/pred/incident_type> ?incidentTypeLabel .
} GROUP BY ?incidentTypeLabel ORDER BY DESC(?numberOfDisasters) LIMIT 10
```

**Seed 1 (wrong) — answer:** `Flood: 9, Fire: 1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Flood: 9, Fire: 1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Flood: 9, Fire: 1`

```sparql
(none)
```

### 7.94 `holdout-v2-fema-disaster-declarations-multitable` — q-012 (T4)

**Question:** What is the average federal share obligated for PA Projects associated with each Incident Type?

**Expected answer:** `Hurricane | 380394.3
Tornado | 36049.375
Flood | 85036.66
Fire | 44496.625
Severe Storm | 26836.178
Severe Ice Storm | 118390.06
Coastal Storm | 279543.0
Biological | 1.2554569E7
Other | 172645.95
Winter Storm | 1718087.4`

**Gold SPARQL:**
```sparql
SELECT ?incidentTypeLabel (AVG(xsd:float(?federalShare)) AS ?averageFederalShare) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations-multitable> WHERE {
  ?disaster <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/Disaster> .
  ?disaster <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/pred/has_incident_type> ?incidentType .
  ?incidentType <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/IncidentType> .
  ?incidentType <http://www.w3.org/2000/01/rdf-schema#label> ?incidentTypeLabel .
  ?disaster <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/pred/has_pa_project> ?paProject .
  ?paProject <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/PAProject> .
  ?paProject <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/pred/federalshareobligated> ?federalShare .
} GROUP BY ?incidentTypeLabel
```

**Seed 1 (wrong) — answer:** `Biological: 355547.76, Severe Storm(s): 5184.75, Winter Storm: 7276.41`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Biological: 355547.76, Severe Storm(s): 5184.75, Winter Storm: 7276.41`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Biological: 355547.76, Severe Storm(s): 5184.75, Winter Storm: 7276.41`

```sparql
(none)
```

### 7.95 `holdout-v2-ftc-consent-decrees` — q-001 (T1)

**Question:** How many matters are there in total?

**Expected answer:** `599`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?matter) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ftc-consent-decrees> WHERE { ?matter <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/type/Matter> . }
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

### 7.96 `holdout-v2-ftc-consent-decrees` — q-004 (T1)

**Question:** How many distinct industries are recorded?

**Expected answer:** `34`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?industry) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ftc-consent-decrees> WHERE { ?industry <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/type/Industry> . }
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

### 7.97 `holdout-v2-ftc-consent-decrees` — q-006 (T2)

**Question:** How many matters were filed in fiscal year 2015?

**Expected answer:** `26`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?matter) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ftc-consent-decrees> WHERE {
  ?matter <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/type/Matter> .
  ?matter <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/matter_fy> ?matterFY .
  FILTER(?matterFY = "2015")
}
```

**Seed 1 (wrong) — answer:** `4`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `4`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `4`

```sparql
(none)
```

### 7.98 `holdout-v2-ftc-consent-decrees` — q-007 (T2)

**Question:** Count the number of fiscal years with a value greater than '2010'.

**Expected answer:** `9`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?fiscalYear) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ftc-consent-decrees> WHERE {
  ?fiscalYear <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/type/FiscalYear> .
  ?fiscalYear <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/fy_value> ?fyValue .
  FILTER(?fyValue > "2010")
}
```

**Seed 1 (wrong) — answer:** `2`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `2`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `2`

```sparql
(none)
```

### 7.99 `holdout-v2-ftc-consent-decrees` — q-008 (T2)

**Question:** How many matters have a fiscal year of 2010?

**Expected answer:** `26`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?matter) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ftc-consent-decrees> WHERE {
  ?matter <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/type/Matter> .
  ?matter <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/matter_fy> ?fy .
  FILTER(?fy = "2010")
}
```

**Seed 1 (wrong) — answer:** `1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `1`

```sparql
(none)
```

### 7.100 `holdout-v2-ftc-consent-decrees` — q-009 (T3)

**Question:** How many matters are associated with each fiscal year?

**Expected answer:** `1996 | 33
1997 | 28
1998 | 41
1999 | 26
2000 | 32
2001 | 23
2002 | 27
2003 | 28
2004 | 22
2005 | 15
... (24 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?fiscalYearLabel (COUNT(?matter) AS ?matterCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ftc-consent-decrees> WHERE {
  ?matter <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/type/Matter> .
  ?matter <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/matter_in_fy> ?fiscalYear .
  ?fiscalYear <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/type/FiscalYear> .
  ?fiscalYear <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/fy_value> ?fiscalYearLabel .
} GROUP BY ?fiscalYearLabel ORDER BY ?fiscalYearLabel
```

**Seed 1 (wrong) — answer:** `1999: 1, 2000: 1, 2001: 1, 2014: 1, 2015: 1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `1999: 1, 2000: 1, 2001: 1, 2014: 1, 2015: 1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `1999: 1, 2000: 1, 2001: 1, 2014: 1, 2015: 1`

```sparql
(none)
```

### 7.101 `holdout-v2-ftc-consent-decrees` — q-011 (T3)

**Question:** Which industries have matters in fiscal year 2015, and how many matters are in each of those industries?

**Expected answer:** `Health Care - Prescription Drugs | 9
Health Care - Medical Equipment/Devices | 3
Professional Services (Non Health Care) - Other | 3
Manufacturing - Industrial Goods | 2
Manufacturing - Consumer Goods (non Food & Bev.) | 1
Energy - Petroleum | 1
Health Care - Hospitals/Clinics | 1
Retail - Grocery/Supermarkets | 1
Manufacturing - Food & Beverages | 1
Information and Technology - Other | 1
... (11 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?industryLabel (COUNT(?matter) AS ?matterCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ftc-consent-decrees> WHERE {
  ?matter <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/type/Matter> .
  ?matter <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/matter_in_industry> ?industry .
  ?industry <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/type/Industry> .
  ?industry <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/industry_label> ?industryLabel .
  ?matter <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/matter_fy> "2015" .
} GROUP BY ?industryLabel ORDER BY DESC(?matterCount)
```

**Seed 1 (wrong) — answer:** `ind_06cbd9503dc1: 3, ind_dc2a8db9b8fc: 1, ind_15092ed49747: 1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `ind_06cbd9503dc1: 3, ind_dc2a8db9b8fc: 1, ind_15092ed49747: 1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `ind_06cbd9503dc1: 3, ind_dc2a8db9b8fc: 1, ind_15092ed49747: 1`

```sparql
(none)
```

### 7.102 `holdout-v2-ftc-consent-decrees` — q-012 (T4)

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

**Seed 1 (wrong) — answer:** `ind_ef94db3bd321: 4 matters, average fiscal year 2005; ind_dc2a8db9b8fc: 1 matter, average fiscal year 2015; ind_0cb7e63fa287: 1 matter, average fiscal year 2014; ind_650d9d3d417a: 1 matter, average fiscal year 2000; ind_06cbd9503dc1: 1 matter, average fiscal year 2015; ind_d3851b91ae5b: 1 matter, a`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `ind_ef94db3bd321: 4 matters, average fiscal year 2005; ind_dc2a8db9b8fc: 1 matter, average fiscal year 2015; ind_0cb7e63fa287: 1 matter, average fiscal year 2014; ind_650d9d3d417a: 1 matter, average fiscal year 2000; ind_06cbd9503dc1: 1 matter, average fiscal year 2015; ind_d3851b91ae5b: 1 matter, a`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `ind_ef94db3bd321: 4 matters, average fiscal year 2005; ind_dc2a8db9b8fc: 1 matter, average fiscal year 2015; ind_0cb7e63fa287: 1 matter, average fiscal year 2014; ind_650d9d3d417a: 1 matter, average fiscal year 2000; ind_06cbd9503dc1: 1 matter, average fiscal year 2015; ind_d3851b91ae5b: 1 matter, a`

```sparql
(none)
```

### 7.103 `holdout-v2-ftc-consent-decrees` — q-013 (T4)

**Question:** List the matter names and their enforcement types for matters that occurred in fiscal year 2015.

**Expected answer:** `STERIS/Synergy Health | Preliminary Injunction
Sysco/USF Holding | Preliminary Injunction
Verisk Analytics/EagleView Technology | Preliminary Injunction
Lorillard/Reynolds American | Consent Order Accepted for Comment
Par Petroleum/Mid Pac Petroleum | Consent Order Accepted for Comment
Lafarge S.A./Holcim | Consent Order Accepted for Comment
Zeppelin Foundation Friedrichshafen/TRW Automotive Holdings | Consent Order Accepted for Comment
Covidien/Medtronic | Consent Order Accepted for Comment
Surgery Partners/Symbion Holdings | Consent Order Accepted for Comment
Impax Laboratories/Tower Holdings | Consent Order Accepted for Comment
... (26 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?matterName ?enforcementTypeLabel FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ftc-consent-decrees> WHERE {
  ?matter <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/type/Matter> .
  ?matter <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/matter_name> ?matterName .
  ?matter <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/matter_in_fy> ?fiscalYear .
  ?fiscalYear <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/type/FiscalYear> .
  ?fiscalYear <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/fy_value> "2015" .
  ?matter <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/matter_has_enforcement_type> ?enforcementType .
  ?enforcementType <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/type/EnforcementType> .
  ?enforcementType <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/enforcement_type_label> ?enforcementTypeLabel .
}
```

**Seed 1 (wrong) — answer:** `Verisk Analytics/EagleView Technology, etype_e84d4d1eebc8, Lafarge S.A./Holcim, etype_15d57c3be768`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Verisk Analytics/EagleView Technology, etype_e84d4d1eebc8, Lafarge S.A./Holcim, etype_15d57c3be768`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Verisk Analytics/EagleView Technology, etype_e84d4d1eebc8, Lafarge S.A./Holcim, etype_15d57c3be768`

```sparql
(none)
```

### 7.104 `holdout-v2-ftc-consent-decrees` — q-014 (T4)

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

**Seed 1 (wrong) — answer:** `1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `1`

```sparql
(none)
```

### 7.105 `holdout-v2-hrsa-hpsa` — q-001 (T1)

**Question:** How many Health Professional Shortage Areas are there?

**Expected answer:** `724`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?hpsa) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-hrsa-hpsa> WHERE { ?hpsa <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/HealthProfessionalShortageArea> . }
```

**Seed 1 (wrong) — answer:** `3`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `3`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `3`

```sparql
(none)
```

### 7.106 `holdout-v2-hrsa-hpsa` — q-002 (T1)

**Question:** List all the names of organizations.

**Expected answer:** `020670, 020730, 020700, 022030, 021040, 025310, 025320, 02E00103, 021400, 022090 ... (82 total)`

**Gold SPARQL:**
```sparql
SELECT ?organizationName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-hrsa-hpsa> WHERE { ?organization <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/Organization> . ?organization <https://omnix.dev/types/Organization/attrs/name> ?organizationName . }
```

**Seed 1 (wrong) — answer:** `DEPARTMENT OF HEALTH, COMMUNITY ACTION OF LARAMIE COUNTY, INC., AMERICAN INDIAN COUNCIL ON ALCOHOLISM, Republic of the Marshall Islands`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `DEPARTMENT OF HEALTH, COMMUNITY ACTION OF LARAMIE COUNTY, INC., AMERICAN INDIAN COUNCIL ON ALCOHOLISM, Republic of the Marshall Islands`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `DEPARTMENT OF HEALTH, COMMUNITY ACTION OF LARAMIE COUNTY, INC., AMERICAN INDIAN COUNCIL ON ALCOHOLISM, Republic of the Marshall Islands`

```sparql
(none)
```

### 7.107 `holdout-v2-hrsa-hpsa` — q-004 (T1)

**Question:** How many unique zip codes are recorded?

**Expected answer:** `423`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?zipCode) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-hrsa-hpsa> WHERE { ?zipCode <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/ZipCode> . }
```

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

### 7.108 `holdout-v2-hrsa-hpsa` — q-005 (T2)

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

**Seed 1 (wrong) — answer:** `0`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `0`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `0`

```sparql
(none)
```

### 7.109 `holdout-v2-hrsa-hpsa` — q-006 (T2)

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

**Seed 1 (wrong) — answer:** `41538.666666666664`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `41538.666666666664`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `41538.666666666664`

```sparql
(none)
```

### 7.110 `holdout-v2-hrsa-hpsa` — q-007 (T2)

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

**Seed 1 (wrong) — answer:** `DEPARTMENT OF HEALTH, HULETT CLINIC`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `DEPARTMENT OF HEALTH, HULETT CLINIC`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `DEPARTMENT OF HEALTH, HULETT CLINIC`

```sparql
(none)
```

### 7.111 `holdout-v2-hrsa-hpsa` — q-008 (T2)

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

**Seed 1 (wrong) — answer:** `0`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `0`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `0`

```sparql
(none)
```

### 7.112 `holdout-v2-hrsa-hpsa` — q-009 (T3)

**Question:** How many Health Professional Shortage Areas are associated with each State, where the HPSA has a 'Primary Care' discipline class?

**Expected answer:** `Wisconsin | 389
Puerto Rico | 140
Wyoming | 103
West Virginia | 43
U.S. Virgin Islands | 14
Federated States of Micronesia | 10
Northern Mariana Islands | 4
Republic of Palau | 3
Marshall Islands | 2
American Samoa | 2
... (12 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?stateName (COUNT(?hpsa) AS ?hpsaCount)
FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-hrsa-hpsa>
WHERE {
  ?hpsa <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/HealthProfessionalShortageArea> .
  ?hpsa <https://omnix.dev/onto/state_name> ?state .
  ?state <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/State> .
  ?state <https://omnix.dev/types/State/attrs/name> ?stateName .
  ?hpsa <https://omnix.dev/types/HealthProfessionalShortageArea/attrs/hpsa_discipline_class> "Primary Care" .
}
GROUP BY ?stateName
ORDER BY DESC(?hpsaCount)
```

**Seed 1 (wrong) — answer:** `American Samoa: 1, Wisconsin: 1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `American Samoa: 1, Wisconsin: 1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `American Samoa: 1, Wisconsin: 1`

```sparql
(none)
```

### 7.113 `holdout-v2-hrsa-hpsa` — q-010 (T3)

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

**Seed 1 (wrong) — answer:** `Brown County, WI: 9, Clark County, WI: 1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Brown County, WI: 9, Clark County, WI: 1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Brown County, WI: 9, Clark County, WI: 1`

```sparql
(none)
```

### 7.114 `holdout-v2-hrsa-hpsa` — q-011 (T3)

**Question:** How many Health Professional Shortage Areas are associated with each Organization?

**Expected answer:** `02E01268 | 2
08E01122 | 2
034190 | 2
05E00499 | 1
020910 | 1
021250 | 1
021030 | 1
022230 | 1
020890 | 1
0537600 | 1`

**Gold SPARQL:**
```sparql
SELECT ?organizationName (COUNT(?hpsa) AS ?hpsaCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-hrsa-hpsa> WHERE {
  ?hpsa <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/HealthProfessionalShortageArea> .
  ?hpsa <https://omnix.dev/onto/bhcmis_organization_identification_number> ?organization .
  ?organization <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/Organization> .
  ?organization <https://omnix.dev/types/Organization/attrs/name> ?organizationName .
} GROUP BY ?organizationName
ORDER BY DESC(?hpsaCount)
LIMIT 10
```

**Seed 1 (wrong) — answer:** `American Samoa: 1, Republic of the Marshall Islands: 1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `American Samoa: 1, Republic of the Marshall Islands: 1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `American Samoa: 1, Republic of the Marshall Islands: 1`

```sparql
(none)
```

### 7.115 `holdout-v2-medicare-part-d-pricing` — q-001 (T1)

**Question:** How many unique spending records are there?

**Expected answer:** `10531`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?spendingRecord) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing> WHERE {
  ?spendingRecord <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/SpendingRecord> .
}
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

### 7.116 `holdout-v2-medicare-part-d-pricing` — q-002 (T1)

**Question:** List all available drug names.

**Expected answer:** `Daytrana, Deferasirox, Daypro, Daybue, Daysee, Depakote ER, Depakote Sprinkle, Dentagel, Depakote, Depo-Estradiol ... (3474 total)`

**Gold SPARQL:**
```sparql
SELECT ?drugName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing> WHERE {
  ?drug <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/Drug> .
  ?drug <http://www.w3.org/2000/01/rdf-schema#label> ?drugName .
}
```

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

### 7.117 `holdout-v2-medicare-part-d-pricing` — q-004 (T1)

**Question:** How many distinct generic drug types are recorded?

**Expected answer:** `1938`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?genericDrug) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing> WHERE {
  ?genericDrug <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/GenericDrug> .
}
```

**Seed 1 (wrong) — answer:** `9`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `9`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `9`

```sparql
(none)
```

### 7.118 `holdout-v2-medicare-part-d-pricing` — q-005 (T2)

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

### 7.119 `holdout-v2-medicare-part-d-pricing` — q-006 (T2)

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

**Seed 1 (wrong) — answer:** `6744195.9955`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `6744195.9955`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `7144145.4964`

```sparql
(none)
```

### 7.120 `holdout-v2-medicare-part-d-pricing` — q-007 (T2)

**Question:** Find the total number of spending records where the average spend per claim in 2023 contains '3' in its string representation.

**Expected answer:** `7316`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?spendingRecord) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing> WHERE {
  ?spendingRecord <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/SpendingRecord> .
  ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/avg_spend_per_claim_2023> ?avgSpendPerClaim .
  FILTER(CONTAINS(STR(?avgSpendPerClaim), "3"))
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

### 7.121 `holdout-v2-medicare-part-d-pricing` — q-008 (T2)

**Question:** What is the maximum total claims in 2023 for spending records that have '802020' as their total spending in 2023?

**Expected answer:** `2321`

**Gold SPARQL:**
```sparql
SELECT (MAX(xsd:float(?totalClaims)) AS ?maxTotalClaims) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing> WHERE {
  ?spendingRecord <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/SpendingRecord> .
  ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/total_spending_2023> ?totalSpending .
  ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/total_claims_2023> ?totalClaims .
  FILTER(?totalSpending = "802020")
}
```

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

### 7.122 `holdout-v2-medicare-part-d-pricing` — q-009 (T3)

**Question:** Which drugs have spending records with a total spending of more than 1,000,000 in 2023, and how many such records does each drug have?

**Expected answer:** `Potassium Chloride* | 21
Gabapentin | 18
Fenofibrate | 18
Atorvastatin Calcium | 17
Diclofenac Sodium* | 16
Doxepin HCl* | 15
Metoprolol Succinate | 15
Baclofen* | 15
Clobetasol Propionate | 15
Famotidine* | 14
... (2022 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?drugLabel (COUNT(?spendingRecord) AS ?numberOfSpendingRecords) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing> WHERE {
  ?drug <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/Drug> .
  ?drug <http://www.w3.org/2000/01/rdf-schema#label> ?drugLabel .
  ?drug <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/has_spending_record> ?spendingRecord .
  ?spendingRecord <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/SpendingRecord> .
  ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/total_spending_2023> ?totalSpending .
  FILTER (xsd:float(?totalSpending) > 1000000) .
} GROUP BY ?drugLabel ORDER BY DESC(?numberOfSpendingRecords)
```

**Seed 1 (wrong) — answer:** `abae677041 (3), 8bb21a38bd (1), 614567674b (1), 2d400da65e (1), 85209ee882 (1), c7926667b3 (1), a31c4c19d4 (1)`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `abae677041 (3), 8bb21a38bd (1), 614567674b (1), 2d400da65e (1), 85209ee882 (1), c7926667b3 (1), a31c4c19d4 (1)`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `abae677041 (3), 8bb21a38bd (1), 614567674b (1), 2d400da65e (1), 85209ee882 (1), c7926667b3 (1), a31c4c19d4 (1)`

```sparql
(none)
```

### 7.123 `holdout-v2-medicare-part-d-pricing` — q-010 (T3)

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

**Seed 1 (wrong) — answer:** `77f2526428: 1, 220f49a482: 1, 4c59c55b40: 1, 8cb85c0619: 1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `77f2526428: 1, 220f49a482: 1, 4c59c55b40: 1, 8cb85c0619: 1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `77f2526428: 1, 220f49a482: 1, 4c59c55b40: 1, 8cb85c0619: 1`

```sparql
(none)
```

### 7.124 `holdout-v2-medicare-part-d-pricing` — q-011 (T3)

**Question:** List the generic drugs and the number of brand drugs associated with each, where the brand drug's total manufacturers count is greater than 1.

**Expected answer:** `Pen Needle, Diabetic | 10
Budesonide | 6
Diltiazem HCl | 6
Methylphenidate HCl | 5
Estradiol | 4
Metformin HCl | 4
Alcohol Antiseptic Pads | 4
Verapamil HCl | 3
Bupropion HCl | 3
Mesalamine | 3
... (910 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?genericName (COUNT(DISTINCT ?brandDrug) AS ?numberOfBrandDrugs) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing> WHERE {
  ?genericDrug <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/GenericDrug> .
  ?genericDrug <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/generic_name> ?genericName .
  ?brandDrug <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/Drug> .
  ?brandDrug <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/has_generic> ?genericDrug .
  ?brandDrug <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/total_manufacturers> ?totalManufacturers .
  FILTER (xsd:integer(?totalManufacturers) > 1) .
} GROUP BY ?genericName ORDER BY DESC(?numberOfBrandDrugs)
```

**Seed 1 (wrong) — answer:** `Amlodipine/Atorvastatin, 2`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Amlodipine/Atorvastatin, 2`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Amlodipine/Atorvastatin, 2`

```sparql
(none)
```

### 7.125 `holdout-v2-medicare-part-d-pricing` — q-012 (T4)

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

**Seed 1 (wrong) — answer:** `fda557bbb8: 0, e96519cbb2: 1, 9923db14dd: 1, f5581019d8: 0, 77f2526428: 0, 3cdbfa478f: 1, 8d39fc6858: 1, b7501100ce: 1, 04d90f2e70: 1, efed445056: 1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `fda557bbb8: 0, e96519cbb2: 1, 9923db14dd: 1, f5581019d8: 0, 77f2526428: 0, 3cdbfa478f: 1, 8d39fc6858: 1, b7501100ce: 1, 04d90f2e70: 1, efed445056: 1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `fda557bbb8: 0, e96519cbb2: 1, 9923db14dd: 1, f5581019d8: 0, 77f2526428: 0, 3cdbfa478f: 1, 8d39fc6858: 1, b7501100ce: 1, 04d90f2e70: 1, efed445056: 1`

```sparql
(none)
```

### 7.126 `holdout-v2-medicare-part-d-pricing` — q-013 (T4)

**Question:** List the generic names of drugs that have an associated spending record flagged as an 'Outlier' and whose average spend per claim in 2023 is greater than 500.

**Expected answer:** `Pentostatin, Cyanocobalamin (Vitamin B-12), Somatropin, Oritavancin Diphosphate, Omeprazole/Sodium Bicarbonate, Rifabutin, Nafcillin Sodium, Nicardipine HCl, Nilutamide, Nimodipine ... (256 total)`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?genericName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing> WHERE {
  ?drug <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/Drug> .
  ?drug <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/generic_name> ?genericName .
  ?drug <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/has_spending_record> ?spendingRecord .
  ?spendingRecord <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/SpendingRecord> .
  ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/has_outlier_status> ?outlierFlag .
  ?outlierFlag <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/OutlierFlag> .
  ?outlierFlag <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/outlier_label> "Outlier" .
  ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/avg_spend_per_claim_2023> ?avgSpendPerClaim .
  FILTER(xsd:float(?avgSpendPerClaim) > 500) .
}
```

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer. (The provided context contains spending records but does not include the 'Drug' table required to map drug_id to generic names).`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer. (The provided context contains spending records but does not include the 'Drug' table required to map drug_id to generic names).`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer. (The provided context contains spending records but does not include the 'Drug' table required to map drug_id to generic names).`

```sparql
(none)
```

### 7.127 `holdout-v2-ncua-credit-union-call-reports` — q-001 (T1)

**Question:** How many distinct financial metrics are recorded in the system?

**Expected answer:** `46255`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?financialMetric) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ncua-credit-union-call-reports> WHERE { ?financialMetric <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/FinancialMetric> . }
```

**Seed 1 (wrong) — answer:** `2`

```sparql
(none)
```

**Seed 2 (error) — answer:** `ERROR_Client error '429 Too Many Requests' for url 'https://openrouter.ai/api/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/429`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `2`

```sparql
(none)
```

### 7.128 `holdout-v2-ncua-credit-union-call-reports` — q-004 (T1)

**Question:** How many call reports are there in total?

**Expected answer:** `4631`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?callReport) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ncua-credit-union-call-reports> WHERE { ?callReport <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/CallReport> . }
```

**Seed 1 (wrong) — answer:** `10`

```sparql
(none)
```

**Seed 2 (error) — answer:** `ERROR_Client error '429 Too Many Requests' for url 'https://openrouter.ai/api/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/429`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `10`

```sparql
(none)
```

### 7.129 `holdout-v2-ncua-credit-union-call-reports` — q-005 (T2)

**Question:** How many financial metrics have a metric code that contains 'RATIO'?

**Expected answer:** `23116`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?financialMetric) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ncua-credit-union-call-reports> WHERE { ?financialMetric <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/FinancialMetric> . ?financialMetric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_code> ?metricCode . FILTER(CONTAINS(LCASE(STR(?metricCode)), 'ratio')) }
```

**Seed 1 (wrong) — answer:** `0`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `0`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `0`

```sparql
(none)
```

### 7.130 `holdout-v2-ncua-credit-union-call-reports` — q-006 (T2)

**Question:** List the names of financial metrics where the metric value is greater than 0.001.

**Expected answer:** `Loan-to-Share Ratio, Net Worth Ratio, Total Assets, Total Shares and Deposits, Total Shares and Deposits, Net Worth Ratio, Loan-to-Share Ratio, Number of Members, Net Charge-Offs to Loans, Loan-to-Share Ratio ... (38536 total)`

**Gold SPARQL:**
```sparql
SELECT ?metricName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ncua-credit-union-call-reports> WHERE { ?financialMetric <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/FinancialMetric> . ?financialMetric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_name> ?metricName . ?financialMetric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_value> ?metricValue . FILTER(xsd:float(?metricValue) > 0.001) }
```

**Seed 1 (wrong) — answer:** `Total Assets`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Total Assets`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Total Assets`

```sparql
(none)
```

### 7.131 `holdout-v2-ncua-credit-union-call-reports` — q-007 (T2)

**Question:** Find the maximum metric value among all financial metrics that have 'Total' in their name.

**Expected answer:** `177693540000`

**Gold SPARQL:**
```sparql
SELECT (MAX(xsd:float(?metricValue)) AS ?maxMetricValue) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ncua-credit-union-call-reports> WHERE { ?financialMetric <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/FinancialMetric> . ?financialMetric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_name> ?metricName . ?financialMetric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_value> ?metricValue . FILTER(CONTAINS(LCASE(STR(?metricName)), 'total')) }
```

**Seed 1 (wrong) — answer:** `1040297330`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `1040297330`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `1040297330`

```sparql
(none)
```

### 7.132 `holdout-v2-ncua-credit-union-call-reports` — q-008 (T2)

**Question:** How many financial metrics have a metric value greater than 0.001?

**Expected answer:** `38536`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?financialMetric) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ncua-credit-union-call-reports> WHERE {
  ?financialMetric <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/FinancialMetric> .
  ?financialMetric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_value> ?metricValue .
  FILTER(xsd:float(?metricValue) > 0.001)
}
```

**Seed 1 (wrong) — answer:** `10`

```sparql
(none)
```

**Seed 2 (error) — answer:** `ERROR_Client error '429 Too Many Requests' for url 'https://openrouter.ai/api/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/429`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `10`

```sparql
(none)
```

### 7.133 `holdout-v2-ncua-credit-union-call-reports` — q-009 (T3)

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

**Seed 1 (error) — answer:** `ERROR_Client error '429 Too Many Requests' for url 'https://openrouter.ai/api/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/429`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `UNITED STATES SENATE: 4, TREASURY DEPARTMENT: 6, THREE RIVERS: 25, LIBRARY OF CONGRESS: 1, STATE DEPARTMENT: 7, URE: 2, EP: 4, NUVISION: 27, FEDCHOICE: 5, CAPITAL EDUCATORS: 13`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `UNITED STATES SENATE: 4, TREASURY DEPARTMENT: 6, THREE RIVERS: 25, LIBRARY OF CONGRESS: 1, STATE DEPARTMENT: 7, URE: 2, EP: 4, NUVISION: 27, FEDCHOICE: 5, CAPITAL EDUCATORS: 13`

```sparql
(none)
```

### 7.134 `holdout-v2-ncua-credit-union-call-reports` — q-010 (T3)

**Question:** What are the names of credit unions that have a 'Net Worth Ratio' greater than 0.05?

**Expected answer:** `TROY, KAHUKU, CORRY, CENTURY, BRONCO, IQ, GP, UNITED LOCAL, COBURN, TAHQUAMENON AREA ... (4457 total)`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?creditUnionName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ncua-credit-union-call-reports> WHERE {
  ?creditUnion <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/CreditUnion> .
  ?creditUnion <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/creditunion_cu_name> ?creditUnionName .
  ?financialMetric <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/FinancialMetric> .
  ?financialMetric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_for_creditunion> ?creditUnion .
  ?financialMetric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_name> "Net Worth Ratio" .
  ?financialMetric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_value> ?metricValue .
  FILTER (xsd:float(?metricValue) > 0.05)
}
```

**Seed 1 (error) — answer:** `ERROR_Client error '429 Too Many Requests' for url 'https://openrouter.ai/api/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/429`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows provided do not contain the names of the credit unions, only their CU numbers (150, 77, 138, 53, 151, 19, 119, 48, 132, 108). Therefore, the rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows provided do not contain the names of the credit unions, only their CU numbers (150, 77, 138, 53, 151, 19, 119, 48, 132, 108). Therefore, the rows are insufficient to compute the exact answer.`

```sparql
(none)
```

### 7.135 `holdout-v2-ncua-credit-union-call-reports` — q-011 (T4)

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

**Seed 1 (error) — answer:** `ERROR_Client error '429 Too Many Requests' for url 'https://openrouter.ai/api/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/429`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer. The provided context does not contain the 'Total Assets' metric.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer. The provided context does not contain the 'Total Assets' metric.`

```sparql
(none)
```

### 7.136 `holdout-v2-ncua-credit-union-call-reports` — q-012 (T4)

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

**Seed 1 (error) — answer:** `ERROR_Client error '429 Too Many Requests' for url 'https://openrouter.ai/api/v1/chat/completions'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/429`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `19, 150, 53, 13, 138, 77, 6, 132, 22, 107`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `19, 150, 53, 13, 138, 77, 6, 132, 22, 107`

```sparql
(none)
```

### 7.137 `holdout-v2-nih-reporter-non-clinical` — q-001 (T1)

**Question:** How many projects are there in total?

**Expected answer:** `5287`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?project) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-nih-reporter-non-clinical> WHERE {
  ?project <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-nih-reporter-non-clinical/type/Project> .
}
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

### 7.138 `holdout-v2-nih-reporter-non-clinical` — q-004 (T1)

**Question:** How many distinct institutions are recorded?

**Expected answer:** `643`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?institution) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-nih-reporter-non-clinical> WHERE {
  ?institution <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-nih-reporter-non-clinical/type/Institution> .
}
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

### 7.139 `holdout-v2-nih-reporter-non-clinical` — q-005 (T2)

**Question:** How many projects have 'cancer' in their title?

**Expected answer:** `343`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?project) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-nih-reporter-non-clinical> WHERE {
  ?project <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-nih-reporter-non-clinical/type/Project> .
  ?project <https://omnix.dev/holdout-v2/holdout-v2-nih-reporter-non-clinical/pred/project_title> ?title .
  FILTER(CONTAINS(LCASE(STR(?title)), "cancer"))
}
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

### 7.140 `holdout-v2-nih-reporter-non-clinical` — q-006 (T2)

**Question:** How many agencies have an abbreviation starting with 'N'?

**Expected answer:** `29`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?agency) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-nih-reporter-non-clinical> WHERE {
  ?agency <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-nih-reporter-non-clinical/type/Agency> .
  ?agency <https://omnix.dev/holdout-v2/holdout-v2-nih-reporter-non-clinical/pred/agency_abbreviation> ?abbreviation .
  FILTER(STRSTARTS(?abbreviation, "N"))
}
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

### 7.141 `holdout-v2-nih-reporter-non-clinical` — q-007 (T3)

**Question:** For each study section, how many projects were reviewed by it that started in the fiscal year '2022'?

**Expected answer:** `NSS | 26
NST-1 Study Section[NST-1] | 16
Special Emphasis Panel[ZRG1-CB-J(55)R] | 14
Digestive Diseases and Nutrition C Study Section[DDK-C] | 14
Molecular Neurogenetics Study Section[MNG] | 14
Career Development Study Section (J)[NCI-J] | 13
Diseases and Pathophysiology of the Visual System Study Section[DPVS] | 12
Biomaterials and Biointerfaces Study Section[BMBI] | 12
ZFD1-SRC(99) | 12
Kidney, Urologic and Hematologic Diseases D Study Section[DDK-D] | 12
... (1136 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?study_section_name (COUNT(DISTINCT ?project) AS ?project_count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-nih-reporter-non-clinical> WHERE {
  ?study_section <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-nih-reporter-non-clinical/type/StudySection> .
  ?study_section <https://omnix.dev/holdout-v2/holdout-v2-nih-reporter-non-clinical/pred/study_section_name> ?study_section_name .
  ?project <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-nih-reporter-non-clinical/type/Project> .
  ?project <https://omnix.dev/holdout-v2/holdout-v2-nih-reporter-non-clinical/pred/reviewed_by> ?study_section .
  ?project <https://omnix.dev/holdout-v2/holdout-v2-nih-reporter-non-clinical/pred/fiscal_year> "2022" .
} GROUP BY ?study_section_name
ORDER BY DESC(?project_count)
```

**Seed 1 (wrong) — answer:** `0e16e67ceeeb: 0, 2329546d3511: 0, 23a25cd3fcdf: 1, 59de50e5ff28: 0, d46335cd82fb: 1, e3f2e8321ba4: 0, e7977b921154: 0, e932dcbbbcf2: 0`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `0e16e67ceeeb: 0, 2329546d3511: 0, 23a25cd3fcdf: 1, 59de50e5ff28: 0, d46335cd82fb: 1, e3f2e8321ba4: 0, e7977b921154: 0, e932dcbbbcf2: 0`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `0e16e67ceeeb: 0, 2329546d3511: 0, 23a25cd3fcdf: 1, 59de50e5ff28: 0, d46335cd82fb: 1, e3f2e8321ba4: 0, e7977b921154: 0, e932dcbbbcf2: 0`

```sparql
(none)
```

### 7.142 `holdout-v2-nih-reporter-non-clinical` — q-008 (T4)

**Question:** List the names of investigators who have worked on projects awarded to institutions in 'UNITED STATES' and reviewed by a study section.

**Expected answer:** `ALONSO, ESTELLA M., ALSHAWI, SARAH ANN, AMAYA, KENNETH, AMIRKHANIAN, YURI A, AMUTAH-ONUKAGHA, NDIDIAMAKA, ANDERSON, HANNAH A, ANKER, JEFFREY N, ANTIC, SRDJAN D, ARANOW, CYNTHIA, ARGÜELLO-MIRANDA, ORLANDO ... (2362 total)`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?investigator_name FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-nih-reporter-non-clinical> WHERE {
  ?investigator <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-nih-reporter-non-clinical/type/Investigator> .
  ?investigator <https://omnix.dev/holdout-v2/holdout-v2-nih-reporter-non-clinical/pred/investigator_name> ?investigator_name .
  ?project <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-nih-reporter-non-clinical/type/Project> .
  ?project <https://omnix.dev/holdout-v2/holdout-v2-nih-reporter-non-clinical/pred/has_investigator> ?investigator .
  ?project <https://omnix.dev/holdout-v2/holdout-v2-nih-reporter-non-clinical/pred/awarded_to_institution> ?institution .
  ?institution <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-nih-reporter-non-clinical/type/Institution> .
  ?institution <https://omnix.dev/holdout-v2/holdout-v2-nih-reporter-non-clinical/pred/org_country> "UNITED STATES" .
  ?project <https://omnix.dev/holdout-v2/holdout-v2-nih-reporter-non-clinical/pred/reviewed_by> ?study_section .
  ?study_section <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-nih-reporter-non-clinical/type/StudySection> .
}
```

**Seed 1 (wrong) — answer:** `Insufficient rows to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Insufficient rows to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Insufficient rows to compute the exact answer.`

```sparql
(none)
```

### 7.143 `holdout-v2-noaa-storm-events` — q-001 (T1)

**Question:** How many storm events are recorded?

**Expected answer:** `10000`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?stormEvent) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-noaa-storm-events> WHERE {
  ?stormEvent <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/type/StormEvent> .
}
```

**Seed 1 (wrong) — answer:** `9`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `9`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `9`

```sparql
(none)
```

### 7.144 `holdout-v2-noaa-storm-events` — q-003 (T1)

**Question:** How many distinct states are mentioned in the dataset?

**Expected answer:** `64`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?state) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-noaa-storm-events> WHERE {
  ?state <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/type/State> .
}
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

### 7.145 `holdout-v2-noaa-storm-events` — q-005 (T2)

**Question:** How many storm events had direct deaths greater than 5?

**Expected answer:** `7`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?event) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-noaa-storm-events> WHERE {
  ?event <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/type/StormEvent> .
  ?event <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/pred/deaths_direct> ?deaths_direct .
  FILTER (xsd:integer(?deaths_direct) > 5)
}
```

**Seed 1 (wrong) — answer:** `0`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `0`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `0`

```sparql
(none)
```

### 7.146 `holdout-v2-noaa-storm-events` — q-006 (T2)

**Question:** How many storm events had crop damage greater than 100000?

**Expected answer:** `33`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?event) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-noaa-storm-events> WHERE {
  ?event <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/type/StormEvent> .
  ?event <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/pred/damage_crops> ?damage_crops .
  FILTER (xsd:integer(?damage_crops) > 100000)
}
```

**Seed 1 (wrong) — answer:** `0`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `0`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `0`

```sparql
(none)
```

### 7.147 `holdout-v2-noaa-storm-events` — q-007 (T2)

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
(none)
```

**Seed 2 (wrong) — answer:** `0`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `0`

```sparql
(none)
```

### 7.148 `holdout-v2-noaa-storm-events` — q-008 (T2)

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

**Seed 1 (wrong) — answer:** `0`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `0`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `0`

```sparql
(none)
```

### 7.149 `holdout-v2-noaa-storm-events` — q-009 (T3)

**Question:** How many storm events occurred in each state?

**Expected answer:** `Texas | 1005
Oklahoma | 523
California | 425
Kansas | 358
Illinois | 340
Georgia | 335
Nebraska | 323
Louisiana | 299
Missouri | 297
South Dakota | 285
... (64 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?stateName (COUNT(?stormEvent) AS ?numberOfStormEvents) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-noaa-storm-events> WHERE {
  ?stormEvent <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/type/StormEvent> .
  ?stormEvent <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/pred/event_in_state> ?state .
  ?state <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/type/State> .
  ?state <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/pred/state_name> ?stateName .
} GROUP BY ?stateName ORDER BY DESC(?numberOfStormEvents)
```

**Seed 1 (wrong) — answer:** `29: 1, 13: 1, 56: 1, 51: 1, 9: 1, 53: 1, 48: 2, 4: 1, 1: 1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `29: 1, 13: 1, 56: 1, 51: 1, 9: 1, 53: 1, 48: 2, 4: 1, 1: 1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `29: 1, 13: 1, 56: 1, 51: 1, 9: 1, 53: 1, 48: 2, 4: 1, 1: 1`

```sparql
(none)
```

### 7.150 `holdout-v2-noaa-storm-events` — q-010 (T3)

**Question:** What are the names of event types that caused direct deaths greater than 0?

**Expected answer:** `Heat, Thunderstorm Wind, Flash Flood, Excessive Heat, Winter Weather, Wildfire, Cold/Wind Chill, Heavy Rain, Winter Storm, Flood ... (14 total)`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?eventTypeLabel FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-noaa-storm-events> WHERE {
  ?stormEvent <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/type/StormEvent> .
  ?stormEvent <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/pred/event_of_type> ?eventType .
  ?eventType <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/type/EventType> .
  ?eventType <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/pred/event_type> ?eventTypeLabel .
  ?stormEvent <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/pred/deaths_direct> ?deathsDirect .
  FILTER (xsd:integer(?deathsDirect) > 0)
}
```

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

### 7.151 `holdout-v2-noaa-storm-events` — q-011 (T3)

**Question:** List the months and the total number of storm events that caused property damage greater than 1000.

**Expected answer:** `July | 319
June | 203
August | 195
March | 136
April | 127
September | 86
May | 76
January | 43
December | 41
February | 41
... (12 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?monthName (COUNT(?stormEvent) AS ?totalStormEvents) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-noaa-storm-events> WHERE {
  ?stormEvent <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/type/StormEvent> .
  ?stormEvent <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/pred/event_in_month> ?month .
  ?month <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/type/Month> .
  ?month <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/pred/month_name> ?monthName .
  ?stormEvent <https://omnix.dev/holdout-v2/holdout-v2-noaa-storm-events/pred/damage_property> ?damageProperty .
  FILTER (xsd:integer(?damageProperty) > 1000)
} GROUP BY ?monthName ORDER BY DESC(?totalStormEvents)
```

**Seed 1 (wrong) — answer:** `August, 1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `August, 1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `August, 1`

```sparql
(none)
```

### 7.152 `holdout-v2-npi-registry` — q-003 (T1)

**Question:** How many unique practice locations are recorded?

**Expected answer:** `10000`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?practiceLocation) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-npi-registry> WHERE {
  ?practiceLocation <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/type/PracticeLocation> .
}
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

### 7.153 `holdout-v2-npi-registry` — q-005 (T2)

**Question:** How many providers have 'LAKE REGIONAL' in their display name?

**Expected answer:** `24`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?provider) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-npi-registry> WHERE {
  ?provider <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/type/Provider> .
  ?provider <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/display_name> ?displayName .
  FILTER(CONTAINS(LCASE(STR(?displayName)), LCASE('LAKE REGIONAL')))
}
```

**Seed 1 (wrong) — answer:** `0`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `0`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `0`

```sparql
(none)
```

### 7.154 `holdout-v2-npi-registry` — q-007 (T2)

**Question:** How many specialties have 'Technician' in their specialty name?

**Expected answer:** `16`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?specialty) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-npi-registry> WHERE {
  ?specialty <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/type/Specialty> .
  ?specialty <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/specialty_name> ?specialtyName .
  FILTER(CONTAINS(LCASE(STR(?specialtyName)), LCASE('Technician')))
}
```

**Seed 1 (wrong) — answer:** `7`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `7`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `7`

```sparql
(none)
```

### 7.155 `holdout-v2-npi-registry` — q-009 (T3)

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

**Seed 1 (wrong) — answer:** `1508471889__loc: 1, 1750383493__loc: 1, 1447098678__loc: 1, 1235886961__loc: 1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `1508471889__loc: 1, 1750383493__loc: 1, 1447098678__loc: 1, 1235886961__loc: 1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `1508471889__loc: 1, 1750383493__loc: 1, 1447098678__loc: 1, 1235886961__loc: 1`

```sparql
(none)
```

### 7.156 `holdout-v2-npi-registry` — q-010 (T3)

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

**Seed 1 (wrong) — answer:** `OH__dublin (9), NY__new_york (1)`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `OH__dublin (9), NY__new_york (1)`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `OH__dublin (9), NY__new_york (1)`

```sparql
(none)
```

### 7.157 `holdout-v2-npi-registry` — q-011 (T3)

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

**Seed 1 (wrong) — answer:** `6, 6, 29, 2, 1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `6, 6, 29, 2, 1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `6, 6, 29, 2, 1`

```sparql
(none)
```

### 7.158 `holdout-v2-ofr-financial-stability` — q-004 (T1)

**Question:** How many unique years are recorded in the dataset?

**Expected answer:** `27`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?yearValue) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ofr-financial-stability> WHERE { ?year <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/type/Year> . ?year <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/year> ?yearValue . }
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

### 7.159 `holdout-v2-ofr-financial-stability` — q-005 (T2)

**Question:** What is the total number of readings for stress levels with a lower bound greater than 0?

**Expected answer:** `4666`

**Gold SPARQL:**
```sparql
SELECT (SUM(xsd:integer(?stressLevelReadingCount)) AS ?totalReadingCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ofr-financial-stability> WHERE { ?stressLevel <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/type/StressLevel> . ?stressLevel <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/lower_bound> ?lowerBound . ?stressLevel <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/reading_count> ?stressLevelReadingCount . FILTER (xsd:integer(?lowerBound) > 0) }
```

**Seed 1 (wrong) — answer:** `22260`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `22260`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `23373`

```sparql
(none)
```

### 7.160 `holdout-v2-ofr-financial-stability` — q-008 (T2)

**Question:** How many indicator readings have a value greater than 0.5?

**Expected answer:** `12343`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?reading) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ofr-financial-stability> WHERE {
  ?reading <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/type/IndicatorReading> .
  ?reading <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/value> ?value .
  FILTER(xsd:float(?value) > 0.5)
}
```

**Seed 1 (wrong) — answer:** `2`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `2`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `2`

```sparql
(none)
```

### 7.161 `holdout-v2-ofr-financial-stability` — q-011 (T4)

**Question:** What are the names of indicators that have readings associated with a 'Low' stress level, and how many such readings does each indicator have?

**Expected answer:** `Safe Assets | 2946
Equity Valuation | 2469
Emerging Markets | 2447
Funding | 2215
Volatility | 2166
Credit | 2114
Other Advanced Economies | 2110
United States | 1272
OFR Financial Stress Index | 968`

**Gold SPARQL:**
```sparql
SELECT ?indicatorName (COUNT(?reading) AS ?readingCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ofr-financial-stability> WHERE {
  ?indicator <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/type/Indicator> .
  ?indicator <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/indicator_name> ?indicatorName .
  ?indicator <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/indicator_has_reading> ?reading .
  ?reading <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/type/IndicatorReading> .
  ?reading <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/reading_has_stress_level> ?stressLevel .
  ?stressLevel <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/type/StressLevel> .
  ?stressLevel <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/stress_level_label> "Low" .
} GROUP BY ?indicatorName
ORDER BY DESC(?readingCount)
LIMIT 10
```

**Seed 1 (wrong) — answer:** `safe_assets, 6`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `safe_assets, 6`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `safe_assets, 6`

```sparql
(none)
```

### 7.162 `holdout-v2-ofr-financial-stability` — q-012 (T4)

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

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `1.6205`

```sparql
(none)
```

### 7.163 `holdout-v2-ofr-financial-stability` — q-013 (T4)

**Question:** Find the indicator categories that contain indicators with readings in the '2010' decade, and count how many such indicators each category has.

**Expected answer:** `Subindex | 5
Regional | 3
Composite | 1`

**Gold SPARQL:**
```sparql
SELECT ?categoryName (COUNT(DISTINCT ?indicator) AS ?indicatorCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ofr-financial-stability> WHERE {
  ?category <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/type/IndicatorCategory> .
  ?category <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/category_name> ?categoryName .
  ?category <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/category_includes_indicator> ?indicator .
  ?indicator <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/type/Indicator> .
  ?indicator <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/indicator_has_reading> ?reading .
  ?reading <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/type/IndicatorReading> .
  ?reading <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/reading_on_period> ?period .
  ?period <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/type/Period> .
  ?period <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/period_in_month> ?month .
  ?month <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/type/Month> .
  ?month <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/month_in_year> ?year .
  ?year <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/type/Year> .
  ?year <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/decade> "2010" .
} GROUP BY ?categoryName
ORDER BY DESC(?indicatorCount)
```

**Seed 1 (wrong) — answer:** `Insufficient rows to compute the exact answer. The provided readings are for the year 2000, not the 2010 decade.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

### 7.164 `holdout-v2-pacer-federal-dockets` — q-001 (T1)

**Question:** How many dockets are there in total?

**Expected answer:** `3980`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?docket) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-pacer-federal-dockets> WHERE {
  ?docket <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/Docket> .
}
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

### 7.165 `holdout-v2-pacer-federal-dockets` — q-004 (T1)

**Question:** How many distinct case types are recorded?

**Expected answer:** `17`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?caseType) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-pacer-federal-dockets> WHERE {
  ?caseType <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/CaseType> .
}
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

### 7.166 `holdout-v2-pacer-federal-dockets` — q-005 (T2)

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

**Seed 1 (wrong) — answer:** `3:26-cv-00152, 2:26-mc-00616, 2:26-sw-00292, 8:26-cr-00042, 8:26-cr-00042, 2:26-mj-02252, 2:26-cm-00018, 4:26-cr-00077, 5:26-cr-50017, 2:26-mb-09070`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `3:26-cv-00152, 2:26-mc-00616, 2:26-sw-00292, 8:26-cr-00042, 8:26-cr-00042, 2:26-mj-02252, 2:26-cm-00018, 4:26-cr-00077, 5:26-cr-50017, 2:26-mb-09070`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `3:26-cv-00152, 2:26-mc-00616, 2:26-sw-00292, 8:26-cr-00042, 8:26-cr-00042, 2:26-mj-02252, 2:26-cm-00018, 4:26-cr-00077, 5:26-cr-50017, 2:26-mb-09070`

```sparql
(none)
```

### 7.167 `holdout-v2-pacer-federal-dockets` — q-007 (T2)

**Question:** How many dockets have a short case name starting with 'S'?

**Expected answer:** `313`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?docket) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-pacer-federal-dockets> WHERE {
  ?docket <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/Docket> .
  ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_case_name_short> ?short_name .
  FILTER(STRSTARTS(?short_name, "S"))
}
```

**Seed 1 (wrong) — answer:** `3`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `3`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `3`

```sparql
(none)
```

### 7.168 `holdout-v2-pacer-federal-dockets` — q-008 (T3)

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

**Seed 1 (wrong) — answer:** `alnd, cand, ared`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `alnd, cand, ared`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `alnd, cand, ared`

```sparql
(none)
```

### 7.169 `holdout-v2-pacer-federal-dockets` — q-009 (T3)

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

**Seed 1 (wrong) — answer:** `r. austin huffaker jr., 1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `r. austin huffaker jr., 1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `r. austin huffaker jr., 1`

```sparql
(none)
```

### 7.170 `holdout-v2-pacer-federal-dockets` — q-010 (T3)

**Question:** What are the different nature of suits associated with dockets filed in the year '2026'?

**Expected answer:** `Contract: Other, Habeas Corpus - Alien Detainee, Personal Inj. Prod. Liability, Civil Rights: Jobs, Motor Vehicle, Civil Rights: Americans with Disabilities - Other, Anti-Trust, Mandamus & Other, Civil Rights: Other, Personal Property: Other ... (60 total)`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?nos_label FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-pacer-federal-dockets> WHERE {
  ?docket <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/Docket> .
  ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_has_nature_of_suit> ?nos .
  ?nos <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/NatureOfSuit> .
  ?nos <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/nos_label> ?nos_label .
  ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_filed_in_year> ?year .
  ?year <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/Year> .
  ?year <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/year_value> "2026" .
}
```

**Seed 1 (wrong) — answer:** `890 Other Statutory Actions, 895 Freedom of Information Act`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `890 Other Statutory Actions, 895 Freedom of Information Act`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `890 Other Statutory Actions, 895 Freedom of Information Act`

```sparql
(none)
```

### 7.171 `holdout-v2-pacer-federal-dockets` — q-011 (T4)

**Question:** What is the average number of dockets assigned to judges in each court?

**Expected answer:** `District Court, D. Guam | 5
District Court, District of Columbia | 1.42857142857142857143
District Court, M.D. Alabama | 4
District Court, N.D. Alabama | 2.1
District Court, S.D. Alabama | 3.8
District Court, D. Alaska | 7.75
District Court, D. Arizona | 1.4
District Court, E.D. Arkansas | 4
District Court, W.D. Arkansas | 4
District Court, C.D. California | 1
... (91 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?court_name (AVG(?docket_count) AS ?average_dockets_per_judge) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-pacer-federal-dockets> WHERE {
  ?court <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/Court> .
  ?court <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/court_full_name> ?court_name .
  {
    SELECT ?court (COUNT(DISTINCT ?docket) AS ?docket_count) WHERE {
      ?judge <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/Judge> .
      ?judge <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/judge_sits_in_court> ?court .
      ?docket <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/Docket> .
      ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_assigned_to_judge> ?judge .
      ?court <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/Court> .
    } GROUP BY ?court ?judge
  }
} GROUP BY ?court_name
LIMIT 100
```

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

### 7.172 `holdout-v2-pacer-federal-dockets` — q-012 (T4)

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

**Seed 1 (wrong) — answer:** `dcd, 2026, 1.0`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `dcd, 2026, 1.0`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `dcd, 2026, 1.0`

```sparql
(none)
```

### 7.173 `holdout-v2-pacer-federal-dockets` — q-013 (T4)

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

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

### 7.174 `holdout-v2-patentsview` — q-001 (T1)

**Question:** How many citations are there in the database?

**Expected answer:** `117660`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?x) AS ?n) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview> WHERE { ?x <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Citation> . }
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

### 7.175 `holdout-v2-patentsview` — q-002 (T1)

**Question:** How many patents are in the database?

**Expected answer:** `92836`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?x) AS ?n) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview> WHERE { ?x <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Patent> . }
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

### 7.176 `holdout-v2-patentsview` — q-003 (T1)

**Question:** How many unique CPC classes are defined in the system?

**Expected answer:** `25447`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?x) AS ?n) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview> WHERE { ?x <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/CPCClass> . }
```

**Seed 1 (wrong) — answer:** `7`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `6 (C08, C07, C01, C21, C13, C22, Y10)

Wait, let me re-count:
1. C08
2. C07
3. C01
4. C21
5. C13
6. C22
7. Y10

7`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `7`

```sparql
(none)
```

### 7.177 `holdout-v2-patentsview` — q-004 (T1)

**Question:** How many inventors are listed in the database?

**Expected answer:** `14039`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?x) AS ?n) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview> WHERE { ?x <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Inventor> . }
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

### 7.178 `holdout-v2-patentsview` — q-005 (T2)

**Question:** How many utility patents have more than 20 claims?

**Expected answer:** `635`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?patent) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview> WHERE { ?patent <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Patent> . ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/patent_type> "utility" . ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/num_claims> ?numClaims . FILTER(xsd:integer(?numClaims) > 20) }
```

**Seed 1 (wrong) — answer:** `4`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `4`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `4`

```sparql
(none)
```

### 7.179 `holdout-v2-patentsview` — q-006 (T2)

**Question:** How many inventors are listed with a sequence number greater than 10?

**Expected answer:** `77`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?inventor) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview> WHERE { ?inventor <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Inventor> . ?inventor <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/inventor_sequence> ?seq . FILTER(xsd:integer(?seq) > 10) }
```

**Seed 1 (wrong) — answer:** `0`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `0`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `0`

```sparql
(none)
```

### 7.180 `holdout-v2-patentsview` — q-007 (T2)

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

**Seed 1 (wrong) — answer:** `Divergent Technologies, Inc., GOOGLE TECHNOLOGY HOLDINGS LLC`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Divergent Technologies, Inc., GOOGLE TECHNOLOGY HOLDINGS LLC`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Divergent Technologies, Inc., GOOGLE TECHNOLOGY HOLDINGS LLC`

```sparql
(none)
```

### 7.181 `holdout-v2-patentsview` — q-008 (T3)

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

**Seed 1 (wrong) — answer:** `10000001: 1, 10000011: 1, 10000007: 1, 10000025: 1, 10000010: 2, 10000023: 1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `10000001: 1, 10000011: 1, 10000007: 1, 10000025: 1, 10000010: 2, 10000023: 1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `10000001: 1, 10000011: 1, 10000007: 1, 10000025: 1, 10000010: 2, 10000023: 1`

```sparql
(none)
```

### 7.182 `holdout-v2-patentsview` — q-009 (T3)

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

**Seed 1 (wrong) — answer:** `Samuel Melvin Smith, 1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Samuel Melvin Smith, 1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Samuel Melvin Smith, 1`

```sparql
(none)
```

### 7.183 `holdout-v2-patentsview` — q-010 (T3)

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

**Seed 1 (wrong) — answer:** `10000011 (3), 10000007 (2), 10000018 (2), 10000028 (1), 10000008 (1), 10000001 (1)`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `10000011 (3), 10000007 (2), 10000018 (2), 10000028 (1), 10000008 (1), 10000001 (1)`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `10000011 (3), 10000007 (2), 10000018 (2), 10000028 (1), 10000008 (1), 10000001 (1)`

```sparql
(none)
```

### 7.184 `holdout-v2-samhsa-n-ssats` — q-001 (T1)

**Question:** How many treatment facilities are there?

**Expected answer:** `16066`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?facility) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-samhsa-n-ssats> WHERE { ?facility <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/TreatmentFacility> . }
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

### 7.185 `holdout-v2-samhsa-n-ssats` — q-004 (T1)

**Question:** How many states are represented in the dataset?

**Expected answer:** `53`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?state) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-samhsa-n-ssats> WHERE { ?state <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/State> . }
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

### 7.186 `holdout-v2-samhsa-n-ssats` — q-005 (T2)

**Question:** How many states have a facility count greater than 500?

**Expected answer:** `8`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?state) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-samhsa-n-ssats> WHERE {
  ?state <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/State> .
  ?state <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/facility_count> ?facility_count_str .
  FILTER(xsd:integer(?facility_count_str) > 500)
}
```

**Seed 1 (wrong) — answer:** `3`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `3`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `3`

```sparql
(none)
```

### 7.187 `holdout-v2-samhsa-n-ssats` — q-008 (T3)

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

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

### 7.188 `holdout-v2-samhsa-n-ssats` — q-009 (T3)

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

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

### 7.189 `holdout-v2-samhsa-n-ssats` — q-010 (T4)

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

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

### 7.190 `holdout-v2-samhsa-n-ssats` — q-011 (T4)

**Question:** What is the average number of distinct services offered by treatment facilities that accept 'Medicaid' as a payment type?

**Expected answer:** `5.38`

**Gold SPARQL:**
```sparql
SELECT (AVG(?distinct_services) AS ?average_distinct_services) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-samhsa-n-ssats> WHERE {
  ?facility <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/TreatmentFacility> .
  ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/accepts_payment> ?paymentType .
  ?paymentType <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/PaymentType> .
  ?paymentType <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/payment_name> "Medicaid" .
  {
    SELECT ?facility (COUNT(DISTINCT ?service) AS ?distinct_services) WHERE {
      ?facility <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/TreatmentFacility> .
      ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/offers_service> ?service .
      ?service <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/Service> .
    } GROUP BY ?facility
  }
}
```

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

### 7.191 `holdout-v2-scdb-supreme-court` — q-001 (T1)

**Question:** How many unique Supreme Court cases are recorded?

**Expected answer:** `9341`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?case) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-scdb-supreme-court> WHERE {
  ?case <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-scdb-supreme-court/type/Case> .
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

### 7.192 `holdout-v2-scdb-supreme-court` — q-003 (T1)

**Question:** How many distinct issue areas are there?

**Expected answer:** `14`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?issueArea) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-scdb-supreme-court> WHERE {
  ?issueArea <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-scdb-supreme-court/type/IssueArea> .
}
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

### 7.193 `holdout-v2-scdb-supreme-court` — q-006 (T2)

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

**Seed 1 (wrong) — answer:** `9`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `9`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `9`

```sparql
(none)
```

### 7.194 `holdout-v2-scdb-supreme-court` — q-009 (T3)

**Question:** How many cases were decided in each term, specifically for terms after 1980?

**Expected answer:** `1981 | 177
1982 | 166
1983 | 173
1984 | 165
1985 | 165
1986 | 161
1987 | 153
1988 | 152
1989 | 140
1990 | 129
... (44 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?term_name (COUNT(DISTINCT ?case) AS ?case_count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-scdb-supreme-court> WHERE {
  ?case <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-scdb-supreme-court/type/Case> .
  ?case <https://omnix.dev/holdout-v2/holdout-v2-scdb-supreme-court/pred/case_decided_in_term> ?term .
  ?term <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-scdb-supreme-court/type/Term> .
  ?term <https://omnix.dev/holdout-v2/holdout-v2-scdb-supreme-court/pred/term> ?term_name .
  FILTER (xsd:integer(?term_name) > 1980)
} GROUP BY ?term_name ORDER BY ?term_name
```

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

### 7.195 `holdout-v2-scdb-supreme-court` — q-011 (T4)

**Question:** List the names of Justices who voted in cases where the party winning was '1' and the case was decided in the '1996' term.

**Expected answer:** `WHRehnquist, JPStevens, SDOConnor, AScalia, AMKennedy, DHSouter, CThomas, RBGinsburg, SGBreyer`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?justice_name FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-scdb-supreme-court> WHERE {
  ?vote <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-scdb-supreme-court/type/Vote> .
  ?vote <https://omnix.dev/holdout-v2/holdout-v2-scdb-supreme-court/pred/vote_by_justice> ?justice .
  ?justice <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-scdb-supreme-court/type/Justice> .
  ?justice <https://omnix.dev/holdout-v2/holdout-v2-scdb-supreme-court/pred/justice_name> ?justice_name .
  ?vote <https://omnix.dev/holdout-v2/holdout-v2-scdb-supreme-court/pred/vote_in_case> ?case .
  ?case <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-scdb-supreme-court/type/Case> .
  ?case <https://omnix.dev/holdout-v2/holdout-v2-scdb-supreme-court/pred/party_winning> "1" .
  ?case <https://omnix.dev/holdout-v2/holdout-v2-scdb-supreme-court/pred/case_decided_in_term> ?term .
  ?term <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-scdb-supreme-court/type/Term> .
  ?term <https://omnix.dev/holdout-v2/holdout-v2-scdb-supreme-court/pred/term> "1996" .
}
```

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

### 7.196 `holdout-v2-sec-edgar-10k` — q-001 (T1)

**Question:** How many filers are there?

**Expected answer:** `488`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?filer) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-sec-edgar-10k> WHERE { ?filer <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/type/Filer> . }
```

**Seed 1 (wrong) — answer:** `9`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `9`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `8`

```sparql
(none)
```

### 7.197 `holdout-v2-sec-edgar-10k` — q-003 (T1)

**Question:** How many filings are recorded in the system?

**Expected answer:** `511`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?filing) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-sec-edgar-10k> WHERE { ?filing <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/type/Filing> . }
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

### 7.198 `holdout-v2-sec-edgar-10k` — q-005 (T2)

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

**Seed 1 (wrong) — answer:** `7`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `7`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `7`

```sparql
(none)
```

### 7.199 `holdout-v2-sec-edgar-10k` — q-006 (T2)

**Question:** What is the total number of filers whose fiscal year ends in January?

**Expected answer:** `22`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?filer) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-sec-edgar-10k> WHERE {
  ?filer <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/type/Filer> .
  ?filer <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filer_fiscal_year_end> ?fiscalYearEnd .
  FILTER(STRSTARTS(?fiscalYearEnd, "01"))
}
```

**Seed 1 (wrong) — answer:** `4`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `4`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `4`

```sparql
(none)
```

### 7.200 `holdout-v2-sec-edgar-10k` — q-007 (T3)

**Question:** How many filings are associated with each filer whose entity type is 'operating'?

**Expected answer:** `CitroTech Inc. | 11
FEDERAL HOME LOAN MORTGAGE CORP | 3
SOUTHERN CO | 3
Xerox Holdings Corp | 2
EPR PROPERTIES | 2
ENBRIDGE INC | 2
Public Storage | 2
SolarWindow Technologies, Inc. | 2
VALLEY NATIONAL BANCORP | 2
FIFTH THIRD BANCORP | 2
... (488 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?filer_name (COUNT(?filing) AS ?filing_count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-sec-edgar-10k> WHERE {
  ?filer <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/type/Filer> .
  ?filer <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filer_entity_type> "operating" .
  ?filer <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/has_filing> ?filing .
  ?filing <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/type/Filing> .
  ?filer <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filer_name> ?filer_name .
} GROUP BY ?filer_name
ORDER BY DESC(?filing_count)
```

**Seed 1 (wrong) — answer:** `0000074046: 1, 0000712770: 1, 0001348952: 1, 0001083490: 1, 0001802457: 1, 0001782170: 1, 0001577670: 1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `0000074046: 1, 0000712770: 1, 0001348952: 1, 0001083490: 1, 0001802457: 1, 0001782170: 1, 0001577670: 1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `0000074046: 1, 0000712770: 1, 0001348952: 1, 0001083490: 1, 0001802457: 1, 0001782170: 1, 0001577670: 1`

```sparql
(none)
```

### 7.201 `holdout-v2-sec-edgar-10k` — q-008 (T3)

**Question:** How many filings are associated with each filer?

**Expected answer:** `CitroTech Inc. | 11
SOUTHERN CO | 3
FEDERAL HOME LOAN MORTGAGE CORP | 3
FIFTH THIRD BANCORP | 2
Soluna Holdings, Inc | 2
FEDERAL AGRICULTURAL MORTGAGE CORP | 2
EPR PROPERTIES | 2
ENBRIDGE INC | 2
VALLEY NATIONAL BANCORP | 2
Xerox Holdings Corp | 2`

**Gold SPARQL:**
```sparql
SELECT ?filer_name (COUNT(?filing) AS ?filing_count)
FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-sec-edgar-10k>
WHERE {
  ?filer <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/type/Filer> .
  ?filer <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/has_filing> ?filing .
  ?filing <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/type/Filing> .
  ?filer <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filer_name> ?filer_name .
}
GROUP BY ?filer_name
ORDER BY DESC(?filing_count)
LIMIT 10
```

**Seed 1 (wrong) — answer:** `0000074046: 1, 0001083490: 1, 0001412408: 1, 0001837774: 1, 0000092122: 1, 0001577670: 1, 0000036104: 1, 0001560258: 1, 0000720762: 1, 0000037996: 1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `0000074046: 1, 0001083490: 1, 0001412408: 1, 0001837774: 1, 0000092122: 1, 0001577670: 1, 0000036104: 1, 0001560258: 1, 0000720762: 1, 0000037996: 1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `0000074046: 1, 0001083490: 1, 0001412408: 1, 0001837774: 1, 0000092122: 1, 0001577670: 1, 0000036104: 1, 0001560258: 1, 0000720762: 1, 0000037996: 1`

```sparql
(none)
```

### 7.202 `holdout-v2-sec-edgar-10k` — q-009 (T3)

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

**Seed 1 (wrong) — answer:** `0001626878, 0001837774, 0001083490, 0001539680, 0000036104, 0000884650, 0000924719, 0001869601, 0001001838, 0001471824`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `0001626878, 0001837774, 0001083490, 0001539680, 0000036104, 0000884650, 0000924719, 0001869601, 0001001838, 0001471824`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `0001626878, 0001837774, 0001083490, 0001539680, 0000036104, 0000884650, 0000924719, 0001869601, 0001001838, 0001471824`

```sparql
(none)
```

### 7.203 `holdout-v2-sec-edgar-10k` — q-011 (T4)

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

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer. Only one filer name (Aon plc) is provided in the context, and its state of incorporation (L2) does not contain the letter 'A'.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer. (The provided context only contains the state of incorporation for one filer, Aon plc, which is 'L2'.)`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer. (The provided context only contains the state of incorporation for one filer, Aon plc, which is 'L2'.)`

```sparql
(none)
```

### 7.204 `holdout-v2-sec-edgar-10k` — q-012 (T4)

**Question:** List the SIC codes and their descriptions for filers that have a fiscal year ending in 'December'.

**Expected answer:** `3690 | Miscellaneous Electrical Machinery, Equipment & Supplies
6513 | Operators of  Apartment Buildings
0700 | Agricultural Services
3845 | Electromedical & Electrotherapeutic Apparatus
4911 | Electric Services
4011 | Railroads, Line-Haul Operating
1040 | Gold and Silver Ores
6500 | Real Estate
7822 | Services-Motion Picture & Video Tape Distribution
7990 | Services-Miscellaneous Amusement & Recreation
... (145 total rows)`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?sic_code ?sic_description FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-sec-edgar-10k> WHERE {
  ?filer <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/type/Filer> .
  ?filer <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filer_has_fiscal_year> ?fiscal_year .
  ?fiscal_year <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/type/FiscalYear> .
  ?fiscal_year <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/fiscal_year_end_month> "December" .
  ?filer <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filer_in_sic> ?sic_code_entity .
  ?sic_code_entity <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/type/SICCode> .
  ?sic_code_entity <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/sic_code> ?sic_code .
  ?sic_code_entity <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/sic_description> ?sic_description .
}
```

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows provided are insufficient to compute the exact answer.`

```sparql
(none)
```

### 7.205 `holdout-v2-usda-agricultural-statistics` — q-001 (T1)

**Question:** How many distinct commodities are recorded in the dataset?

**Expected answer:** `58`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?commodity) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-usda-agricultural-statistics> WHERE {
  ?commodity <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-usda-agricultural-statistics/type/Commodity> .
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

### 7.206 `holdout-v2-usda-agricultural-statistics` — q-007 (T2)

**Question:** How many states have 'I' in their state name?

**Expected answer:** `28`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?state) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-usda-agricultural-statistics> WHERE {
  ?state <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-usda-agricultural-statistics/type/State> .
  ?state <https://omnix.dev/holdout-v2/holdout-v2-usda-agricultural-statistics/pred/state_name> ?state_name .
  FILTER(CONTAINS(LCASE(STR(?state_name)), "i"))
}
```

**Seed 1 (wrong) — answer:** `7`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `7`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `7`

```sparql
(none)
```

### 7.207 `holdout-v2-usda-agricultural-statistics` — q-009 (T3)

**Question:** How many observations are there for each commodity?

**Expected answer:** `AG LAND | 3068
SOYBEANS | 745
WHEAT | 721
CORN | 628
COTTON | 476
VEGETABLE TOTALS | 439
APPLES | 292
GRAPES | 285
CHERRIES | 188
LETTUCE | 175
... (58 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?commodityLabel (COUNT(?observation) AS ?observationCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-usda-agricultural-statistics> WHERE {
  ?observation <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-usda-agricultural-statistics/type/Observation> .
  ?observation <https://omnix.dev/holdout-v2/holdout-v2-usda-agricultural-statistics/pred/observation_of_commodity> ?commodity .
  ?commodity <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-usda-agricultural-statistics/type/Commodity> .
  ?commodity <http://www.w3.org/2000/01/rdf-schema#label> ?commodityLabel .
} GROUP BY ?commodityLabel ORDER BY DESC(?observationCount)
```

**Seed 1 (wrong) — answer:** `BEANS: 2, CORN: 3, SOYBEANS: 3, COTTON: 2`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `BEANS: 2, CORN: 3, SOYBEANS: 3, COTTON: 2`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `BEANS: 2, CORN: 3, SOYBEANS: 3, COTTON: 2`

```sparql
(none)
```

### 7.208 `holdout-v2-usda-agricultural-statistics` — q-010 (T4)

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

**Seed 1 (wrong) — answer:** `SOYBEANS: 0.48233333333333334`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `SOYBEANS: 0.48233333333333334`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `SOYBEANS: 0.48233333333333334`

```sparql
(none)
```

### 7.209 `holdout-v2-usda-agricultural-statistics` — q-011 (T4)

**Question:** List the states and the count of distinct statistic categories observed in each state.

**Expected answer:** `OREGON | 3
NEW JERSEY | 3
CALIFORNIA | 3
NORTH DAKOTA | 3
ALABAMA | 3
MONTANA | 3
WASHINGTON | 3
WISCONSIN | 3
PENNSYLVANIA | 3
INDIANA | 3
... (50 total rows)`

**Gold SPARQL:**
```sparql
SELECT ?stateName (COUNT(DISTINCT ?statCat) AS ?distinctStatisticCategories) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-usda-agricultural-statistics> WHERE {
  ?observation <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-usda-agricultural-statistics/type/Observation> .
  ?observation <https://omnix.dev/holdout-v2/holdout-v2-usda-agricultural-statistics/pred/observation_in_state> ?state .
  ?state <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-usda-agricultural-statistics/type/State> .
  ?state <https://omnix.dev/holdout-v2/holdout-v2-usda-agricultural-statistics/pred/state_name> ?stateName .
  ?observation <https://omnix.dev/holdout-v2/holdout-v2-usda-agricultural-statistics/pred/observation_stat_category> ?statisticCategory .
  ?statisticCategory <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-usda-agricultural-statistics/type/StatisticCategory> .
  ?statisticCategory <https://omnix.dev/holdout-v2/holdout-v2-usda-agricultural-statistics/pred/stat_cat> ?statCat .
} GROUP BY ?stateName
```

**Seed 1 (wrong) — answer:** `AZ: 1, CA: 1, MI: 1, NJ: 1, NY: 1, OR: 1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `AZ: 1, CA: 1, MI: 1, NJ: 1, NY: 1, OR: 1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `AZ: 1, CA: 1, MI: 1, NJ: 1, NY: 1, OR: 1`

```sparql
(none)
```

### 7.210 `holdout-v2-usda-agricultural-statistics` — q-012 (T4)

**Question:** Which commodities have observations with a 'PRACTICE, SUPPRESSION' domain description, and what are their units?

**Expected answer:** `COTTON | PCT OF AREA PLANTED
PEANUTS | PCT OF AREA PLANTED
WHEAT | PCT OF AREA PLANTED
VEGETABLE TOTALS | PCT OF AREA PLANTED
BARLEY | PCT OF AREA PLANTED
POTATOES | PCT OF AREA PLANTED
CORN | PCT OF AREA PLANTED
SOYBEANS | PCT OF AREA PLANTED
RICE | PCT OF AREA PLANTED
SORGHUM | PCT OF AREA PLANTED
... (22 total rows)`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?commodityLabel ?unitDescription FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-usda-agricultural-statistics> WHERE {
  ?observation <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-usda-agricultural-statistics/type/Observation> .
  ?observation <https://omnix.dev/holdout-v2/holdout-v2-usda-agricultural-statistics/pred/domain_desc> "PRACTICE, SUPPRESSION" .
  ?observation <https://omnix.dev/holdout-v2/holdout-v2-usda-agricultural-statistics/pred/observation_of_commodity> ?commodity .
  ?commodity <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-usda-agricultural-statistics/type/Commodity> .
  ?commodity <http://www.w3.org/2000/01/rdf-schema#label> ?commodityLabel .
  ?observation <https://omnix.dev/holdout-v2/holdout-v2-usda-agricultural-statistics/pred/observation_unit> ?unit .
  ?unit <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-usda-agricultural-statistics/type/Unit> .
  ?unit <https://omnix.dev/holdout-v2/holdout-v2-usda-agricultural-statistics/pred/unit_desc> ?unitDescription .
}
```

**Seed 1 (wrong) — answer:** `COTTON (PCT OF OPERATIONS), CORN (PCT OF OPERATIONS), CORN (PCT OF AREA PLANTED), VEGETABLE TOTALS (PCT OF OPERATIONS)`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `COTTON (PCT OF OPERATIONS), CORN (PCT OF OPERATIONS), CORN (PCT OF AREA PLANTED), VEGETABLE TOTALS (PCT OF OPERATIONS)`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `COTTON (PCT OF OPERATIONS), CORN (PCT OF OPERATIONS), CORN (PCT OF AREA PLANTED), VEGETABLE TOTALS (PCT OF OPERATIONS)`

```sparql
(none)
```

### 7.211 `holdout-v2-uspto-trademarks` — q-001 (T1)

**Question:** How many distinct trademarks are there?

**Expected answer:** `434`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?trademark) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-uspto-trademarks> WHERE { ?trademark <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/Trademark> . }
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

### 7.212 `holdout-v2-uspto-trademarks` — q-004 (T1)

**Question:** How many different countries are represented in the dataset?

**Expected answer:** `22`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?country) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-uspto-trademarks> WHERE { ?country <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/Country> . }
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

### 7.213 `holdout-v2-uspto-trademarks` — q-005 (T2)

**Question:** How many trademarks have a serial number starting with '770000'?

**Expected answer:** `89`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?trademark) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-uspto-trademarks> WHERE {
  ?trademark <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/Trademark> .
  ?trademark <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/serial_number> ?serialNumber .
  FILTER(STRSTARTS(?serialNumber, "770000"))
}
```

**Seed 1 (wrong) — answer:** `3`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `3`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `3`

```sparql
(none)
```

### 7.214 `holdout-v2-uspto-trademarks` — q-007 (T2)

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

**Seed 1 (wrong) — answer:** `3`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `3`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `3`

```sparql
(none)
```

### 7.215 `holdout-v2-uspto-trademarks` — q-009 (T3)

**Question:** How many trademarks are owned by each owner, for owners located in the city of 'NEW YORK'?

**Expected answer:** `NEWTEK BUSINESS SERVICES CORP. | 2
LIBERTY SKIS CORPORATION | 1
BANDSINTOWN MEDIA LLC | 1
TPC NEW YORK CORP. | 1
C.A.R.E. WEAR | 1`

**Gold SPARQL:**
```sparql
SELECT ?ownerName (COUNT(DISTINCT ?trademark) AS ?trademarkCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-uspto-trademarks> WHERE {
  ?owner <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/Owner> .
  ?owner <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/owner_owns_trademark> ?trademark .
  ?trademark <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/Trademark> .
  ?owner <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/owner_name> ?ownerName .
  ?owner <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/city> "NEW YORK" .
} GROUP BY ?ownerName ORDER BY DESC(?trademarkCount) LIMIT 10
```

**Seed 1 (wrong) — answer:** `NEWTEK BUSINESS SERVICES CORP.: 1, D&J NYC LLC: 1, Gucci America, Inc.: 1, Best Brands Consumer Products, Inc.: 1, Wathne Limited: 1, Sucre Designs, LLC: 1, HarperCollins Publishers L.L.C.: 1`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `NEWTEK BUSINESS SERVICES CORP.: 1, D&J NYC LLC: 1, Gucci America, Inc.: 1, Best Brands Consumer Products, Inc.: 1, Wathne Limited: 1, Sucre Designs, LLC: 1, HarperCollins Publishers L.L.C.: 1`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `NEWTEK BUSINESS SERVICES CORP.: 1, D&J NYC LLC: 1, Gucci America, Inc.: 1, Best Brands Consumer Products, Inc.: 1, Wathne Limited: 1, Sucre Designs, LLC: 1, HarperCollins Publishers L.L.C.: 1`

```sparql
(none)
```

### 7.216 `holdout-v2-uspto-trademarks` — q-010 (T3)

**Question:** What are the descriptions of goods and services that belong to Nice Class '001'?

**Expected answer:** `Potting soil; unexposed photographic film, Chemicals for use in industry; unprocessed artificial resins, unprocessed synthetic resins and artificial resin material, namely, unprocessed artificial resins and synthetic resins as raw material in the form of liquids and chips; unprocessed plastics in the form of chips and pellets; raw materials in the nature of unprocessed artificial and synthetic resins in the form of powders, liquids or chips for the production of laminate in particular for use as coatings for floors, reservoirs and swimmi, chemical ingredient for cosmetic products, Chemical ingredient for cosmetic products, Water soluble polymers for use in the manufacture of industrial applications, Botanical extracts for use in the manufacture of nutraceuticals and pharmaceuticals, Organic growing media which may or may not be mixed with other materials to grow plants, grasses and similar items and for other horticultural uses, Botanical extracts for use as nutritional ingredients in the further manufacture of goods and botanical extracts for use in the manufacture of nutraceuticals, Chemicals for use in manufacturing lubricants, coatings, polyeurthanes, fibers, apparel and cosmetics, Chemicals for use in the manufacture of a wide variety of goods ... (11 total)`

**Gold SPARQL:**
```sparql
SELECT DISTINCT ?goodsDescription FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-uspto-trademarks> WHERE {
  ?goods <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/GoodsServices> .
  ?goods <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/goods_in_nice_class> ?niceClass .
  ?niceClass <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/NiceClass> .
  ?niceClass <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/class_code> "001" .
  ?goods <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/goods_description> ?goodsDescription .
}
```

**Seed 1 (wrong) — answer:** `Insufficient rows to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `Insufficient rows to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `Insufficient rows to compute the exact answer.`

```sparql
(none)
```

### 7.217 `holdout-v2-uspto-trademarks` — q-011 (T3)

**Question:** Count the number of trademarks associated with each status type.

**Expected answer:** `Registration cancelled because registrant did not file an acceptable declaration under Section 8.  To view all documents in this file, click on the Trademark Document Retrieval link at the top of this page. | 141
Abandoned because the applicant failed to respond or filed a late response to an Office action.  To view all documents in this file, click on the Trademark Document Retrieval link at the top of this page. | 116
The registration has been renewed. | 98
Abandoned because no Statement of Use or Extension Request timely filed after Notice of Allowance was issued.  To view all documents in this file, click on the Trademark Document Retrieval link at the top of this page. | 66
Abandoned because the applicant filed an express abandonment.  To view all documents in this file, click on the Trademark Document Retrieval link at the top of this page. | 4
Registration cancelled under Section 7 because the registrant surrendered the registration. | 4
Abandoned after an appeal of the examining attorney's final refusal.  For further information, see TTABVUE on the Trademark Trial and Appeal Board web page. | 2
Abandoned after an inter partes decision by the Trademark Trial and Appeal Board.  For further information, see TTABVUE on the Trademark Trial and Appeal Board web page. | 2
Abandoned due to incomplete response.  The response did not satisfy all issues in the Office action.  To view all documents in this file, click on the Trademark Document Retrieval link at the top of this page. | 1`

**Gold SPARQL:**
```sparql
SELECT ?statusDescription (COUNT(DISTINCT ?trademark) AS ?trademarkCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-uspto-trademarks> WHERE {
  ?trademark <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/Trademark> .
  ?trademark <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/trademark_has_status> ?statusType .
  ?statusType <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/StatusType> .
  ?statusType <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/status_description> ?statusDescription .
} GROUP BY ?statusDescription ORDER BY DESC(?trademarkCount) LIMIT 10
```

**Seed 1 (wrong) — answer:** `status_606: 2, status_710: 2, status_602: 3, status_800: 3`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `status_606: 2, status_710: 2, status_602: 3, status_800: 3`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `status_606: 2, status_710: 2, status_602: 3, status_800: 3`

```sparql
(none)
```

### 7.218 `holdout-v2-uspto-trademarks` — q-012 (T4)

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

**Seed 1 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 2 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

**Seed 3 (wrong) — answer:** `The rows are insufficient to compute the exact answer.`

```sparql
(none)
```

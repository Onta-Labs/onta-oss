# Holdout v2.0 Baseline Eval Report

- **Model:** google/gemini-3-flash-preview @ T=0
- **Tenant:** demo-tenant
- **Seeds:** 1, 2, 3  (3 independent /ask calls per question)
- **Questions:** 302  |  **KGs:** 26  |  **Total /ask calls:** 906
- **Global exclude_questions size:** 301
- **Wall clock:** 1444.3s (24.1 min)
- **Estimated LLM cost:** ~$1.47 (@ ~2500 in / ~350 out tokens per call, Gemini 2.5 Flash OpenRouter pricing)

## 1. Headline Accuracy

**91.4% [89.4, 93.0]**  (828/906 seed-level correct)

Majority-vote headline (per-question, n=302): **90.7% [86.9, 93.5]**

## 2. Per-Domain Accuracy

| Domain | Correct / n | Accuracy (Wilson 95% CI) |
|---|---|---|
| finance | 164/177 | 92.7% [87.8, 95.7] |
| healthcare | 226/246 | 91.9% [87.8, 94.7] |
| legal | 224/252 | 88.9% [84.4, 92.2] |
| scientific_public_sector | 214/231 | 92.6% [88.5, 95.4] |

## 3. Per-Tier Accuracy

| Tier | Correct / n | Accuracy (Wilson 95% CI) |
|---|---|---|
| T1 | 309/312 | 99.0% [97.2, 99.7] |
| T2 | 216/234 | 92.3% [88.2, 95.1] |
| T3 | 176/204 | 86.3% [80.9, 90.3] |
| T4 | 127/156 | 81.4% [74.6, 86.7] |

## 4. Per-KG Accuracy (sorted descending)

| KG | Domain | Correct / n | Accuracy (Wilson 95% CI) |
|---|---|---|---|
| holdout-v2-cdc-fluview | healthcare | 39/39 | 100.0% [91.0, 100.0] |
| holdout-v2-cdc-wonder-mortality | healthcare | 39/39 | 100.0% [91.0, 100.0] |
| holdout-v2-doe-energy-research-grants | scientific_public_sector | 36/36 | 100.0% [90.4, 100.0] |
| holdout-v2-nih-reporter-non-clinical | scientific_public_sector | 24/24 | 100.0% [86.2, 100.0] |
| holdout-v2-noaa-storm-events | scientific_public_sector | 33/33 | 100.0% [89.6, 100.0] |
| holdout-v2-scdb-supreme-court | legal | 33/33 | 100.0% [89.6, 100.0] |
| holdout-v2-cftc-swap-data | finance | 35/36 | 97.2% [85.8, 99.5] |
| holdout-v2-fec-enforcement | legal | 34/36 | 94.4% [81.9, 98.5] |
| holdout-v2-ncua-credit-union-call-reports | finance | 34/36 | 94.4% [81.9, 98.5] |
| holdout-v2-usda-agricultural-statistics | scientific_public_sector | 34/36 | 94.4% [81.9, 98.5] |
| holdout-v2-ftc-consent-decrees | legal | 39/42 | 92.9% [81.0, 97.5] |
| holdout-v2-medicare-part-d-pricing | healthcare | 36/39 | 92.3% [79.7, 97.3] |
| holdout-v2-ofr-financial-stability | finance | 36/39 | 92.3% [79.7, 97.3] |
| holdout-v2-pacer-federal-dockets | legal | 36/39 | 92.3% [79.7, 97.3] |
| holdout-v2-hrsa-hpsa | healthcare | 30/33 | 90.9% [76.4, 96.9] |
| holdout-v2-cms-nursing-home-compare | healthcare | 27/30 | 90.0% [74.4, 96.5] |
| holdout-v2-fdic-call-reports | finance | 27/30 | 90.0% [74.4, 96.5] |
| holdout-v2-fema-disaster-declarations | scientific_public_sector | 24/27 | 88.9% [71.9, 96.1] |
| holdout-v2-sec-edgar-10k | finance | 32/36 | 88.9% [74.7, 95.6] |
| holdout-v2-doj-enforcement-actions | legal | 31/36 | 86.1% [71.3, 93.9] |
| holdout-v2-samhsa-n-ssats | healthcare | 28/33 | 84.8% [69.1, 93.3] |
| holdout-v2-epa-water-quality-portal | scientific_public_sector | 33/39 | 84.6% [70.3, 92.8] |
| holdout-v2-fema-disaster-declarations-multitable | scientific_public_sector | 30/36 | 83.3% [68.1, 92.1] |
| holdout-v2-npi-registry | healthcare | 27/33 | 81.8% [65.6, 91.4] |
| holdout-v2-patentsview | legal | 24/30 | 80.0% [62.7, 90.5] |
| holdout-v2-uspto-trademarks | legal | 27/36 | 75.0% [58.9, 86.2] |

## 5. Per-Question Agreement Across 3 Seeds

| Correct runs | # questions | % |
|---|---|---|
| 3/3 | 273 | 90.4% |
| 2/3 | 1 | 0.3% |
| 1/3 | 7 | 2.3% |
| 0/3 | 21 | 7.0% |

## 6. Runtime and Cost

- Wall clock: **1444.3s** (24.07 min)
- Total /ask calls: **906**
- Estimated LLM cost: **~$1.47** (Gemini 2.5 Flash; see constants in build_baseline_report.py)

## 7. Systematic Failures (0/3 questions)

**21 / 302 questions failed on all 3 seeds.**

### 7.1 `holdout-v2-cms-nursing-home-compare` — q-008 (T3)

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

**Seed 1 (wrong) — answer:** `nursingHomeName: Creekside at the Springs, penaltyCount: 1
nursingHomeName: GOSNELL HEALTH AND REHAB, penaltyCount: 1
nursingHomeName: MADERA REHABILITATION & NURSING CENTER, penaltyCount: 3
nursingHomeName: NORTH VALLEY NURSING CENTER, penaltyCount: 1
nursingHomeName: BERKLEY WEST HEALTHCARE CENTER`

```sparql
SELECT ?nursingHomeName (COUNT(DISTINCT ?penalty) AS ?penaltyCount)
FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cms-nursing-home-compare>
WHERE {
  ?penalty <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/Penalty> .
  ?penalty <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/penalty_type> ?penaltyType .
  FILTER(CONTAINS(LCASE(?penaltyType), "fine"))
  
  ?penalty <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/penalty_against_facility> ?nursingHome .
  ?nursingHome <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/NursingHome> .
  ?nursingHome <http://www.w3.org/2000/01/rdf-schema#label> ?nursingHomeName .
}
GROUP BY ?nursingHomeName
```

**Seed 2 (wrong) — answer:** `nursingHomeName: AVALON VILLA CARE CENTER, penaltyCount: 2
nursingHomeName: SHAFTER NURSING CARE, penaltyCount: 2
nursingHomeName: SEAL BEACH HEALTH AND REHABILITATION CENTER, penaltyCount: 1
nursingHomeName: Pacific Care Nursing Center, penaltyCount: 3
nursingHomeName: LONE TREE POST ACUTE, penalty`

```sparql
SELECT ?nursingHomeName (COUNT(DISTINCT ?penalty) AS ?penaltyCount)
FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cms-nursing-home-compare>
WHERE {
  ?penalty <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/Penalty> .
  ?penalty <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/penalty_type> ?penaltyType .
  FILTER(CONTAINS(LCASE(?penaltyType), "fine"))
  
  ?penalty <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/penalty_against_facility> ?nursingHome .
  ?nursingHome <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/NursingHome> .
  ?nursingHome <http://www.w3.org/2000/01/rdf-schema#label> ?nursingHomeName .
}
GROUP BY ?nursingHomeName
```

**Seed 3 (wrong) — answer:** `nursingHomeName: AUTUMN HILLS HEALTH CARE CENTER, penaltyCount: 1
nursingHomeName: SPRINGS ROAD HEALTHCARE, penaltyCount: 10
nursingHomeName: SHORELINE HEALTHCARE CENTER, penaltyCount: 1
nursingHomeName: VISTA PACIFICA CONVALESCENT HOSPITAL, penaltyCount: 1
nursingHomeName: LONG BEACH HEALTHCARE CEN`

```sparql
SELECT ?nursingHomeName (COUNT(DISTINCT ?penalty) AS ?penaltyCount)
FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cms-nursing-home-compare>
WHERE {
  ?penalty <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/Penalty> .
  ?penalty <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/penalty_type> ?penaltyType .
  FILTER(CONTAINS(LCASE(?penaltyType), "fine"))
  
  ?penalty <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/penalty_against_facility> ?nursingHome .
  ?nursingHome <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/NursingHome> .
  ?nursingHome <http://www.w3.org/2000/01/rdf-schema#label> ?nursingHomeName .
}
GROUP BY ?nursingHomeName
```

### 7.2 `holdout-v2-doj-enforcement-actions` — q-010 (T4)

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
SELECT ?crimeTypeName (AVG(xsd:float(?payment)) AS ?averageTotalPayment) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doj-enforcement-actions> WHERE { ?action <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/type/Action> . ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_total_payment> ?payment . ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_by_company> ?company . ?company <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/company_is_us_public_co> ?isPublic . FILTER(CONTAINS(LCASE(?isPublic), "yes")) ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_for_crime> ?crimeType . ?crimeType <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/crimetype_name> ?crimeTypeName . } GROUP BY ?crimeTypeName
```

**Seed 2 (wrong) — answer:** `crimeTypeName: Environmental, averageTotalPayment: 8155653.5
crimeTypeName: Act to Prevent Pollution from Ships, averageTotalPayment: 5200000.0
crimeTypeName: Fraud - General, averageTotalPayment: 3.8201296E8
crimeTypeName: Fraud - Health Care, averageTotalPayment: 2.49031296E8
crimeTypeName: Antitr`

```sparql
SELECT ?crimeTypeName (AVG(xsd:float(?payment)) AS ?averageTotalPayment) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doj-enforcement-actions> WHERE { ?action <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/type/Action> . ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_total_payment> ?payment . ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_by_company> ?company . ?company <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/company_is_us_public_co> ?isPublic . FILTER(CONTAINS(LCASE(STR(?isPublic)), "yes") || CONTAINS(LCASE(STR(?isPublic)), "true") || CONTAINS(LCASE(STR(?isPublic)), "1")) ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_for_crime> ?crimeType . ?crimeType <http://www.w3.org/2000/01/rdf-schema#label> ?crimeTypeName } GROUP BY ?crimeTypeName
```

**Seed 3 (error) — answer:** `No results found.`

```sparql
SELECT ?crimeTypeName (AVG(xsd:float(?payment)) AS ?averageTotalPayment) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doj-enforcement-actions> WHERE { ?action <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/type/Action> . ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_total_payment> ?payment . ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_by_company> ?company . ?company <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/company_is_us_public_co> ?isPublic . FILTER(CONTAINS(LCASE(?isPublic), "yes")) ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_for_crime> ?crimeType . ?crimeType <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/crimetype_name> ?crimeTypeName . } GROUP BY ?crimeTypeName
```

### 7.3 `holdout-v2-epa-water-quality-portal` — q-010 (T3)

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

**Seed 1 (wrong) — answer:** `station_name: well 6, sample_count: 3
station_name: Monitoring Location LAVO_WQ15, sample_count: 3
station_name: Pine Gulch Creek at Stream Gauge near Bolinas Lagoon, sample_count: 3
station_name: Tennessee Valley Creek, sample_count: 3
station_name: Redwood Creek at Muir Beach, sample_count: 14
sta`

```sparql
SELECT ?station_name (COUNT(DISTINCT ?sample) AS ?sample_count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-epa-water-quality-portal> WHERE { ?sample <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/Sample> . ?sample <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/sample_at_station> ?station . ?station <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/station_name> ?station_name . } GROUP BY ?station_name
```

**Seed 2 (wrong) — answer:** `station_name: Monitoring Location LAVO_WQ36, sample_count: 1
station_name: Monitoring Location REDW_WQ02, sample_count: 1
station_name: Green Gulch North (Northern Branch), sample_count: 9
station_name: Green Gulch Control, sample_count: 13
station_name: Bear Valley Creek, sample_count: 9
station_na`

```sparql
SELECT ?station_name (COUNT(DISTINCT ?sample) AS ?sample_count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-epa-water-quality-portal> WHERE { ?sample <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/Sample> . ?sample <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/sample_at_station> ?station . ?station <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/station_name> ?station_name . } GROUP BY ?station_name
```

**Seed 3 (wrong) — answer:** `station_name: West Corner of Big Valley Rancheria Marina, sample_count: 8
station_name: Rodman Slough, sample_count: 8
station_name: Oso Flaco Creek at Oso Flaco Lake Road, sample_count: 17
station_name: Main Street Canal at Main Street Crossing, sample_count: 12
station_name: Klamath River above Tr`

```sparql
SELECT ?station_name (COUNT(DISTINCT ?sample) AS ?sample_count)
FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-epa-water-quality-portal>
WHERE {
  ?sample <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/Sample> .
  ?sample <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/sample_at_station> ?station .
  ?station <http://www.w3.org/2000/01/rdf-schema#label> ?station_name .
}
GROUP BY ?station_name
```

### 7.4 `holdout-v2-epa-water-quality-portal` — q-011 (T4)

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

**Seed 1 (wrong) — answer:** `90.66666666666666666667`

```sparql
SELECT (AVG(?sampleCount) AS ?averageSamples) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-epa-water-quality-portal> WHERE { { SELECT ?org (COUNT(DISTINCT ?sample) AS ?sampleCount) WHERE { ?org <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/Organization> . ?station <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/station_operated_by> ?org . ?station <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/station_in_county> ?county . ?county <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/county_name> ?countyName . FILTER(CONTAINS(LCASE(?countyName), "imperial")) ?sample <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/sample_by_organization> ?org . } GROUP BY ?org } }
```

**Seed 2 (wrong) — answer:** `90.66666666666666666667`

```sparql
SELECT (AVG(?sampleCount) AS ?averageSamples) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-epa-water-quality-portal> WHERE { { SELECT ?org (COUNT(DISTINCT ?sample) AS ?sampleCount) WHERE { ?org <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/Organization> . ?station <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/station_operated_by> ?org . ?station <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/station_in_county> ?county . ?county <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/county_name> ?countyName . FILTER(CONTAINS(LCASE(?countyName), "imperial")) ?sample <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/sample_by_organization> ?org . } GROUP BY ?org } }
```

**Seed 3 (wrong) — answer:** `90.66666666666666666667`

```sparql
SELECT (AVG(?sampleCount) AS ?averageSamples) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-epa-water-quality-portal> WHERE { { SELECT ?org (COUNT(DISTINCT ?sample) AS ?sampleCount) WHERE { ?org <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/Organization> . ?station <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/station_operated_by> ?org . ?station <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/station_in_county> ?county . ?county <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/county_name> ?countyName . FILTER(CONTAINS(LCASE(?countyName), "imperial")) ?sample <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/sample_by_organization> ?org . } GROUP BY ?org } }
```

### 7.5 `holdout-v2-fdic-call-reports` — q-005 (T3)

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

**Seed 1 (wrong) — answer:** `callReportLabel: 2024-Q2, metricCount: 87450`

```sparql
SELECT ?callReportLabel (COUNT(DISTINCT ?metric) AS ?metricCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fdic-call-reports> WHERE { ?callReport <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/type/CallReport> . ?callReport <http://www.w3.org/2000/01/rdf-schema#label> ?callReportLabel . ?callReport <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/callreport_repdte> ?reportDate . FILTER(CONTAINS(LCASE(STR(?reportDate)), "20240630")) ?callReport <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/callreport_has_metric> ?metric . } GROUP BY ?callReportLabel
```

**Seed 2 (wrong) — answer:** `callReportLabel: 2024-Q2, metricCount: 87409`

```sparql
SELECT ?callReportLabel (COUNT(DISTINCT ?metric) AS ?metricCount)
FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fdic-call-reports>
WHERE {
  ?callReport <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/type/CallReport> .
  ?callReport <http://www.w3.org/2000/01/rdf-schema#label> ?callReportLabel .
  ?callReport <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/callreport_repdte> ?reportDate .
  FILTER(CONTAINS(LCASE(?reportDate), "20240630"))
  
  ?callReport <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/callreport_has_metric> ?metric .
  ?metric <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/type/FinancialMetric> .
}
GROUP BY ?callReportLabel
```

**Seed 3 (wrong) — answer:** `callReportLabel: 2024-Q2, metricCount: 87409`

```sparql
SELECT ?callReportLabel (COUNT(DISTINCT ?metric) AS ?metricCount)
FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fdic-call-reports>
WHERE {
  ?callReport <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/type/CallReport> .
  ?callReport <http://www.w3.org/2000/01/rdf-schema#label> ?callReportLabel .
  ?callReport <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/callreport_repdte> ?reportDate .
  FILTER(CONTAINS(LCASE(?reportDate), "20240630"))
  
  ?callReport <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/pred/callreport_has_metric> ?metric .
  ?metric <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fdic-call-reports/type/FinancialMetric> .
}
GROUP BY ?callReportLabel
```

### 7.6 `holdout-v2-fema-disaster-declarations` — q-003 (T1)

**Question:** How many distinct incident types are recorded in Declarations?

**Expected answer:** `21`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?incidentType) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations> WHERE { ?declaration <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/Declaration> . ?declaration <https://omnix.dev/types/Declaration/attrs/type> ?incidentType . }
```

**Seed 1 (wrong) — answer:** `18`

```sparql
SELECT (COUNT(DISTINCT ?type) AS ?distinct_incident_types_count)

FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations>
WHERE {
  ?declaration <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/FemaDeclaration> .
  ?declaration <https://omnix.dev/types/FemaDeclaration/attrs/incident_type> ?type .
}
```

**Seed 2 (wrong) — answer:** `18`

```sparql
SELECT (COUNT(DISTINCT ?type) AS ?count)  FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations>
WHERE { ?fema_declaration <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/FemaDeclaration> . ?fema_declaration <https://omnix.dev/types/FemaDeclaration/attrs/incident_type> ?type . }
```

**Seed 3 (wrong) — answer:** `18`

```sparql
SELECT (COUNT(DISTINCT ?incident_type) AS ?incident_type_count)  FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations>
WHERE { ?declaration <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/FemaDeclaration> . ?declaration <https://omnix.dev/types/FemaDeclaration/attrs/incident_type> ?incident_type . }
```

### 7.7 `holdout-v2-fema-disaster-declarations-multitable` — q-005 (T2)

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

**Seed 1 (wrong) — answer:** `15593`

```sparql
SELECT (COUNT(DISTINCT ?project) AS ?projectCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations-multitable> WHERE { ?project <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/PAProject> . ?project <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/pred/projectamount> ?amount . FILTER(xsd:float(?amount) > 5000) }
```

**Seed 2 (wrong) — answer:** `15593`

```sparql
SELECT (COUNT(DISTINCT ?project) AS ?projectCount)
FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations-multitable>
WHERE {
  ?project <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/PAProject> .
  ?project <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/pred/projectamount> ?amount .
  FILTER(xsd:float(?amount) > 5000)
}
```

**Seed 3 (wrong) — answer:** `15593`

```sparql
SELECT (COUNT(DISTINCT ?project) AS ?projectCount)
FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations-multitable>
WHERE {
  ?project <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/PAProject> .
  ?project <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/pred/projectamount> ?amount .
  FILTER(xsd:float(?amount) > 5000)
}
```

### 7.8 `holdout-v2-fema-disaster-declarations-multitable` — q-007 (T2)

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

**Seed 1 (wrong) — answer:** `totalObligated: 996.2
totalObligated: 783.9
totalObligated: 972.27
totalObligated: 896.16
totalObligated: 987.72
totalObligated: 792.04
totalObligated: 803.03
totalObligated: 806.94
totalObligated: 982.23
totalObligated: 832.85
totalObligated: 805.78
totalObligated: 783.9
totalObligated: 837.25
tota`

```sparql
SELECT ?totalObligated FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations-multitable> WHERE { ?project <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/PAProject> . ?project <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/pred/totalobligated> ?totalObligated . FILTER(xsd:float(?totalObligated) < "1000"^^<http://www.w3.org/2001/XMLSchema#float>) }
```

**Seed 2 (wrong) — answer:** `totalObligated: 946.1
totalObligated: 894.86
totalObligated: 956.31
totalObligated: 914.55
totalObligated: 858.22
totalObligated: 881.75
totalObligated: 293.93
totalObligated: 505.01
totalObligated: 812.12
totalObligated: 391.95
totalObligated: 282.2
totalObligated: 995.95
totalObligated: 996.2
tota`

```sparql
SELECT ?totalObligated FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations-multitable> WHERE { ?project <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/PAProject> . ?project <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/pred/totalobligated> ?totalObligated . FILTER(xsd:float(?totalObligated) < 1000.0) }
```

**Seed 3 (wrong) — answer:** `totalObligated: 881.75
totalObligated: 293.93
totalObligated: 505.01
totalObligated: 812.12
totalObligated: 391.95
totalObligated: 282.2
totalObligated: 995.95
totalObligated: 776.42
totalObligated: 932.48
totalObligated: 963.33
totalObligated: 429.19
totalObligated: 996.09
totalObligated: 883.97
to`

```sparql
SELECT ?totalObligated FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations-multitable> WHERE { ?project <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/PAProject> . ?project <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/pred/totalobligated> ?totalObligated . FILTER(xsd:float(?totalObligated) < "1000"^^<http://www.w3.org/2001/XMLSchema#float>) }
```

### 7.9 `holdout-v2-ftc-consent-decrees` — q-014 (T4)

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

**Seed 1 (wrong) — answer:** `4.04166666666666666667`

```sparql
SELECT (AVG(?enforcementCount) AS ?averageEnforcementTypes) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ftc-consent-decrees> WHERE { { SELECT ?fy (COUNT(DISTINCT ?enforcementType) AS ?enforcementCount) WHERE { ?matter <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/type/Matter> . ?matter <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/matter_in_fy> ?fy . ?matter <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/matter_has_enforcement_type> ?enforcementType . } GROUP BY ?fy } }
```

**Seed 2 (wrong) — answer:** `4.04166666666666666667`

```sparql
SELECT (AVG(?enforcement_count) AS ?average_enforcement_types) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ftc-consent-decrees> WHERE { { SELECT ?fy (COUNT(DISTINCT ?enforcement_type) AS ?enforcement_count) WHERE { ?fy <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/type/FiscalYear> . ?fy <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/fy_includes_matter> ?matter . ?matter <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/matter_has_enforcement_type> ?enforcement_type . } GROUP BY ?fy } }
```

**Seed 3 (wrong) — answer:** `4.04166666666666666667`

```sparql
SELECT (AVG(?enforcementCount) AS ?averageEnforcementTypes) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ftc-consent-decrees> WHERE { { SELECT ?fy (COUNT(DISTINCT ?enforcementType) AS ?enforcementCount) WHERE { ?matter <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/type/Matter> . ?matter <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/matter_in_fy> ?fy . ?matter <https://omnix.dev/holdout-v2/holdout-v2-ftc-consent-decrees/pred/matter_has_enforcement_type> ?enforcementType . } GROUP BY ?fy } }
```

### 7.10 `holdout-v2-hrsa-hpsa` — q-008 (T2)

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

**Seed 1 (wrong) — answer:** `16`

```sparql
SELECT (COUNT(DISTINCT ?entity) AS ?count)

FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-hrsa-hpsa>
WHERE {
  ?entity <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/HealthProfessionalShortageArea> .
  ?entity <https://omnix.dev/types/HealthProfessionalShortageArea/attrs/hpsa_degree_of_shortage> ?shortage .
  FILTER(xsd:float(?shortage) > "3"^^<http://www.w3.org/2001/XMLSchema#integer>)
}
```

**Seed 2 (wrong) — answer:** `16`

```sparql
SELECT (COUNT(DISTINCT ?hpsa) AS ?count)

FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-hrsa-hpsa>
WHERE {
  ?hpsa <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/HealthProfessionalShortageArea> .
  ?hpsa <https://omnix.dev/types/HealthProfessionalShortageArea/attrs/hpsa_degree_of_shortage> ?shortage .
  FILTER(xsd:float(?shortage) > "3"^^<http://www.w3.org/2001/XMLSchema#float>)
}
```

**Seed 3 (wrong) — answer:** `16`

```sparql
SELECT (COUNT(DISTINCT ?hpsa) AS ?hpsa_count)  FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-hrsa-hpsa>
WHERE { ?hpsa <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/HealthProfessionalShortageArea> . ?hpsa <https://omnix.dev/types/HealthProfessionalShortageArea/attrs/hpsa_degree_of_shortage> ?shortage . FILTER(xsd:float(?shortage) > "3.0"^^xsd:float) }
```

### 7.11 `holdout-v2-medicare-part-d-pricing` — q-013 (T4)

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

**Seed 1 (wrong) — answer:** `genericName: Tazarotene
genericName: Aflibercept
genericName: Agalsidase Beta
genericName: Iloperidone
genericName: Toremifene Citrate
genericName: Dapagliflozin Propanediol
genericName: Estradiol Acetate
genericName: Loteprednol Etabonate
genericName: Riluzole
genericName: Amlodipine Besylate/Valsa`

```sparql
SELECT DISTINCT ?genericName
FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing>
WHERE {
  ?drug <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/Drug> .
  ?drug <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/generic_name> ?genericName .
  ?drug <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/has_spending_record> ?spendingRecord .
  
  ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/has_outlier_status> ?outlierFlag .
  ?outlierFlag <http://www.w3.org/2000/01/rdf-schema#label> ?outlierLabel .
  FILTER(CONTAINS(LCASE(STR(?outlierLabel)), "outlier"))
  
  ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/avg_spend_per_claim_2023> ?avgSpend .
  FILTER(xsd:float(?avgSpend) > 500)
}
```

**Seed 2 (wrong) — answer:** `genericName: Desmopressin Acetate
genericName: Deferoxamine Mesylate
genericName: Emtricitabine/Tenofov Alafenam
genericName: Desonide
genericName: Tolterodine Tartrate
genericName: Dexlansoprazole
genericName: Valsartan
genericName: Prenatal Vit No.170/Iron/Folic
genericName: Dextroamphetamine Sulf`

```sparql
SELECT DISTINCT ?genericName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing> WHERE { ?drug <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/Drug> . ?drug <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/generic_name> ?genericName . ?drug <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/has_spending_record> ?record . ?record <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/has_outlier_status> ?outlier . ?outlier <http://www.w3.org/2000/01/rdf-schema#label> ?outlierLabel . FILTER(CONTAINS(LCASE(STR(?outlierLabel)), "outlier")) ?record <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/avg_spend_per_claim_2023> ?avgSpendStr . FILTER(xsd:float(?avgSpendStr) > 500) }
```

**Seed 3 (wrong) — answer:** `genericName: Docetaxel
genericName: PEGfilgrastim-Pbbk
genericName: Mesalamine
genericName: Levothyroxine Sodium
genericName: Diclofenac Epolamine
genericName: Selegiline
genericName: Cannabidiol (Cbd)
genericName: Escitalopram Oxalate
genericName: Fosamprenavir Calcium
genericName: Insulin Detemir
`

```sparql
SELECT DISTINCT ?genericName
FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing>
WHERE {
  ?drug <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/Drug> .
  ?drug <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/generic_name> ?genericName .
  ?drug <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/has_spending_record> ?spendingRecord .
  
  ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/has_outlier_status> ?outlierFlag .
  ?outlierFlag <http://www.w3.org/2000/01/rdf-schema#label> ?outlierLabel .
  FILTER(CONTAINS(LCASE(STR(?outlierLabel)), "outlier"))
  
  ?spendingRecord <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/avg_spend_per_claim_2023> ?avgSpend .
  FILTER(xsd:float(?avgSpend) > 500)
}
```

### 7.12 `holdout-v2-npi-registry` — q-009 (T3)

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

**Seed 1 (wrong) — answer:** `location_label: 112 S PINE ST, provider_count: 8
location_label: 402 E LINCOLN AVE, provider_count: 4
location_label: 108 CECIL ST STE G, provider_count: 4
location_label: 4304 S BEARFIELD RD, provider_count: 1
location_label: 164 BLUE PRIDE DR STE 105, provider_count: 1
location_label: 3870 COLUMBI`

```sparql
SELECT ?location_label (COUNT(DISTINCT ?provider) AS ?provider_count)
FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-npi-registry>
WHERE {
  ?location <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/type/PracticeLocation> .
  ?location <http://www.w3.org/2000/01/rdf-schema#label> ?location_label .
  ?location <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/practice_location_of_provider> ?provider .
  ?provider <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/type/Provider> .
}
GROUP BY ?location_label
```

**Seed 2 (wrong) — answer:** `location_label: 35888 MARIES ROAD 202, provider_count: 1
location_label: 104 W LAFAYETTE ST, provider_count: 6
location_label: 54 HOSPITAL DR, provider_count: 150
location_label: 451 TAVERN CREEK RD, provider_count: 1
location_label: 601 E HWY 28, provider_count: 2
location_label: 980 PARKSIDE VILLA`

```sparql
SELECT ?location_label (COUNT(DISTINCT ?provider) AS ?provider_count)
FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-npi-registry>
WHERE {
  ?location <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/type/PracticeLocation> .
  ?location <http://www.w3.org/2000/01/rdf-schema#label> ?location_label .
  ?location <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/practice_location_of_provider> ?provider .
  ?provider <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/type/Provider> .
}
GROUP BY ?location_label
```

**Seed 3 (wrong) — answer:** `location_label: 164 BLUE PRIDE DR, provider_count: 2
location_label: 305 HIGHWAY 50 E, provider_count: 1
location_label: 130 CALO LN, provider_count: 33
location_label: 504 N STURGEON ST, provider_count: 4
location_label: 200 COLLEGE BLVD, provider_count: 2
location_label: 200 SKILES BLVD, provider_`

```sparql
SELECT ?location_label (COUNT(DISTINCT ?provider) AS ?provider_count)
FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-npi-registry>
WHERE {
  ?location <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/type/PracticeLocation> .
  ?location <http://www.w3.org/2000/01/rdf-schema#label> ?location_label .
  ?location <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/practice_location_of_provider> ?provider .
  ?provider <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/type/Provider> .
}
GROUP BY ?location_label
```

### 7.13 `holdout-v2-npi-registry` — q-011 (T3)

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
SELECT ?specialtyName (COUNT(DISTINCT ?provider) AS ?providerCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-npi-registry> WHERE { ?specialty <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/type/Specialty> . ?specialty <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/specialty_name> ?specialtyName . FILTER(CONTAINS(LCASE(?specialtyName), "unknown")) . ?specialty <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/specialty_practiced_by> ?provider . ?provider <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/type/Provider> . } GROUP BY ?specialtyName
```

**Seed 2 (wrong) — answer:** `specialty_name: Unknown, provider_count: 46`

```sparql
SELECT ?specialty_name (COUNT(DISTINCT ?provider) AS ?provider_count)
FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-npi-registry>
WHERE {
  ?specialty <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/type/Specialty> .
  ?specialty <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/specialty_name> ?specialty_name .
  FILTER(CONTAINS(LCASE(?specialty_name), "unknown"))
  ?specialty <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/specialty_practiced_by> ?provider .
}
GROUP BY ?specialty_name
```

**Seed 3 (wrong) — answer:** `specialty_name: Unknown, provider_count: 46`

```sparql
SELECT ?specialty_name (COUNT(DISTINCT ?provider) AS ?provider_count)
FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-npi-registry>
WHERE {
  ?specialty <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/type/Specialty> .
  ?specialty <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/specialty_name> ?specialty_name .
  FILTER(CONTAINS(LCASE(?specialty_name), "unknown"))
  ?specialty <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/specialty_practiced_by> ?provider .
}
GROUP BY ?specialty_name
```

### 7.14 `holdout-v2-ofr-financial-stability` — q-012 (T4)

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

**Seed 1 (wrong) — answer:** `year_label: 2025, average_value: -0.56803215
year_label: 2015, average_value: -0.3658912
year_label: 2013, average_value: -0.8732289
year_label: 2011, average_value: 0.6664116
year_label: 2026, average_value: -0.6092964
year_label: 2010, average_value: 0.3793468
year_label: 2004, average_value: -0.6`

```sparql
SELECT ?year_label (AVG(xsd:float(?val)) AS ?average_value) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ofr-financial-stability> WHERE { ?reading <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/type/IndicatorReading> . ?reading <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/value> ?val . ?reading <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/reading_on_period> ?period . ?period <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/day_of_week> ?dow . FILTER(CONTAINS(LCASE(?dow), "friday")) ?period <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/period_in_month> ?month . ?month <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/month_in_year> ?year . ?year <http://www.w3.org/2000/01/rdf-schema#label> ?year_label . } GROUP BY ?year_label
```

**Seed 2 (wrong) — answer:** `year_label: 2022, average_value: 0.31145057
year_label: 2021, average_value: -1.1410996
year_label: 2019, average_value: -0.80633986
year_label: 2025, average_value: -0.568032
year_label: 2020, average_value: -0.14758496
year_label: 2016, average_value: -0.064895436
year_label: 2026, average_value: `

```sparql
SELECT ?year_label (AVG(xsd:float(?val)) AS ?average_value) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ofr-financial-stability> WHERE { ?reading <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/type/IndicatorReading> . ?reading <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/value> ?val . ?reading <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/reading_on_period> ?period . ?period <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/day_of_week> ?day . FILTER(CONTAINS(LCASE(?day), "friday")) ?period <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/period_in_month> ?month . ?month <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/month_in_year> ?year . ?year <http://www.w3.org/2000/01/rdf-schema#label> ?year_label . } GROUP BY ?year_label
```

**Seed 3 (wrong) — answer:** `year_label: 2000, average_value: 1.04432
year_label: 2006, average_value: -1.257222
year_label: 2004, average_value: -0.6876069
year_label: 2005, average_value: -1.2797387
year_label: 2003, average_value: 0.44251335
year_label: 2019, average_value: -0.8063396
year_label: 2001, average_value: 1.25137`

```sparql
SELECT ?year_label (AVG(xsd:float(?val)) AS ?average_value) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ofr-financial-stability> WHERE { ?reading <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/type/IndicatorReading> . ?reading <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/value> ?val . ?reading <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/reading_on_period> ?period . ?period <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/day_of_week> ?day . FILTER(CONTAINS(LCASE(?day), "friday")) ?period <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/period_in_month> ?month . ?month <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/month_in_year> ?year . ?year <http://www.w3.org/2000/01/rdf-schema#label> ?year_label . } GROUP BY ?year_label
```

### 7.15 `holdout-v2-pacer-federal-dockets` — q-013 (T4)

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

**Seed 1 (wrong) — answer:** `judgeName: Staci G Cornelius, docketCount: 1
judgeName: C Lynwood Smith, Jr, docketCount: 1
judgeName: Christy D. Comstock, docketCount: 4
judgeName: Susan O Hickey, docketCount: 1
judgeName: Unassigned Judge, docketCount: 30
judgeName: Dena M. Coggins, docketCount: 2
judgeName: Jennifer L. Thurston`

```sparql
SELECT ?judgeName (COUNT(DISTINCT ?docket) AS ?docketCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-pacer-federal-dockets> WHERE { ?judge <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/Judge> . ?judge <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/judge_name> ?judgeName . ?docket <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/Docket> . ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_assigned_to_judge> ?judge . ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_filed_in_court> ?court . ?court <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/court_full_name> ?courtFullName . FILTER(CONTAINS(LCASE(?courtFullName), "district")) } GROUP BY ?judgeName
```

**Seed 2 (wrong) — answer:** `judgeName: Frances M. Tydingco-Gatewood, docketCount: 5
judgeName: Kristi K. Dubose, docketCount: 5
judgeName: Terry F. Moorer, docketCount: 7
judgeName: William E. Cassady, docketCount: 2
judgeName: Katherine P. Nelson, docketCount: 1
judgeName: Jeffrey U. Beaverstock, docketCount: 4
judgeName: Mar`

```sparql
SELECT ?judgeName (COUNT(DISTINCT ?docket) AS ?docketCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-pacer-federal-dockets> WHERE { ?judge <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/Judge> . ?judge <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/judge_name> ?judgeName . ?docket <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/Docket> . ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_assigned_to_judge> ?judge . ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_filed_in_court> ?court . ?court <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/court_full_name> ?courtFullName . FILTER(CONTAINS(LCASE(?courtFullName), "district")) } GROUP BY ?judgeName
```

**Seed 3 (wrong) — answer:** `judgeName: Frances M. Tydingco-Gatewood, docketCount: 5
judgeName: Eileen S. Willett, docketCount: 3
judgeName: James F. Metcalf, docketCount: 1
judgeName: Sparkle L. Sooknanan, docketCount: 2
judgeName: Colleen Kollar-Kotelly, docketCount: 1
judgeName: Ana C. Reyes, docketCount: 2
judgeName: Beryl `

```sparql
SELECT ?judgeName (COUNT(DISTINCT ?docket) AS ?docketCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-pacer-federal-dockets> WHERE { ?judge <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/Judge> . ?judge <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/judge_name> ?judgeName . ?docket <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/type/Docket> . ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_assigned_to_judge> ?judge . ?docket <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/docket_filed_in_court> ?court . ?court <https://omnix.dev/holdout-v2/holdout-v2-pacer-federal-dockets/pred/court_full_name> ?courtFullName . FILTER(CONTAINS(LCASE(?courtFullName), "district")) } GROUP BY ?judgeName
```

### 7.16 `holdout-v2-patentsview` — q-007 (T2)

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

**Seed 1 (wrong) — answer:** `assignee_label: Ningbo Defeng Power Technology Co., Ltd.
assignee_label: SCHLUMBERGER TECHNOLOGY CORPORATION
assignee_label: HEFEI MEYER OPTOELECTRONIC TECHNOLOGY INC.
assignee_label: Commonwealth Of Australia represented by Department of Defence, Defence Science and Technology Organisation
assignee`

```sparql
SELECT DISTINCT ?assignee_label
FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview>
WHERE {
  ?assignee <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Assignee> .
  ?assignee <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/disambig_assignee_organization> ?org_name .
  ?assignee <http://www.w3.org/2000/01/rdf-schema#label> ?assignee_label .
  FILTER(CONTAINS(LCASE(STR(?org_name)), LCASE("Technology")))
}
```

**Seed 2 (wrong) — answer:** `assignee_label: AGENCY FOR SCIENCE, TECHNOLOGY AND RESEARCH
assignee_label: BAIJIE TENG TECHNOLOGY CORPORATION
assignee_label: HEFEI BOE OPTOELECTRONICS TECHNOLOGY CO., LTD.
assignee_label: SHENZHEN CHINA STAR OPROELECTRONICS SEMICONDUCTOR DISPLAY TECHNOLOGY CO., LTD.
assignee_label: THINKON NEW TEC`

```sparql
SELECT DISTINCT ?assignee_label
FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview>
WHERE {
  ?assignee <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Assignee> .
  ?assignee <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/disambig_assignee_organization> ?org_name .
  ?assignee <http://www.w3.org/2000/01/rdf-schema#label> ?assignee_label .
  FILTER(CONTAINS(LCASE(STR(?org_name)), LCASE("Technology")))
}
```

**Seed 3 (wrong) — answer:** `assignee_label: AGENCY FOR SCIENCE, TECHNOLOGY AND RESEARCH
assignee_label: BAIJIE TENG TECHNOLOGY CORPORATION
assignee_label: HEFEI BOE OPTOELECTRONICS TECHNOLOGY CO., LTD.
assignee_label: SHENZHEN CHINA STAR OPROELECTRONICS SEMICONDUCTOR DISPLAY TECHNOLOGY CO., LTD.
assignee_label: GE ENERGY POWER`

```sparql
SELECT DISTINCT ?assignee_label
FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview>
WHERE {
  ?assignee <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Assignee> .
  ?assignee <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/disambig_assignee_organization> ?org_name .
  ?assignee <http://www.w3.org/2000/01/rdf-schema#label> ?assignee_label .
  FILTER(CONTAINS(LCASE(STR(?org_name)), LCASE("Technology")))
}
```

### 7.17 `holdout-v2-patentsview` — q-008 (T3)

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

**Seed 1 (wrong) — answer:** `patent_title: Method and apparatus for implementing a flexible virtual local area network, outgoing_citation_count: 6
patent_title: Method of separating a back layer on a singulated semiconductor wafer attached to carrier substrates, outgoing_citation_count: 44
patent_title: Toner compositions inclu`

```sparql
SELECT ?patent_title (COUNT(?citation) AS ?outgoing_citation_count)
FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview>
WHERE {
  ?patent <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Patent> .
  ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/patent_title> ?patent_title .
  ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/patent_type> ?type .
  ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/patent_date> ?date .
  
  FILTER(CONTAINS(LCASE(?type), "utility"))
  FILTER(?date > "2000-12-31")
  
  OPTIONAL {
    ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/has_outgoing_citation> ?citation .
  }
}
GROUP BY ?patent_title
```

**Seed 2 (wrong) — answer:** `patent_title: System and method for learning-based image super-resolution, citation_count: 4
patent_title: Scheduling scheme(s) for a multi-die storage device, citation_count: 17
patent_title: Home appliance, citation_count: 10
patent_title: Aerofoil for an aircraft, and an aircraft, citation_count:`

```sparql
SELECT ?patent_title (COUNT(?citation) AS ?citation_count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview> WHERE { ?patent <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Patent> . ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/patent_title> ?patent_title . ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/patent_type> ?type . ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/patent_date> ?date . FILTER(CONTAINS(LCASE(?type), "utility")) FILTER(?date > "2000-12-31") OPTIONAL { ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/has_outgoing_citation> ?citation . } } GROUP BY ?patent_title
```

**Seed 3 (wrong) — answer:** `patent_title: Cosmetic spray, outgoing_citation_count: 1
patent_title: Cross-platform ordering and payment-processing system and method, outgoing_citation_count: 1
patent_title: Synthetic artificial peptides with antimicrobial effect, outgoing_citation_count: 2
patent_title: Lettuce variety ‘Brentwo`

```sparql
SELECT ?patent_title (COUNT(?citation) AS ?outgoing_citation_count)
FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview>
WHERE {
  ?patent <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Patent> .
  ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/patent_title> ?patent_title .
  ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/patent_type> ?type .
  ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/patent_date> ?date .
  
  FILTER(CONTAINS(LCASE(?type), "utility"))
  FILTER(?date > "2000-12-31")
  
  OPTIONAL {
    ?patent <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/has_outgoing_citation> ?citation .
  }
}
GROUP BY ?patent_title
```

### 7.18 `holdout-v2-samhsa-n-ssats` — q-009 (T3)

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
facilityName: 11284
`

```sparql
SELECT DISTINCT ?facilityName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-samhsa-n-ssats> WHERE { ?facility <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/TreatmentFacility> . ?facility <http://www.w3.org/2000/01/rdf-schema#label> ?facilityName . ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/accepts_payment> ?payment . ?payment <http://www.w3.org/2000/01/rdf-schema#label> ?paymentLabel . FILTER(CONTAINS(LCASE(?paymentLabel), "medicaid")) ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/serves_client_type> ?clientType . ?clientType <http://www.w3.org/2000/01/rdf-schema#label> ?clientLabel . FILTER(CONTAINS(LCASE(?clientLabel), "adolescents")) }
```

**Seed 2 (wrong) — answer:** `facilityName: 11474
facilityName: 11498
facilityName: 11496
facilityName: 11472
facilityName: 11477
facilityName: 11480
facilityName: 11464
facilityName: 11487
facilityName: 11460
facilityName: 11459
facilityName: 11462
facilityName: 11461
facilityName: 11499
facilityName: 11516
facilityName: 11528
`

```sparql
SELECT DISTINCT ?facilityName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-samhsa-n-ssats> WHERE { ?facility <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/TreatmentFacility> . ?facility <http://www.w3.org/2000/01/rdf-schema#label> ?facilityName . ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/accepts_payment> ?payment . ?payment <http://www.w3.org/2000/01/rdf-schema#label> ?paymentLabel . FILTER(CONTAINS(LCASE(?paymentLabel), "medicaid")) ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/serves_client_type> ?clientType . ?clientType <http://www.w3.org/2000/01/rdf-schema#label> ?clientLabel . FILTER(CONTAINS(LCASE(?clientLabel), "adolescents")) }
```

**Seed 3 (wrong) — answer:** `facilityName: 11553
facilityName: 11563
facilityName: 11566
facilityName: 11581
facilityName: 11561
facilityName: 11573
facilityName: 11574
facilityName: 11618
facilityName: 11619
facilityName: 11621
facilityName: 11622
facilityName: 11624
facilityName: 11625
facilityName: 11578
facilityName: 11541
`

```sparql
SELECT DISTINCT ?facilityName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-samhsa-n-ssats> WHERE { ?facility <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/TreatmentFacility> . ?facility <http://www.w3.org/2000/01/rdf-schema#label> ?facilityName . ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/accepts_payment> ?payment . ?payment <http://www.w3.org/2000/01/rdf-schema#label> ?paymentLabel . FILTER(CONTAINS(LCASE(?paymentLabel), "medicaid")) ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/serves_client_type> ?clientType . ?clientType <http://www.w3.org/2000/01/rdf-schema#label> ?clientLabel . FILTER(CONTAINS(LCASE(?clientLabel), "adolescents")) }
```

### 7.19 `holdout-v2-uspto-trademarks` — q-007 (T2)

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
SELECT (COUNT(DISTINCT ?niceClass) AS ?count)
FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-uspto-trademarks>
WHERE {
  ?niceClass <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/NiceClass> .
  ?niceClass <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/class_code> ?code .
  FILTER(?code > "030")
}
```

**Seed 2 (wrong) — answer:** `16`

```sparql
SELECT (COUNT(DISTINCT ?niceClass) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-uspto-trademarks> WHERE { ?niceClass <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/NiceClass> . ?niceClass <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/class_code> ?code . FILTER(?code > "030") }
```

**Seed 3 (wrong) — answer:** `16`

```sparql
SELECT (COUNT(DISTINCT ?niceClass) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-uspto-trademarks> WHERE { ?niceClass <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/NiceClass> . ?niceClass <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/class_code> ?code . FILTER(?code > "030") }
```

### 7.20 `holdout-v2-uspto-trademarks` — q-008 (T2)

**Question:** How many trademarks have 'BETTER SEX' as a mark element?

**Expected answer:** `5`

**Gold SPARQL:**
```sparql
SELECT (COUNT(?trademark) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-uspto-trademarks> WHERE {
  ?trademark <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/Trademark> .
  ?trademark <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/mark_element> ?markElement .
  FILTER(?markElement = "BETTER SEX")
}
```

**Seed 1 (wrong) — answer:** `6`

```sparql
SELECT (COUNT(DISTINCT ?trademark) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-uspto-trademarks> WHERE { ?trademark <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/Trademark> . ?trademark <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/mark_element> ?mark_element . FILTER(CONTAINS(LCASE(?mark_element), LCASE("BETTER SEX"))) }
```

**Seed 2 (wrong) — answer:** `6`

```sparql
SELECT (COUNT(DISTINCT ?trademark) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-uspto-trademarks> WHERE { ?trademark <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/Trademark> . ?trademark <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/mark_element> ?mark_element . FILTER(CONTAINS(LCASE(?mark_element), LCASE("BETTER SEX"))) }
```

**Seed 3 (wrong) — answer:** `6`

```sparql
SELECT (COUNT(DISTINCT ?trademark) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-uspto-trademarks> WHERE { ?trademark <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/Trademark> . ?trademark <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/mark_element> ?mark_element . FILTER(CONTAINS(LCASE(?mark_element), LCASE("BETTER SEX"))) }
```

### 7.21 `holdout-v2-uspto-trademarks` — q-012 (T4)

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

**Seed 1 (wrong) — answer:** `1.64769647696476964770`

```sparql
SELECT (AVG(?goodsCount) AS ?averageGoodsPerOwner) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-uspto-trademarks> WHERE { { SELECT ?owner (COUNT(DISTINCT ?goods) AS ?goodsCount) WHERE { ?owner <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/Owner> . ?owner <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/owner_owns_trademark> ?trademark . ?trademark <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/trademark_covers_goods> ?goods . } GROUP BY ?owner } }
```

**Seed 2 (wrong) — answer:** `1.64769647696476964770`

```sparql
SELECT (AVG(?goodsCount) AS ?averageGoodsPerOwner) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-uspto-trademarks> WHERE { { SELECT ?owner (COUNT(DISTINCT ?goods) AS ?goodsCount) WHERE { ?owner <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/Owner> . ?owner <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/owner_owns_trademark> ?trademark . ?trademark <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/trademark_covers_goods> ?goods . } GROUP BY ?owner } }
```

**Seed 3 (wrong) — answer:** `1.64769647696476964770`

```sparql
SELECT (AVG(?goodsCount) AS ?averageGoodsPerOwner) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-uspto-trademarks> WHERE { { SELECT ?owner (COUNT(DISTINCT ?goods) AS ?goodsCount) WHERE { ?owner <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/Owner> . ?owner <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/owner_owns_trademark> ?trademark . ?trademark <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/trademark_covers_goods> ?goods . } GROUP BY ?owner } }
```

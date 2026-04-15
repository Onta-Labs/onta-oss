# Holdout v2.0 Baseline Eval Report

- **Model:** anthropic/claude-opus-4.6 @ T=0
- **Tenant:** demo-tenant
- **Seeds:** 1, 2, 3  (3 independent /ask calls per question)
- **Questions:** 302  |  **KGs:** 26  |  **Total /ask calls:** 906
- **Global exclude_questions size:** 301
- **Wall clock:** 4260.4s (71.0 min)
- **Estimated LLM cost:** ~$1.47 (@ ~2500 in / ~350 out tokens per call, Gemini 2.5 Flash OpenRouter pricing)

## 1. Headline Accuracy

**92.2% [90.2, 93.7]**  (835/906 seed-level correct)

Majority-vote headline (per-question, n=302): **92.1% [88.4, 94.6]**

## 2. Per-Domain Accuracy

| Domain | Correct / n | Accuracy (Wilson 95% CI) |
|---|---|---|
| finance | 163/177 | 92.1% [87.2, 95.2] |
| healthcare | 222/246 | 90.2% [85.9, 93.4] |
| legal | 237/252 | 94.0% [90.4, 96.4] |
| scientific_public_sector | 213/231 | 92.2% [88.0, 95.0] |

## 3. Per-Tier Accuracy

| Tier | Correct / n | Accuracy (Wilson 95% CI) |
|---|---|---|
| T1 | 308/312 | 98.7% [96.8, 99.5] |
| T2 | 217/234 | 92.7% [88.7, 95.4] |
| T3 | 179/204 | 87.7% [82.5, 91.6] |
| T4 | 131/156 | 84.0% [77.4, 88.9] |

## 4. Per-KG Accuracy (sorted descending)

| KG | Domain | Correct / n | Accuracy (Wilson 95% CI) |
|---|---|---|---|
| holdout-v2-cdc-wonder-mortality | healthcare | 39/39 | 100.0% [91.0, 100.0] |
| holdout-v2-fec-enforcement | legal | 36/36 | 100.0% [90.4, 100.0] |
| holdout-v2-ftc-consent-decrees | legal | 42/42 | 100.0% [91.6, 100.0] |
| holdout-v2-nih-reporter-non-clinical | scientific_public_sector | 24/24 | 100.0% [86.2, 100.0] |
| holdout-v2-noaa-storm-events | scientific_public_sector | 33/33 | 100.0% [89.6, 100.0] |
| holdout-v2-pacer-federal-dockets | legal | 39/39 | 100.0% [91.0, 100.0] |
| holdout-v2-scdb-supreme-court | legal | 33/33 | 100.0% [89.6, 100.0] |
| holdout-v2-doe-energy-research-grants | scientific_public_sector | 35/36 | 97.2% [85.8, 99.5] |
| holdout-v2-fdic-call-reports | finance | 29/30 | 96.7% [83.3, 99.4] |
| holdout-v2-cftc-swap-data | finance | 34/36 | 94.4% [81.9, 98.5] |
| holdout-v2-usda-agricultural-statistics | scientific_public_sector | 34/36 | 94.4% [81.9, 98.5] |
| holdout-v2-hrsa-hpsa | healthcare | 31/33 | 93.9% [80.4, 98.3] |
| holdout-v2-cdc-fluview | healthcare | 36/39 | 92.3% [79.7, 97.3] |
| holdout-v2-epa-water-quality-portal | scientific_public_sector | 36/39 | 92.3% [79.7, 97.3] |
| holdout-v2-medicare-part-d-pricing | healthcare | 36/39 | 92.3% [79.7, 97.3] |
| holdout-v2-ofr-financial-stability | finance | 36/39 | 92.3% [79.7, 97.3] |
| holdout-v2-doj-enforcement-actions | legal | 33/36 | 91.7% [78.2, 97.1] |
| holdout-v2-sec-edgar-10k | finance | 33/36 | 91.7% [78.2, 97.1] |
| holdout-v2-cms-nursing-home-compare | healthcare | 27/30 | 90.0% [74.4, 96.5] |
| holdout-v2-patentsview | legal | 27/30 | 90.0% [74.4, 96.5] |
| holdout-v2-ncua-credit-union-call-reports | finance | 31/36 | 86.1% [71.3, 93.9] |
| holdout-v2-fema-disaster-declarations-multitable | scientific_public_sector | 30/36 | 83.3% [68.1, 92.1] |
| holdout-v2-npi-registry | healthcare | 27/33 | 81.8% [65.6, 91.4] |
| holdout-v2-samhsa-n-ssats | healthcare | 26/33 | 78.8% [62.2, 89.3] |
| holdout-v2-fema-disaster-declarations | scientific_public_sector | 21/27 | 77.8% [59.2, 89.4] |
| holdout-v2-uspto-trademarks | legal | 27/36 | 75.0% [58.9, 86.2] |

## 5. Per-Question Agreement Across 3 Seeds

| Correct runs | # questions | % |
|---|---|---|
| 3/3 | 273 | 90.4% |
| 2/3 | 6 | 2.0% |
| 1/3 | 4 | 1.3% |
| 0/3 | 19 | 6.3% |

## 6. Runtime and Cost

- Wall clock: **4260.4s** (71.01 min)
- Total /ask calls: **906**
- Estimated LLM cost: **~$1.47** (Gemini 2.5 Flash; see constants in build_baseline_report.py)

## 7. Systematic Failures (0/3 questions)

**19 / 302 questions failed on all 3 seeds.**

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

**Seed 1 (wrong) — answer:** `providerName: BERKELEY NURSING & REHAB CENTER, fineCount: 34
providerName: EVERVELLA OF SWANSEA, fineCount: 24
providerName: GOLDEN ROSE CARE CENTER, fineCount: 22
providerName: CONTINENTAL NURSING & REHAB CENTER, fineCount: 22
providerName: Blue Lake Post Acute, fineCount: 18
providerName: AHC OF L`

```sparql
SELECT ?providerName (COUNT(DISTINCT ?penalty) AS ?fineCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cms-nursing-home-compare> WHERE { ?nh <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/NursingHome> . ?nh <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/provider_name> ?providerName . ?nh <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/has_penalty> ?penalty . ?penalty <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/Penalty> . ?penalty <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/penalty_type> ?penaltyType . FILTER(CONTAINS(LCASE(?penaltyType), "fine")) } GROUP BY ?providerName ORDER BY DESC(?fineCount)
```

**Seed 2 (wrong) — answer:** `providerName: BERKELEY NURSING & REHAB CENTER, fineCount: 34
providerName: EVERVELLA OF SWANSEA, fineCount: 24
providerName: GOLDEN ROSE CARE CENTER, fineCount: 22
providerName: CONTINENTAL NURSING & REHAB CENTER, fineCount: 22
providerName: Blue Lake Post Acute, fineCount: 18
providerName: AHC OF L`

```sparql
SELECT ?providerName (COUNT(DISTINCT ?penalty) AS ?fineCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cms-nursing-home-compare> WHERE { ?nh <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/NursingHome> . ?nh <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/provider_name> ?providerName . ?nh <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/has_penalty> ?penalty . ?penalty <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/Penalty> . ?penalty <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/penalty_type> ?penaltyType . FILTER(CONTAINS(LCASE(?penaltyType), "fine")) } GROUP BY ?providerName ORDER BY DESC(?fineCount)
```

**Seed 3 (wrong) — answer:** `providerName: BERKELEY NURSING & REHAB CENTER, fineCount: 34
providerName: EVERVELLA OF SWANSEA, fineCount: 24
providerName: GOLDEN ROSE CARE CENTER, fineCount: 22
providerName: CONTINENTAL NURSING & REHAB CENTER, fineCount: 22
providerName: Blue Lake Post Acute, fineCount: 18
providerName: AHC OF L`

```sparql
SELECT ?providerName (COUNT(DISTINCT ?penalty) AS ?fineCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-cms-nursing-home-compare> WHERE { ?nh <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/NursingHome> . ?nh <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/provider_name> ?providerName . ?nh <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/has_penalty> ?penalty . ?penalty <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/type/Penalty> . ?penalty <https://omnix.dev/holdout-v2/holdout-v2-cms-nursing-home-compare/pred/penalty_type> ?penaltyType . FILTER(CONTAINS(LCASE(?penaltyType), "fine")) } GROUP BY ?providerName ORDER BY DESC(?fineCount)
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

**Seed 1 (wrong) — answer:** `crimeTypeName: Fraud - Tax, avgTotalPayment: 6.4136934E8
crimeTypeName: Bank Secrecy Act, avgTotalPayment: 5.03358624E8
crimeTypeName: Fraud - Securities, avgTotalPayment: 3.87600992E8
crimeTypeName: Fraud - General, avgTotalPayment: 3.82012992E8
crimeTypeName: Fraud - Health Care, avgTotalPayment: `

```sparql
SELECT ?crimeTypeName (AVG(<http://www.w3.org/2001/XMLSchema#float>(?totalPayment)) AS ?avgTotalPayment) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doj-enforcement-actions> WHERE { ?action <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/type/Action> . ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_total_payment> ?totalPayment . ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_by_company> ?company . ?company <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/company_is_us_public_co> ?isUSPublic . FILTER(CONTAINS(LCASE(STR(?isUSPublic)), "true")) . ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_for_crime> ?crimeType . ?crimeType <http://www.w3.org/2000/01/rdf-schema#label> ?crimeTypeName . FILTER(STR(?totalPayment) != "" && !CONTAINS(LCASE(STR(?totalPayment)), "n/a")) } GROUP BY ?crimeTypeName ORDER BY DESC(?avgTotalPayment)
```

**Seed 2 (wrong) — answer:** `crimeTypeName: Fraud - Tax, avgTotalPayment: 6.4136928E8
crimeTypeName: Bank Secrecy Act, avgTotalPayment: 5.0335856E8
crimeTypeName: Fraud - Securities, avgTotalPayment: 4.9096128E8
crimeTypeName: Fraud - General, avgTotalPayment: 4.0805936E8
crimeTypeName: Fraud - Health Care, avgTotalPayment: 2.9`

```sparql
SELECT ?crimeTypeName (AVG(<http://www.w3.org/2001/XMLSchema#float>(?totalPayment)) AS ?avgTotalPayment) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doj-enforcement-actions> WHERE { ?action <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/type/Action> . ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_total_payment> ?totalPayment . ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_by_company> ?company . ?company <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/company_is_us_public_co> ?isUSPublic . FILTER(CONTAINS(LCASE(STR(?isUSPublic)), "true") || CONTAINS(LCASE(STR(?isUSPublic)), "1") || CONTAINS(LCASE(STR(?isUSPublic)), "yes")) . ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_for_crime> ?crimeType . ?crimeType <http://www.w3.org/2000/01/rdf-schema#label> ?crimeTypeName . FILTER(STR(?totalPayment) != "" && STR(?totalPayment) != "N/A" && STR(?totalPayment) != "0") } GROUP BY ?crimeTypeName ORDER BY DESC(?avgTotalPayment)
```

**Seed 3 (wrong) — answer:** `crimeTypeName: Fraud - Tax, avgTotalPayment: 6.4136928E8
crimeTypeName: Bank Secrecy Act, avgTotalPayment: 5.03358592E8
crimeTypeName: Fraud - Securities, avgTotalPayment: 4.9096128E8
crimeTypeName: Fraud - General, avgTotalPayment: 4.08059296E8
crimeTypeName: Fraud - Health Care, avgTotalPayment: 2`

```sparql
SELECT ?crimeTypeName (AVG(<http://www.w3.org/2001/XMLSchema#float>(?totalPayment)) AS ?avgTotalPayment) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-doj-enforcement-actions> WHERE { ?action <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/type/Action> . ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_total_payment> ?totalPayment . ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_by_company> ?company . ?company <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/company_is_us_public_co> ?isUSPublic . FILTER(CONTAINS(LCASE(STR(?isUSPublic)), "true") || CONTAINS(LCASE(STR(?isUSPublic)), "1") || CONTAINS(LCASE(STR(?isUSPublic)), "yes")) . ?action <https://omnix.dev/holdout-v2/holdout-v2-doj-enforcement-actions/pred/action_for_crime> ?crimeType . ?crimeType <http://www.w3.org/2000/01/rdf-schema#label> ?crimeTypeName . FILTER(STR(?totalPayment) != "" && STR(?totalPayment) != "N/A" && STR(?totalPayment) != "0") } GROUP BY ?crimeTypeName ORDER BY DESC(?avgTotalPayment)
```

### 7.3 `holdout-v2-epa-water-quality-portal` — q-011 (T4)

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
SELECT (AVG(?sampleCount) AS ?avgSamplesPerOrg) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-epa-water-quality-portal> WHERE { { SELECT ?org (COUNT(DISTINCT ?sample) AS ?sampleCount) WHERE { ?county <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/County> . ?county <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/county_name> ?countyName . FILTER(CONTAINS(LCASE(?countyName), "imperial")) . ?station <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/Station> . ?station <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/station_in_county> ?county . ?station <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/station_operated_by> ?org . ?sample <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/Sample> . ?sample <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/sample_by_organization> ?org . } GROUP BY ?org } }
```

**Seed 2 (wrong) — answer:** `90.66666666666666666667`

```sparql
SELECT (AVG(?sampleCount) AS ?avgSamplesPerOrg) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-epa-water-quality-portal> WHERE { { SELECT ?org (COUNT(DISTINCT ?sample) AS ?sampleCount) WHERE { ?county <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/County> . ?county <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/county_name> ?countyName . FILTER(CONTAINS(LCASE(?countyName), "imperial")) . ?station <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/Station> . ?station <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/station_in_county> ?county . ?station <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/station_operated_by> ?org . ?sample <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/Sample> . ?sample <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/sample_by_organization> ?org . } GROUP BY ?org } }
```

**Seed 3 (wrong) — answer:** `90.66666666666666666667`

```sparql
SELECT (AVG(?sampleCount) AS ?avgSamplesPerOrg) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-epa-water-quality-portal> WHERE { { SELECT ?org (COUNT(DISTINCT ?sample) AS ?sampleCount) WHERE { ?county <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/County> . ?county <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/county_name> ?countyName . FILTER(CONTAINS(LCASE(?countyName), "imperial")) . ?station <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/Station> . ?station <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/station_in_county> ?county . ?station <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/station_operated_by> ?org . ?sample <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/type/Sample> . ?sample <https://omnix.dev/holdout-v2/holdout-v2-epa-water-quality-portal/pred/sample_by_organization> ?org . } GROUP BY ?org } }
```

### 7.4 `holdout-v2-fema-disaster-declarations` — q-003 (T1)

**Question:** How many distinct incident types are recorded in Declarations?

**Expected answer:** `21`

**Gold SPARQL:**
```sparql
SELECT (COUNT(DISTINCT ?incidentType) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations> WHERE { ?declaration <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/Declaration> . ?declaration <https://omnix.dev/types/Declaration/attrs/type> ?incidentType . }
```

**Seed 1 (wrong) — answer:** `18`

```sparql
SELECT (COUNT(DISTINCT ?incidentType) AS ?count)  FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations>
WHERE { ?d <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/FemaDeclaration> . ?d <https://omnix.dev/types/FemaDeclaration/attrs/incident_type> ?incidentType . }
```

**Seed 2 (wrong) — answer:** `18`

```sparql
SELECT (COUNT(DISTINCT ?incidentType) AS ?count)  FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations>
WHERE { ?d <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/FemaDeclaration> . ?d <https://omnix.dev/types/FemaDeclaration/attrs/incident_type> ?incidentType . }
```

**Seed 3 (wrong) — answer:** `18`

```sparql
SELECT (COUNT(DISTINCT ?incidentType) AS ?count)  FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations>
WHERE { ?decl <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/FemaDeclaration> . ?decl <https://omnix.dev/types/FemaDeclaration/attrs/incident_type> ?incidentType . }
```

### 7.5 `holdout-v2-fema-disaster-declarations` — q-007 (T3)

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

**Seed 1 (error) — answer:** `No results found.`

```sparql
SELECT ?regionName (COUNT(DISTINCT ?fema) AS ?declarationCount)  FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations>
WHERE { ?fema <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/FemaDeclaration> . ?fema <https://omnix.dev/types/FemaDeclaration/attrs/hm_program_declared> "true" . ?fema <https://omnix.dev/onto/region> ?region . ?region <https://omnix.dev/types/Region/attrs/name> ?regionName . } GROUP BY ?regionName ORDER BY DESC(?declarationCount)
```

**Seed 2 (error) — answer:** `No results found.`

```sparql
SELECT ?regionName (COUNT(DISTINCT ?fema) AS ?declarationCount)  FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations>
WHERE { ?fema <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/FemaDeclaration> . ?fema <https://omnix.dev/types/FemaDeclaration/attrs/hm_program_declared> "true" . ?fema <https://omnix.dev/onto/region> ?region . ?region <https://omnix.dev/types/Region/attrs/name> ?regionName . } GROUP BY ?regionName ORDER BY DESC(?declarationCount)
```

**Seed 3 (error) — answer:** `No results found.`

```sparql
SELECT ?regionName (COUNT(DISTINCT ?fema) AS ?declarationCount)  FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations>
WHERE { ?fema <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/types/FemaDeclaration> . ?fema <https://omnix.dev/types/FemaDeclaration/attrs/hm_program_declared> "true" . ?fema <https://omnix.dev/onto/region> ?region . ?region <https://omnix.dev/types/Region/attrs/name> ?regionName . } GROUP BY ?regionName ORDER BY DESC(?declarationCount)
```

### 7.6 `holdout-v2-fema-disaster-declarations-multitable` — q-005 (T2)

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
SELECT (COUNT(DISTINCT ?project) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations-multitable> WHERE { ?project <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/PAProject> . ?project <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/pred/projectamount> ?projectamount . FILTER (<http://www.w3.org/2001/XMLSchema#float>(?projectamount) > "5000"^^<http://www.w3.org/2001/XMLSchema#float>) }
```

**Seed 2 (wrong) — answer:** `15593`

```sparql
SELECT (COUNT(DISTINCT ?project) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations-multitable> WHERE { ?project <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/PAProject> . ?project <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/pred/projectamount> ?projectamount . FILTER (<http://www.w3.org/2001/XMLSchema#float>(?projectamount) > "5000"^^<http://www.w3.org/2001/XMLSchema#float>) }
```

**Seed 3 (wrong) — answer:** `15593`

```sparql
SELECT (COUNT(DISTINCT ?project) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations-multitable> WHERE { ?project <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/PAProject> . ?project <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/pred/projectamount> ?projectamount . FILTER (<http://www.w3.org/2001/XMLSchema#float>(?projectamount) > "5000"^^<http://www.w3.org/2001/XMLSchema#float>) }
```

### 7.7 `holdout-v2-fema-disaster-declarations-multitable` — q-007 (T2)

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

**Seed 1 (wrong) — answer:** `projectLabel: 339-U0MDD-00, totalObligated: 293.93
projectLabel: 339-U0MDD-00, totalObligated: 881.75
projectLabel: 201-99201-00, totalObligated: 776.42
projectLabel: 339-99339-00, totalObligated: 932.48
projectLabel: 201-35000-00, totalObligated: 883.97
projectLabel: 073-99073-00, totalObligated: 9`

```sparql
SELECT ?projectLabel ?totalObligated FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations-multitable> WHERE { ?project <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/PAProject> . ?project <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/pred/totalobligated> ?totalObligated . ?project <http://www.w3.org/2000/01/rdf-schema#label> ?projectLabel . FILTER(xsd:float(?totalObligated) < "1000"^^<http://www.w3.org/2001/XMLSchema#float>) }
```

**Seed 2 (wrong) — answer:** `projectLabel: 359-01324-00, totalObligated: 896.16
projectLabel: 000-U7NXZ-00, totalObligated: 996.2
projectLabel: 037-19B59-00, totalObligated: 783.9
projectLabel: 147-99147-00, totalObligated: 972.27
projectLabel: 117-04A86-00, totalObligated: 987.72
projectLabel: 013-99013-00, totalObligated: 792`

```sparql
SELECT ?projectLabel ?totalObligated FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations-multitable> WHERE { ?project <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/PAProject> . ?project <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/pred/totalobligated> ?totalObligated . ?project <http://www.w3.org/2000/01/rdf-schema#label> ?projectLabel . FILTER(xsd:float(?totalObligated) < "1000"^^<http://www.w3.org/2001/XMLSchema#float>) }
```

**Seed 3 (wrong) — answer:** `projectLabel: 117-04A86-00, totalObligated: 987.72
projectLabel: 013-99013-00, totalObligated: 792.04
projectLabel: 013-99013-00, totalObligated: 803.03
projectLabel: 339-99339-00, totalObligated: 932.48
projectLabel: 201-35000-00, totalObligated: 883.97
projectLabel: 073-99073-00, totalObligated: 9`

```sparql
SELECT ?projectLabel ?totalObligated FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-fema-disaster-declarations-multitable> WHERE { ?project <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/type/PAProject> . ?project <https://omnix.dev/holdout-v2/holdout-v2-fema-disaster-declarations-multitable/pred/totalobligated> ?totalObligated . ?project <http://www.w3.org/2000/01/rdf-schema#label> ?projectLabel . FILTER(xsd:float(?totalObligated) < "1000"^^<http://www.w3.org/2001/XMLSchema#float>) }
```

### 7.8 `holdout-v2-medicare-part-d-pricing` — q-013 (T4)

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

**Seed 1 (wrong) — answer:** `genericName: Dextroamphetamine Sulfate
genericName: Stiripentol
genericName: TriHEPtanoin
genericName: Avatrombopag Maleate
genericName: Doxycycline Hyclate
genericName: Dolutegravir Sodium/Lamivudine
genericName: Docetaxel
genericName: Somatropin
genericName: Halcinonide
genericName: Insulin Lispro`

```sparql
SELECT DISTINCT ?genericName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing> WHERE { ?drug <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/Drug> . ?drug <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/generic_name> ?genericName . ?drug <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/has_spending_record> ?sr . ?sr <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/SpendingRecord> . ?sr <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/has_outlier_status> ?outlier . ?outlier <http://www.w3.org/2000/01/rdf-schema#label> ?outlierLabel . FILTER(CONTAINS(LCASE(?outlierLabel), "outlier")) . ?sr <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/avg_spend_per_claim_2023> ?avgSpendPerClaim . FILTER(<http://www.w3.org/2001/XMLSchema#float>(?avgSpendPerClaim) > "500"^^<http://www.w3.org/2001/XMLSchema#float>) }
```

**Seed 2 (wrong) — answer:** `genericName: Deferasirox
genericName: Penicillamine
genericName: Mesalamine
genericName: Rosuvastatin Calcium
genericName: Divalproex Sodium
genericName: Cobimetinib Fumarate
genericName: Lipase/Protease/Amylase
genericName: Isavuconazonium Sulfate
genericName: Cromolyn Sodium
genericName: Burosumab`

```sparql
SELECT DISTINCT ?genericName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing> WHERE { ?drug <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/Drug> . ?drug <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/generic_name> ?genericName . ?drug <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/has_spending_record> ?sr . ?sr <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/SpendingRecord> . ?sr <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/avg_spend_per_claim_2023> ?avgSpendPerClaim . ?sr <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/has_outlier_status> ?outlierFlag . ?outlierFlag <http://www.w3.org/2000/01/rdf-schema#label> ?outlierLabel . FILTER(CONTAINS(LCASE(?outlierLabel), "outlier")) . FILTER(<http://www.w3.org/2001/XMLSchema#float>(?avgSpendPerClaim) > "500"^^<http://www.w3.org/2001/XMLSchema#float>) }
```

**Seed 3 (wrong) — answer:** `genericName: Hydrocortisone Butyrate
genericName: Gabapentin
genericName: Trastuzumab
genericName: Metformin HCl
genericName: Propranolol HCl
genericName: Trastuzumab-Hyaluronidase-Oysk
genericName: Trastuzumab-Pkrb
genericName: Tasimelteon
genericName: Immun Glob G(IgG)/Pro/IgA 0-50
genericName: Ga`

```sparql
SELECT DISTINCT ?genericName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-medicare-part-d-pricing> WHERE { ?drug <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/Drug> . ?drug <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/generic_name> ?genericName . ?drug <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/has_spending_record> ?sr . ?sr <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/type/SpendingRecord> . ?sr <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/has_outlier_status> ?outlier . ?outlier <http://www.w3.org/2000/01/rdf-schema#label> ?outlierLabel . FILTER(CONTAINS(LCASE(?outlierLabel), "outlier")) . ?sr <https://omnix.dev/holdout-v2/holdout-v2-medicare-part-d-pricing/pred/avg_spend_per_claim_2023> ?avgSpendPerClaim . FILTER(<http://www.w3.org/2001/XMLSchema#float>(?avgSpendPerClaim) > "500"^^<http://www.w3.org/2001/XMLSchema#float>) }
```

### 7.9 `holdout-v2-ncua-credit-union-call-reports` — q-012 (T4)

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

**Seed 1 (wrong) — answer:** `cuName: TLC COMMUNITY
cuName: MICHIGAN FIRST
cuName: ASSOCIATED CREDIT UNION OF TEXAS
cuName: EPB EMPLOYEES
cuName: CLIFFORD-JACOBS EMPLOYEES
cuName: NEW SOUTH CREDIT UNION
cuName: A.A.E.C.
cuName: SYNERGY PARTNERS
cuName: ALLVAC SAVINGS AND
cuName: ARROWHEAD CENTRAL
cuName: AMERICAN PRIDE
cuName: F`

```sparql
SELECT DISTINCT ?cuName WHERE { GRAPH <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ncua-credit-union-call-reports> { ?cu <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/CreditUnion> . ?cu <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/creditunion_cu_name> ?cuName . ?cu <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/has_call_report> ?cr . ?cr <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/callreport_has_metric> ?metric . ?metric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_name> ?metricName . FILTER(CONTAINS(LCASE(?metricName), "net worth ratio")) . ?metric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_value> ?metricValue . BIND(xsd:float(?metricValue) AS ?numValue) . FILTER(?numValue > "0.05"^^<http://www.w3.org/2001/XMLSchema#float>) } }
```

**Seed 2 (wrong) — answer:** `cuName: YUMA COUNTY
cuName: POLICE CREDIT UNION OF CONNECTICUT
cuName: CAREY POVERELLO
cuName: HARRISON POLICE & FIREMEN'S
cuName: ATLANTIC FINANCIAL
cuName: FINANCIAL RESOURCES
cuName: TSU
cuName: CONNECTS
cuName: U S EMPLOYEES
cuName: ACT 1ST
cuName: MUTUAL SAVINGS
cuName: EDUCATIONAL EMPLOYEES
cu`

```sparql
SELECT DISTINCT ?cuName WHERE { GRAPH <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ncua-credit-union-call-reports> { ?cu <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/CreditUnion> . ?cu <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/creditunion_cu_name> ?cuName . ?cu <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/has_call_report> ?cr . ?cr <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/callreport_has_metric> ?metric . ?metric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_name> ?metricName . FILTER(CONTAINS(LCASE(?metricName), "net worth ratio")) . ?metric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_value> ?metricValue . FILTER(xsd:float(?metricValue) > "0.05"^^<http://www.w3.org/2001/XMLSchema#float>) } }
```

**Seed 3 (wrong) — answer:** `cuName: MILLSTREAM AREA
cuName: AURGROUP
cuName: PSE CREDIT UNION, INC.
cuName: PRODUCERS EMPLOYEES'
cuName: ST. PAUL A.M.E.  ZION CHURCH
cuName: U.P.S.
cuName: WIREMEN'S
cuName: MUNICIPAL EMPL.CREDIT UNION OF BALT
cuName: SAN JOAQUIN POWER EMPLOYEES
cuName: FORWARD FINANCIAL
cuName: LANDMARK
cuName`

```sparql
SELECT DISTINCT ?cuName WHERE { GRAPH <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ncua-credit-union-call-reports> { ?cu <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/type/CreditUnion> . ?cu <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/creditunion_cu_name> ?cuName . ?cu <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/has_call_report> ?cr . ?cr <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/callreport_has_metric> ?metric . ?metric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_name> ?metricName . FILTER(CONTAINS(LCASE(?metricName), "net worth ratio")) . ?metric <https://omnix.dev/holdout-v2/holdout-v2-ncua-credit-union-call-reports/pred/metric_value> ?metricValue . FILTER(xsd:float(?metricValue) > "0.05"^^<http://www.w3.org/2001/XMLSchema#float>) } }
```

### 7.10 `holdout-v2-npi-registry` — q-009 (T3)

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

**Seed 1 (wrong) — answer:** `practiceLocationLabel: 125 S MAIN CROSS ST, providerCount: 737
practiceLocationLabel: 54 HOSPITAL DR, providerCount: 150
practiceLocationLabel: 445 E DUBLIN GRANVILLE RD, providerCount: 66
practiceLocationLabel: 525 METRO PL N STE 300, providerCount: 59
practiceLocationLabel: 525 METRO PL N STE 100,`

```sparql
SELECT ?practiceLocationLabel (COUNT(DISTINCT ?provider) AS ?providerCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-npi-registry> WHERE { ?pl <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/type/PracticeLocation> . ?pl <http://www.w3.org/2000/01/rdf-schema#label> ?practiceLocationLabel . ?pl <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/practice_location_of_provider> ?provider . } GROUP BY ?practiceLocationLabel ORDER BY DESC(?providerCount)
```

**Seed 2 (wrong) — answer:** `practiceLocationLabel: 125 S MAIN CROSS ST, providerCount: 737
practiceLocationLabel: 54 HOSPITAL DR, providerCount: 150
practiceLocationLabel: 445 E DUBLIN GRANVILLE RD, providerCount: 66
practiceLocationLabel: 525 METRO PL N STE 300, providerCount: 59
practiceLocationLabel: 525 METRO PL N STE 100,`

```sparql
SELECT ?practiceLocationLabel (COUNT(DISTINCT ?provider) AS ?providerCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-npi-registry> WHERE { ?pl <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/type/PracticeLocation> . ?pl <http://www.w3.org/2000/01/rdf-schema#label> ?practiceLocationLabel . ?pl <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/practice_location_of_provider> ?provider . } GROUP BY ?practiceLocationLabel ORDER BY DESC(?providerCount)
```

**Seed 3 (wrong) — answer:** `practiceLocationLabel: 125 S MAIN CROSS ST, providerCount: 737
practiceLocationLabel: 54 HOSPITAL DR, providerCount: 150
practiceLocationLabel: 445 E DUBLIN GRANVILLE RD, providerCount: 66
practiceLocationLabel: 525 METRO PL N STE 300, providerCount: 59
practiceLocationLabel: 525 METRO PL N STE 100,`

```sparql
SELECT ?practiceLocationLabel (COUNT(DISTINCT ?provider) AS ?providerCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-npi-registry> WHERE { ?pl <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/type/PracticeLocation> . ?pl <http://www.w3.org/2000/01/rdf-schema#label> ?practiceLocationLabel . ?pl <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/practice_location_of_provider> ?provider . } GROUP BY ?practiceLocationLabel ORDER BY DESC(?providerCount)
```

### 7.11 `holdout-v2-npi-registry` — q-011 (T3)

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
SELECT ?specialtyName (COUNT(DISTINCT ?provider) AS ?providerCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-npi-registry> WHERE { ?specialty <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/type/Specialty> . ?specialty <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/specialty_name> ?specialtyName . FILTER(CONTAINS(LCASE(?specialtyName), LCASE("Unknown"))) . ?specialty <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/specialty_practiced_by> ?provider . } GROUP BY ?specialtyName ORDER BY DESC(?providerCount)
```

**Seed 2 (wrong) — answer:** `specialtyName: Unknown, providerCount: 46`

```sparql
SELECT ?specialtyName (COUNT(DISTINCT ?provider) AS ?providerCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-npi-registry> WHERE { ?specialty <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/type/Specialty> . ?specialty <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/specialty_name> ?specialtyName . FILTER(CONTAINS(LCASE(?specialtyName), LCASE("Unknown"))) . ?specialty <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/specialty_practiced_by> ?provider . } GROUP BY ?specialtyName ORDER BY DESC(?providerCount)
```

**Seed 3 (wrong) — answer:** `specialtyName: Unknown, providerCount: 46`

```sparql
SELECT ?specialtyName (COUNT(DISTINCT ?provider) AS ?providerCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-npi-registry> WHERE { ?specialty <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/type/Specialty> . ?specialty <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/specialty_name> ?specialtyName . FILTER(CONTAINS(LCASE(?specialtyName), LCASE("Unknown"))) . ?specialty <https://omnix.dev/holdout-v2/holdout-v2-npi-registry/pred/specialty_practiced_by> ?provider . } GROUP BY ?specialtyName ORDER BY DESC(?providerCount)
```

### 7.12 `holdout-v2-ofr-financial-stability` — q-012 (T4)

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

**Seed 1 (wrong) — answer:** `yearValue: 2000, avgValue: 1.0443202
yearValue: 2001, avgValue: 1.2513729
yearValue: 2002, avgValue: 1.2831706
yearValue: 2003, avgValue: 0.44251332
yearValue: 2004, avgValue: -0.6876069
yearValue: 2005, avgValue: -1.2797383
yearValue: 2006, avgValue: -1.2572222
yearValue: 2007, avgValue: -0.5077646`

```sparql
SELECT ?yearValue (AVG(xsd:float(?value)) AS ?avgValue) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ofr-financial-stability> WHERE { ?reading <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/type/IndicatorReading> . ?reading <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/value> ?value . ?reading <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/reading_on_period> ?period . ?period <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/type/Period> . ?period <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/day_of_week> ?dow . FILTER(CONTAINS(LCASE(?dow), "friday")) . ?period <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/period_in_month> ?month . ?month <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/month_in_year> ?year . ?year <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/year> ?yearValue . } GROUP BY ?yearValue ORDER BY ?yearValue
```

**Seed 2 (wrong) — answer:** `yearValue: 2000, avgValue: 1.04432
yearValue: 2001, avgValue: 1.2513734
yearValue: 2002, avgValue: 1.28317
yearValue: 2003, avgValue: 0.4425135
yearValue: 2004, avgValue: -0.68760675
yearValue: 2005, avgValue: -1.2797388
yearValue: 2006, avgValue: -1.2572227
yearValue: 2007, avgValue: -0.50776446
ye`

```sparql
SELECT ?yearValue (AVG(xsd:float(?value)) AS ?avgValue) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ofr-financial-stability> WHERE { ?reading <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/type/IndicatorReading> . ?reading <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/value> ?value . ?reading <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/reading_on_period> ?period . ?period <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/type/Period> . ?period <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/day_of_week> ?dow . FILTER(CONTAINS(LCASE(?dow), "friday")) . ?period <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/period_in_month> ?month . ?month <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/month_in_year> ?year . ?year <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/year> ?yearValue . } GROUP BY ?yearValue ORDER BY ?yearValue
```

**Seed 3 (wrong) — answer:** `yearValue: 2000, avgValue: 1.04432
yearValue: 2001, avgValue: 1.2513735
yearValue: 2002, avgValue: 1.2831705
yearValue: 2003, avgValue: 0.44251356
yearValue: 2004, avgValue: -0.68760675
yearValue: 2005, avgValue: -1.2797377
yearValue: 2006, avgValue: -1.2572229
yearValue: 2007, avgValue: -0.50776464`

```sparql
SELECT ?yearValue (AVG(xsd:float(?value)) AS ?avgValue) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-ofr-financial-stability> WHERE { ?reading <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/type/IndicatorReading> . ?reading <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/value> ?value . ?reading <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/reading_on_period> ?period . ?period <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/type/Period> . ?period <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/day_of_week> ?dow . FILTER(CONTAINS(LCASE(?dow), "friday")) . ?period <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/period_in_month> ?month . ?month <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/type/Month> . ?month <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/month_in_year> ?year . ?year <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/type/Year> . ?year <https://omnix.dev/holdout-v2/holdout-v2-ofr-financial-stability/pred/year> ?yearValue . } GROUP BY ?yearValue ORDER BY ?yearValue
```

### 7.13 `holdout-v2-patentsview` — q-007 (T2)

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

**Seed 1 (wrong) — answer:** `orgName: THINKON NEW TECHNOLOGY JAPAN CORPORATION
orgName: Beijing E-Town Semiconductor Technology Co., Ltd.
orgName: Industrial Technology Research Institute
orgName: FOXCONN INTERCONNECT TECHNOLOGY LIMITED
orgName: FLINTSTONE TECHNOLOGY LIMITED
orgName: GE ENERGY POWER CONVERSION TECHNOLOGY LIMITE`

```sparql
SELECT DISTINCT ?orgName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview> WHERE { ?assignee <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Assignee> . ?assignee <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/disambig_assignee_organization> ?orgName . FILTER(CONTAINS(LCASE(?orgName), "technology")) }
```

**Seed 2 (wrong) — answer:** `orgName: THINKON NEW TECHNOLOGY JAPAN CORPORATION
orgName: Beijing E-Town Semiconductor Technology Co., Ltd.
orgName: Industrial Technology Research Institute
orgName: FOXCONN INTERCONNECT TECHNOLOGY LIMITED
orgName: FLINTSTONE TECHNOLOGY LIMITED
orgName: Ningbo Defeng Power Technology Co., Ltd.
org`

```sparql
SELECT DISTINCT ?orgName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview> WHERE { ?assignee <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Assignee> . ?assignee <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/disambig_assignee_organization> ?orgName . FILTER(CONTAINS(LCASE(?orgName), "technology")) }
```

**Seed 3 (wrong) — answer:** `orgName: University of Electronic Science and Technology of China
orgName: Lummus Technology LLC
orgName: SUZHOU CHINA STAR OPTOELECTRONICS TECHNOLOGY CO., LTD.
orgName: SHENZHEN GOODIX TECHNOLOGY CO., LTD.
orgName: NuFlare Technology, Inc.
orgName: SIKA TECHNOLOGY AG
orgName: GE ENERGY POWER CONVER`

```sparql
SELECT DISTINCT ?orgName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-patentsview> WHERE { ?assignee <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-patentsview/type/Assignee> . ?assignee <https://omnix.dev/holdout-v2/holdout-v2-patentsview/pred/disambig_assignee_organization> ?orgName . FILTER(CONTAINS(LCASE(?orgName), "technology")) }
```

### 7.14 `holdout-v2-samhsa-n-ssats` — q-008 (T3)

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

**Seed 1 (wrong) — answer:** `facilityName: 6944
facilityName: 6946
facilityName: 6948
facilityName: 6954
facilityName: 6959
facilityName: 6851
facilityName: 6933
facilityName: 6934
facilityName: 6935
facilityName: 6938
facilityName: 6939
facilityName: 6940
facilityName: 6942
facilityName: 6943
facilityName: 6953
facilityName: 6`

```sparql
SELECT DISTINCT ?facilityName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-samhsa-n-ssats> WHERE { ?facility <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/TreatmentFacility> . ?facility <http://www.w3.org/2000/01/rdf-schema#label> ?facilityName . ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/accepts_payment> ?payment . ?payment <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/PaymentType> . ?payment <http://www.w3.org/2000/01/rdf-schema#label> ?paymentName . FILTER(CONTAINS(LCASE(?paymentName), "medicaid")) . ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/serves_client_type> ?clientType . ?clientType <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/ClientType> . ?clientType <http://www.w3.org/2000/01/rdf-schema#label> ?clientTypeName . FILTER(CONTAINS(LCASE(?clientTypeName), "adolescents")) . }
```

**Seed 2 (wrong) — answer:** `facilityName: 11763
facilityName: 11733
facilityName: 11730
facilityName: 11746
facilityName: 11744
facilityName: 11731
facilityName: 11764
facilityName: 11721
facilityName: 11735
facilityName: 11732
facilityName: 11727
facilityName: 11762
facilityName: 11726
facilityName: 11723
facilityName: 11724
`

```sparql
SELECT DISTINCT ?facilityName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-samhsa-n-ssats> WHERE { ?facility <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/TreatmentFacility> . ?facility <http://www.w3.org/2000/01/rdf-schema#label> ?facilityName . ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/accepts_payment> ?payment . ?payment <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/PaymentType> . ?payment <http://www.w3.org/2000/01/rdf-schema#label> ?paymentName . FILTER(CONTAINS(LCASE(?paymentName), "medicaid")) . ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/serves_client_type> ?clientType . ?clientType <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/ClientType> . ?clientType <http://www.w3.org/2000/01/rdf-schema#label> ?clientTypeName . FILTER(CONTAINS(LCASE(?clientTypeName), "adolescents")) . }
```

**Seed 3 (wrong) — answer:** `facilityName: 11514
facilityName: 11468
facilityName: 11470
facilityName: 11474
facilityName: 11494
facilityName: 11497
facilityName: 11498
facilityName: 11504
facilityName: 11506
facilityName: 11515
facilityName: 11528
facilityName: 11496
facilityName: 11503
facilityName: 11472
facilityName: 11477
`

```sparql
SELECT DISTINCT ?facilityName FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-samhsa-n-ssats> WHERE { ?facility <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/TreatmentFacility> . ?facility <http://www.w3.org/2000/01/rdf-schema#label> ?facilityName . ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/accepts_payment> ?payment . ?payment <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/PaymentType> . ?payment <http://www.w3.org/2000/01/rdf-schema#label> ?paymentName . FILTER(CONTAINS(LCASE(?paymentName), "medicaid")) . ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/serves_client_type> ?clientType . ?clientType <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/ClientType> . ?clientType <http://www.w3.org/2000/01/rdf-schema#label> ?clientTypeName . FILTER(CONTAINS(LCASE(?clientTypeName), "adolescents")) . }
```

### 7.15 `holdout-v2-samhsa-n-ssats` — q-010 (T4)

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

**Seed 1 (wrong) — answer:** `79.9`

```sparql
SELECT (AVG(?serviceCount) AS ?avgServicesPerFacility) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-samhsa-n-ssats> WHERE { { SELECT ?stateName (COUNT(DISTINCT ?service) AS ?serviceCount) WHERE { ?facility <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/TreatmentFacility> . ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/located_in_state> ?state . ?state <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/State> . ?state <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/state_name> ?stateName . ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/offers_service> ?service . } GROUP BY ?stateName } }
```

**Seed 2 (wrong) — answer:** `79.9`

```sparql
SELECT (AVG(?serviceCount) AS ?avgServicesPerFacility) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-samhsa-n-ssats> WHERE { { SELECT ?stateName (COUNT(DISTINCT ?service) AS ?serviceCount) WHERE { ?facility <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/TreatmentFacility> . ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/located_in_state> ?state . ?state <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/State> . ?state <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/state_name> ?stateName . ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/offers_service> ?service . } GROUP BY ?stateName } }
```

**Seed 3 (wrong) — answer:** `79.9`

```sparql
SELECT (AVG(?serviceCount) AS ?avgServicesPerFacility) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-samhsa-n-ssats> WHERE { { SELECT ?stateName (COUNT(DISTINCT ?service) AS ?serviceCount) WHERE { ?facility <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/TreatmentFacility> . ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/located_in_state> ?state . ?state <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/type/State> . ?state <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/state_name> ?stateName . ?facility <https://omnix.dev/holdout-v2/holdout-v2-samhsa-n-ssats/pred/offers_service> ?service . } GROUP BY ?stateName } }
```

### 7.16 `holdout-v2-sec-edgar-10k` — q-007 (T3)

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

**Seed 1 (wrong) — answer:** `filerName: Wilhelmina International, Inc., filerEntityType: operating, filingCount: 1
filerName: Chain Bridge I, filerEntityType: operating, filingCount: 1
filerName: ADM TRONICS UNLIMITED, INC., filerEntityType: operating, filingCount: 1
filerName: Riley Exploration Permian, Inc., filerEntityType: `

```sparql
SELECT ?filerName ?filerEntityType (COUNT(DISTINCT ?filing) AS ?filingCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-sec-edgar-10k> WHERE { ?filer <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/type/Filer> . ?filer <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filer_entity_type> ?filerEntityType . FILTER(CONTAINS(LCASE(?filerEntityType), "operating")) . ?filer <http://www.w3.org/2000/01/rdf-schema#label> ?filerName . ?filer <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/has_filing> ?filing . } GROUP BY ?filer ?filerName ?filerEntityType
```

**Seed 2 (wrong) — answer:** `filerName: HUNTINGTON BANCSHARES INC /MD/, filerEntityType: operating, filingCount: 1
filerName: Kalaris Therapeutics, Inc., filerEntityType: operating, filingCount: 1
filerName: Global Innovative Platforms Inc., filerEntityType: operating, filingCount: 1
filerName: PhenixFIN Corp, filerEntityType: `

```sparql
SELECT ?filerName ?filerEntityType (COUNT(DISTINCT ?filing) AS ?filingCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-sec-edgar-10k> WHERE { ?filer <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/type/Filer> . ?filer <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filer_entity_type> ?filerEntityType . FILTER(CONTAINS(LCASE(?filerEntityType), "operating")) . ?filer <http://www.w3.org/2000/01/rdf-schema#label> ?filerName . ?filer <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/has_filing> ?filing . } GROUP BY ?filer ?filerName ?filerEntityType
```

**Seed 3 (wrong) — answer:** `filerName: TRANSACT TECHNOLOGIES INC, filerEntityType: operating, filingCount: 1
filerName: MIMEDX GROUP, INC., filerEntityType: operating, filingCount: 1
filerName: BOEING CO, filerEntityType: operating, filingCount: 1
filerName: Birchtech Corp., filerEntityType: operating, filingCount: 1
filerName`

```sparql
SELECT ?filerName ?filerEntityType (COUNT(DISTINCT ?filing) AS ?filingCount) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-sec-edgar-10k> WHERE { ?filer <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/type/Filer> . ?filer <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/filer_entity_type> ?filerEntityType . FILTER(CONTAINS(LCASE(?filerEntityType), "operating")) . ?filer <http://www.w3.org/2000/01/rdf-schema#label> ?filerName . ?filer <https://omnix.dev/holdout-v2/holdout-v2-sec-edgar-10k/pred/has_filing> ?filing . } GROUP BY ?filer ?filerName ?filerEntityType
```

### 7.17 `holdout-v2-uspto-trademarks` — q-007 (T2)

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
SELECT (COUNT(DISTINCT ?nc) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-uspto-trademarks> WHERE { ?nc <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/NiceClass> . ?nc <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/class_code> ?classCode . FILTER(STR(?classCode) > "030") }
```

**Seed 2 (wrong) — answer:** `16`

```sparql
SELECT (COUNT(DISTINCT ?nc) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-uspto-trademarks> WHERE { ?nc <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/NiceClass> . ?nc <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/class_code> ?classCode . FILTER(STR(?classCode) > "030") }
```

**Seed 3 (wrong) — answer:** `16`

```sparql
SELECT (COUNT(DISTINCT ?nc) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-uspto-trademarks> WHERE { ?nc <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/NiceClass> . ?nc <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/class_code> ?classCode . FILTER(STR(?classCode) > "030") }
```

### 7.18 `holdout-v2-uspto-trademarks` — q-008 (T2)

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
SELECT (COUNT(DISTINCT ?t) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-uspto-trademarks> WHERE { ?t <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/Trademark> . ?t <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/mark_element> ?me . FILTER(CONTAINS(LCASE(?me), LCASE("BETTER SEX"))) }
```

**Seed 2 (wrong) — answer:** `6`

```sparql
SELECT (COUNT(DISTINCT ?t) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-uspto-trademarks> WHERE { ?t <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/Trademark> . ?t <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/mark_element> ?markElement . FILTER(CONTAINS(LCASE(?markElement), LCASE("BETTER SEX"))) }
```

**Seed 3 (wrong) — answer:** `6`

```sparql
SELECT (COUNT(DISTINCT ?t) AS ?count) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-uspto-trademarks> WHERE { ?t <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/Trademark> . ?t <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/mark_element> ?markElement . FILTER(CONTAINS(LCASE(?markElement), LCASE("BETTER SEX"))) }
```

### 7.19 `holdout-v2-uspto-trademarks` — q-012 (T4)

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
SELECT (AVG(?gsCount) AS ?avgGoodsServicesPerOwner) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-uspto-trademarks> WHERE { { SELECT ?owner (COUNT(DISTINCT ?gs) AS ?gsCount) WHERE { ?owner <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/Owner> . ?tm <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/Trademark> . ?tm <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/trademark_owned_by> ?owner . ?tm <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/trademark_covers_goods> ?gs . } GROUP BY ?owner } }
```

**Seed 2 (wrong) — answer:** `1.64769647696476964770`

```sparql
SELECT (AVG(?gsCount) AS ?avgGoodsServicesPerOwner) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-uspto-trademarks> WHERE { { SELECT ?owner (COUNT(DISTINCT ?gs) AS ?gsCount) WHERE { ?owner <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/Owner> . ?tm <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/Trademark> . ?tm <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/trademark_owned_by> ?owner . ?tm <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/trademark_covers_goods> ?gs . } GROUP BY ?owner } }
```

**Seed 3 (wrong) — answer:** `1.64769647696476964770`

```sparql
SELECT (AVG(?gsCount) AS ?avgGoodsServicesPerOwner) FROM <https://omnix.dev/graphs/demo-tenant/kg/holdout-v2-uspto-trademarks> WHERE { { SELECT ?owner (COUNT(DISTINCT ?gs) AS ?gsCount) WHERE { ?owner <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/Owner> . ?tm <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/type/Trademark> . ?tm <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/trademark_owned_by> ?owner . ?tm <https://omnix.dev/holdout-v2/holdout-v2-uspto-trademarks/pred/trademark_covers_goods> ?gs . } GROUP BY ?owner } }
```

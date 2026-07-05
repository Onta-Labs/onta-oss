# Ontology-decomposition quality fixtures

Test data + an **expected-shape spec** for a harness that scores how well an ingest
**decomposes** flat tabular/web rows into a well-shaped ontology.

## What "good decomposition" means (the thing we score)

A *good* ingest turns a flat row into structure:

- Real-world, **reusable** things (a hospital, a city, an org, a sector) become
  **entity NODES** with **relationships** pointing at them — so two rows that name
  the same hospital share **one** node.
- **Multi-valued** fields ("Family Medicine, Geriatric Medicine") become
  **repeated** assertions, not one glued string.
- **Roles** ("MD" / "NP" / "PA-C") become **distinct SUBTYPES** under a common
  parent — not one bucket type, and not a flat string column.
- **Composite** geography ("Wichita, Kansas") **splits** into a City node + a State
  node.
- Pure **identifiers / measurements / prices / flags** stay **LITERALS** with the
  right datatype.

A *bad* (naive-flatten) ingest mirrors the column shape: every field a literal on
one mega-type, multi-values kept as one string, roles collapsed or left as a
`credential` string, geography left as `"City, ST"`. Each fixture below deliberately
plants traps that a naive flatten gets wrong, so the harness can tell the two apart.

## How to use these fixtures

Each domain is **one JSON file: a flat array of ~15 row dicts** (string/number/bool
scalars, multi-values comma-joined, geography embedded) — the shape a web/CSV
provider actually returns. Feed a file's rows through the ingest under test, read
back the resulting ontology + instance graph, and assert against that domain's
section below.

`clinical_trials.json` additionally ships `clinical_trials.seed_ontology.json`:
**seed that ontology first**, then ingest, then assert the reuse contract.

All data is **synthetic** — fictional names, no real people/PII. Any resemblance to
real NPIs, tickers, or trials is coincidental.

---

## Vocabulary this spec uses (grounded in the resolver code)

These are the actual mechanisms the ingest uses; the assertions below are written in
terms of them. Sources in `cograph_client/`:

| Concept | Where it lives | Concrete form |
|---|---|---|
| Entity type | `resolver/models.py` `ExtractedEntity.type_name` | PascalCase **singular** (`Physician`, `City`, `Organization`) |
| Type URI | `graph/ontology_queries.py` `type_uri()` | `https://cograph.tech/types/{TypeName}` |
| Entity (instance) URI | `resolver/schema_resolver.py` | `https://cograph.tech/entities/{Type}/{safe_id}` |
| Attribute (literal) | `ExtractedAttribute.name` + `.datatype` | snake_case name; datatype ∈ `PRIMITIVE_TYPES` |
| `PRIMITIVE_TYPES` | `graph/ontology_queries.py:731` | `{string, integer, float, boolean, datetime, uri, geo}` — **note `float`, not `decimal`** |
| xsd mapping | `graph/ontology_queries.py` `_DATATYPE_TO_XSD` | `integer→xsd:integer`, `float→xsd:float`, `boolean→xsd:boolean`, `string→xsd:string` |
| Relationship (edge) | `ExtractedRelationship.predicate` | snake_case predicate; URI `https://cograph.tech/onto/{predicate}` |
| Relationship target | `ColumnMapping.target_type` | the node type the edge points at |
| Subtype | `ExtractedEntity.parent_type` + `parent_chain` | materialized `rdfs:subClassOf https://cograph.tech/types/{Parent}` |
| Type reuse verdict | `resolver/type_matcher.py` `MatchVerdict` | `SAME` / `SUBTYPE` / `DIFFERENT` / `FLAGGED`; `TypeMatch.is_new` bool |
| Reuse thresholds | `type_matcher.py` | embedding `≥0.92 → SAME`, `<0.55 → DIFFERENT`, `0.55–0.92 → LLM` |
| Multi-value split (relationship col) | `resolver/csv_resolver.py:1413` | split on `\|`, or on `, ` **iff every part `<30` chars and `≥2` parts** |
| Multi-value split (attribute col) | `resolver/csv_resolver.py` | split on `\|` only |

### Reading the per-field tables

Each field is assigned exactly one **role**:

- **LITERAL** — an attribute on its owning entity, with the stated datatype.
- **NODE** — the field's value(s) become a separate entity of the stated **target
  type**, reached by a **relationship**. (A "NODE" field is always also the subject
  of a relationship; the "predicate" column names it.)
- **SUBTYPE-DRIVER** — the field is not stored verbatim; it *selects the subtype* of
  the row's primary entity (see the SUBTYPES block).
- **COMPOSITE→SPLIT** — the single string must be split into **two** nodes.
- **MULTI-VALUED** — repeated: N values ⇒ N assertions/edges, never one glued value.

Predicate / type / attribute names below are **canonical intent**, matched
**semantically** (snake_case predicate, PascalCase-singular type). Assert on the
*structure and target type*, not on an exact predicate spelling — `works_at` vs
`affiliated_with` are both acceptable for the same edge; a harness may accept a
small synonym set per row. What is **not** negotiable: the role (node vs literal vs
subtype), the target type identity, multi-value cardinality, the subtype/parent
shape, and the reuse verdicts.

### "New types minted" range

Each domain gives a **[min, max]** for the count of **NEW ontology TYPES** created
by ingesting that file into an **empty** ontology (except clinical_trials, which
seeds first). Count **types** (classes), **not** instances and **not** attributes.

- **Below min ⇒ under-decomposition** (naive flatten collapsed nodes/subtypes into
  literals or one bucket type). This is the primary failure the harness catches.
- **Above max ⇒ over-fragmentation** (e.g. minting a distinct type per row, or a
  separate type for every category token — the `SchemaResolver`-not-reentrant /
  over-fragmented-ontology smell). Also a failure.

The counts assume the standard decomposition (primary entity + its dimension nodes +
role subtypes + the common parent). Where a modeling choice is genuinely optional
(e.g. category strings as literal-multi-values *vs* Category nodes), the range spans
both defensible readings; the notes call this out.

---

## 1. `healthcare_providers.json` — subtypes + shared-org + embedded geo

15 rows mixing physicians (MD/DO), nurse practitioners (NP), and physician
assistants (PA-C). **Primary entity: a healthcare Provider** (keyed by `npi`).

**Trap:** the naive flatten types every row `Physician` (or dumps `credential` as a
string). Correct decomposition makes role a **subtype under a shared parent**, splits
the comma-joined `specialty`, and lifts `city`/`state`/`hospital_affiliation` into
shared nodes.

### Field roles

| Field | Role | Target type / datatype | Predicate (intent) | Notes |
|---|---|---|---|---|
| `npi` | LITERAL | `string` | — | identifier; **stays literal** (the entity key) |
| `full_name` | LITERAL | `string` | — | label of the Provider |
| `credential` | SUBTYPE-DRIVER | — | — | `MD`/`DO`→Physician, `NP`→NursePractitioner, `PA-C`→PhysicianAssistant |
| `provider_type` | SUBTYPE-DRIVER | — | — | corroborates `credential`; same subtype selection |
| `specialty` | NODE, **MULTI-VALUED** | `Specialty` (or `MedicalSpecialty`) | `has_specialty` | comma-joined ⇒ **≥2 edges** when 2 specialties listed |
| `hospital_affiliation` | NODE | `Organization` (or `Hospital`) | `affiliated_with` / `works_at` | **shared**: 5 rows → Rivergate = **one** node |
| `city` | NODE | `City` | `located_in` / `practices_in` | embedded geo; shared across rows |
| `state` | NODE | `State` | `in_state` / `located_in` | shared; `City located_in State` also acceptable |
| `phone` | LITERAL | `string` | — | keep literal (leading-zero / punctuation safe) |
| `accepting_new_patients` | LITERAL | `boolean` | — | `xsd:boolean` |

### Subtypes (the headline assertion)

Three subtypes, one common parent:

```
Provider  (or HealthcareProvider)   ← common parent, MUST exist
├─ Physician              (rdfs:subClassOf Provider)   ← MD, DO rows
├─ NursePractitioner      (rdfs:subClassOf Provider)   ← NP rows
└─ PhysicianAssistant     (rdfs:subClassOf Provider)   ← PA-C rows
```

Assert: (a) all three role types exist; (b) each is `rdfs:subClassOf` the **same**
parent; (c) the parent is a Provider-ish supertype, **not** one of the three roles
(i.e. NP is **not** modeled as a subClassOf Physician); (d) **no** row is left with
`credential`/`provider_type` as a bare string attribute on a single flat type. A
harness may accept the parent under either the name `Provider` or
`HealthcareProvider`.

### Multi-value check

Rows with 2 specialties (`"Family Medicine, Geriatric Medicine"`,
`"Internal Medicine, Cardiology"`, `"Pediatrics, Adolescent Medicine"`,
`"Psychiatry, Addiction Medicine"`, `"Gastroenterology, Internal Medicine"`,
`"Family Medicine, Urgent Care"`) must yield **two** `has_specialty` edges each.
Single-specialty rows yield exactly one. **`Internal Medicine` and `Family Medicine`
each appear in multiple rows ⇒ one shared Specialty node per distinct name.**

### Shared-node check

- `hospital_affiliation` has **4 distinct** values (Rivergate Medical Center, Summit
  Ridge Hospital, Lakeshore General Hospital, Copperfield Clinic) across 15 rows ⇒
  **exactly 4** Organization nodes, reused (not 15).
- `city` has **4 distinct** values (Elmbrook, Fallingwood, Marrowdale, Ashcombe);
  `state` has **2** (OH, MI) ⇒ 4 City nodes + 2 State nodes.

### Expected NEW types minted: **min 6, max 9**

Baseline 6: `Provider` (parent) + `Physician` + `NursePractitioner` +
`PhysicianAssistant` + `Organization` + `City`. Add `State` (7) and `Specialty` (8)
under the standard reading; 9 allows one extra defensible split (e.g. `Hospital` as a
subtype of `Organization`). **< 6 ⇒ roles were flattened** (the core failure).
**> 9 ⇒ over-fragmentation** (e.g. a distinct type per specialty, or per city).

---

## 2. `coffee_shops.json` — places, category multi-value, same-name dedupe trap

15 coffee-shop rows. **Primary entity: a CoffeeShop** (or Place / Cafe), keyed by
`shop_id`.

**Trap:** two independent ones. (a) `categories` is comma-joined and must be
multi-valued. (b) **Same shop name in different cities must stay DISTINCT** — a
too-eager entity-resolver that merges on name alone will wrongly collapse them.

### Field roles

| Field | Role | Target type / datatype | Predicate (intent) | Notes |
|---|---|---|---|---|
| `shop_id` | LITERAL | `string` | — | entity key; stays literal |
| `name` | LITERAL | `string` | — | label; **not** an identity key by itself (see dedupe) |
| `categories` | MULTI-VALUED (NODE *or* literal-multi) | `Category` | `has_category` / `in_category` | comma-joined ⇒ **≥2** per multi-category row |
| `rating` | LITERAL | `float` | — | `xsd:float` |
| `price_level` | LITERAL | `string` | — | enum-ish `$`/`$$`/`$$$`; **stays literal** |
| `address` | LITERAL | `string` | — | street line; **must NOT be split** into nodes |
| `city` | NODE | `City` | `located_in` | embedded; shared across rows |
| `state` | NODE | `State` | `in_state` / `located_in` | shared |
| `outdoor_seating` | LITERAL | `boolean` | — | `xsd:boolean` |

### Dedupe / entity-resolution trap (the headline assertion)

Duplicate **names across different cities** — these must remain **separate**
CoffeeShop entities (distinct URIs):

- **Fernwood Roasters** — `CS-1001` (Portland, OR) **and** `CS-1003` (Seattle, WA)
- **The Copper Kettle** — `CS-1002` (Portland, OR) **and** `CS-1006` (Denver, CO)
- **Marlowe & Bean** — `CS-1004` (Seattle, WA) **and** `CS-1011` (Chicago, IL)
- **Driftwood Coffee Co** — `CS-1005` (Santa Cruz, CA) **and** `CS-1009` (Austin, TX)

Assert: **15 distinct CoffeeShop entities** (one per `shop_id`); none of the four
name-collisions above is merged into a single node. Conversely, the **City** and
**Category** nodes they point at *should* be shared where the value repeats — so the
harness must not "fix" the dedupe trap by refusing to share dimension nodes.

### Multi-value + shared-node checks

- `categories`: split on `, ` (every token `<30` chars, `≥2` parts) ⇒ rows like
  `"Coffee, Bakery, Breakfast"` produce **3** category assertions. `Coffee` recurs in
  nearly every row ⇒ **one** shared `Coffee` category node (if categories are nodes).
- `city` distinct values: **6** (Portland, Seattle, Santa Cruz, Denver, Austin,
  Chicago) ⇒ 6 City nodes. `state` distinct: **6** (OR, WA, CA, CO, TX, IL).

### Expected NEW types minted: **min 2, max 4**

Baseline 2 (categories-as-literal reading): `CoffeeShop` + `City`. Add `State` (3)
and `Category` (4) under the node reading. **< 2 ⇒ `city` was left as an embedded
literal** (geo not decomposed). **> 4 ⇒ over-fragmentation** (e.g. a type per
category value like `CoffeeCategory`/`BakeryCategory`, or a distinct type per shop).
Whether `categories` are `Category` **nodes** or repeated **string literals** is an
accepted modeling choice; either satisfies the multi-value assertion, and the range
spans both.

---

## 3. `llm_models.json` — org reusability + modality multi-value

15 model rows; many share one `organization`. **Primary entity: an LLM/Model**
(`Model` or `LanguageModel`), keyed by `model_id`.

**Trap:** `organization` is the reusability test — 15 rows, only 5 distinct orgs; a
good ingest makes **5 Organization nodes** reused across models, not 15 org strings.
`modality` is the multi-value test.

### Field roles

| Field | Role | Target type / datatype | Predicate (intent) | Notes |
|---|---|---|---|---|
| `model_id` | LITERAL | `string` | — | entity key |
| `display_name` | LITERAL | `string` | — | label |
| `organization` | NODE | `Organization` | `developed_by` / `published_by` | **shared** — the reusability assertion |
| `modality` | MULTI-VALUED (NODE *or* literal-multi) | `Modality` | `supports_modality` | comma-joined ⇒ up to **4** per row |
| `context_length` | LITERAL | `integer` | — | `xsd:integer` |
| `input_price` | LITERAL | `float` | — | `xsd:float` (USD per 1M tokens) |
| `output_price` | LITERAL | `float` | — | `xsd:float` |
| `open_source` | LITERAL | `boolean` | — | `xsd:boolean` |

### Reusability check (the headline assertion)

`organization` has **exactly 5 distinct** values across the 15 rows: **Nimbus AI**
(4 models), **Vantage Labs** (3), **Helios Systems** (3), **Emberform Research** (3),
**Lantern Intelligence** (2). Assert: **exactly 5** Organization nodes, each pointed
at by the correct number of models via a shared predicate — **not** 15 distinct org
nodes, and **not** an `organization` string literal on each Model.

### Multi-value check

`modality` split on `, ` ⇒ `"text, image, audio, video"` (Solstice Pro) yields **4**
modality assertions; `"text"` yields 1. Distinct modality tokens across the file:
`text`, `image`, `audio`, `video` ⇒ **4** shared Modality nodes (if nodes).

### Expected NEW types minted: **min 2, max 3**

Baseline 2: `Model` + `Organization`. Add `Modality` (3) if modalities are nodes.
**< 2 ⇒ `organization` stayed a literal** (reusability lost — the core failure).
**> 3 ⇒ over-fragmentation** (e.g. a type per modality, or a distinct Org type per
company like `NimbusAI`).

---

## 4. `sp500_companies.json` — shared sector/industry + composite HQ split + CEO node

15 company rows. **Primary entity: a Company**, keyed by `ticker`.

**Trap:** `headquarters` is a **composite `"City, State"`** that must **split into two
nodes** (City + State) — the naive flatten keeps it as one string. `sector` and
`industry` are shared reusability dimensions; `ceo` is a Person node.

### Field roles

| Field | Role | Target type / datatype | Predicate (intent) | Notes |
|---|---|---|---|---|
| `ticker` | LITERAL | `string` | — | entity key |
| `company_name` | LITERAL | `string` | — | label |
| `sector` | NODE | `Sector` | `in_sector` | **shared** across companies |
| `industry` | NODE | `Industry` | `in_industry` | **shared**; `Industry in_sector Sector` also acceptable |
| `headquarters` | **COMPOSITE→SPLIT** | `City` + `State` | `headquartered_in` (City), `in_state` (State) | `"Wichita, Kansas"` ⇒ City **and** State node |
| `ceo` | NODE | `Person` | `has_ceo` / `led_by` | one Person per distinct CEO |
| `market_cap` | LITERAL | `integer` | — | `xsd:integer` (whole USD) |
| `employees` | LITERAL | `integer` | — | `xsd:integer` |

### Composite-split check (the headline assertion)

`headquarters` values are `"City, State"` with **full state names**. Each must split:
the City part becomes a `City` node, the State part a `State` node. Assert: **no**
Company carries `headquarters` as a single literal string; every HQ yields both a
City edge and a State edge. Distinct cities: **12** (Wichita, Dearborn, Cambridge,
South San Francisco, Houston, Chicago, Charlotte, Santa Clara, Boise, Battle Creek,
Columbus, Indianapolis — note **Houston** ×2 and **Santa Clara** ×3 recur). Distinct
states: **10** (Kansas, Michigan, Massachusetts, California, Texas, Illinois, North
Carolina, Idaho, Ohio, Indiana — **California** ×4, **Texas** ×2, **Michigan** ×2
recur). Shared where repeated: Houston = 1 City node used by 2 companies; California
= 1 State node used by 4.

> **Note — the resolver's own split heuristic.** `"South San Francisco, California"`
> has a right-hand part (`California`, 10 chars) and left part (`South San Francisco`,
> 19 chars) — both `<30`, so the comma-space relationship-split fires and this is a
> clean 2-node split, not a kept-whole address. All 15 HQ values satisfy the `<30`
> per-part rule, so a correct ingest splits **every** one.

### Reusability check

- `sector`: **9 distinct** (Industrials, Consumer Discretionary, Health Care, Energy,
  Financials, Information Technology, Consumer Staples, Utilities, Real Estate) ⇒ **9**
  Sector nodes reused across companies (Health Care×2, Energy×2, Financials×2,
  Information Technology×3, Consumer Staples×2).
- `industry`: **14 distinct** (Semiconductors recurs — `SLST` and `SLSC`) ⇒ **14**
  Industry nodes.

### Person / dedupe note

`ceo` "Ignatius Vandermeer" appears on **both** `SLST` and `SLSC` (Solstice
Semiconductor + …Materials). Distinct CEO names: **14** (one repeat). A harness may
accept either 14 Person nodes (name-merged) or 15 (per-company) — **do not fail
either way**; this is not the dedupe fixture. **Data caveat:** one row (`PTNC`) has a
deliberately noisy `ceo` value `" Signature withheld"` (leading space, not a real
name) — a robust ingest should still make a Person node or skip it gracefully; do not
assert a specific name for that row.

### Expected NEW types minted: **min 4, max 6**

Baseline 4: `Company` + `Sector` + `Industry` + `Person`. Add `City` (5) and `State`
(6) from the HQ split. **< 6 signals the HQ composite was NOT split** (City/State
missing) — the core failure here; **< 4 additionally means sector/industry/ceo were
flattened.** **> 6 ⇒ over-fragmentation.** Because the HQ split is the headline, a
strict harness may set **min 6** to force City+State; a lenient one accepts min 4 but
must then separately assert `headquarters` is not a literal.

---

## 5. `clinical_trials.json` — RECONCILE don't duplicate (seeded)

15 trial rows. **Primary entity: a ClinicalTrial**, keyed by `nct_id`. This fixture
is **not** primarily about node-vs-literal — it tests that ingest **reuses**
pre-existing ontology types instead of minting near-synonym duplicates.

### Setup (required)

**Before** ingesting, seed the ontology from
[`clinical_trials.seed_ontology.json`](./clinical_trials.seed_ontology.json). It
pre-declares two types with attributes and a few instances:

- **`Condition`** — attrs `icd10_code` (string), `body_system` (string); seed
  instances **Type 2 Diabetes**, **Myocardial Infarction**.
- **`Organization`** — attrs `headquarters_city` (string), `org_kind` (string); seed
  instances **Helix Pharmaceuticals**, **Crestwood Biologics**.

The fixture deliberately uses **field names that differ from the type names**
(`condition` not `Condition`-labeled, `sponsor` not `Organization`) so a correct
reconcile must match on **meaning**, not string identity.

### Field roles

| Field | Role | Target type / datatype | Predicate (intent) | Notes |
|---|---|---|---|---|
| `nct_id` | LITERAL | `string` | — | entity key |
| `title` | LITERAL | `string` | — | label / free text |
| `condition` | NODE → **REUSE `Condition`** | `Condition` | `studies_condition` / `condition` | must attach to the **existing** type |
| `sponsor` | NODE → **REUSE `Organization`** | `Organization` | `sponsored_by` | must attach to the **existing** type |
| `phase` | LITERAL | `string` | — | enum-ish (`Phase 1`…`Phase 4`); stays literal |
| `status` | LITERAL | `string` | — | recruiting status; stays literal |
| `enrollment` | LITERAL | `integer` | — | `xsd:integer` |
| `start_year` | LITERAL | `integer` | — | `xsd:integer` (or `datetime` if coerced to a year) |

### Reconcile check (the headline assertion)

Corresponds to `MatchVerdict.SAME` / `TypeMatch.is_new == False` in
`resolver/type_matcher.py`.

1. **Reuse, don't mint types.** `condition` values attach to the seeded **`Condition`**
   type; `sponsor` values attach to the seeded **`Organization`** type. The ingest
   **must NOT create** any of these near-synonym duplicate **types**:
   `MedicalCondition`, `Disease`, `Diagnosis`, `Indication`, `Sponsor`, `Company`,
   `Institution`, `TrialSponsor` (full list in the seed file's
   `must_not_mint_types`).
2. **Reuse seed instances.** Rows referencing **Type 2 Diabetes** (`NCT04100001/06/14`),
   **Myocardial Infarction** (`…02/10`), **Helix Pharmaceuticals**
   (`…01/03/06/09/15`), **Crestwood Biologics** (`…02/05/08/12`) must point at the
   **same** instance nodes the seed created — **not** duplicate instances.
3. **New instances of existing types are fine.** Values not in the seed —
   conditions **Alzheimer Disease, COPD, Melanoma, Chronic Kidney Disease, Psoriasis**;
   sponsors **Meridian Respiratory Institute, Tidewater Therapeutics** — may be minted
   as **new instances**, but each must be typed as the **existing** `Condition` /
   `Organization`, adding **zero new types**.
4. **Attributes survive.** The seed instances keep their `icd10_code` / `body_system`
   / `org_kind` etc. after ingest (ingest augments, doesn't clobber the schema).

### Cardinality

`condition` distinct values: **7** (Type 2 Diabetes, Myocardial Infarction, Alzheimer
Disease, COPD, Melanoma, Chronic Kidney Disease, Psoriasis) — 2 pre-seeded, **5 new
instances**. `sponsor` distinct values: **4** (Helix Pharmaceuticals, Crestwood
Biologics, Meridian Respiratory Institute, Tidewater Therapeutics) — 2 pre-seeded,
**2 new instances**.

### Expected NEW **types** minted: **min 0, max 1**

The correct answer is **0** — everything reconciles onto `Condition` and
`Organization` (plus `ClinicalTrial`, which the harness may pre-seed too, or count
separately). If the harness does **not** pre-seed `ClinicalTrial`, then **exactly 1**
new type (`ClinicalTrial`) is expected and the max is 1. **≥2 new types ⇒ the
reconcile FAILED** — a `MedicalCondition`/`Sponsor`-style duplicate was minted. This
is the tightest range in the suite: the whole point is that a well-behaved reconcile
adds essentially no new types here despite 7+4 distinct dimension values.

---

## Cross-fixture summary (the assertion cheat-sheet)

| Fixture | Rows | Headline trap | Must-become NODES | SUBTYPES → parent | COMPOSITE split | REUSE | New types [min,max] |
|---|---|---|---|---|---|---|---|
| `healthcare_providers` | 15 | roles→subtypes, not one type | Organization, City, State, Specialty | Physician/NursePractitioner/PhysicianAssistant → **Provider** | — | Rivergate ×5 → 1 org | **[6, 9]** |
| `coffee_shops` | 15 | same name diff city stays distinct | City, State, (Category) | — | — | 15 distinct shops; City/Category shared | **[2, 4]** |
| `llm_models` | 15 | org reusability | Organization, (Modality) | — | — | 5 orgs across 15 models | **[2, 3]** |
| `sp500_companies` | 15 | HQ "City, State" splits | Sector, Industry, City, State, Person | — | `headquarters` → City + State | Sectors/Industries shared | **[4, 6]** |
| `clinical_trials` | 15 | reconcile onto seeded types | Condition, Organization (both pre-seeded) | — | — | reuse `Condition`+`Organization`, no `MedicalCondition`/`Sponsor` | **[0, 1]** |

**Datatype quick-reference** (assert these literals carry the right `xsd` range):
`float` → `rating`, `input_price`, `output_price`; `integer` → `context_length`,
`market_cap`, `employees`, `enrollment`, `start_year`; `boolean` →
`accepting_new_patients`, `outdoor_seating`, `open_source`; everything else (ids,
names, phones, addresses, price_level, phase, status) → `string`.

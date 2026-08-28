# Clinical Trials Blueprint v0

The know-how for a living clinical-trials graph. Not the graph.

Install writes the model, the ClinicalTrials.gov definition, the refresh
rules, the tasks, and the questions that prove it still works. It does
not write trial records. Those appear in *your* workspace after
acquisition runs. First run is **install → acquire → answer**, not
install → answer.

This is the reference implementation, the public-page asset, and the
Sprint 5 demo (INF-566). It is a protocol package in OSS. It is not a
hosted registry and not a Data Feed.

| | |
|---|---|
| **Id** | `infona/clinical-trials` |
| **Version** | `0.1.0` · `acquisition_revision` 1 |
| **License** | Apache-2.0 (know-how). Sample is CC0-1.0. |
| **Sources** | ClinicalTrials.gov v2 (no credential). NPPES NPI Registry (enrichment, no credential). |

## What you get

A domain person should recognise this model. It is not a dump of whatever
`demo-tenant` happens to contain.

- **Types:** `ClinicalTrial`, `Organization`, `MedicalCondition`,
  `Intervention`, `Investigator`, `Facility`.
- **Identity:** NCT on a trial; normalized name on org / condition /
  intervention; investigator name, with NPI decisive when present;
  facility name + country. City, state, and country stay on `Facility`.
  They are not types. ZIP is not modeled.
- **Tasks:** first pull, stale-status refresh, verify one NCT, answer a
  supported question, watch status flips.
- **Freshness:** `overall_status` and dates stale after **14 days**
  (weekly). `enrollment` stale after **45 days** (monthly). NCT and
  official title do not go stale. A disappeared NCT is marked
  `WITHDRAWN`, not deleted.
- **10 supported questions** a medical-affairs or competitive-intel
  person would actually ask (recruiting Phase 3 obesity trials, sponsor
  counts, investigator overlap, upcoming primary completion, US sites,
  status flips).
- **10 evals** with expected answers / still-works rules. Structural
  evals pin the model. Question evals that depend on live
  ClinicalTrials.gov must be re-derived after refresh. Sample-derived
  answers carry `captured_at`.

The continuously-maintained reference workspace (live acquired rows) is
premium/cloud. Those records are not in this package.

## Sample

Optional, separated, synthetic, captured **2026-06-01**, 25 entities,
marked sample on every row. See [`sample/README.md`](sample/README.md).
Never current. Drop the `sample` section and this package still
validates.

## Validate

From an `infona-oss` checkout, with the package on `PYTHONPATH`:

```bash
python -m infona_client.blueprint validate \
  infona_client/blueprint/seeds/clinical-trials
```

Or in Python:

```python
from infona_client.blueprint import validate_blueprint_package
from infona_client.blueprint.seeds import CLINICAL_TRIALS

assert validate_blueprint_package(CLINICAL_TRIALS) == []
```

Empty list means valid against the INF-563 v1 schema.

## Install — not in this package

Export and install are INF-565.
They are not implemented here. There is no `install_blueprint`, no
premium registry, and no path that writes this ontology into a workspace
yet.

When INF-565 lands, install must apply the ontology slice through the
existing schema path and any kept sample through `insert_facts` +
`refresh_after_write`. It must not `eval` author-supplied code. Until
then, this directory is the inspectable artifact: validate it, read it,
do not pretend it activated a graph.

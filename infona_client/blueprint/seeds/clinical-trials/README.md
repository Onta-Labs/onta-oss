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

## Install

```bash
python -m infona_client.blueprint install \
  infona_client/blueprint/seeds/clinical-trials \
  --tenant YOUR_TENANT --kg clinical-trials
```

Same engine as `POST /graphs/{tenant}/blueprints/install` (INF-575).
Re-install of this pin is a no-op. `inspect` shows the lock.
`uninstall` removes the ontology slice, skills, and sample this package
wrote, and leaves the rest of the workspace (INF-577).

Export (workspace → directory) is INF-565. Fork copies this package into
a new identity with `lineage.parent` pointing here (INF-579); it does
not copy instance data and does not clobber this seed. A workspace may
keep this pin and add a private overlay; an upstream update of this
package must not clobber that overlay (INF-578).

## First run (INF-593)

Install does not acquire live data. After install:

```bash
python -m infona_client.blueprint first-run infona/clinical-trials \
  --tenant YOUR_TENANT
```

ClinicalTrials.gov is `credential: none`, so first-run starts
`acquire_condition_set` without a key. A `byok` source fails closed
until the workspace supplies `KEY_ENV=value`. First-run answers the
package's first supported question (Phase 3 obesity recruiting) on
this tenant graph; `--question` only overrides the echoed prompt.
Sample rows, if still present, are labelled and `sample_is_current`
is always false.

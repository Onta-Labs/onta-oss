# Blueprint manifest — v1-frozen

**Tickets:** [INF-563](https://linear.app/infona/issue/INF-563), [INF-587](https://linear.app/infona/issue/INF-587).
**Contract:** parent `docs/blueprints/INF-559-package-contract-v0.md`.
**Schema version:** `v1-frozen`.

A Blueprint is the *means* to acquire and maintain a domain. It is not the
graph. Install writes the model, the source bindings, the tasks, the
policies, the skills, and the evals. Records appear in *your* workspace
after you connect what the sources need and acquisition runs (INF-564).

This package is the protocol: schema, validator, fixtures (ADR 0014).
Canonical package = a directory of UTF-8 plain text with `blueprint.yaml`
at the root. A zip/tar is an envelope, not the format. No author-supplied
code. The hosted registry, private extensions, entitlement, and paid
source bindings are premium.

## Validate

```bash
# in the OSS checkout root
PYTHONPATH=. python -m infona_client.blueprints \
  infona_client/blueprints/fixtures/clinical-trials

PYTHONPATH=. python -m pytest tests/test_blueprint_manifest_v1.py
```

Or in Python:

```python
from infona_client.blueprints import validate_manifest, validate_sample

manifest = validate_manifest("path/to/blueprint.json")
if manifest.sample:
    validate_sample(manifest.sample)  # independently droppable
```

Unknown top-level keys are rejected. `latest` is not a legal `version`.

## Sections

| Key | Role |
|---|---|
| `concepts`, `relationships` | Domain model. Every attribute declares `kind: literal \| type_ranged` so install can pick `attrs/<leaf>` vs `onto/<leaf>` (INF-576). A type-ranged attribute must have a matching `relationships[]` row with cardinality. |
| `tasks`, `rules` | What the agent is expected to *do*. Tasks are named jobs. Rules are conflict and tombstone *policy*. Not cron rows. |
| `sources` | Definitions and field mappings. `credential` is `none` or `byok` (an env-var *name* in `key_env`). Never a secret. |
| `acquisition` | How to go get the data. Changing a step bumps `acquisition_revision`, not a silent MINOR. |
| `validation`, `freshness` | Policies. Entity-resolution config lives under `validation.entity_resolution`. Freshness is stale-after windows, not last-refresh status. |
| `skills` | Type-attached prose, *named* computed functions, expected MCP tools. No implementations (no SPARQL, no lambda body). |
| `examples`, `evals` | Supported questions and regression evals. `leak_policy` must be `workspace_only` (INF-567 platform guard is later). |
| `sample` | Optional, own section. INF-587 in full. |
| metadata | `schema_version`, `id`, `namespace`, `name`, `version` (semver), `acquisition_revision`, `license`, `attribution`, `published_at`, `last_reviewed_at`, `lineage`. |

## Unrepresentable (no field at all)

These live in the workspace. The schema has no property for them — not
optional, not nullable. The validator rejects them as unknown top-level
keys. A test walks the exported JSON Schema and asserts each name is
absent.

- actual records (outside `sample`)
- credentials / secret values / platform keys
- scheduled jobs (cron, last-run, next-run)
- citations and per-cell provenance
- freshness *status* (last refresh, source health, is-current)

Source-definition URLs (`url`, `docs_url`) stay. They name the binding.

## Semver

| Change | Signal |
|---|---|
| Remove a concept; rename an identity key; narrow a range; flip literal ↔ type-ranged; make an optional attribute required; remove a source | **MAJOR** (`version`) |
| Add an optional attribute or concept; add a source, skill, function, or question; change an ER threshold | **MINOR** (`version`) |
| Wording, eval fixtures, sample refresh | **PATCH** (`version`) |
| Change an acquisition instruction or a freshness window | **`acquisition_revision`** — not a silent MINOR |

`classify_change(old, new)` in `versioning.py` is the mechanical form.

## Sample policy (INF-587)

A sample must be:

- **Separated** — own top-level `sample` key. `validate_sample` works
  without the rest of the package. Drop the key and the Blueprint still
  validates.
- **Synthetic or openly licensed** — `origin` is `synthetic` or `open`;
  the sample has its own `license`. No scraped or proprietary records.
- **Timestamped** — `captured_at` is a date field, not README prose.
- **Size-limited** — hard cap **25 entities** or **64 KiB** serialized,
  whichever first. 25 is the INF-559 interview number: enough for a map
  and two labeled questions, not enough to skip acquisition. 64 KiB stops
  a dump hiding behind a passing count.
- **Never current** — `kind` must be `"sample"`; every entity has
  `is_sample: true`. `surface_label(captured_at)` returns
  `sample, captured <ISO-date>`. `feeds_freshness_panel()` is always
  false.

### Read-side surfaces (stub)

Explorer and the public Blueprint page do not yet render installed sample
rows (INF-558 is a fake-door catalog; INF-571 is the freshness panel).
When those surfaces land they must:

1. Call `infona_client.blueprints.surface_label` for every sample-derived
   row or answer. Do not invent a second caption.
2. Never pass sample rows into the maintenance/verification freshness
   panel (`feeds_freshness_panel()` is the gate).

Do not build that UI in this package.

## BYOK

A keyed source binding reachable from this OSS schema is
bring-your-own-key: `credential: byok` plus `key_env: SOME_ENV_NAME`.
The package never ships or implies a shared platform key. Guarded in
general by `tests/test_api_registry_byok_guard.py` and ADR 0011.

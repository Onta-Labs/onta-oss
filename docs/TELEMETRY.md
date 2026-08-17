# Anonymous job telemetry (ONTA-548)

Infona can send **anonymous job telemetry** so we can see which jobs people
run and the source-type mix — not who they are.

**Disabled by default. Opt-in only.**

## Turn it on

```bash
export INFONA_TELEMETRY=1
```

Or answer **yes** at the first-run CLI prompt (`infona` / `infona init` on a
TTY). That writes `~/.infona/telemetry.json` with `"opt_in": true`.

A self-declared coarse use-case is optional and never required:

```bash
export INFONA_TELEMETRY_USE_CASE=research   # research | ops | product | other
```

## Turn it off (always wins)

```bash
export INFONA_TELEMETRY=0
```

`INFONA_TELEMETRY=0` wins over a previous CLI yes and over
`INFONA_TELEMETRY=1` being absent from a consent file. Unset the var and
delete `~/.infona/telemetry.json` to return to the factory default (off).

## What leaves your machine

**Only when enabled.** One JSON object per job, allowlisted fields only:

| Field | Meaning |
|---|---|
| `event` | Always `job` |
| `install_id` | Random UUID minted on this machine. **Not** a workspace, user, or tenant id |
| `job_type` | `ingest` · `ask` · `er rebuild` · `export` |
| `row_count_bucket` | `0` · `1-10` · `11-100` · `101-1000` · `1001-10000` · `10000+` — never the exact count |
| `source_type` | `csv` · `json` · `jsonl` · `text` · `http` · `unknown` — never a filename |
| `error_class` | Exception type (`ValueError`) or HTTP family (`http_4xx`) — never the message |
| `use_case` | Optional; only if you set `INFONA_TELEMETRY_USE_CASE` to the coarse enum |

## What never leaves your machine

- Data values, column names, file names, paths
- Graph content, Cypher, SPARQL, prompts, answers
- Workspace / tenant ids, emails, API keys, user ids
- Exact row counts

The first-run CLI prompt states this in the same words.

## Destination

No hosted collector is bundled. Set an operator-owned HTTPS endpoint:

```bash
export INFONA_TELEMETRY_URL=https://example.invalid/v1/job
```

The client POSTs the JSON object with `Content-Type: application/json`, a
**2 second** timeout, on a daemon thread. Failures are swallowed — telemetry
never fails a user job.

There is **no default URL** and **no shipped platform key**. A PostHog
capture URL plus a *public write-only project key you own* is fine (BYOK).
Do not put a private platform secret in this repo.

For local inspection (tests / air-gapped):

```bash
export INFONA_TELEMETRY=1
export INFONA_TELEMETRY_SINK=stderr          # or file
export INFONA_TELEMETRY_FILE=~/.infona/telemetry.jsonl
```

`INFONA_TELEMETRY_STATE` overrides the consent-file path (tests).

## Where it is hooked

Python job entrypoints (one call each):

- ingest — `infona_client/resolver/file_ingest_job.py` (`finish` / `fail`)
- ask — `infona_client/api/routes/ask.py`
- export — `infona_client/api/routes/export.py`
- er rebuild — `infona_client/resolver/er/rebuild.py`

The TypeScript CLI (`packages/cli/src/telemetry.ts`) only asks for consent.
It does not send events; the server does.

This package is **not** `infona_client/analytics/` (product/app events) and
**not** `infona_client/usage/` (per-tenant metering).

## Internal query — installs, job mix, vertical mix

Events are one JSON object per job. Load them however you collect
(file sink, your POST endpoint, PostHog after mapping properties).

```sql
-- Conceptual query over the event stream.

-- Installs (opted-in installs that ran at least one job)
SELECT COUNT(DISTINCT install_id) AS installs
FROM telemetry_events
WHERE event = 'job';

-- Job mix
SELECT job_type, COUNT(*) AS jobs
FROM telemetry_events
WHERE event = 'job'
GROUP BY job_type
ORDER BY jobs DESC;

-- Vertical mix
--   vertical = source_type (csv / json / …)
--   plus optional self-declared use_case (research / ops / product / other)
--   never a person, email, tenant, or filename
SELECT
  source_type,
  COALESCE(use_case, 'undeclared') AS use_case,
  COUNT(*) AS jobs,
  COUNT(DISTINCT install_id) AS installs
FROM telemetry_events
WHERE event = 'job'
GROUP BY 1, 2
ORDER BY jobs DESC;
```

HogQL (if you point `INFONA_TELEMETRY_URL` at a PostHog capture URL and
map the body onto `event` + `properties`):

```sql
SELECT
  count(distinct properties.install_id) AS installs,
  properties.job_type,
  properties.source_type,
  coalesce(properties.use_case, 'undeclared') AS use_case,
  count() AS jobs
FROM events
WHERE event = 'job'
GROUP BY properties.job_type, properties.source_type, use_case
```

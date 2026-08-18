# Quickstart timing (ONTA-549)

The README heading is **10-minute quickstart**. This page is the measured
split, not a target. Re-run with `./scripts/time_quickstart.sh`.

## Verdict

**Keep the 10-minute claim.** One cold-path run on a real machine landed in
**1 min 42 s** clone → FLAURA2. That is well under 10 minutes with headroom
for a first-time `neo4j:5-community` pull and a slower link. Do not tighten
the heading to "1 minute." Do not start a GHCR prebuild to chase it.

Native **Linux was not measured**. Do not invent that number.

## Environment (one machine)

| | |
|---|---|
| When | 2026-08-17 (PDT) / 2026-08-18 UTC |
| Host | macOS 26.5.1 (25F80), Darwin 25.5.0 arm64 |
| Docker | Colima, context `colima`, engine 29.5.2, Compose 5.4.0 |
| VM | Ubuntu 24.04.4 LTS, 4 CPU, 6 GB RAM |
| Node / npm | v24.16.0 / 11.13.0 |
| Label | **warm daemon, empty project** |

Not done (destructive on this shared daemon): `docker system prune -a`,
`docker builder prune`. The advertised cold image path is
`docker compose build --no-cache`. Builder cache was already **0 B**.
`python:3.12-slim` was **absent** and pulled during the API build.
`neo4j:5-community` was **already present** (12 days old, 360 MB content /
991 MB). A first-time Neo4j pull is **not** in the table.

Default host ports 8000 / 7474 / 7687 were in use by other long-running
processes. Boot used **18000 / 17474 / 17687** via a throwaway Compose
`!override` (same images and healthchecks; not a compose-file change).

Docker Desktop / Colima / Node install time is **not** in the table.
Those are the README "Need:" line.

## Split

Zero-key path after ONTA-544: no OpenRouter account. First result is
cached-plan replay of FLAURA2, **not** live inference.

| phase | seconds | clock | notes |
|---|---:|---|---|
| prerequisites.clone | 20.3 | 20.3s | full `git clone https://github.com/infona-ai/infona-oss.git` (~58 MB) |
| prerequisites.npm | 1.0 | 1.0s | `npm i -g @infona-ai/cli` into an isolated prefix + empty npm cache |
| build.no-cache | 54.3 | 54.3s | `docker compose build --no-cache api` (includes `python:3.12-slim` pull + `pip install -e .`) |
| boot.healthy | 7.9 | 7.9s | compose up, wait until `GET /health` is `status=healthy` and `neo4j=true`. Repeated 7.9s on a second boot |
| oss_setup npm ci + CLI build | 11.5 | 11.5s | `npm ci --ignore-scripts` 10.3s + `npm run build -w packages/cli` 1.2s (isolated npm cache; part of `oss_up.sh` when `node_modules` is missing) |
| first_result | 6.7 | 6.7s | `load_prebuilt_trials.sh` + `infona ask "Which Phase 3 NSCLC trials is AstraZeneca running?" --kg trials` |
| **sum (clone → FLAURA2)** | **101.7** | **1m 42s** | |
| rebuild.warm | 0.4 | 0.4s | `docker compose build api` with layers cached. Cheap |

`infona ask` printed:

```
A: FLAURA2

(cached-plan replay — not live inference)
```

Entity-resolution rebuild was **not** timed. It is not on the zero-key
first-result path.

## How this was timed

`./scripts/time_quickstart.sh` records clone, isolated `npm i -g`,
`docker compose build --no-cache`, boot, and the zero-key ask. It does
not stop other compose projects, does not prune the daemon, and isolates
`HOME` for the ask so a leftover `~/.infona/config.json` cannot steal
the kg.

`oss_setup` npm ci / CLI build and the full (non-shallow) clone were
timed in the same session on the same machine and folded into the table
above.

## Re-run

```bash
./scripts/time_quickstart.sh
```

Skip phases already paid for:

```bash
INFONA_QS_SKIP_CLONE=1 INFONA_QS_SKIP_NPM=1 INFONA_QS_SKIP_BUILD=1 \
  ./scripts/time_quickstart.sh
```

README-ready claim: [`docs/_fragments/ONTA-549.md`](_fragments/ONTA-549.md).
Zero-key commands: [`docs/_fragments/ONTA-544.md`](_fragments/ONTA-544.md).

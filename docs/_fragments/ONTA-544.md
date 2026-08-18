# Zero-key first run (ONTA-544)

A stranger should see **FLAURA2** before they create an API key.

The prebuilt path **replays a cached Cypher plan**. It is **not live inference**.
`/ask` stays always-LLM Cypher whenever a real model key (or `INFONA_LLM_BASE_URL`)
is configured.

## Zero-key (lead with this)

```bash
git clone https://github.com/infona-ai/infona-oss.git && cd infona-oss
cp .env.example .env          # leave OPENROUTER_API_KEY empty / as the placeholder
npm i -g @infona-ai/cli       # or use npx @infona-ai/cli in place of infona
./scripts/oss_up.sh           # Neo4j + API + loads the prebuilt trials graph
infona ask "Which Phase 3 NSCLC trials is AstraZeneca running?" --kg trials
```

That question should return **FLAURA2**, labelled as a **cached-plan replay**
(not live inference). `./scripts/oss_up.sh` compose-ups, waits until `/health`
reports Neo4j up, writes `~/.infona/config.json`, and runs
`./scripts/load_prebuilt_trials.sh`.

Reload the snapshot later (still no key):

```bash
./scripts/load_prebuilt_trials.sh
infona ask "Which Phase 3 NSCLC trials is AstraZeneca running?" --kg trials
```

Need: Docker + Node 20+ (for the `infona` CLI). **No OpenRouter account.**

## Bring a key to ingest *your own* data

Schema inference and live `/ask` call an LLM. Paste `OPENROUTER_API_KEY` into
`.env` and re-run compose (or export it) when you want to ingest a CSV that is
not the shipped snapshot:

```bash
# .env: OPENROUTER_API_KEY=sk-or-...
infona ingest examples/trials.csv --kg my-data
infona ask "Which Phase 3 NSCLC trials is AstraZeneca running?" --kg my-data
```

Live `/ask` is always-LLM Cypher. The cached plan is not consulted when a real
key is present.

## What shipped

| Path | Role |
|---|---|
| `examples/prebuilt/trials_snapshot.json` | Frozen ingest-shaped facts from `examples/trials.csv` |
| `examples/prebuilt/ask_plan_flaura2.json` | Stored Cypher + template steps for the hero question |
| `scripts/load_prebuilt_trials.sh` | Loads the snapshot into Neo4j (no LLM) |
| `infona_client/nlp/ask_cached_plan.py` | Replay hook (no-key / `INFONA_ASK_CACHED_PLAN=1` only) |

`INFONA_ASK_CACHED_PLAN=1` forces replay even with a key (tests). `=0` disables
it. Placeholder keys from `.env.example` (`sk-or-...`) count as **no key**.

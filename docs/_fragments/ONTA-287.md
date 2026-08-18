# ONTA-287 — quickstart smoke

Every README claim about the 10-minute quickstart — `./scripts/oss_up.sh` until
`/health` reports `neo4j: true` and `status: healthy`, `infona ask "Which Phase
3 NSCLC trials is AstraZeneca running?" --kg trials` returning **FLAURA2**, and
the ONTA-543 `er rebuild` merge plus unresolved `credit_rating` — is guarded by
`.github/workflows/oss-quickstart-smoke.yml`. A mocked / zero-key job runs on
every PR (no key, no spend; cached-plan path after ONTA-544). A live-key
ingest+ask job runs only on push-to-main and `workflow_dispatch` against a
dedicated `OPENROUTER_API_KEY` secret, never on fork PRs.

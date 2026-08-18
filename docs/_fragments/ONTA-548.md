## Telemetry (opt-in)

Infona does **not** phone home unless you turn it on.

```bash
export INFONA_TELEMETRY=1          # opt in
export INFONA_TELEMETRY=0          # force off (wins over a previous yes)
```

The first-run CLI prompt (`infona` / `infona init` on a TTY) asks the same
question in plain language and writes `~/.infona/telemetry.json`. There is
no opt-out default.

### What leaves your machine

**Only when enabled**, one anonymous JSON object per job:

- job type (`ingest` / `ask` / `er rebuild` / `export`)
- a **row-count bucket** (not the exact count)
- source type (`csv` / `json` / `jsonl` / `text` / `http` — never a filename)
- error class (exception type or HTTP family — never the message)

A random `install_id` (UUID) identifies the install, not you.

### What never leaves your machine

Your data, column names, file names, graph content, workspace / tenant ids,
prompts, answers, Cypher, SPARQL, emails, API keys.

Default collector (only when enabled) is the public Infona-oss PostHog
project. Override with `INFONA_TELEMETRY_URL`, or set it to `off`. Local
inspection: `INFONA_TELEMETRY_SINK=stderr` / `file`.

Full contract + the internal installs / job-mix / vertical-mix query:
[docs/TELEMETRY.md](../TELEMETRY.md).

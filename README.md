<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/brand/infona-lockup-reverse-tight.png">
    <img src="docs/brand/infona-lockup-ink-tight.png" alt="Infona" width="240">
  </picture>
</p>

<p align="center">
  <strong>Messy CSV, JSON, or text → one LLM schema pass → deterministic rows into Neo4j → ask in English (Cypher).</strong>
</p>

<p align="center">
  Luna (or your configured model) sees the file once and names types, attributes, relationships.
  Every cell maps through that schema via <code>insert_facts</code>. Then
  <code>ask</code> compiles English to Cypher on the populated graph.
</p>

<p align="center">
  <a href="https://infona.ai">infona.ai</a> (waitlist / demo) ·
  <a href="docs/BOUNDARY.md">what's free</a> ·
  <a href="docs/API.md">API</a>
</p>

<p align="center">
  <a href="docs/API.md"><img src="https://img.shields.io/badge/docs-API-brightgreen" alt="docs"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-green" alt="Apache-2.0"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.12%2B-blue" alt="Python 3.12+"></a>
  <a href="docs/neo4j-local.md"><img src="https://img.shields.io/badge/Neo4j-Cypher-008CC1?logo=neo4j&logoColor=white" alt="Neo4j"></a>
  <a href="https://www.npmjs.com/package/@infona-ai/mcp"><img src="https://img.shields.io/npm/v/@infona-ai/mcp?label=mcp" alt="npm @infona-ai/mcp"></a>
  <a href="https://github.com/infona-ai/infona-oss/actions/workflows/test.yml"><img src="https://img.shields.io/github/actions/workflow/status/infona-ai/infona-oss/test.yml?branch=main&label=tests" alt="tests"></a>
</p>

<p align="center">
  <img src="docs/readme/hero.png" alt="English question compiled to Cypher on Neo4j: AstraZeneca runs FLAURA2, indication NSCLC."/>
</p>

<p align="center"><em>Ask in English, get Cypher, get the row. <code>ingest</code> → <code>ask</code> is the payoff. The graph is the trials.csv sample.</em></p>

---

## 10-minute quickstart

Need: Docker + Node 20+ (for the `infona` CLI). A stranger gets a real answer with **no API key**.

### Zero-key (cached-plan replay)

The prebuilt path **replays a cached Cypher plan**. It is **not live inference**.
`/ask` stays always-LLM Cypher whenever a real model key (or `INFONA_LLM_BASE_URL`) is configured.

```bash
git clone https://github.com/infona-ai/infona-oss.git && cd infona-oss
cp .env.example .env          # leave OPENROUTER_API_KEY empty / as the placeholder
npm i -g @infona-ai/cli       # or use npx @infona-ai/cli in place of infona
./scripts/oss_up.sh           # Neo4j + API + loads the prebuilt trials graph
infona ask "Which Phase 3 NSCLC trials is AstraZeneca running?" --kg trials
```

That question should return **FLAURA2**, labelled as a **cached-plan replay** (not live inference).
`./scripts/oss_up.sh` compose-ups, waits until `/health` reports Neo4j up, writes `~/.infona/config.json`, and runs `./scripts/load_prebuilt_trials.sh`.

Reload the snapshot later (still no key):

```bash
./scripts/load_prebuilt_trials.sh
infona ask "Which Phase 3 NSCLC trials is AstraZeneca running?" --kg trials
```

Advertised bound stays **10 minutes**. Measured **1 min 42 s** cold on **macOS 26.5.1 + Colima** (Ubuntu 24.04 VM, 4 CPU, 6 GB; warm daemon, empty project, `docker compose build --no-cache`) from `git clone` to that zero-key ask. Native Linux was **not** measured. First-time `neo4j:5-community` pull is extra; 10 minutes still covers it.

Placeholder keys from `.env.example` (`sk-or-...`) count as **no key**.
`INFONA_ASK_CACHED_PLAN=1` forces replay even with a key (tests). `=0` disables it.

### 1. Messy suppliers — URI merge

`examples/suppliers-messy.csv` is **synthetic** (Acme / Globex / Initech, fake tax IDs). No real customer data.

Schema inference needs a key (paste `OPENROUTER_API_KEY=sk-or-...` into `.env`):

```bash
infona ingest examples/suppliers-messy.csv --kg suppliers
infona er rebuild --kg suppliers
```

Ingest writes every row as its own Supplier fragment. `er rebuild` re-blocks the already-ingested graph and **collapses fragment URIs** (6→3). Headquarters and credit_rating then land as graph state: **Austin is the current HQ** (San Francisco stays stored, closed); equal-trust `credit_rating` stays dual-current and flagged.

```
Rebuilding entity resolution for suppliers…
  Supplier         6 → 3  (−3 fragments across 2 clusters)

  merge  https://graph.infona.ai/entities/Supplier/ERP-1001
         losers:     https://graph.infona.ai/entities/Supplier/CRM-4402, https://graph.infona.ai/entities/Supplier/DIR-8891
         reason:     signal-richest
         score:      1.00
         provenance: erp @ 2026-03-01T12:00:00+00:00 (source_of_truth)

  merge  https://graph.infona.ai/entities/Supplier/ERP-2001
         losers:     https://graph.infona.ai/entities/Supplier/CRM-5503
         reason:     signal-richest
         score:      1.00
         provenance: erp @ 2026-03-01T12:00:00+00:00 (source_of_truth)

  conflict  headquarters
         entity:     https://graph.infona.ai/entities/Supplier/ERP-1001
         winner:     Austin  (erp, source_of_truth, 2026-03-01T12:00:00+00:00)
         loser:      San Francisco  (directory, supplementary, 2024-06-01T00:00:00+00:00)
         reason:     authority

  unresolved  credit_rating
         entity:     https://graph.infona.ai/entities/Supplier/ERP-1001
         crm: BBB @ 2026-03-01T12:00:00+00:00 (source_of_truth)
         erp: A @ 2026-03-01T12:00:00+00:00 (source_of_truth)
         flagged: equal-trust sources — not silently guessed

Done. 3 fragments absorbed.
```

- **merge** — three Acme name variants (and two Globex) became one entity each. The surviving URI is the signal-richest fragment; its `provenance` is the source row that won (erp, timestamp, authority). That URI collapse **is** applied to the graph.
- **conflict / headquarters** — the report names Austin as the authority-axis winner (ERP `source_of_truth` over a stale directory scrape). Austin is current; San Francisco stays stored and closed.
- **unresolved / credit_rating** — ERP says `A`, CRM says `BBB`. Same authority, same timestamp. The report **flags** the pair instead of silently picking; both stay dual-current.

Fixture notes: [`examples/suppliers-messy.md`](examples/suppliers-messy.md).
Hermetic proof: `tests/test_suppliers_messy_fixture.py`.

### 2. ingest → ask — the payoff

With a key, live `/ask` is always-LLM Cypher. The cached plan is not consulted when a real key is present.

```bash
infona ingest examples/trials.csv --kg my-data
infona ask "Which Phase 3 NSCLC trials is AstraZeneca running?" --kg my-data
```

That question should return **FLAURA2**. `examples/trials.csv` is a 16-row oncology sample (8 sponsors, 11 drugs, 7 indications) — public program names, synthetic `TRIAL-*` IDs, no patient data.

![infona ingest inferring a schema from trials.csv, then writing Trial, Sponsor, Drug, Indication nodes into the graph](docs/readme/demo-ingest.svg)

![infona ask compiling English to Cypher and lighting three sponsor paths — FLAURA2, MARIPOSA, CROWN — into NSCLC](docs/readme/demo-ask.svg)

The looping SVGs are generated from [`scripts/render_readme_demos.py`](scripts/render_readme_demos.py). Local Neo4j notes: [docs/neo4j-local.md](docs/neo4j-local.md). `infona init --local` connects without starting Docker. If something fails, the CLI should name the next command.

Python package (library, not the `infona` CLI — that is `@infona-ai/cli`). Same version as the npm packages:

```bash
pip install infona-client
```

Import path is `infona_client`. Graph IRIs live under `https://graph.infona.ai/`.

---

### Optional: 3rd-party REST / SQL extract (dlt)

`pip install infona-client` does **not** pull [dlt](https://dlthub.com). Install the extra on the **backend** only (`pip install 'infona-client[dlt]'`). Infona is the destination — there is no dlt warehouse sink. The CLI reads `env:VAR` locally and sends an inline token; the API never reads the server process environment.

```bash
# frozen body for POST /graphs/{tenant}/ingest/dlt
cat > spec.json <<'EOF'
{
  "source": {
    "kind": "rest_api",
    "base_url": "https://api.example.com",
    "auth": {"type": "bearer", "secret_ref": "env:EXAMPLE_TOKEN"},
    "resources": ["v1/contacts"]
  },
  "map": {"v1/contacts": {"type": "Contact", "id_field": "id"}},
  "kg": "crm"
}
EOF
infona ingest --dlt spec.json --kg crm
```

SQL is the same shape with `"kind": "sql"` and `"dsn": "env:EXAMPLE_DSN"`. Hosted Explorer Connect / Run is premium (ONTA-554) and hits this same route.

---

## Eval

Published pin: **6 / 8** (75%) on 16 synthetic oncology rows — two misses stay visible. That is the pin, not a footnote. The homepage loop is ingest → **FLAURA2**. Full table, misses, and repro: [docs/EVAL.md](docs/EVAL.md). Backing JSON: [docs/eval/public_results.json](docs/eval/public_results.json). Eval is Python-only; there is no `infona eval` CLI.

---

## What leaves your machine

Infona does **not** phone home unless you turn it on. Default **off**.

```bash
export INFONA_TELEMETRY=1          # opt in
export INFONA_TELEMETRY=0          # force off (wins over a previous yes)
```

The first-run CLI prompt (`infona` / `infona init` on a TTY) asks the same question and writes `~/.infona/telemetry.json`. There is no opt-out default.

**Only when enabled**, one anonymous JSON object per job:

- job type (`ingest` / `ask` / `er rebuild` / `export`)
- a **row-count bucket** (not the exact count)
- source type (`csv` / `json` / `jsonl` / `text` / `http` — never a filename)
- error class (exception type or HTTP family — never the message)

A random `install_id` (UUID) identifies the install, not you.

**Never leaves:** your data, column names, file names, graph content, workspace / tenant ids, prompts, answers, Cypher, emails, API keys.

When enabled, the default collector is the public Infona-oss PostHog project (write-only project token). Override with `INFONA_TELEMETRY_URL`, set it to `off`, or use `INFONA_TELEMETRY_SINK=stderr` / `file` locally.

Full contract: [docs/TELEMETRY.md](docs/TELEMETRY.md).

---

## What this is not

Infona is **not** a memory or context layer that stuffs retrieved chunks into a prompt window. This repo registers **no** default open-web page fetcher; you bring retrieval or you skip web fetch ([docs/BOUNDARY.md](docs/BOUNDARY.md)). It is **not** RAG over a vector index, and it is **not** "chat with your CSV."

---

## What you get

| | |
|---|---|
| **Entity resolution** | `infona er rebuild` collapses fragment URIs (6→3 on the suppliers fixture). Winner URI, reason, score, provenance timestamp. Authority-axis winners become the current graph value (Austin HQ; SF stored/closed). Equal-trust `credit_rating` stays dual-current and flagged. |
| **Provenance** | Source + timestamp + authority on the winning fact in the report. Answers carry per-fact citations (`tests/test_answer_citations.py`). |
| **Schema from one pass** | Luna (or your configured model) sees the file once. Types, attributes, relationships. No per-row LLM. |
| **Deterministic rows** | Every cell maps through that schema via `insert_facts`. |
| **A real graph** | Neo4j. Sponsors, trials, drugs, indications are nodes. |
| **Ask** | Always-LLM Cypher when a key is present. Cached-plan replay when it is not. Fail-closed when the plan is a silent wrong total. |
| **CLI + MCP + HTTP** | Same canonical routes. `infona`, `@infona-ai/mcp`, `POST /graphs/{tenant}/ask`. |
| **Export** | JSON or CSV back out. The graph is yours. |

```
CSV / JSON / text
  → schema inference (1 LLM call; skipped for the prebuilt snapshot)
  → deterministic row mapping
  → Neo4j knowledge graph (GraphStore / Cypher)
  → er rebuild (URI collapse; field winners applied; equal-trust flagged)
  → ask (cached-plan replay with no key; always-LLM Cypher with a key)
```

Writes go through `insert_facts` / `refresh_after_write`. Instance relationships use `https://graph.infona.ai/onto/<leaf>`. Ask is always-LLM Cypher when a model is configured. Grounding, probes, and few-shots **inform** the model; they do not replace it.

```bash
export OPENROUTER_API_KEY=sk-or-...
export INFONA_QUERY_PROVIDER=openrouter
export INFONA_QUERY_MODEL=openai/gpt-oss-120b
```

### MCP (agents)

Same `ask`, same graph, same exact rows — as a tool result:

```json
{
  "mcpServers": {
    "infona": {
      "command": "npx",
      "args": ["-y", "-p", "@infona-ai/mcp", "infona-mcp"],
      "env": {
        "INFONA_API_URL": "http://localhost:8000",
        "INFONA_TENANT": "default"
      }
    }
  }
}
```

`ask`, `search`, `agent`, `ingest_csv`, `ingest_dlt`, `export_kg`, ontology, jobs — same backend the CLI hits. [packages/mcp/README.md](packages/mcp/README.md).

### What's free

- **OSS (this repo):** ingest, ontology, ask, MCP / CLI / HTTP, export, free sources, BYOK registry, plugin seams.
- **Bring your own retrieval:** OSS registers **no** open-web page fetcher. Enrichment that needs a URL fetch declines unless *you* register one — or you use hosted Infona.
- **Hosted-only:** managed keys Infona bills, paid search/scrape ladders, curated Enhanced ontology, Explorer, billing.

Full table: **[docs/BOUNDARY.md](docs/BOUNDARY.md)**.

**Product path:** FastAPI + **Neo4j GraphStore (Cypher)**. SPARQL / Neptune are not product backends.

---

## License and contributing

Apache 2.0 — [LICENSE](LICENSE), [NOTICE](NOTICE).

Shipped packages share one lockstep version: [`infona-client`](https://pypi.org/project/infona-client/) on PyPI and [`@infona-ai/cli`](https://www.npmjs.com/package/@infona-ai/cli) / [`@infona-ai/mcp`](https://www.npmjs.com/package/@infona-ai/mcp) on npm. Release notes: [CHANGELOG.md](CHANGELOG.md).

[docs/API.md](docs/API.md) · [docs/BOUNDARY.md](docs/BOUNDARY.md) · [ROADMAP.md](ROADMAP.md) · [SECURITY.md](SECURITY.md) · [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) · [CHANGELOG.md](CHANGELOG.md) · [CONTRIBUTING.md](CONTRIBUTING.md) · [CLA.md](CLA.md) · [AGENTS.md](AGENTS.md)

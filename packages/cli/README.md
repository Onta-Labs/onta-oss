# @infona-ai/cli

Node.js SDK and CLI for [Infona](https://infona.ai) — turn raw data into a queryable context graph (a knowledge graph you query in natural language).

Package: **`@infona-ai/cli`**. Primary binary: **`infona`**. Compatibility alias: **`onta`** (same entrypoint). Env vars: **`INFONA_*` only** (no `ONTA_*` fallback).

## Quickstart

```bash
npx -y -p @infona-ai/cli infona
# or: npm install -g @infona-ai/cli && infona
```

**First run with no config** opens an interactive **connect wizard** (not a silent
cloud login and not a silent force-local):

```text
  Connect Infona
    1) Local open-access  (http://localhost:8000, tenant default)
    2) Browser sign-in    (hosted Infona cloud)
    3) API key            (paste key + optional URL)
```

- **Local** probes the API, writes `~/.infona/config.json`, and **never** opens a browser for open-access.
- **Browser** is for hosted cloud only.
- **API key** is for CI/headless or self-hosted with auth.

Re-run any time: `infona init` (confirms before overwriting saved credentials).
Non-interactive local: `infona init --local` (probes `localhost:8000`, writes
open-access config, **no readline** — safe for scripts). Re-writing the same
local open-access config is idempotent; overwriting a *different* saved
connection (e.g. cloud API key) requires a TTY confirm, or `--force` when
non-interactive: `infona init --local --force`.

Then the interactive shell:

```text
  /ingest <file>      Ingest a CSV/JSON/text file
  /ask <question>     Ask in natural language
  /kg list|switch|create|delete <name>
  /types [query]      Types in the active KG, with entity counts
  /type <name>        Drill into one type — attributes & relationships
  /enrich <Type> <attrs...>   Plan + run an enrichment job (interactive)
  /enrich watch <job_id>      Live progress for a running job
  /enrich jobs                List recent enrichment jobs
  /enrich review <job_id>     Walk through conflicts and accept/reject
  /status             Graph stats
  /login              Re-authenticate
  /quit
```

Bare lines (no leading `/`) auto-route to `/ask`. Full walkthrough at [infona.ai/docs/quickstart](https://infona.ai/docs/quickstart).

## Self-hosted / OSS repo mode

From the **infona-oss** checkout, after Neo4j + the API are up:

```bash
./scripts/oss_setup.sh     # health probe → write local config → best-effort CLI link
infona                     # bare; uses ~/.infona/config.json (no --local needed)
```

One-off flags (do **not** rewrite config):

```bash
infona --local                          # http://localhost:8000, tenant default
infona --no-login                       # assume open-access at INFONA_API_URL
INFONA_API_URL=http://my-host:8000 infona
```

**Precedence:** flags (one-off) > env (`INFONA_API_URL` / `INFONA_API_KEY`) >
`~/.infona/config.json` > connect wizard when empty.

When self-hosted, the prompt shows the host suffix: `infona@localhost:8000 (kg) ▸`.

## Auto-enrichment

Fill and verify attributes on entities of a given type by looking them up in external sources, with a human review step before any write:

```text
> /enrich LineItem brand manufacturer
Plan: enrich LineItem.brand, .manufacturer · tier: lite · policy: stage
Job queued: enr_xxxxxxxx · 12,450 entities
[████████████████████] filled 6,200 · verified 1,400 · conflicts 320
Status: review · 320 conflicts pending. Run /enrich review enr_xxxxxxxx
```

Use `/enrich watch <job_id>` for live progress, `/enrich jobs` to list recent jobs, and `/enrich review <job_id>` to walk through conflicts and accept/reject each one. The `lite` tier uses Wikidata only (free, no API key).

## Install

```bash
npm install @infona-ai/cli        # or: npm install -g @infona-ai/cli
```

Requires Node 20+. The global install exposes the `infona` command (and the `onta` compatibility alias).

## Browsing what got ingested

After ingest, look around before asking questions:

```text
infona (mentors) [37,715] ▸ /types
  Type           Entities
  Mentor              988
  Skill               412
  Industry             38

infona (mentors) [37,715] ▸ /type Mentor
  Mentor  1,000 entities

  Attributes (6)
    .name           string      988  ( 99%)
    .level          string      714  ( 71%)
    ...

  Relationships (6)
    .title         → JobTitle    988  ( 99%) (+775 string)
    .skills        → Skill       987  ( 99%)
    ...
```

`/types <query>` filters by substring; `/type <name>` accepts case-insensitive prefix. Auto-attached system metadata (`rdfs:label`, `ingested_at`, `source`) is hidden by default — pass `--system` to see it. The `(+775 string)` annotation appears when the resolver produced both a literal value and a typed-entity link for the same column.

## SDK

```ts
import { Client, InfonaError } from "@infona-ai/cli";

const client = new Client({ apiKey: process.env.INFONA_API_KEY });

await client.ingest("sales.csv", { kg: "sales" });
const result = await client.ask("What's the average deal size by region?", { kg: "sales" });
console.log(result.answer);
```

### Constructor

```ts
new Client({
  apiKey?: string,    // env: INFONA_API_KEY
  baseUrl?: string,   // env: INFONA_API_URL (default: https://api.infona.ai)
  tenant?: string,    // env: INFONA_TENANT (default: demo-tenant)
})
```

### Methods

- `ingest(pathOrText, { kg?, contentType? })` — auto-detects CSV by extension and uses the two-step schema/rows flow; otherwise sends raw content.
- `ask(question, { kg? })` — returns `{ answer, sparql?, ... }`.
- `listKgs()`, `createKg(name, description?)`, `deleteKg(name)` — context-graph CRUD.
- `ontologyTypes()` — list every type in the tenant ontology with attributes and parents.
- `ontologyResolve(ask, { knowledge_graph? })` — resolve a fuzzy natural-language ontology-evolution ask (no exact type/attribute/relationship names needed) against the current schema. Returns `{ applied, proposals, summary }`; high-confidence changes land automatically, ambiguous/new-type ones come back as `proposals`.
- `ontologyApply(proposal)` — confirm and commit a single `ResolvedChange` from `ontologyResolve`'s `proposals`. Pass the object through unchanged; returns `{ applied, operations, summary }`.
- `typeCounts(kg)` — `[{ name, entity_count }]` for the given KG, sorted desc. Powers `/types`.
- `typeUsage(kg, name, { includeSystem? })` — full breakdown for one type: attributes (with usage counts), relationships, and 3 sample entities. Powers `/type`. System predicates filtered by default.
- `exploreRecords(kg, type, { limit?, cursor? })` — one keyset-paginated page of entity instances (`{ columns, rows, total, next_cursor }`).
- `exploreTypeEdges(kg)` — undirected type→type edges for an overview graph (`[{ source, target, weight }]`).
- `normalizeSuggest(kg, type)`, `normalizeRules({ kg?, status? })`, `normalizeConfirmRule(id)`, `normalizeRejectRule(id)`, `normalizeApplyRule(id)` — inferred-normalization rule lifecycle.
- `ontologyRecommend(body?)` — recommend ontology relationships/changes for a KG.

All errors throw `InfonaError` (alias: `InfonaError`).

### Raw / passthrough API (`client.raw.*`)

Every method above throws on a non-2xx status and some reshape the payload
(e.g. `listKgs()` unwraps `{ kgs: [] }`). When you instead want the backend
`Response` **verbatim** — to forward it 1:1 (e.g. from a web proxy route) or to
branch on `status` without a `try/catch` — use the `raw` namespace. Each raw
method maps to one canonical operation with the path encoded inside the SDK, so
callers pass **no path string**:

```ts
const client = new Client({ apiKey, tenant });

// Forward the backend response unchanged from a proxy:
const res = await client.raw.enrichJobs();          // GET …/enrich/jobs
return new Response(res.body, { status: res.status, headers: res.headers });

// A non-2xx is a Response, not a throw — and the body is never reshaped:
const r = await client.raw.enrichJob("missing");
if (r.status === 404) { /* … */ }
```

`client.raw` covers agent, ask, ingest (+ csv schema/rows), enrich jobs
(create/list/get/conflicts/apply/cancel), ontology (types/resolve/recommend/apply),
kgs (list/create/delete), explore (summary/records/type-edges/type-counts/search),
search + grep (semantic instance search / index-free literal scan of one KG),
normalize (suggest/rules GET+POST/confirm/reject/apply) and tenants
(list/create/delete). Each returns `Promise<Response>` and only ever rejects on a
network error or timeout (i.e. when there is no HTTP response to return).

## One-shot CLI

For scripts and CI — every command is a single HTTP round-trip (use the `infona` bin, or `npx -y -p @infona-ai/cli infona …`):

```bash
# List / create / delete context graphs
infona kg list
infona kg create my-data --description "demo"
infona kg delete my-data

# Ingest data
infona ingest data.csv --kg my-data
infona ingest --text "Alice works at Acme" --kg my-data

# Ask questions
infona ask "How many companies?" --kg my-data
infona ask "Top 5 deals" --kg my-data --debug

# Ontology + clear
infona ontology types
infona clear --kg my-data --yes
```

### Environment

Prefer `INFONA_*` for new configs:

- `INFONA_API_KEY` — required for headless / CI use; `infona init` (API key path) or `infona login` writes one to `~/.infona/config.json`.
- `INFONA_API_URL` — default `https://api.infona.ai`. OSS setup / local wizard sets `http://localhost:8000`.
- `INFONA_TENANT` — default `demo-tenant` on cloud, `default` on localhost open-access.

### Config file

`~/.infona/config.json` (mode `600`). Example after `./scripts/oss_setup.sh` or `infona init --local`:

```json
{
  "apiUrl": "http://localhost:8000",
  "tenant": "default"
}
```

> PDF ingest is not supported on any surface (CLI, API, MCP). Extract text or tables first; CSV is the best-supported path.

## License

Apache-2.0. See [LICENSE](./LICENSE).

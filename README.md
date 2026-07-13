# Onta

Turn any CSV into a context graph — a knowledge graph you can query in natural language.

One LLM call infers the schema. All rows are mapped deterministically. Ask questions, get answers backed by SPARQL.

91.4% accuracy across 26 knowledge graphs (302 questions, 4 domains, execution-verified ground truth).

## Quickstart (5 minutes)

### 1. Start the graph database

```bash
docker compose up -d
```

No Docker? Run the pip-only embedded store instead — it serves the same
endpoints, so nothing else changes:

```bash
pip install pyoxigraph python-multipart
python scripts/local_sparql.py --data ./local-graph
```

### 2. Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

### 3. Configure

```bash
cp .env.example .env
# Add your OpenRouter API key:
# OPENROUTER_API_KEY=sk-or-...
```

### 4. Start the server

```bash
source .env && uvicorn cograph_client.api.app:create_app --factory --port 8000
```

### 5. Ingest and query

```bash
# Install the CLI (Node 20+)
npm install -g @onta/cli

# Ingest the sample dataset (--local targets http://localhost:8000)
onta --local ingest examples/bookstore.csv --kg bookstore

# Ask questions
onta --local ask "How many books are there?" --kg bookstore
onta --local ask "Which genre has the most books?" --kg bookstore
onta --local ask "What is the average price of Dystopian books?" --kg bookstore
onta --local ask "List all books by J.R.R. Tolkien" --kg bookstore
```

No API key needed for local usage. No AWS account needed.

## How It Works

```
CSV file
  |
  v
Schema Inference (1 LLM call)
  |  Determines: entity type, attributes, relationships
  v
Deterministic Row Mapping (0 LLM calls)
  |  Each row -> typed entity with triples
  v
SPARQL Knowledge Graph (Fuseki or Neptune)
  |
  v
Natural Language Query -> SPARQL -> Answer
```

**Ingestion:** Your CSV columns are analyzed by an LLM to determine which are attributes (numbers, dates) and which are relationships to other entities (authors, genres, cities). One call, not one per row.

**Querying:** Your question is translated to SPARQL using the ontology + few-shot examples from the RAG bank. Results come back as a human-readable answer.

## CLI

The Node CLI (`npm install -g @onta/cli`, requires Node 20+) covers both an interactive shell and one-shot subcommands. Run bare `onta` to drop into the shell:

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

`/types` and `/type` are the fastest way to look around after an ingest — see the [npm README](packages/cograph/README.md) for screenshots. Bare lines auto-route to `/ask`.

### Self-hosted CLI mode

The CLI runs against a self-hosted backend without a hosted-version account — pass `--local` (or `--no-login`) to skip the browser sign-in:

```bash
onta --local                                   # defaults to http://localhost:8000
onta --no-login                                # uses ONTA_API_URL env var
ONTA_API_URL=http://my-host:8000 onta
```

When self-hosted, the prompt shows the host suffix: `onta@localhost:8000 (kg) ▸`. The backend detects open-access vs auth-required mode by looking at `OMNIX_API_KEYS` — empty means no auth, `tenant=default`.

### Auto-enrichment

Enrichment fills and verifies attributes on entities of a given type by looking them up in external sources, surfacing conflicts (existing value vs source value) for human review before writing.

```text
> /enrich LineItem brand manufacturer
Plan: enrich LineItem.brand, .manufacturer in parts · tier: lite · policy: stage
Job queued: enr_xxxxxxxx · 12,450 entities · est. $0.00
Watch progress? [Y/n] y
[████████████████████] 12,450/12,450 · filled 6,200 · verified 1,400 · conflicts 320
Status: review · 320 conflicts pending. Run /enrich review enr_xxxxxxxx

> /enrich review enr_xxxxxxxx
LineItem #4471: "K&N 33-2304 air filter, red"
  brand: "KN" → "K&N" (confidence 0.97, kn-filters.com)
Accept? [a]/[r]/[s]/[A]ll/[q]uit:
```

In this OSS build, the **lite** tier uses Wikidata as the only source (free, no API key). The `base`/`core`/`pro` tiers are scaffolded but require additional adapters (web search, LLM extraction) wired in by the hosted version.

Or one-shot, useful in scripts and CI:

```bash
# Ingest
onta ingest data.csv --kg my-dataset

# Query
onta ask "How many records are there?" --kg my-dataset

# Manage KGs
onta kg list
onta kg create my-dataset -d "Description"
onta kg delete my-dataset

# View ontology (legacy — prefer /types and /type in the shell)
onta ontology types

# Clear data
onta clear --kg my-dataset -y
```

## MCP Server (AI Agent Integration)

Connect Onta to Claude, Cursor, Windsurf, or any MCP-compatible agent:

```json
{
  "mcpServers": {
    "onta": {
      "command": "npx",
      "args": ["-y", "@onta/mcp"]
    }
  }
}
```

Tools: `ask`, `search`, `agent`, `ingest_csv`, `list_knowledge_graphs`,
`create_knowledge_graph`, `delete_knowledge_graph`, `view_ontology`,
`evolve_ontology`, `apply_ontology_change(s)`, `schedule`, `list_jobs`,
`get_job`, `wait_for_job`.
See the [@onta/mcp README](packages/cograph-mcp/README.md) for configuration.

## API

All endpoints at `http://localhost:8000`. No auth required for local usage.

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/graphs/{tenant}/ask` | Natural language query |
| POST | `/graphs/{tenant}/ingest/csv/schema` | Infer CSV schema |
| POST | `/graphs/{tenant}/ingest/csv/rows` | Insert rows |
| GET | `/graphs/{tenant}/kgs` | List context graphs |
| POST | `/graphs/{tenant}/query` | Raw SPARQL query |
| GET | `/graphs/{tenant}/ontology/schema` | View ontology |
| POST | `/graphs/{tenant}/enrich/jobs` | Create + queue an enrichment job |
| GET | `/graphs/{tenant}/enrich/jobs` | List enrichment jobs |
| GET | `/graphs/{tenant}/enrich/jobs/{job_id}` | Status + progress |
| GET | `/graphs/{tenant}/enrich/jobs/{job_id}/conflicts` | Pending conflicts |
| POST | `/graphs/{tenant}/enrich/jobs/{job_id}/apply` | Apply accepted changes |
| DELETE | `/graphs/{tenant}/enrich/jobs/{job_id}` | Cancel a job |
| GET | `/health` | Health check |

Interactive docs at [localhost:8000/docs](http://localhost:8000/docs) when running.

## Model Configuration

Query LLM is selectable per deployment: OpenRouter (any model it hosts),
Cerebras, or Anthropic.

```bash
# OpenRouter (recommended)
export OPENROUTER_API_KEY=sk-or-...
export OMNIX_QUERY_PROVIDER=openrouter
export OMNIX_QUERY_MODEL=google/gemini-2.5-flash

# Or Cerebras (fast inference)
export OMNIX_QUERY_PROVIDER=cerebras
export OMNIX_CEREBRAS_API_KEY=csk-...
```

## Eval Results

| KG | Domain | Score (20 questions) |
|----|--------|---------------------|
| zillow-austin | Real Estate | 100% |
| video-games | Entertainment | 89% |
| events-sf | Events | 85% |
| clinical-trials | Medical | 85% |
| cfpb-complaints | Financial | 80% |

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full technical deep-dive.

- **Backend:** FastAPI + SPARQL (Fuseki or Neptune)
- **Ingestion:** LLM schema inference -> deterministic mapping -> typed triples
- **Query:** Ontology retrieval -> RAG examples -> SPARQL generation -> execution
- **Eval:** 4-tier questions, pandas ground truth, programmatic + LLM judges

## License

Apache 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Contributions require a one-time [CLA](CLA.md) signature — a single comment
on your first pull request. See [CONTRIBUTING.md](CONTRIBUTING.md).

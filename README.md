# Infona (OSS)

Turn any CSV into a **context graph** — a knowledge graph you can query in natural language.

**Product:** [infona.ai](https://infona.ai) · **Company:** Infona

One LLM call infers the schema. All rows map deterministically. Ask questions; get exact answers backed by **Cypher on Neo4j**.

This repository is the **OSS product runtime** (Python API, client, Node CLI/MCP). It is **not** the scientific freeze package for *Structure Once, Query Cheaply*.

| Package | Where |
|---------|--------|
| **Product OSS (this repo)** | Runtime + tests + examples |
| **Eval-MH paper freeze (public)** | [infona-ai/structure-once-query-cheaply](https://github.com/infona-ai/structure-once-query-cheaply) |
| **What’s free vs hosted** | **[docs/BOUNDARY.md](docs/BOUNDARY.md)** ← read this first |

## What’s free / what’s not (30 seconds)

- **OSS:** ingest, ontology, ask, MCP/CLI/API, export, free sources, BYOK registry, plugin seams.
- **Bring your own retrieval:** this build **does not** register an open-web page fetcher. Enrichment that needs arbitrary URL fetch will decline unless **you** register a fetcher — or use hosted Infona.
- **Hosted-only:** managed keys Infona bills, paid web search/scrape ladders, curated Enhanced ontology content, Explorer polish, billing.

Full table: [docs/BOUNDARY.md](docs/BOUNDARY.md).

## Quickstart (~10 minutes)

### 1. Start Neo4j

```bash
docker compose up -d
# Neo4j only (default). Legacy Fuseki: docker compose --profile legacy-sparql up -d
```

```bash
export NEO4J_URI=bolt://localhost:7687
export NEO4J_USER=neo4j
export NEO4J_PASSWORD=infona-dev-password
export INFONA_GRAPH_BACKEND=neo4j   # default; explicit is fine
```

Bootstrap constraints (idempotent):

```bash
python - <<'PY'
import asyncio
from infona_client.graph.store import get_graph_store

async def main():
    store = get_graph_store()
    print(await store.bootstrap_schema())
    await store.close()
asyncio.run(main())
PY
```

Details: [docs/neo4j-local.md](docs/neo4j-local.md).

### 2. Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

> Import path: `infona_client`. Graph IRIs: `https://graph.infona.ai/`.  
> CLI / MCP: `infona` and `infona-mcp` (`@infona-ai/cli`, `@infona-ai/mcp`). Config: `~/.infona`.

### 3. Configure

```bash
cp .env.example .env
# OPENROUTER_API_KEY=sk-or-...   # (or Cerebras / Anthropic — see Model Configuration)
```

### 4. Start the API

```bash
source .env && uvicorn infona_client.api.app:create_app --factory --port 8000
```

No API key required for local open-access (`INFONA_API_KEYS` empty → tenant `default`).

### 5. Ingest, ask, export

```bash
# CLI (Node 20+)
npm install -g @infona-ai/cli   # or: npm ci && npm run build -w packages/cli

infona --local ingest examples/bookstore.csv --kg bookstore
infona --local ask "How many books are there?" --kg bookstore
infona --local ask "List all books by J.R.R. Tolkien" --kg bookstore

# Get data back out (F10)
infona --local export --kg bookstore -f json -o bookstore.json
infona --local export --kg bookstore -f csv --type Book -o books.csv
```

HTTP:

```bash
curl -s "http://localhost:8000/graphs/default/kgs/bookstore/export?format=json" | head
```

## How it works

```
CSV / JSON / text
  → schema inference (1 LLM call)
  → deterministic row mapping
  → Neo4j knowledge graph (GraphStore / Cypher)
  → natural language question → Cypher → answer
```

**Retrieval note:** OSS structures and queries what you put in. Open-web scrape is opt-in (BYOR) or hosted.

## CLI (self-hosted)

```bash
infona --local                    # shell → http://localhost:8000
infona --local kg list
infona --local ingest data.csv --kg my-dataset
infona --local ask "How many records?" --kg my-dataset
infona --local export --kg my-dataset -f csv -o out.csv
infona --local ontology types
```

See [packages/cli/README.md](packages/cli/README.md).

## MCP (agents)

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

Tools include `ask`, `search`, `agent`, `ingest_csv`, KG CRUD, ontology evolve, jobs. See [packages/mcp/README.md](packages/mcp/README.md).

## API (local)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/graphs/{tenant}/ask` | Natural language query |
| POST | `/graphs/{tenant}/ingest/csv/schema` | Infer CSV schema |
| POST | `/graphs/{tenant}/ingest/csv/rows` | Insert rows |
| GET | `/graphs/{tenant}/kgs` | List graphs |
| GET | `/graphs/{tenant}/kgs/{kg}/export` | **Export JSON/CSV** |
| GET | `/graphs/{tenant}/ontology/schema` | Ontology |
| GET | `/health` | Health |

Interactive docs: [http://localhost:8000/docs](http://localhost:8000/docs).

## Model configuration

```bash
export OPENROUTER_API_KEY=sk-or-...
export INFONA_QUERY_PROVIDER=openrouter
export INFONA_QUERY_MODEL=google/gemini-2.5-flash
```

Local models (Ollama / vLLM) via OpenAI-compatible endpoints are possible if you point the query provider at them; document your own setup — the honest default for a first run is an OpenRouter key.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) and [docs/BOUNDARY.md](docs/BOUNDARY.md).

- **Backend:** FastAPI + Neo4j GraphStore (Cypher)
- **Ingestion:** LLM schema inference → deterministic mapping
- **Query:** ontology + few-shot bank → Cypher → answer
- **Legacy SPARQL / Fuseki:** still in-tree for older paths; not the default

## License

Apache 2.0 — [LICENSE](LICENSE), [NOTICE](NOTICE).  
Contributions: [CLA.md](CLA.md), [CONTRIBUTING.md](CONTRIBUTING.md).

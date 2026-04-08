# Omnix — Living Knowledge Graph Platform

Omnix turns CSV data into a queryable knowledge graph with automatic ontology inference. One LLM call infers the schema, then all data is mapped deterministically. Ask natural language questions, get accurate answers backed by SPARQL queries.

**Current accuracy:** 88% average across 5 domains (real estate, medical, financial, entertainment, events) on 20-question evals.

## Quick Start

```bash
# Install
python -m venv .venv
source .venv/bin/activate
pip install -e .

# Set your API endpoint (defaults to the demo environment)
export OMNIX_API_URL=http://localhost:8000
export OMNIX_API_KEY=dev-key-001
export OMNIX_TENANT=demo-tenant
```

## CLI Usage

### Knowledge Graphs

```bash
# List all knowledge graphs
omnix kg list

# Create a new KG
omnix kg create my-dataset -d "Description of this dataset"

# Delete a KG and all its data
omnix kg delete my-dataset
```

### Ingest Data

```bash
# CSV — 1 LLM call infers schema from 10 rows, all rows mapped deterministically
omnix ingest data.csv --kg my-dataset

# JSON
omnix ingest data.json --kg my-dataset

# PDF — extracts text via Claude Vision, then ingests
omnix ingest report.pdf --kg my-dataset

# Plain text file
omnix ingest notes.txt --kg my-dataset

# Inline text
omnix ingest --text "John works at Acme Corp in Austin" --kg my-dataset
```

### Query

```bash
# Ask a natural language question
omnix ask "How many properties are listed in Austin?"

# View the ontology (shared across all KGs)
omnix ontology types
```

### Clear Data

```bash
# Clear instance data from a specific KG
omnix clear --kg my-dataset -y

# Clear all instance data (preserves ontology)
omnix clear -y

# Clear everything including ontology
omnix clear --include-ontology -y
```

## Query Model Configuration

SPARQL generation uses Gemini 2.5 Flash via OpenRouter by default. Change via env vars:

```bash
# Switch model
export OMNIX_QUERY_MODEL=google/gemini-2.5-flash

# Switch provider (openrouter, cerebras, anthropic)
export OMNIX_QUERY_PROVIDER=openrouter
export OPENROUTER_API_KEY=sk-or-...

# Or override per-query
omnix ask "question" --model google/gemini-2.5-flash
```

### Eval Results (April 2026, 20 questions per KG)

| KG | Domain | Score |
|----|--------|-------|
| zillow-austin | Real Estate | 100% (20/20) |
| video-games | Entertainment | 89% (24/27) |
| events-sf | Events | 85% (17/20) |
| clinical-trials | Medical | 85% (17/20) |
| cfpb-complaints | Financial | 80% (16/20) |

### Running Evals

```bash
# Run 20-question eval against a specific KG
omnix eval data.csv --kg my-dataset --query-only -n 20 --fast-judge --cache-questions

# Run with concurrency
omnix eval data.csv --kg my-dataset --query-only -n 20 --fast-judge --concurrency 10
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OMNIX_API_URL` | Demo ELB endpoint | Backend API base URL |
| `OMNIX_API_KEY` | `dev-key-001` | API authentication key |
| `OMNIX_TENANT` | `demo-tenant` | Tenant identifier |
| `ANTHROPIC_API_KEY` | — | Required for PDF ingestion via CLI |
| `CEREBRAS_API_KEY` | — | Cerebras inference (default query provider) |
| `OPENROUTER_API_KEY` | — | OpenRouter inference (fallback) |
| `OMNIX_QUERY_MODEL` | `google/gemini-2.5-flash` | Model for SPARQL generation |
| `OMNIX_QUERY_PROVIDER` | `openrouter` | Provider: openrouter, cerebras, anthropic |

## Demo Web App

```bash
cd web
npm install
npm run dev
# Open http://localhost:3000/demo
```

## Architecture

- **Backend**: FastAPI + AWS Neptune (RDF/SPARQL) + Claude for entity extraction
- **Ontology**: Shared schema across knowledge graphs (types, attributes, relationships)
- **Knowledge Graphs**: Named graphs in Neptune, each with separate instance data
- **Ingestion**: LLM extraction → schema resolution → validation → Neptune insert
- **CSV Pipeline**: Sample-based schema inference (1 LLM call) + deterministic row mapping
- **Query**: NL question → SPARQL generation (structured output) → Neptune → formatted answer

# Cograph

Turn any CSV into a knowledge graph you can query in natural language.

One LLM call infers the schema. All rows are mapped deterministically. Ask questions, get answers backed by SPARQL.

91.4% accuracy across 26 knowledge graphs (302 questions, 4 domains, execution-verified ground truth).

## Quickstart (5 minutes)

### 1. Start the graph database

```bash
docker compose up -d
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
# Ingest the sample dataset
cograph ingest examples/bookstore.csv --kg bookstore

# Ask questions
cograph ask "How many books are there?" --kg bookstore
cograph ask "Which genre has the most books?" --kg bookstore
cograph ask "What is the average price of Dystopian books?" --kg bookstore
cograph ask "List all books by J.R.R. Tolkien" --kg bookstore
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

```bash
# Ingest
cograph ingest data.csv --kg my-dataset

# Query
cograph ask "How many records are there?" --kg my-dataset

# Manage KGs
cograph kg list
cograph kg create my-dataset -d "Description"
cograph kg delete my-dataset

# View ontology
cograph ontology types

# Clear data
cograph clear --kg my-dataset -y

# Evaluate accuracy
cograph eval data.csv --kg my-dataset --query-only -n 20 --fast-judge
```

## MCP Server (AI Agent Integration)

Connect Cograph to Claude, Cursor, Windsurf, or any MCP-compatible agent:

```json
{
  "mcpServers": {
    "cograph": {
      "command": "python",
      "args": ["-m", "cograph_client.mcp_server"]
    }
  }
}
```

Tools: `ask`, `list_knowledge_graphs`, `ingest_csv`, `view_ontology`.

## API

All endpoints at `http://localhost:8000`. No auth required for local usage.

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/graphs/{tenant}/ask` | Natural language query |
| POST | `/graphs/{tenant}/ingest/csv/schema` | Infer CSV schema |
| POST | `/graphs/{tenant}/ingest/csv/rows` | Insert rows |
| GET | `/graphs/{tenant}/kgs` | List knowledge graphs |
| POST | `/graphs/{tenant}/query` | Raw SPARQL query |
| GET | `/graphs/{tenant}/ontology/schema` | View ontology |
| GET | `/health` | Health check |

Interactive docs at [localhost:8000/docs](http://localhost:8000/docs) when running.

## Model Configuration

Works with any OpenAI-compatible API. Default: Gemini 2.5 Flash via OpenRouter.

```bash
# OpenRouter (default)
export OPENROUTER_API_KEY=sk-or-...

# Or use Ollama (free, local)
export COGRAPH_QUERY_PROVIDER=ollama
export COGRAPH_QUERY_MODEL=llama3.1

# Or Groq, Cerebras, Anthropic, etc.
export COGRAPH_QUERY_PROVIDER=cerebras
export COGRAPH_CEREBRAS_API_KEY=csk-...
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

Apache 2.0. See [LICENSE](LICENSE).

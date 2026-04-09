# Contributing to Omnix

## Dev Setup

```bash
# Clone
git clone https://github.com/git-moeen/omnix-oss.git
cd omnix-oss

# Start graph DB
docker compose up -d

# Install
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Configure
cp .env.example .env
# Add your OPENROUTER_API_KEY to .env

# Run tests
pytest tests/ -v --tb=short
```

## Running Locally

```bash
source .env
uvicorn omnix.api.app:create_app --factory --port 8000
```

## Project Structure

```
omnix/
  api/          FastAPI routes and middleware
  auth/         API key authentication
  graph/        SPARQL client and query builders
  nlp/          Query pipeline, prompts, example bank, embeddings
  resolver/     Schema inference, type matching, CSV mapping
  models/       Pydantic data models
  functions/    Compute function registry
  cli.py        CLI entry point
  config.py     Settings (OMNIX_ env prefix)
  eval.py       Eval framework
  mcp_server.py MCP server for AI agents
```

## Code Style

- Python 3.12+
- Type hints on all function signatures
- snake_case for functions and variables, PascalCase for classes
- No print statements in library code, use structlog
- Keep functions short. If it needs a comment explaining what it does, it's too long.

## Making Changes

1. Fork the repo
2. Create a branch: `git checkout -b my-change`
3. Make your changes
4. Run tests: `pytest tests/ -v`
5. Commit with a clear message: `git commit -m "fix: description of what and why"`
6. Open a PR against `main`

## Commit Messages

Format: `type: description`

Types: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `perf`

Examples:
- `feat: add Blazegraph backend support`
- `fix: handle empty CSV columns in schema inference`
- `docs: add Ollama configuration guide`

## Tests

```bash
# Run all tests
pytest tests/ -v

# Run a specific test file
pytest tests/test_validator.py -v

# Run with coverage
pytest tests/ --cov=omnix --cov-report=term-missing
```

Tests mock the Neptune/Fuseki client. No running graph DB needed for unit tests.

## Areas We'd Love Help With

- Additional graph DB backends (Blazegraph, Oxigraph, RDFLib)
- More LLM provider support (Ollama, vLLM, Together)
- Better eval question generation (more natural language, less attribute-name references)
- Entity resolution ("TX" = "Texas")
- Documentation improvements

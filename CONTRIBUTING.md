# Contributing to Infona

## What can ship here (OSS boundary — read first)

`infona-oss` ships publicly: **npm** (`@infona-ai/cli`, `@infona-ai/mcp`) and
**PyPI** (`infona-client`) publish on the same release with one shared version.

The repo itself is public. **Public publication is a one-way door** — once code
ships, it's in mirrors, archives, and forks within hours. Everything in this
repo must be OSS-safe.

**Ships here (OSS):**
- `infona_client/` — ingest, resolver, **core ER engine** (normalize, block,
  score, merge), REST API surface, embedding service
- `packages/cli` (TS SDK + CLI, published as `@infona-ai/cli`) and `packages/mcp` (MCP server, published as `@infona-ai/mcp`)
- Plugin **protocols**: `register_external_verifier` (auth),
  `register_adapter` (enrichment)
- Default OSS adapters: Wikidata enrichment, static-keys auth
- Tests for all of the above

**Does NOT ship here (proprietary — lives in the parent `infona/` repo):**
- Paid enrichment adapters (Exa, Perplexity, GS1, Anthropic web_search)
- Production Clerk auth integration (`infona-auth-clerk`)
- Infona Explorer web app, AWS/SAM infra, deploy workflows
- Entitlement / billing / rate-limit logic
- Advanced ER tooling (review-queue UI, embedding matchers, active learning)
- Eval / paper artifacts (`eval_holdout_v2/`, `eval_reports/`) — local-only
  (gitignored). The public freeze lives in
  [structure-once-query-cheaply](https://github.com/infona-ai/structure-once-query-cheaply).

The canonical, fuller table with reasoning lives in the parent repo at
[`docs/oss_proprietary_boundary.md`](https://github.com/infona-ai/infona/blob/main/docs/oss_proprietary_boundary.md).
When in doubt, surface the question before writing code.

**Entitlement gating is NOT done in OSS (incl. the MCP server).** The MCP server
and its `agent` tool are OSS and are advertised freely — planning a turn is free.
A plan the agent executes may contain a *paid* step (e.g. web enrichment), but the
authorization for that step is enforced **server-side, behind the HTTP API**, by
the proprietary backend (a 4xx on `POST /graphs/{tenant}/agent` confirm, the same
way the direct paid routes are gated). The MCP `agent` tool reaches the backend
through the exact same authenticated HTTP client (`X-API-Key` → tenant) as every
other tool, so confirming a plan via the agent **cannot bypass** a gate the direct
path enforces — there is deliberately no entitlement check to duplicate in OSS
(per the proprietary list above). Do **not** add billing/entitlement logic here to
"gate" the agent: that belongs in the parent repo.

**This is mechanically enforced** (MOE-21). Run the same checks CI runs:

```bash
bash scripts/check_boundary.sh                 # static: no proprietary imports/hosts/paths/secrets
bash scripts/check_npm_bundle.sh               # inspect published tarballs for forbidden paths
python scripts/sync_release_version.py         # fail if cli / mcp / infona-client versions drift
python scripts/sync_release_version.py --check-published  # fail if npm/PyPI latest disagree
```

CI runs `check_boundary.sh` on every PR (`.github/workflows/boundary.yml`)
and the in-repo lockstep check on every test job. A daily
`release-lockstep.yml` run compares latest npm + PyPI.
`.github/workflows/pypi-publish.yml` bumps **one** version, then publishes
`@infona-ai/cli`, `@infona-ai/mcp`, and `infona-client` together. PyPI auth
is the `PYPI_API_TOKEN` repo secret. Re-runs skip versions already on npm
or PyPI. The job inspects npm and PyPI artifacts (`check_npm_bundle.sh`,
`check_pypi_bundle.sh`) and refuses to upload if the three in-repo
versions drifted. Registry drift after a partial publish is the daily
`release-lockstep.yml` job, not a post-upload `/latest` poll (Warehouse
JSON can lag the upload). A PR that adds `from infona.<anything>` under
`infona_client/` or `packages/` fails.

## Contributor License Agreement (CLA)

First-time contributors sign the project [CLA](CLA.md) once, by commenting
on their pull request:

> I have read the CLA Document and I hereby sign the CLA

The CLA bot (`.github/workflows/cla.yml`) blocks merge until every PR
author has signed, and records signatures in `.github/signatures/cla.json`
on the `cla-signatures` branch. Comment `recheck` to re-run a stale check.

Why a CLA on an Apache-2.0 repo: it keeps the project able to add license
options later (commercial licensing, or a different license for a future
version) without tracking down every past contributor. The trade in your
favor is written into the CLA itself: anything you contribute stays
available under Apache-2.0 as released — relicensing can never be
retroactive — and you keep the copyright to your contribution.

## Dev Setup

### Users (library install, no clone)

`infona-client` publishes to PyPI on the same release as `@infona-ai/cli`
and `@infona-ai/mcp` (one shared version). Users:

```bash
pip install infona-client
```

Same one-liner with uv: `uv pip install infona-client`.
The CLI remains the npm package (`@infona-ai/cli`); this wheel is the Python
library + API.

### Contributors (editable install)

```bash
# Clone
git clone https://github.com/infona-ai/infona-oss.git
cd infona-oss

# Start graph DB only (API is a separate compose service)
docker compose up -d neo4j

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
uvicorn infona_client.api.app:create_app --factory --port 8000
```

## Project Structure

```
infona_client/
  api/          FastAPI routes and middleware
  auth/         API key authentication
  graph/        Neo4j GraphStore, writers, ontology helpers
  nlp/          Query pipeline, prompts, example bank, embeddings
  resolver/     Schema inference, type matching, CSV mapping
  models/       Pydantic data models
  functions/    Compute function registry
  config.py     Settings (INFONA_ env prefix)
  eval.py       Eval framework

packages/
  cli/          Node SDK + CLI (published as `@infona-ai/cli`)
  mcp/          MCP server for AI agents (published as `@infona-ai/mcp`)
```

## Code Style

- Python 3.12+
- Type hints on all function signatures
- snake_case for functions and variables, PascalCase for classes
- No print statements in library code, use structlog
- Keep functions short. If it needs a comment explaining what it does, it's too long.
- **File budget:** new `infona_client` / `packages` / `tests` source files stay
  around 500 lines (hard cap **550**). Existing oversized files are pinned by
  `tests/test_file_size_budget.py` and must not grow — extract a seam instead
  of adding to a 2k+ module. See [AGENTS.md](AGENTS.md).

## Coding agents

[AGENTS.md](AGENTS.md) is the contract for coding agents and AI-assisted
contributors (boundary, file budget, write/retrieval convergence, always-LLM
`/ask`). Humans should follow the same rules.

## Making Changes

1. Fork the repo
2. Create a branch: `git checkout -b my-change`
3. Make your changes
4. Run tests: `pytest tests/ -v`
5. Commit with a clear message: `git commit -m "fix: description of what and why"`
6. Open a PR against `main`
7. First PR? Sign the [CLA](CLA.md) when the bot prompts you (see above)

## Commit Messages

Format: `type: description`

Types: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `perf`

Examples:
- `feat: add Ollama query-provider preset`
- `fix: handle empty CSV columns in schema inference`
- `docs: clarify Neo4j-only local graph path`

## Tests

```bash
# Run all tests
pytest tests/ -v

# Run a specific test file
pytest tests/test_validator.py -v

# Run with coverage
pytest tests/ --cov=infona_client --cov-report=term-missing
```

Unit tests use in-process stores (`MemoryGraphStore`) and mocks. No running
graph DB is required for the default suite. Optional Neo4j integration tests
are marked separately (see [docs/neo4j-local.md](docs/neo4j-local.md)).

## Areas We'd Love Help With

- More LLM provider support (Ollama, vLLM, Together)
- Better eval question generation (more natural language, less attribute-name references)
- Entity resolution ("TX" = "Texas")
- Documentation improvements

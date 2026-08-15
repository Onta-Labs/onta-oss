# Agent / AI contributor contract

This is the public OSS runtime ([infona-ai/infona-oss](https://github.com/infona-ai/infona-oss)).
Humans and coding agents follow the same rules.

Longer how-tos: [CONTRIBUTING.md](CONTRIBUTING.md), [docs/BOUNDARY.md](docs/BOUNDARY.md),
[docs/neo4j-local.md](docs/neo4j-local.md).

## Public, CLA, license

- **Apache-2.0.** Public publication is a one-way door.
- **CLA:** first-time authors sign [CLA.md](CLA.md) on the PR. The CLA allowlist
  is `git-moeen`, `dependabot[bot]`, `*[bot]`. Author commits as a CLA-listed
  identity (maintainers: `Moeen Miri` /
  `4954564+git-moeen@users.noreply.github.com`).
- Never commit secrets, platform keys, internal hostnames, or cloud account IDs.

## OSS / proprietary boundary

This package must import **on its own**. Hard rules:

- Never `from infona.<anything>` or `import infona.<anything>` in this repo.
- Never reference proprietary identifiers (internal cloud accounts, secret ARNs,
  deployed load-balancer hostnames, production API keys, private eval-dataset
  paths).
- **BYOR** (bring your own retrieval): OSS registers **no** default open-web
  page fetcher. Callers register a fetcher or skip web fetch. Do not re-add a
  default `StaticHttpFetcher` registration.
- **BYOK** keyed registry entries are fine (the user's own env / secret). Never
  ship or imply a shared platform key.
- Paid adapters, production Clerk, Explorer, billing/entitlement, and cloud
  infra live in the **private parent**, not here.
- Product graph path is **Neo4j-only** (Cypher / GraphStore). Neptune / SPARQL
  HTTP is residual quarantine. `NeptuneClient` is still imported — do **not**
  delete it in drive-by cleanup.

Enforced by `scripts/check_boundary.sh` and
`tests/test_api_registry_byok_guard.py`.

## File budget

Soft cap **~500** lines. Hard cap for **new** source and test files: **550**.

- Scan: `infona_client/**/*.py`, `packages/**/*.ts` (skip `node_modules`,
  `dist`, `.venv`, `*.d.ts`), and `tests/**/*.py`.
- Files already over 550 on `main` are **allowlisted at their pinned count**
  in `tests/test_file_size_budget.py`. They must not grow (`+20` slack absorbs
  newline churn only).
- Do **not** add lines to a 2k+ file. Extract a seam next to the owner
  package, keep public imports stable, then lower the pin.
- Prefer splitting a test file when you add cases rather than growing a giant.
- Pins may be lowered or removed after an extract. Do not raise a pin without
  a one-line justification in the PR.

This is a ratchet, not a rewrite. Remaining mega-files
(`enrichment/executor.py`, `enrich_cap.py`, `eval.py`, …) come down in
follow-up PRs. `web_ingest_cap.py` left OSS in #390 (hosted in the private
parent). Extracted facades (`schema_resolver.py`, `pipeline.py`,
`memory_store.py`, `explore.py`, `csv_resolver.py`, `cypher_generate.py`,
`resolver/models.py`, `client.ts`, `cli.ts`, `shell.ts`,
`packages/mcp/src/index.ts`) must stay small — do not re-inflate them.

## Convergence (do not fork these)

**Writes.** Instance facts go through `infona_client.graph.kg_writer`:
`insert_facts` + `refresh_after_write`. Deletes / rewrites: `delete_facts` /
`rewrite_subject`. No hand-rolled `insert_triples` or instance-graph `DELETE`.

**Instance edges.** Type-ranged relationships use
`https://graph.infona.ai/onto/<leaf>`. `types/<Type>/attrs/<leaf>` is literals
and schema declaration only. A relationship written on `attrs/` is invisible
to NL.

**Entity URIs.** Mint with `graph.ontology_queries.entity_uri` only.

**Retrieval.** Consult the web only via `infona_client/retrieval/`. No
per-rail fetch, SSRF guard, or provider client.

**Interfaces.** One canonical HTTP route per operation. CLI, MCP, and any
future client call the same backend; do not invent a bespoke path.

Guards: `tests/test_write_path_convergence.py`,
`tests/test_entity_uri_convergence.py`,
`tests/test_retrieval_path_convergence.py`.

## `/ask` is always-LLM Cypher

`POST /graphs/{tenant}/ask` generates Cypher with an LLM. Do **not**:

- short-circuit production `/ask` with golden-string Cypher
- hardcode persona-CSV / warehouse-SKU answers
- revive SPARQL as the product query language

Grounding, probes, and few-shots **inform** the model; they do not replace it.

## How to split a file

1. Pick **one** already-sectional helper — not a god-module rewrite, and not
   `enrichment/executor.py` / `enrich_cap.py` in the same PR as an unrelated
   change.
2. Extract next to the owner package (`nlp/`, `graph/`, `packages/cli/src/`).
3. Keep `from infona_client.foo.bar import X` working — re-export from the
   old module. Same for TS public entrypoints (`cli.ts` → `askDebug.ts` is
   the pattern).
4. One concern per file. Both sides ≤550 when possible; otherwise the parent
   must shrink and its pin drop.
5. Run the package tests that import the moved symbols.

## Tests

- Synthetic names only in fixtures (no real customer data; no spider-bench
  leakage into the example bank).
- Default suite is hermetic (`MemoryGraphStore`, mocks). No live Neo4j
  required. Optional Neo4j tests: [docs/neo4j-local.md](docs/neo4j-local.md).
- Run targeted pytest plus the relevant npm workspace:

```bash
pytest tests/test_file_size_budget.py -q
pytest tests/<touched>.py -q
npm test --workspace packages/cli   # if you touched TS
```

## Env and IRIs

- Env prefix: `INFONA_*` only.
- Graph IRIs: `https://graph.infona.ai/…`.
- Python import: `infona_client`. npm: `@infona-ai/cli`, `@infona-ai/mcp`.

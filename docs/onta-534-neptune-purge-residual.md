# ONTA-534 — Neptune / SPARQL purge residual inventory (slice 1)

**Ticket:** ONTA-534  
**Predecessor:** ONTA-527 (Neo4j-only backend + SPARQL test purge, #327)  
**Date:** 2026-08-12  
**Branch:** `feat/onta-534-neptune-purge`

Production is **Neo4j-only** (Amazon Neptune decommissioned 2026-08-11).
ONTA-527 removed the backend switch, made store resolution fail-closed, and
410'd public SPARQL HTTP. This ticket finishes the purge **where safe**.

Full deletion of `NeptuneClient` is **not** safe in one PR: 38 production
modules still type-hint or call it for residual SPARQL reads. This document is
the residual map + what slice 1 shipped.

---

## 1. Slice 1 shipped (this PR)

| Change | Status |
|--------|--------|
| **NL→SPARQL production path fail-closed** | Done — `NLQueryPipeline.ask(..., use_cypher=False)` raises `SparqlAskPathRetired` instead of running SPARQL. Product `/ask` always takes `_ask_cypher`. |
| **Dead SPARQL body removed from `ask()`** | Done — ~650 lines of SPARQL retry loop deleted from `ask()`; generator helpers (`_generate_sparql`, prompts, validators) kept for unit tests / residual. |
| **Legacy backend fail-closed** | Already ONTA-527 — `graph_backend()` raises `GraphConfigError` on anything but `neo4j`. Re-asserted by `tests/test_neo4j_only_backend.py`. |
| **Residual call-site inventory** | This file |
| **Hermetic suite** | Must stay green (no new failures on open-access path) |

---

## 2. Residual `NeptuneClient` importers (ratchet)

Enforced by `tests/test_neo4j_only_backend.py::_RESIDUAL_NEPTUNE_IMPORTERS`
(**may shrink, never grow**). Count at slice 1: **38** modules under
`infona_client/`.

### API routes (17) — DI + residual SPARQL arms

Handlers still declare `Depends(get_neptune_client)`. Many prefer GraphStore
and fall through to SPARQL only on `GraphConfigError` (no process store).

| Module | Notes |
|--------|-------|
| `api/app.py` | Constructs vestigial `NeptuneClient` on lifespan (opens no connection) |
| `api/deps.py` | `get_neptune_client` DI |
| `api/routes/ask.py` | Passes client into pipeline; NL path is Cypher |
| `api/routes/agent.py` | Same pattern |
| `api/routes/explore.py` | Hottest residual SPARQL + GraphStore dual arms |
| `api/routes/ontology.py` | Catalog dual path |
| `api/routes/knowledge_graphs.py` | KG lifecycle dual path |
| `api/routes/grep.py` | GraphStore scan vs SPARQL |
| `api/routes/export.py` | Declares Depends; export itself is GraphStore |
| `api/routes/actions.py` | Residual |
| `api/routes/corrections.py` | Residual |
| `api/routes/enrich.py` | Residual |
| `api/routes/functions.py` | Residual |
| `api/routes/ingest.py` | Residual |
| `api/routes/lambda_functions.py` | Residual |
| `api/routes/normalize.py` | Residual |
| `api/routes/operator.py` | Residual |

### Writers / resolvers / enrichment (11)

| Module | Notes |
|--------|-------|
| `resolver/schema_resolver.py` | Ingest; heavy historical Neptune touch |
| `resolver/ontology_resolver.py` | Residual |
| `enrichment/executor.py` / `strategy.py` | Residual |
| `normalization/{execute,inference,policy,rules}.py` | Residual |
| `verification/policy.py` | Residual |
| `functions/registry.py` | Residual |
| `agent/registry.py` | Residual |

### Graph substrate (4)

| Module | Notes |
|--------|-------|
| `graph/client.py` | **`NeptuneClient` itself** — delete last |
| `graph/store.py` / `neo4j_store.py` | Mentions in docs/comments |
| `graph/attr_meta_migration.py` | CLI still `--backend neptune\|fuseki` |

### NL (2)

| Module | Notes |
|--------|-------|
| `nlp/pipeline.py` | Still constructs/holds a client for residual helpers + Cypher label lookup; SPARQL `ask` body gone |
| `nlp/ontology_embeddings.py` | Residual SPARQL ontology fetch shapes |

### QC (4)

| Module | Notes |
|--------|-------|
| `qc/__main__.py` / `scenario.py` | CLI constructs `NeptuneClient` for Fuseki QC |
| `qc/audit.py` / `isolation.py` | Residual |

---

## 3. Residual SPARQL / legacy tooling (non-importer)

| Path | Action for later slices |
|------|-------------------------|
| `graph/parser.py` | SPARQL-JSON bindings — quarantine→delete when no reader left |
| `graph/sparql_scope.py` | Keep while any NL/test SPARQL confinement remains |
| `graph/queries.py` | Keep URI helpers; drop SPARQL string builders when unused |
| `nlp/_generate_sparql` + prompts/validator | Delete when no unit test needs them |
| `scripts/local_sparql.py` | Pyoxigraph stand-in — archive/delete |
| `scripts/decomp_harness.py` | Fuseki harness — archive/delete |
| `scripts/neptune_to_neo4j_etl.py` | Migration archaeology — keep until ETL unused |
| `docker-compose.yml` `fuseki` service | Already `profiles: [legacy-sparql]` — remove when QC ported |
| `config.neptune_endpoint` | Drop with client |

---

## 4. Suggested follow-up slices

1. **Slice 2 — collapse dual-route SPARQL arms**  
   Explore / ontology / KG / grep: on `GraphConfigError`, return 503 (no store)
   instead of SPARQL fallthrough. Port remaining live scans to GraphStore.

2. **Slice 3 — DI rename**  
   `get_neptune_client` → gone; inject GraphStore only. Drop
   `app.state.neptune_client` construction.

3. **Slice 4 — delete substrate**  
   `NeptuneClient`, SPARQL parser (if unused), Fuseki compose profile, QC Fuseki
   when QC is GraphStore-native.

4. **Slice 5 — NL cleanup**  
   Delete `_generate_sparql` and SPARQL prompts/validator; rename response field
   `sparql` → `query`/`cypher` (API hard break — coordinate clients).

5. **Slice 6 — rename archaeology**  
   Comments saying “Neptune” → “graph” where still accurate.

---

## 5. Guards already in tree

| Guard | Role |
|-------|------|
| `tests/test_neo4j_only_backend.py` | One `graph_backend()`; reject legacy env; ratchet NeptuneClient importers |
| `tests/test_query_neo4j_hard_break.py` | Public `/query` `/update` `/triples` → 410 |
| `tests/test_ask_cypher_pipeline.py` | Cypher `/ask` path + ONTA-534 fail-closed on `use_cypher=False` |
| Write/retrieval convergence tests | Unrelated but must stay green |

---

## 6. Out of scope (do not purge blindly)

- GeoSPARQL WKT datatype IRIs (OGC, not Amazon Neptune)
- IRI namespace `https://graph.infona.ai/…`
- Write-path / retrieval convergence guards (retarget, do not delete)
- Historical parent-repo docs under `docs/plans/neo4j-*.md`

---

*Generated for ONTA-534 slice 1. Next slice: dual-route SPARQL fallthrough removal.*

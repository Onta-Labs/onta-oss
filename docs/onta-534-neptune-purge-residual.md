# ONTA-534 — Neptune / SPARQL purge residual inventory

**Ticket:** ONTA-534  
**Predecessor:** ONTA-527 (Neo4j-only backend + SPARQL test purge, #327)  
**Updated:** 2026-08-12 (slice 2)  
**Branch:** `feat/onta-534-neptune-purge-complete`

Production is **Neo4j-only** (Amazon Neptune decommissioned 2026-08-11).
ONTA-527 removed the backend switch, made store resolution fail-closed, and
410'd public SPARQL HTTP. Slice 1 retired NL→SPARQL `/ask`. This document is
the residual map + what has shipped.

---

## 1. Shipped slices

| Change | Status |
|--------|--------|
| **NL→SPARQL production path fail-closed** | Done (slice 1, #353) — `SparqlAskPathRetired` on `use_cypher=False` |
| **Dead SPARQL body removed from `ask()`** | Done (slice 1) |
| **Legacy backend fail-closed** | ONTA-527 — `graph_backend()` rejects non-neo4j |
| **`select_entity_uris` no SPARQL HTTP** | Done (slice 2) — GraphStore Cypher / deterministic fixtures; never `neptune.query` |
| **`NeptuneClient` fail-closed under GraphStore** | Done (slice 2) — `SparqlClientRetired` on query/update/ask/batch_exists when process store is configured (escape: `allow_http=True` / `INFONA_SPARQL_HTTP_ENABLED=1`) |
| **Dual-route SPARQL fallthrough → 503** | Done (slice 2) — explore type summary / records / entity detail, grep, knowledge_graphs type usage |
| **`kg_data_status` / `list_kg_names` GraphStore-first** | Done (slice 2) — real `NeptuneClient` uses registry + entity counts; duck-typed test doubles keep SPARQL ASKs |
| **KG `/schema` + `/type-edges` GraphStore-first** | Done (inspect #5) — compose type_counts + type_summary; MCP `inspect_graph_schema` no longer 500 under Neo4j; real `NeptuneClient` empty KG stays GraphStore (no SPARQL hang) |
| **`list_type_schema` catalog-first** | Done (inspect #5) — agent ontology inspect uses ontology catalog under GraphStore |
| **Hermetic suite** | Must stay green |

---

## 2. Residual `NeptuneClient` importers (ratchet)

Enforced by `tests/test_neo4j_only_backend.py::_RESIDUAL_NEPTUNE_IMPORTERS`
(**may shrink, never grow**). Count at slice 2: still **~38** modules under
`infona_client/` (type-hints + residual arms; HTTP execution gated).

### API routes — DI still injects client; GraphStore answers product reads

Handlers still declare `Depends(get_neptune_client)`. Product paths prefer
GraphStore; residual SPARQL arms either 503, raise `SparqlClientRetired`, or
are dead after an early return.

| Module | Notes |
|--------|-------|
| `api/app.py` | Constructs vestigial `NeptuneClient` on lifespan (opens no connection; HTTP fail-closed when store configured) |
| `api/deps.py` | `get_neptune_client` DI — collapse to GraphStore-only in a later slice |
| `api/routes/ask.py` | Passes client into pipeline + `kg_data_status` (store-first) |
| `api/routes/agent.py` | Same pattern |
| `api/routes/explore.py` | GraphStore-first; dual arms 503 / 404 instead of SPARQL hang |
| `api/routes/ontology.py` | Catalog dual path residual |
| `api/routes/knowledge_graphs.py` | Type usage GraphStore-only; other lifecycle dual residual |
| `api/routes/grep.py` | GraphStore scan; SPARQL fallback retired → 503 |
| `api/routes/export.py` | Declares Depends; export is GraphStore |
| `api/routes/actions.py` / `corrections.py` / `enrich.py` / `functions.py` / `ingest.py` / `lambda_functions.py` / `normalize.py` / `operator.py` | Residual type-hints / arms |

### Writers / resolvers / enrichment (11)

Still type-hint or duck-call `neptune` for residual SPARQL reads. Under a
configured GraphStore a **real** `NeptuneClient` fails closed; tests use
duck-typed fakes (`PyoxiNeptune`, `AsyncMock`) that are not gated.

### Graph substrate / NL / QC

| Module | Notes |
|--------|-------|
| `graph/client.py` | **`NeptuneClient` itself** — delete last |
| `nlp/pipeline.py` | Holds client for residual helpers; `ask` + `select_entity_uris` no SPARQL exec |
| `nlp/ontology_embeddings.py` | Residual SPARQL ontology fetch shapes |
| `qc/*` | CLI constructs client for Fuseki QC — set `INFONA_SPARQL_HTTP_ENABLED=1` |

---

## 3. Residual SPARQL / legacy tooling

| Path | Action for later slices |
|------|-------------------------|
| `graph/parser.py` | SPARQL-JSON bindings — keep while residual readers remain |
| `graph/sparql_scope.py` | Keep while unit tests call confinement helpers |
| `graph/queries.py` | Keep URI helpers; drop SPARQL string builders when unused |
| `nlp/_generate_sparql` + prompts/validator | Delete when no unit test needs them |
| `scripts/local_sparql.py` | Quarantined banner (slice 1) — archive/delete |
| `scripts/decomp_harness.py` | Quarantined — archive/delete |
| `scripts/neptune_to_neo4j_etl.py` | Migration archaeology — keep until unused |
| `docker-compose.yml` `fuseki` service | `profiles: [legacy-sparql]` |
| `config.neptune_endpoint` | Drop with client |
| Dead SPARQL arms in explore/kg routes | Delete when no dual-arm tests remain |

---

## 4. Suggested follow-up slices

1. **DI rename** — `get_neptune_client` → gone; inject GraphStore only; drop
   `app.state.neptune_client` construction.
2. **Delete substrate** — `NeptuneClient`, Fuseki compose profile, QC Fuseki
   when QC is GraphStore-native.
3. **NL cleanup** — Delete `_generate_sparql` and SPARQL prompts/validator;
   rename response field `sparql` → `query`/`cypher` (API hard break).
4. **Enrichment / normalize residual SPARQL** — port remaining
   `executor` / `normalization` reads that still duck-call SPARQL on fakes.
5. **Rename archaeology** — comments saying “Neptune” → “graph”.

---

## 5. Guards already in tree

| Guard | Role |
|-------|------|
| `tests/test_neo4j_only_backend.py` | One `graph_backend()`; reject legacy env; ratchet NeptuneClient importers; ask fail-closed; **slice 2:** SparqlClientRetired under store |
| `tests/test_query_neo4j_hard_break.py` | Public `/query` `/update` `/triples` → 410 |
| `tests/test_ask_cypher_pipeline.py` | Cypher `/ask` path + ONTA-534 fail-closed |
| Write/retrieval convergence tests | Unrelated but must stay green |

---

## 6. Out of scope (do not purge blindly)

- GeoSPARQL WKT datatype IRIs (OGC, not Amazon Neptune)
- IRI namespace `https://graph.infona.ai/…`
- Write-path / retrieval convergence guards (retarget, do not delete)
- Historical parent-repo docs under `docs/plans/neo4j-*.md`

---

*ONTA-534 slice 2. Full `NeptuneClient` class deletion remains multi-PR residual.*

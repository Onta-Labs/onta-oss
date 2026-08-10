# Neo4j local / CI (Wave 1 GraphStore)

Companion to [ADR 0012](../../docs/adr/0012-neo4j-cypher-migration.md) and
**[ADR 0013 — RDF semantics in Neo4j](../../docs/adr/0013-rdf-semantics-in-neo4j.md)**
(assertion-centric model). Implementer contract:
[`docs/plans/neo4j-rdf-semantic-model.md`](../../docs/plans/neo4j-rdf-semantic-model.md)
in the parent monorepo (supersedes Wave‑1 “props + typed rels only” as instance
SoT). This OSS package owns the **GraphStore protocol**, the official Python
driver adapter, schema bootstrap, Assertion write path, RDFS helpers, and Docker
Compose service.

### ADR 0013 model (short)

* **Labels:** `Entity`, `Class`, `Property`, `Assertion` (plus legacy
  `OntoType` / `OntoAttr` catalog until cutover).
* **Instance truth:** `:Assertion` nodes — `(a)-[:SUBJECT]->(:Entity)`,
  `(a)-[:PREDICATE]->(:Property)`, object via `[:OBJECT]->(:Entity)` or
  `literal_value` / type via `[:OBJECT_CLASS]->(:Class)`.
* **Provenance on Assertion:** `source_url`, `verified_at`, `run_id`,
  `confidence`.
* **Derived only:** Entity property cache, typed shortcut rels, `INSTANCE_OF`
  (dual-written from type Assertions by `kg_writer` / `pg_ops`).
* **Helpers:** `infona_client.graph.rdfs_helpers` (Python + Cypher templates) —
  compose these; do not 1:1 translate SPARQL.
* **Identity:** Entity/Class/Property `id` = RDF-compatible IRI strings
  (`entity_uri` / `type_uri` / `property_uri`); Assertion id =
  `make_assertion_id` / `mint_assertion_id` (SHA-256 of s|p|o|source).

## Quick start

```bash
# From infona-oss (this repo)
docker compose up -d neo4j
pip install -e ".[neo4j]"

export NEO4J_URI=bolt://localhost:7687
export NEO4J_USER=neo4j
export NEO4J_PASSWORD=infona-dev-password
```

Wait until healthy (`docker compose ps` shows `neo4j` healthy, or open
http://localhost:7474).

## Schema bootstrap (required before uniqueness-sensitive writes)

Uniqueness (ADR 0013 §12): `(tenant_id, kg, id)` on `Entity`, `Class`,
`Property`, and `Assertion`. Apply once per database (idempotent
`IF NOT EXISTS`):

```python
import asyncio
from infona_client.graph.store import get_graph_store

async def main():
    store = get_graph_store()
    assert await store.health()
    names = await store.bootstrap_schema()
    print("applied/present:", names)
    await store.close()

asyncio.run(main())
```

Statement list: `infona_client.graph.schema_bootstrap.SCHEMA_STATEMENTS`.

## Scoped session (isolation)

```python
from infona_client.graph.scope import GraphScope
from infona_client.graph.store import get_graph_store
from infona_client.graph.ontology_queries import entity_uri
import asyncio
from datetime import datetime, timezone

async def smoke():
    store = get_graph_store()
    await store.bootstrap_schema()
    scope = GraphScope.for_instance("demo-tenant", "bookstore")
    session = store.session(scope)
    eid = entity_uri("Book", "lotr")
    ts = datetime.now(timezone.utc).isoformat()
    # Prefer allowlisted templates for application writers:
    await session.execute_template(
        "entity_merge",
        {
            "id": eid,
            "primary_type": "Book",
            "name": "The Fellowship of the Ring",
            "source": "smoke",
            "ts": ts,
            # Intentionally wrong scope — session overwrites these:
            "tenant_id": "evil-tenant",
            "kg": "other-kg",
        },
    )
    rows = await session.execute_template("entity_get", {"id": eid})
    print(rows[0].to_dict())
    await store.close()

asyncio.run(smoke())
```

### What the session enforces

1. **Parameter tokens (always):** Cypher must reference `$tenant_id` and `$kg`
   as **whole** parameter names (word-boundary). `$kg_name` does **not**
   satisfy `$kg`.
2. **Session overwrites** caller-supplied `tenant_id` / `kg` parameters
   (never trust client- or model-supplied scope).
3. **Non-privileged heuristic:** free-form Cypher containing `MATCH` /
   `MERGE` / `CREATE` must also include map-style property keys `tenant_id:`
   and `kg:` in the query text. A red-team pattern that only *mentions*
   `$tenant_id` / `$kg` in `WHERE` without those property keys is rejected.
4. **Global catalog writes** require `GraphScope(..., privileged=True)`.
5. **Entity MERGE/CREATE** that bind `$id` fail closed if `id` is missing or
   blank (`require_entity_write_identity`) before the driver round-trip.
6. Parameterized Cypher only at this layer (no user values concatenated into
   query structure except allowlisted domain labels — see below).

### Isolation is NOT complete for arbitrary Cypher (read this)

Wave 1 does **not** rewrite free-form Cypher. The session gates above are
defense-in-depth heuristics:

| Path | Safety |
|------|--------|
| **`session.execute_template(name, params)`** | **Safe for app code.** Only Cypher from `schema_bootstrap.TEMPLATES` runs; entity-write templates enforce identity. |
| **Future `kg_writer` Neo4j port** | **Safe for app code** (same allowlisted / structured writers). |
| **`execute_read` / `execute_write` free-form** | **Admin / bootstrap / tests only.** Residual risk: a query can include `tenant_id:` / `kg:` tokens and still not bind session params into every pattern; a true Cypher AST rewriter is out of scope for Wave 1. |
| **`privileged=True` scope** | Free-form still requires `$tenant_id` / `$kg` tokens but **skips** the property-key heuristic so break-glass admin queries can run. Treat as highly trusted. |

**Application writers MUST use templates (or kg_writer) only.** Do not paste
ad-hoc Cypher into product paths and assume multi-tenant isolation.

Remaining work for a true rewriter: parse Cypher, inject
`{tenant_id: $tenant_id, kg: $kg}` (or equivalent predicates) on every node/rel
pattern, reject or rewrite label/rel-type injection, and drop free-form from
the non-privileged app surface entirely.

### Domain type labels (model B1)

Static `ENTITY_MERGE` does not set dynamic domain labels (Neo4j cannot
parameterize labels). After merge, call:

```python
from infona_client.graph.labels import set_entity_type_labels

await set_entity_type_labels(session, eid, ["Book"])  # sanitized, reserved-safe
```

Leaves are sanitized (`[^A-Za-z0-9_]` → `_`, digit prefix → `T_`) and rejected
if they collide with reserved system labels (`Entity`, `OntoType`, …).

## Explore / KG-admin reads (E5)

Module: `infona_client.graph.explore_store`. Dual-backend like `kg_writer` /
`ontology_catalog`: pass `store=` / `session=` or set
`INFONA_GRAPH_BACKEND=neo4j`; otherwise helpers return `None` so SPARQL explore
routes stay the default.

```python
from infona_client.graph.explore_store import (
    list_entities_by_type,
    get_entity_detail,
    type_counts,
    count_entities,
)

# Paged list via INSTANCE_OF→Class (match="primary_type" is historical name)
# or match="label"; type_name is ONTA-425 validated. include_subclasses expands
# Class SUBCLASS_OF when True.
page = await list_entities_by_type(
    store=store, tenant_id="demo-tenant", kg="bookstore",
    type_name="Person", limit=50, after_id=None,
)
detail = await get_entity_detail(
    store=store, tenant_id="demo-tenant", kg="bookstore", entity_id=eid,
)
counts = await type_counts(store=store, tenant_id="demo-tenant", kg="bookstore")
n = await count_entities(store=store, tenant_id="demo-tenant", kg="bookstore")
```

**KG list / registry:** full `:KgMeta` registry is deferred (model §10.1 B7).
Until then, `count_entities` is the minimal per-kg size signal when listing
from the instance store alone. Route smoke: `GET …/kgs/{kg}/type-counts`
already prefers GraphStore when the neo4j backend is active.

Allowlisted templates: `entity_list_by_type_page`, `entity_count_by_type`,
`entity_count_total`, `entity_detail`, `entity_rels` (+ existing
`entity_count_by_primary_type`).

## Write rails → GraphStore (E7)

Instance writers resolve the store once per write batch via
`resolve_optional_graph_store()` (`graph/store.py`) and pass `store=` into
`insert_facts` / `delete_facts` / `rewrite_subject`:

| Rail | Module |
|------|--------|
| Ingest (CSV / JSON / discovery) | `resolver/schema_resolver.py` |
| Enrichment | `enrichment/executor.py` |
| Normalization (promote_to_node, list_explode, strip_emoji) | `normalization/execute.py` |
| ER rebuild / merge | `resolver/er/rebuild.py` |

When `INFONA_GRAPH_BACKEND` is unset or `neptune`, the helper returns `None`
and rails keep the Neptune SPARQL path (no Neo4j credentials required). When
backend is `neo4j`, missing store config fails closed (`GraphConfigError`).

**Still SPARQL-only by design (this epic):** normalization SELECTs that find
candidates before the write, ER signal load (`SparqlBlocker`), ontology-graph
config rows (normalization rules/policy stores), companion RDF provenance on
the Neptune path. Full explore/admin rewrite is E9.

Hermetic tests: `tests/test_rails_graph_store_write.py` (MemoryGraphStore).

## NL → Cypher /ask (ADR 0013 semantic helpers)

When `INFONA_GRAPH_BACKEND=neo4j`, `POST /graphs/{tenant}/ask` (and
`NLQueryPipeline.ask`) generate **Cypher over the RDF-semantic model** instead
of SPARQL and execute via GraphStore. **Default remains Neptune SPARQL** when
the env var is unset or `neptune`.

**Quality bar:** answers are measured by the **golden-query suite** (expected
answer sets vs gold) — **not** by SPARQL string match or SPARQL↔Cypher text
equivalence. See parent-repo `docs/plans/neo4j-golden-queries.md` and ADR 0013.
Do **not** build SPARQL→Cypher translators; fixtures and the LLM compose
allowlisted semantic helpers.

```bash
export INFONA_GRAPH_BACKEND=neo4j
export NEO4J_URI=bolt://localhost:7687
export NEO4J_USER=neo4j
export NEO4J_PASSWORD=infona-dev-password
# optional: OPENROUTER_API_KEY for full LLM Cypher; without it, hermetic
# deterministic fixtures answer count / list / property-eq / 1-hop.
```

Or per call: `pipeline.ask(..., use_cypher=True)` with an injected
`graph_store=` (tests use `MemoryGraphStore`).

**What ships:**

| Piece | Module |
|-------|--------|
| Cypher system + user prompts (Assertion model + helpers) | `nlp/prompts.py` (`CYPHER_GENERATION_SYSTEM`) |
| Scope inject / reject / scrub | `nlp/cypher_scope.py` |
| Deterministic fixtures → semantic templates | `nlp/cypher_generate.py` |
| Semantic helper templates + subclass closure | `graph/rdfs_helpers.py` |
| Template registry | `graph/schema_bootstrap.py` |
| Example bank optional `cypher` field | `nlp/example_bank.py` |
| Pipeline branch (catalog ontology, template prefer, 1× retry) | `nlp/pipeline.py` (`_ask_cypher`) |
| Allowlisted NL semantic templates | `entities_of_type`, `entities_of_type_count`, `literal_values`, `related_entities`, `assertions_for_subject`, `subclass_of_closure` |
| Explore legacy templates (still registered) | `entity_count_*`, `entity_list_by_type_page`, … |

Generated Cypher is confined before run: read-only, `$tenant_id`/`$kg` required
(or bare `(e:Entity)` repaired), session **overwrites** model-supplied
tenant/kg params. Prefer allowlisted **semantic** templates when the fixture
(or LLM payload) names one; free-form uses parameterized `execute_read` only.
Ontology for Cypher prefers `ontology_catalog.schema_types_for_kg` when a
GraphStore is present. Type fixtures expand subclass closure into `$type_names`
when the ontology summary carries `parent:` lines.

**Not yet:** multi-hop joins, full Assertion SoT dual-write in writers, enum
recovery, eval rebaseline against golden suite CI, richer NL coverage.
## Tests

```bash
# Hermetic (default CI) — in-memory store + scope unit tests + ADR 0013
pytest tests/test_graph_store.py tests/test_explore_store.py \
  tests/test_kg_writer_store.py tests/test_rails_graph_store_write.py \
  tests/test_rdf_semantic_model.py \
  tests/test_cypher_scope.py tests/test_cypher_prompts.py \
  tests/test_example_bank_cypher.py tests/test_ask_cypher_pipeline.py -q

# Live Neo4j smoke (compose up first)
NEO4J_URI=bolt://localhost:7687 NEO4J_USER=neo4j NEO4J_PASSWORD=infona-dev-password \
  pytest -m neo4j -q
```

## Env vars (BYOK)

| Variable | Default | Meaning |
|----------|---------|---------|
| `NEO4J_URI` | — | Bolt/Neo4j URI (required for auto factory) |
| `NEO4J_USER` | `neo4j` | Username |
| `NEO4J_PASSWORD` | — | Password (required with URI) |
| `NEO4J_DATABASE` | driver default | Optional DB name (Wave 1: single DB) |
| `INFONA_GRAPH_BACKEND` | `neptune` | Set to `neo4j` to enable GraphStore writers/readers and NL→Cypher `/ask` |

No platform or AWS-managed credentials are embedded in this package.

## Public SPARQL hard-break (E9 / ADR 0012 L2)

When `INFONA_GRAPH_BACKEND=neo4j`, the **public** raw SPARQL HTTP surfaces are
**gone** — not shimmed:

| Route | Neptune (default) | Neo4j |
|-------|-------------------|--------|
| `POST /graphs/{tenant}/query` | Scoped SPARQL SELECT/ASK/… | **410 Gone** |
| `POST /graphs/{tenant}/update` | Operator SPARQL Update | **410 Gone** |

Response body points callers at the agent, SDK, and high-level APIs
(`/ask`, `/agent`, `/triples`, `/kgs`, ingest, explore). There is **no** SPARQL
compatibility façade over Neo4j.

**Not deleted:** SPARQL client code, `sparql_scope`, internal Neptune readers,
and the route modules themselves remain for Neptune deployments and remaining
internal paths. Only the public HTTP contract hard-breaks in neo4j mode.

Hermetic tests: `tests/test_query_neo4j_hard_break.py`.

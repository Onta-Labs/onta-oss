# Neo4j local / CI (Wave 1 GraphStore)

Companion to [ADR 0012](../../docs/adr/0012-neo4j-cypher-migration.md) and the
[property-graph model](../../docs/plans/neo4j-property-graph-model.md) in the
parent monorepo. This OSS package owns the **GraphStore protocol**, the
official Python driver adapter, schema bootstrap, and Docker Compose service.

## Quick start

```bash
# From cograph-oss (this repo)
docker compose up -d neo4j
pip install -e ".[neo4j]"

export NEO4J_URI=bolt://localhost:7687
export NEO4J_USER=neo4j
export NEO4J_PASSWORD=onta-dev-password
```

Wait until healthy (`docker compose ps` shows `neo4j` healthy, or open
http://localhost:7474).

## Schema bootstrap (required before uniqueness-sensitive writes)

Model §7 uniqueness is `(Entity.tenant_id, Entity.kg, Entity.id)`. Apply once
per database (idempotent `IF NOT EXISTS`):

```python
import asyncio
from cograph_client.graph.store import get_graph_store

async def main():
    store = get_graph_store()
    assert await store.health()
    names = await store.bootstrap_schema()
    print("applied/present:", names)
    await store.close()

asyncio.run(main())
```

Statement list: `cograph_client.graph.schema_bootstrap.SCHEMA_STATEMENTS`.

## Scoped session (isolation)

```python
from cograph_client.graph.scope import GraphScope
from cograph_client.graph.schema_bootstrap import ENTITY_MERGE_CYPHER, ENTITY_GET_CYPHER
from cograph_client.graph.store import get_graph_store
from cograph_client.graph.ontology_queries import entity_uri
import asyncio
from datetime import datetime, timezone

async def smoke():
    store = get_graph_store()
    await store.bootstrap_schema()
    scope = GraphScope.for_instance("demo-tenant", "bookstore")
    session = store.session(scope)
    eid = entity_uri("Book", "lotr")
    ts = datetime.now(timezone.utc).isoformat()
    await session.execute_write(
        ENTITY_MERGE_CYPHER,
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
    rows = await session.execute_read(ENTITY_GET_CYPHER, {"id": eid})
    print(rows[0].to_dict())
    await store.close()

asyncio.run(smoke())
```

Rules enforced by every session:

1. Cypher must reference `$tenant_id` and `$kg` or the call is rejected.
2. Session **overwrites** caller-supplied `tenant_id` / `kg` parameters.
3. Writes with `tenant_id=__global__` require `GraphScope(..., privileged=True)`.
4. Parameterized Cypher only at this layer.

## Tests

```bash
# Hermetic (default CI) — in-memory store + scope unit tests
pytest tests/test_graph_store.py -q

# Live Neo4j smoke (compose up first)
NEO4J_URI=bolt://localhost:7687 NEO4J_USER=neo4j NEO4J_PASSWORD=onta-dev-password \
  pytest -m neo4j -q
```

## Env vars (BYOK)

| Variable | Default | Meaning |
|----------|---------|---------|
| `NEO4J_URI` | — | Bolt/Neo4j URI (required for auto factory) |
| `NEO4J_USER` | `neo4j` | Username |
| `NEO4J_PASSWORD` | — | Password (required with URI) |
| `NEO4J_DATABASE` | driver default | Optional DB name (Wave 1: single DB) |

No platform or AWS-managed credentials are embedded in this package.

"""Local SPARQL store for development — no Docker, no Java.

Wraps an embedded pyoxigraph Store behind the three HTTP paths the
`fuseki` backend of NeptuneClient expects (/ds/query, /ds/update,
/$/ping), so the infona API server runs against it unchanged:

    python scripts/local_sparql.py                 # in-memory
    python scripts/local_sparql.py --data ./graph  # persisted to disk

then start the API with:

    INFONA_GRAPH_BACKEND=fuseki INFONA_NEPTUNE_ENDPOINT=http://localhost:3030 \
        uvicorn infona_client.api.app:app --port 8000

Dataset semantics:
- Queries **with** ``FROM`` / ``FROM NAMED`` honor those clauses (named-graph
  isolation). This is required for multi-tenant local dogfood — unioning all
  graphs would silently leak across tenants and ontology layers.
- Queries **without** a dataset clause still use the default graph as the
  union of all named graphs, matching Neptune's default-graph semantics for
  the single-tenant quickstart path.
"""

from __future__ import annotations

import argparse
import re

import uvicorn
from fastapi import FastAPI, Form, Response
from pyoxigraph import NamedNode, QueryBoolean, QueryResultsFormat, RdfFormat, Store

app = FastAPI(title="infona local SPARQL store")
store: Store

# SPARQL dataset clauses. FROM NAMED must be stripped before matching bare FROM.
_FROM_NAMED_RE = re.compile(r"FROM\s+NAMED\s*<([^>]+)>", re.IGNORECASE)
_FROM_RE = re.compile(r"FROM\s*<([^>]+)>", re.IGNORECASE)


def _dataset_from_query(query: str) -> tuple[list[NamedNode] | None, list[NamedNode] | None]:
    """Return (default_graph, named_graphs) for pyoxigraph, or (None, None)."""
    named_iris = _FROM_NAMED_RE.findall(query)
    # Remove FROM NAMED clauses so the bare FROM regex does not double-count them.
    stripped = _FROM_NAMED_RE.sub(" ", query)
    default_iris = _FROM_RE.findall(stripped)
    if not default_iris and not named_iris:
        return None, None
    default_graph = [NamedNode(iri) for iri in default_iris] or None
    named_graphs = [NamedNode(iri) for iri in named_iris] or None
    return default_graph, named_graphs


@app.get("/$/ping")
def ping() -> dict:
    return {"status": "ok"}


@app.post("/ds/query")
def query(query: str = Form(...)) -> Response:
    default_graph, named_graphs = _dataset_from_query(query)
    if default_graph is not None or named_graphs is not None:
        results = store.query(
            query,
            use_default_graph_as_union=False,
            default_graph=default_graph,
            named_graphs=named_graphs,
        )
    else:
        # No dataset clause — Neptune-style union default graph for single-tenant use.
        results = store.query(query, use_default_graph_as_union=True)
    if isinstance(results, QueryBoolean):
        payload = results.serialize(format=QueryResultsFormat.JSON)
        return Response(payload, media_type="application/sparql-results+json")
    if hasattr(results, "variables"):  # SELECT -> QuerySolutions
        payload = results.serialize(format=QueryResultsFormat.JSON)
        return Response(payload, media_type="application/sparql-results+json")
    # CONSTRUCT / DESCRIBE -> QueryTriples
    payload = results.serialize(format=RdfFormat.N_TRIPLES)
    return Response(payload, media_type="application/n-triples")


@app.post("/ds/update")
def update(update: str = Form(...)) -> dict:
    store.update(update)
    return {"status": "ok"}


def main() -> None:
    global store
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=3030)
    parser.add_argument(
        "--data", default=None,
        help="Directory to persist the store (default: in-memory)",
    )
    args = parser.parse_args()
    store = Store(args.data) if args.data else Store()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

"""Real-store (pyoxigraph) tests for the MULTI-VALUE scope resolver
(``EnrichmentExecutor.select_scope_value_uris``).

Validates the ACTUAL generated SPARQL against a genuine SPARQL engine — the
regression guard for the persona-eval refresh gap: a scoped refresh over a LIST
of values ("refresh pricing for OpenAI, Google, Deepgram and ElevenLabs") was
extracted as a single crammed literal scope
(``provided_by = "OpenAI, Google, Deepgram, ElevenLabs"``), matched 0 existing
records, premature-clarified, and the caller fell into a fresh discovery build.
The fix resolves the value SET to the concrete entity IRIs whose scope value is a
case/normalization-insensitive MEMBER of the set — matched here against real RDF.

Uses invented types/attrs/values only (no persona tokens). Skipped where
pyoxigraph is not installed (it is not a declared CI test dep); runs in local dev.
"""
from __future__ import annotations

import json

import pytest

pyoxigraph = pytest.importorskip("pyoxigraph")
from pyoxigraph import QueryResultsFormat, Store  # noqa: E402

from cograph_client.enrichment.cache import EnrichmentCache  # noqa: E402
from cograph_client.enrichment.executor import EnrichmentExecutor  # noqa: E402
from cograph_client.enrichment.job_store import InMemoryJobStore  # noqa: E402
from cograph_client.graph.ontology_queries import attr_uri, type_uri  # noqa: E402
from cograph_client.graph.queries import (  # noqa: E402
    kg_graph_uri,
    tenant_graph_uri,
)

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
RDF_PROPERTY = "http://www.w3.org/1999/02/22-rdf-syntax-ns#Property"
RDFS_DOMAIN = "http://www.w3.org/2000/01/rdf-schema#domain"
ONTO = "https://cograph.tech/onto/"
ENT = "https://cograph.tech/entities/"
TENANT, KG, TYPE = "scope-vals-pyoxi", "k1", "Widget"


class PyoxiNeptune:
    """Minimal NeptuneClient shim over an in-process pyoxigraph Store — async
    query()/update() returning SPARQL-1.1 JSON, union-of-named-graphs default
    matching the production backend."""

    def __init__(self) -> None:
        self.store = Store()

    async def query(self, sparql: str) -> dict:
        results = self.store.query(sparql, use_default_graph_as_union=True)
        return json.loads(results.serialize(format=QueryResultsFormat.JSON))

    async def update(self, sparql: str) -> None:
        self.store.update(sparql)


def _executor(n: PyoxiNeptune) -> EnrichmentExecutor:
    class _NoAdapter:
        name = "none"
        is_paid = False

        async def lookup(self, *a, **k):
            return []

    return EnrichmentExecutor(n, InMemoryJobStore(), EnrichmentCache(), _NoAdapter())


async def _seed_literal_scope(n: PyoxiNeptune) -> None:
    """Three Widgets with a literal ``made_by`` in DIFFERENT casing/spacing than a
    caller would type, plus the ontology declaration so the predicate resolves."""
    kgg, onto = kg_graph_uri(TENANT, KG), tenant_graph_uri(TENANT)
    made_by = attr_uri(TYPE, "made_by")
    await n.update(
        f'INSERT DATA {{ GRAPH <{onto}> {{ '
        f'<{made_by}> <{RDF_TYPE}> <{RDF_PROPERTY}> ; '
        f'<{RDFS_DOMAIN}> <{type_uri(TYPE)}> ; '
        f'<{RDFS_LABEL}> "made_by" . }} }}'
    )
    await n.update(
        f'INSERT DATA {{ GRAPH <{kgg}> {{ '
        f'<{ENT}{TYPE}/w1> <{RDF_TYPE}> <{type_uri(TYPE)}> ; <{made_by}> "acme corp" . '
        f'<{ENT}{TYPE}/w2> <{RDF_TYPE}> <{type_uri(TYPE)}> ; <{made_by}> "GLOBEX" . '
        f'<{ENT}{TYPE}/w3> <{RDF_TYPE}> <{type_uri(TYPE)}> ; <{made_by}> "Umbrella" . '
        f'}} }}'
    )


@pytest.mark.asyncio
async def test_select_scope_value_uris_matches_case_insensitive_set():
    """A LIST of values matches the existing records whose literal scope value is a
    case/normalization-insensitive MEMBER of the set — the crammed-literal bug's
    fix. "Acme Corp" (typed) matches "acme corp" (stored); "Initech" (no record)
    is simply absent; "Umbrella" is NOT in the set so it is excluded."""
    n = PyoxiNeptune()
    await _seed_literal_scope(n)
    ex = _executor(n)
    uris = await ex.select_scope_value_uris(
        TENANT, KG, TYPE, "made_by",
        ["Acme Corp", "globex", "Initech"],  # mixed casing; one absent
    )
    assert sorted(uris) == [f"{ENT}{TYPE}/w1", f"{ENT}{TYPE}/w2"], uris


@pytest.mark.asyncio
async def test_select_scope_value_uris_matches_relationship_target_label():
    """When the scope predicate is a RELATIONSHIP to a node, the value set matches
    the target node's rdfs:label (case-insensitively) — so a subset named by the
    related entity's display name resolves."""
    n = PyoxiNeptune()
    kgg, onto = kg_graph_uri(TENANT, KG), tenant_graph_uri(TENANT)
    # made_by is a RELATIONSHIP (onto/<leaf>) pointing at a Vendor node with a label.
    rel = f"{ONTO}made_by"
    await n.update(
        f'INSERT DATA {{ GRAPH <{kgg}> {{ '
        f'<{ENT}{TYPE}/w1> <{RDF_TYPE}> <{type_uri(TYPE)}> ; <{rel}> <{ENT}Vendor/v1> . '
        f'<{ENT}Vendor/v1> <{RDFS_LABEL}> "Deepgram" . '
        f'<{ENT}{TYPE}/w2> <{RDF_TYPE}> <{type_uri(TYPE)}> ; <{rel}> <{ENT}Vendor/v2> . '
        f'<{ENT}Vendor/v2> <{RDFS_LABEL}> "Cartesia" . }} }}'
    )
    ex = _executor(n)
    uris = await ex.select_scope_value_uris(
        TENANT, KG, TYPE, "made_by", ["deepgram"]  # lowercase; label is "Deepgram"
    )
    assert uris == [f"{ENT}{TYPE}/w1"], uris


@pytest.mark.asyncio
async def test_select_scope_value_uris_empty_on_no_match_and_bad_predicate():
    """No member matches → [] (caller fails closed to a clarify, not a whole-type
    enrich); an unresolvable/empty predicate → [] fast (no scan)."""
    n = PyoxiNeptune()
    await _seed_literal_scope(n)
    ex = _executor(n)
    # Values that match nothing.
    assert await ex.select_scope_value_uris(
        TENANT, KG, TYPE, "made_by", ["nobody", "nothing"]
    ) == []
    # Empty value set.
    assert await ex.select_scope_value_uris(TENANT, KG, TYPE, "made_by", []) == []

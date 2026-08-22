"""ONTA-534: the INGEST ontology read comes from the GraphStore catalog.

``SchemaResolver._fetch_ontology`` is the read every ingest starts with — the
snapshot of "what this workspace already declares" that type resolution,
attribute resolution and the ONTA-270 version stamp all plan against. It read
``get_full_ontology_query`` over the SPARQL HTTP client, which is RETIRED under
the shipped Neo4j GraphStore, so the read raised ``SparqlClientRetired``, the
bare ``except`` logged ``ontology_fetch_failed``, and the ingest continued with
``({}, {})``.

An empty snapshot is not a degraded snapshot here. ``TypeMatcher.match``
short-circuits on ``if not existing_types:`` and returns
``is_new=True`` (``type_match_auto_new``, ``reason="empty_ontology"``) without
consulting name, cache, embeddings or LLM — so every type an ingest proposes is
re-minted from scratch instead of matched onto the one the workspace already
has. That is ontology fragmentation, and it is silent.

These tests pin both arms: the catalog arm (declared types/attributes out of a
seeded ``MemoryGraphStore``, with SPARQL wired to explode), the residual SPARQL
arm (still consulted when the store has nothing to say), the exact
``(types, attrs)`` shape the rest of the resolver depends on, and the
consequence end-to-end — with a type declared, the matcher must not take the
empty-ontology branch.

Anti-overfit: synthetic type / attribute names only.
"""

from __future__ import annotations

import pathlib

import pytest

from infona_client.graph.client import SparqlClientRetired
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_catalog import upsert_attribute, upsert_type
from infona_client.graph.queries import tenant_graph_uri
from infona_client.graph.store import configure_graph_store
from infona_client.resolver.schema_resolver import SchemaResolver
from infona_client.resolver.verdict_cache import JsonVerdictCache

pytestmark = pytest.mark.asyncio

TENANT = "ingest-onto-tenant"
ONTOLOGY_GRAPH = tenant_graph_uri(TENANT)

T_ALPHA = "IngestOntoAlpha"
T_BETA = "IngestOntoBeta"
A_CODE = "ingest_onto_code"
A_MASS = "ingest_onto_mass"
A_WHEN = "ingest_onto_when"
R_HOLDS = "ingest_onto_holds"

XSD = "http://www.w3.org/2001/XMLSchema"


class RetiredSparqlNeptune:
    """Stands in for the shipped client: every SPARQL read is retired."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def query(self, sparql: str):
        self.queries.append(sparql)
        raise SparqlClientRetired(
            "SPARQL HTTP client is retired under Neo4j GraphStore (ONTA-534)."
        )

    @property
    def schema_reads(self) -> list[str]:
        """The full-ontology read, identified by its own projection."""
        return [q for q in self.queries if "?typeLabel" in q]


class SchemaSparqlNeptune:
    """Residual SPARQL arm: answers the full-ontology read and nothing else."""

    def __init__(self, type_name: str, attr_name: str, range_uri: str) -> None:
        self.type_name = type_name
        self.attr_name = attr_name
        self.range_uri = range_uri
        self.schema_calls = 0

    async def query(self, sparql: str):
        if "?typeLabel" in sparql:
            self.schema_calls += 1
            return {
                "head": {"vars": ["typeLabel", "attrLabel", "range"]},
                "results": {
                    "bindings": [
                        {
                            "typeLabel": {"type": "literal", "value": self.type_name},
                            "attrLabel": {"type": "literal", "value": self.attr_name},
                            "range": {"type": "uri", "value": self.range_uri},
                        }
                    ]
                },
            }
        return {"head": {"vars": []}, "results": {"bindings": []}}


def _resolver(neptune, tmp_path: pathlib.Path) -> SchemaResolver:
    # Empty-ish key on purpose: nothing here reaches an LLM, and the verdict
    # cache is per-test so no cross-test verdict can answer a match.
    return SchemaResolver(
        neptune,
        "fake-key",
        JsonVerdictCache(tmp_path / "verdicts.json"),
    )


async def _declare(store: MemoryGraphStore) -> None:
    """Tenant catalog: Alpha{code,mass,when,holds→Beta}, Beta{}."""
    scope = {"store": store, "layer": "tenant", "tenant_id": TENANT}
    for name in (T_ALPHA, T_BETA):
        await upsert_type(name=name, **scope)
    await upsert_attribute(
        type_name=T_ALPHA, attr_name=A_CODE, datatype="string", **scope
    )
    await upsert_attribute(
        type_name=T_ALPHA, attr_name=A_MASS, datatype="integer", **scope
    )
    await upsert_attribute(
        type_name=T_ALPHA, attr_name=A_WHEN, datatype="datetime", **scope
    )
    await upsert_attribute(
        type_name=T_ALPHA, attr_name=R_HOLDS, datatype=T_BETA, **scope
    )


@pytest.fixture
def store():
    """Process GraphStore, replacing the autouse fixture's empty one."""
    s = MemoryGraphStore()
    configure_graph_store(s)
    return s


# --------------------------------------------------------------------------- #
# The catalog arm (the ONTA-534 fix)
# --------------------------------------------------------------------------- #


async def test_declared_schema_comes_from_the_catalog_when_sparql_is_retired(
    store, tmp_path
):
    """The regression: real declared types AND attributes, with SPARQL dead.

    On ``main`` the read raises, ``ontology_fetch_failed`` is logged, and the
    ingest plans against ``({}, {})``.
    """
    await _declare(store)
    neptune = RetiredSparqlNeptune()

    types, attrs = await _resolver(neptune, tmp_path)._fetch_ontology(ONTOLOGY_GRAPH)

    assert set(types) == {T_ALPHA, T_BETA}
    assert set(attrs[T_ALPHA]) == {A_CODE, A_MASS, A_WHEN, R_HOLDS}
    # And the retired client was never asked for the ontology.
    assert neptune.schema_reads == []


async def test_the_types_and_attrs_return_contract_is_unchanged(store, tmp_path):
    """Shape, not just content: the rest of the resolver reads these dicts.

    * ``types`` maps name → description, and the description is ``""`` — the
      full-ontology projection never carried ``rdfs:comment``, so a port that
      started sourcing it from the catalog would be feeding the matcher prompt
      text it has never seen before.
    * a declared type with NO attributes keeps a present, EMPTY ``attrs`` entry
      (``existing_attrs[t]`` is indexed directly on the resolve path).
    * a literal keeps its declared datatype; a type-ranged attribute keeps the
      TARGET TYPE NAME as its ``datatype``, which is what
      ``is_primitive_datatype`` / ``PRIMITIVE_TYPES`` read to tell a
      relationship from a column.
    """
    await _declare(store)

    types, attrs = await _resolver(
        RetiredSparqlNeptune(), tmp_path
    )._fetch_ontology(ONTOLOGY_GRAPH)

    assert types == {T_ALPHA: "", T_BETA: ""}
    assert attrs[T_BETA] == {}
    assert attrs[T_ALPHA][A_CODE].datatype == "string"
    assert attrs[T_ALPHA][A_MASS].datatype == "integer"
    assert attrs[T_ALPHA][A_WHEN].datatype == "datetime"
    assert attrs[T_ALPHA][R_HOLDS].datatype == T_BETA
    assert attrs[T_ALPHA][A_CODE].name == A_CODE


async def test_a_declared_type_is_matched_not_re_minted(store, tmp_path):
    """The consequence, end to end.

    ``TypeMatcher.match`` short-circuits to ``is_new=True`` on an EMPTY
    ontology, so with the fetch returning ``({}, {})`` an ingest that proposes a
    type the workspace already declares mints a second one. With the snapshot
    populated the exact-name layer resolves it instead — no LLM, no cache entry,
    no network.
    """
    await _declare(store)
    resolver = _resolver(RetiredSparqlNeptune(), tmp_path)

    existing_types, _ = await resolver._fetch_ontology(ONTOLOGY_GRAPH)
    match = await resolver._type_matcher.match(T_ALPHA, "a proposed type", existing_types)

    assert existing_types, "precondition: the empty-ontology branch must not apply"
    assert match.is_new is False
    assert match.resolved == T_ALPHA


async def test_a_store_injected_on_the_resolver_is_preferred(tmp_path):
    """A caller may thread its own store; the catalog otherwise resolves the
    process one (which the fixtures leave empty here)."""
    injected = MemoryGraphStore()
    await _declare(injected)
    resolver = _resolver(RetiredSparqlNeptune(), tmp_path)
    resolver._graph_store = injected

    types, attrs = await resolver._fetch_ontology(ONTOLOGY_GRAPH)

    assert set(types) == {T_ALPHA, T_BETA}
    assert attrs[T_ALPHA][A_CODE].datatype == "string"


# --------------------------------------------------------------------------- #
# The residual SPARQL arm is preserved
# --------------------------------------------------------------------------- #


async def test_an_empty_catalog_still_falls_through_to_sparql(store, tmp_path):
    """"This workspace declares nothing" and "this store had nothing to say"
    are indistinguishable, so the catalog arm declines rather than handing the
    ingest a confidently empty ontology. The XSD spelling the SPARQL arm has
    always projected still maps to its datatype name."""
    neptune = SchemaSparqlNeptune(T_ALPHA, A_WHEN, f"{XSD}#dateTime")

    types, attrs = await _resolver(neptune, tmp_path)._fetch_ontology(ONTOLOGY_GRAPH)

    assert neptune.schema_calls == 1
    assert types == {T_ALPHA: ""}
    assert attrs[T_ALPHA][A_WHEN].datatype == "datetime"


async def test_a_graph_uri_naming_no_workspace_falls_through_to_sparql(
    store, tmp_path
):
    """A graph URI that encodes no workspace carries no catalog scope, so there
    is nothing for the tenant-catalog read to scope itself to."""
    odd = "https://example.invalid/some/other/graph"
    neptune = SchemaSparqlNeptune(T_BETA, A_CODE, f"{XSD}#string")

    types, _ = await _resolver(neptune, tmp_path)._fetch_ontology(odd)

    assert neptune.schema_calls == 1
    assert types == {T_BETA: ""}


async def test_a_failing_store_degrades_to_sparql_rather_than_raising(tmp_path):
    """Best-effort throughout: a broken store must not take an ingest down."""

    class BrokenStore:
        def session(self, scope):  # test double
            raise RuntimeError("store is down")

    resolver = _resolver(SchemaSparqlNeptune(T_ALPHA, A_CODE, f"{XSD}#string"), tmp_path)
    resolver._graph_store = BrokenStore()

    types, _ = await resolver._fetch_ontology(ONTOLOGY_GRAPH)

    assert types == {T_ALPHA: ""}


async def test_both_arms_dead_degrades_and_never_raises_into_an_ingest(
    store, tmp_path
):
    """The one thing this read may never do is propagate. An ingest into a
    genuinely empty workspace lands here too — and auto-new is the RIGHT
    answer then, which is why the empty snapshot itself is not the bug."""
    types, attrs = await _resolver(
        RetiredSparqlNeptune(), tmp_path
    )._fetch_ontology(ONTOLOGY_GRAPH)

    assert (types, attrs) == ({}, {})

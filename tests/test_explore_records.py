"""Tests for the paged per-type records endpoint (COG-100).

GET /graphs/{tenant}/explore/kgs/{kg}/types/{type}/records
  ?limit=<int>  &cursor=<last-entity-uri>

**Ported by ONTA-527.** Every case here used to seed a SPARQL mock
(``mock_neptune.query.side_effect`` routing on ``"DISTINCT ?e"`` /
``"VALUES ?e"`` / ``"entityCount"``) and assert on what came back. The route
serves this table from the property-graph store now
(``explore.py::_records_from_explore_store`` → ``graph/explore_store.py``), so
those mocks answered a query nobody makes: the tests were green against a
fixture, not against the shipped read path. They are re-seeded through the real
write path (``kg_writer.insert_facts``) into a ``MemoryGraphStore``, and the
NeptuneClient mock is left un-stubbed so ``assert_not_called()`` proves the
store path ran.

Three contract differences the port makes visible, rather than papering over:

* **total** is a live count of the type in the KG. The stats-graph lookup and
  its ``COUNT`` fallback are both gone, so "stats present → 10 / stats absent →
  COUNT 42" collapses into one number that is always the real one.
* **columns** come from the properties observed on the page, not from the
  ontology. An attribute DECLARED on the type but absent from every row on the
  page is no longer a column (pinned as a strict xfail below).
* **system predicates are no longer filtered** — ``onto/ingested_at`` reaches
  the table as a data column (also a strict xfail below). ``onto/source`` and
  ``rdfs:label`` still don't, but for a different reason than before: they land
  on RESERVED Entity keys that ``_public_properties`` strips, not on a
  predicate filter.

Scenarios covered:
  1. Page of rows with attribute columns
  2. Pagination: cursor advances next_cursor; next_cursor is null at final page
  3. Empty type → empty sentinel, no error
  4. Name precedence: attrs/name → rdfs:label → URI slug
"""
import asyncio
import os

import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

os.environ["INFONA_API_KEYS"] = '{"test-key": "test-tenant"}'
os.environ["INFONA_NEPTUNE_ENDPOINT"] = "http://fake-neptune:8182"

from infona_client.api.app import create_app
from infona_client.graph.client import NeptuneClient
from infona_client.graph.iri import IRI_BASE
from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.store import configure_graph_store

TENANT = "test-tenant"
KG = "movies"
TYPE = "Movie"

GRAPH = f"{IRI_BASE}/graphs/{TENANT}/kg/{KG}"
ONTO = f"{IRI_BASE}/onto/"
TYPES = f"{IRI_BASE}/types/"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
LABEL_PRED = "http://www.w3.org/2000/01/rdf-schema#label"

# Entity ids are minted with the SAME helper every writer uses, so keyset order
# (by id) is the real one rather than a fixture's guess.
E1 = entity_uri(TYPE, "m1")
E2 = entity_uri(TYPE, "m2")
E3 = entity_uri(TYPE, "m3")

TYPE_URI = TYPES + TYPE
TITLE_PRED = f"{TYPES}{TYPE}/attrs/title"
YEAR_PRED = f"{TYPES}{TYPE}/attrs/year"
COMPANY_PRED = f"{TYPES}{TYPE}/attrs/company"
NAME_PRED = ONTO + "name"          # the instance predicate of attrs/name
INGESTED_AT_PRED = ONTO + "ingested_at"
SOURCE_PRED = ONTO + "source"


@pytest.fixture
def store():
    st = MemoryGraphStore()
    configure_graph_store(st)
    return st


@pytest.fixture
def mock_neptune():
    """Deliberately un-stubbed: the records path must never call it."""
    client = AsyncMock(spec=NeptuneClient)
    client.health.return_value = True
    client.update.return_value = None
    return client


@pytest.fixture
def client(store, mock_neptune):
    app = create_app()
    app.state.neptune_client = mock_neptune
    return TestClient(app)


@pytest.fixture
def auth_headers():
    return {"X-API-Key": "test-key"}


def _seed(store, triples, graph: str = GRAPH):
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        insert_facts(None, graph, triples, store=store)
    )


def _movie(uri: str, **attrs):
    """A Movie with optional label / title / year / company / name."""
    triples = [(uri, RDF_TYPE, TYPE_URI)]
    for pred, key in (
        (LABEL_PRED, "label"),
        (TITLE_PRED, "title"),
        (YEAR_PRED, "year"),
        (COMPANY_PRED, "company"),
        (NAME_PRED, "name"),
    ):
        if attrs.get(key) is not None:
            triples.append((uri, pred, attrs[key]))
    return triples


def _get(client, auth_headers, **params):
    return client.get(
        f"/graphs/{TENANT}/explore/kgs/{KG}/types/{TYPE}/records",
        params=params or None,
        headers=auth_headers,
    )


# ---------------------------------------------------------------------------
# 1. Happy path: page of rows with attribute columns
# ---------------------------------------------------------------------------

def test_records_basic_page(store, client, mock_neptune, auth_headers):
    """Two entities are returned with title and year columns."""
    _seed(store, _movie(E1, label="The Matrix", title="The Matrix", year="1999"))
    _seed(store, _movie(E2, label="Inception", title="Inception", year="2010"))

    resp = _get(client, auth_headers)
    assert resp.status_code == 200
    data = resp.json()

    # columns: name always first, then attribute names
    assert data["columns"][0] == "name"
    assert "title" in data["columns"]
    assert "year" in data["columns"]

    # rows
    assert len(data["rows"]) == 2
    names = {r["name"] for r in data["rows"]}
    assert "The Matrix" in names
    assert "Inception" in names

    ids = {r["id"] for r in data["rows"]}
    assert E1 in ids and E2 in ids

    assert data["total"] == 2
    # The whole page came from the property-graph store.
    mock_neptune.query.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Pagination: cursor advances; next_cursor set on a full page, null at end
# ---------------------------------------------------------------------------

def test_records_pagination_full_page(store, client, auth_headers):
    """When a full page is returned next_cursor is the last entity URI."""
    for uri, title in ((E1, "Movie A"), (E2, "Movie B"), (E3, "Movie C")):
        _seed(store, _movie(uri, title=title))

    data = _get(client, auth_headers, limit=2).json()
    # Full page (2 of 2 requested) → next_cursor is last entity URI
    assert data["next_cursor"] == E2


def test_records_pagination_last_page(store, client, auth_headers):
    """When fewer than limit entities are returned next_cursor is null."""
    for uri, title in ((E1, "Movie A"), (E2, "Movie B"), (E3, "Movie C")):
        _seed(store, _movie(uri, title=title))

    resp = _get(client, auth_headers, limit=2, cursor=E2)
    assert resp.status_code == 200
    data = resp.json()
    assert data["next_cursor"] is None
    assert len(data["rows"]) == 1


def test_records_cursor_resumes_strictly_after_the_cursor_entity(
    store, client, auth_headers
):
    """Keyset pagination, asserted on ROWS rather than on the emitted SPARQL.

    This replaces ``test_records_cursor_filter_in_sparql``, whose whole subject
    was ``assert E2 in entity_queries[0]`` — a FILTER string in a query the
    route no longer builds. What that string existed to guarantee is checked
    directly here: the page after ``cursor=E2`` starts at E3, never repeats E1
    or E2, and the two pages partition the type.
    """
    for uri, title in ((E1, "Movie A"), (E2, "Movie B"), (E3, "Movie C")):
        _seed(store, _movie(uri, title=title))

    first = _get(client, auth_headers, limit=2).json()
    assert [r["id"] for r in first["rows"]] == [E1, E2]

    second = _get(client, auth_headers, limit=2, cursor=first["next_cursor"]).json()
    assert [r["id"] for r in second["rows"]] == [E3]
    assert {r["id"] for r in first["rows"]}.isdisjoint(
        {r["id"] for r in second["rows"]}
    )


# ---------------------------------------------------------------------------
# 3. Empty type → empty sentinel (no error)
# ---------------------------------------------------------------------------

def test_records_empty_type(store, client, auth_headers):
    """A type with no instances returns the empty sentinel, never an error."""
    other = entity_uri("Person", "p1")
    _seed(store, [(other, RDF_TYPE, TYPES + "Person"), (other, LABEL_PRED, "Not a movie")])

    resp = _get(client, auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {
        "columns": ["name"],
        "rows": [],
        "total": 0,
        "next_cursor": None,
    }


# ---------------------------------------------------------------------------
# 4. System predicates; rdfs:label used as the row name
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    reason=(
        "BUG (introduced by the Neo4j cutover, surfaced by ONTA-527): the "
        "property-graph records path does not filter internal/system "
        "predicates. api/routes/explore.py::get_type_records runs "
        "_is_internal_predicate over every row on its SPARQL branch (and skips "
        "SYSTEM_PREDICATES), but _records_from_explore_store builds columns "
        "straight from EntityDetail.properties, and graph/explore_store.py's "
        "_public_properties strips only RESERVED_ENTITY_PROPERTY_KEYS. "
        "onto/ingested_at and onto/batch_id are ordinary Entity props "
        "(graph/facts.py::classify_triple keeps them by leaf), so the ingest "
        "bookkeeping stamp is rendered to users as a data column in the "
        "Explorer table. rdfs:label and onto/source stay out only incidentally "
        "— they land on the reserved 'name'/'source' keys. Not fixed here: the "
        "fix is a read-side filter in explore_store (and the same defect class "
        "as the grep one xfailed in test_grep_route.py)."
    ),
    strict=True,
)
def test_records_system_predicates_excluded(store, client, auth_headers):
    """ingested_at and source are excluded from columns; label becomes the name."""
    _seed(
        store,
        [
            *_movie(E1, label="Named Movie", title="Named Movie"),
            (E1, INGESTED_AT_PRED, "2024-01-01"),
            (E1, SOURCE_PRED, "import"),
        ],
    )

    data = _get(client, auth_headers).json()
    columns = data["columns"]

    # system predicates must NOT appear as columns
    assert "ingested_at" not in columns
    assert "source" not in columns

    # rdfs:label must NOT appear as a column (used for name only)
    assert "label" not in columns

    # name should be the label value
    assert data["rows"][0]["name"] == "Named Movie"


def test_records_reserved_keys_never_become_columns(store, client, auth_headers):
    """The half of the case above that DOES hold: label / source stay out.

    Split from the xfail so the surviving guarantee keeps failing loudly if it
    regresses, instead of hiding behind the ingested_at bug.
    """
    _seed(
        store,
        [
            *_movie(E1, label="Named Movie", title="Named Movie"),
            (E1, SOURCE_PRED, "import"),
        ],
    )

    data = _get(client, auth_headers).json()
    assert "label" not in data["columns"]
    assert "source" not in data["columns"]
    assert data["rows"][0]["name"] == "Named Movie"


def test_records_name_falls_back_to_id_leaf(store, client, auth_headers):
    """When no rdfs:label, name comes from the last URI segment."""
    _seed(store, _movie(E1, title="Some Title"))

    data = _get(client, auth_headers).json()
    # E1 = ".../Movie/m1" → leaf is "m1"
    assert data["rows"][0]["name"] == "m1"


# ---------------------------------------------------------------------------
# 5. Total is a live count of the type, not of the page
# ---------------------------------------------------------------------------

def test_records_total_counts_the_whole_type_not_the_page(
    store, client, mock_neptune, auth_headers
):
    """``total`` is the type's real instance count, computed live.

    Replaces ``test_records_total_fallback_count``: the precomputed stats-graph
    lookup (and therefore the "stats absent → fall back to COUNT" branch it
    tested) is not on the property-graph path at all, so there is one number
    and it is always the live one.
    """
    for i in range(5):
        _seed(store, _movie(entity_uri(TYPE, f"m{i}"), title=f"Film {i}"))

    data = _get(client, auth_headers, limit=2).json()
    assert len(data["rows"]) == 2
    assert data["total"] == 5
    mock_neptune.query.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Rare attributes: present on one row, blank on the others
# ---------------------------------------------------------------------------

def test_records_rare_attribute_is_a_column_with_blanks(store, client, auth_headers):
    """An attribute on ONE entity of the page is a column; the rest render blank.

    The surviving half of ``test_records_declared_rare_attribute_is_column``
    (COG-112). Its other half — that the attribute is a column because the
    ONTOLOGY declares it, even with no value on the page — is the xfail below.
    """
    _seed(store, _movie(E1, label="Film One", title="Film One"))
    _seed(store, _movie(E2, label="Film Two", title="Film Two", company="Acme Studios"))
    _seed(store, _movie(E3, label="Film Three", title="Film Three"))

    data = _get(client, auth_headers).json()

    assert "company" in data["columns"], data["columns"]
    assert data["columns"][0] == "name"

    by_name = {r["name"]: r for r in data["rows"]}
    # The entity that has it shows the value
    assert by_name["Film Two"]["company"] == "Acme Studios"
    # Entities without it render blank (key present, empty string) — not missing
    assert by_name["Film One"]["company"] == ""
    assert by_name["Film Three"]["company"] == ""
    # Every row carries every column key
    for r in data["rows"]:
        for col in data["columns"]:
            assert col in r


@pytest.mark.xfail(
    reason=(
        "BUG (introduced by the Neo4j cutover, surfaced by ONTA-527): "
        "ontology-DECLARED attributes are no longer table columns. The SPARQL "
        "branch of api/routes/explore.py::get_type_records reads the type's "
        "attribute definitions and makes every declared label a column exempt "
        "from the _MAX_COLS budget (COG-112: an enriched attr present on 1 of "
        "N entities must still be visible, and a declared-but-empty column is "
        "how a user sees an attribute exists at all). "
        "_records_from_explore_store never touches the ontology catalog — "
        "columns are the union of properties observed on the page — so a "
        "declared attribute with no value on this page silently disappears "
        "from the Explorer table. Not fixed here: the fix is to join "
        "ontology_catalog.list_attributes into the store records path."
    ),
    strict=True,
)
def test_records_declared_attribute_with_no_value_is_still_a_column(
    store, client, auth_headers
):
    async def declare():
        from infona_client.graph.ontology_catalog import (
            upsert_attribute,
            upsert_type,
        )

        await upsert_type(name=TYPE, tenant_id=TENANT, layer="tenant")
        await upsert_attribute(
            type_name=TYPE,
            attr_name="company",
            description="enriched later",
            tenant_id=TENANT,
            layer="tenant",
        )

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(declare())
    _seed(store, _movie(E1, label="Solo", title="Solo"))

    data = _get(client, auth_headers).json()
    assert "company" in data["columns"], data["columns"]
    assert data["rows"][0]["company"] == ""


# ---------------------------------------------------------------------------
# 7. The first "name" column resolves from attrs/name → rdfs:label → slug
#    (COG-112: ingest writes (entity_uri, rdfs:label, entity.id) — rdfs:label IS
#    the opaque entity-id slug — and the human-readable name lives in attrs/name.
#    So attrs/name must WIN over rdfs:label; rdfs:label-first still yields the
#    slug. attrs/name must not become a separate second column either.)
# ---------------------------------------------------------------------------


def test_records_name_prefers_attrs_name_over_slug(store, client, auth_headers):
    """An entity with attrs/name but NO rdfs:label shows its attrs/name value in
    the `name` field — not the URI slug. attrs/name must not become a separate
    second column."""
    _seed(store, _movie(E1, name="Jane Doe", title="Some Title"))

    data = _get(client, auth_headers).json()

    # name shows the attrs/name value, NOT the URI slug ("m1")
    assert data["rows"][0]["name"] == "Jane Doe"
    # attrs/name does NOT become a separate second "name" column
    assert data["columns"].count("name") == 1
    assert data["columns"][0] == "name"


def test_records_name_prefers_attrs_name_over_label(store, client, auth_headers):
    """When both attrs/name and a slug-shaped rdfs:label are present, attrs/name
    wins — ingest stores the opaque entity-id slug in rdfs:label, so the
    human-readable attrs/name value must be displayed instead of the slug.

    Both land on the ONE reserved ``name`` property now, so "attrs/name wins"
    is a write-order property of graph/facts.py rather than a read-side
    precedence rule — seeded in the order ingest emits them (label first).
    """
    _seed(store, [(E1, RDF_TYPE, TYPE_URI), (E1, LABEL_PRED, "4akvVWgTcS")])
    _seed(store, [(E1, NAME_PRED, "Jane Doe")])

    data = _get(client, auth_headers).json()
    # attrs/name wins over the slug-shaped rdfs:label
    assert data["rows"][0]["name"] == "Jane Doe"
    # attrs/name does NOT become a separate second "name" column
    assert data["columns"].count("name") == 1


def test_records_name_falls_back_to_slug_when_neither(store, client, auth_headers):
    """With neither rdfs:label nor attrs/name, name falls back to the URI slug."""
    _seed(store, _movie(E1, title="Some Title"))

    data = _get(client, auth_headers).json()
    # E1 = ".../Movie/m1" → leaf is "m1"
    assert data["rows"][0]["name"] == "m1"

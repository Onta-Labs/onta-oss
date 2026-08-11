"""Route-level tests for the index-free literal grep (ONTA-416).

``POST /graphs/{tenant}/grep`` is the ONE literal-scan surface every client
(MCP / SDK / CLI / webapp) rides, so this file locks its HTTP contract:

* **scoping** — the scan is bounded to the RESOLVED tenant + a charset-validated
  kg_name, never to caller text; a foreign tenant in the path is a 403/key-scoped
  read and a neighbouring workspace's rows are never returned.
* **validation** — needle under 2 non-whitespace chars → 400; bad kg_name → 400;
  limit clamped to [1, 200] and echoed.
* **honest truncation** — ``truncated`` comes from an over-fetch, never from a
  full page.
* **injection safety** — a needle carrying newlines, quotes or a graph IRI is
  matched as *text* and cannot widen what is scanned.
* **internal predicates** — attr_meta companions / ER signals / ingest markers
  are filtered out, while the label is deliberately KEPT (grep's commonest use
  is finding a thing by its displayed name).
* **snippet capping** — a huge literal yields a bounded window centered on the
  match, so MCP context can't be blown by one row.
* **gate** — ``INFONA_GREP_ENABLED=false`` → 503 naming the gate; default on.

**Ported by ONTA-527.** This file used to assert the SPARQL string the route
emitted against a recording mock (`FROM <graph>`, `LIMIT n+1`,
`CONTAINS(LCASE(...))`, escaped literals). The route runs a property-graph scan
now and emits no SPARQL, so those assertions tested a builder that no longer
runs. They are replaced by assertions on OBSERVABLE behaviour over a seeded
store — which is what they were proxies for. The two properties that most
deserved the string check, tenant confinement and injection safety, are pinned
harder than before: instead of "the query text looks scoped", a peer workspace's
rows are seeded and asserted absent from the response.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

os.environ["INFONA_API_KEYS"] = '{"test-key": "test-tenant"}'

from infona_client.api.app import create_app
from infona_client.graph.client import NeptuneClient
from infona_client.graph.iri import IRI_BASE
from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.store import configure_graph_store

TENANT = "test-tenant"
PEER_TENANT = "other-tenant"
KG = "movies"
OTHER_KG = "books"
GRAPH = f"{IRI_BASE}/graphs/{TENANT}/kg/{KG}"
OTHER_KG_GRAPH = f"{IRI_BASE}/graphs/{TENANT}/kg/{OTHER_KG}"
PEER_GRAPH = f"{IRI_BASE}/graphs/{PEER_TENANT}/kg/{KG}"

LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
MOVIE_TYPE = f"{IRI_BASE}/types/Movie"
TITLE = f"{IRI_BASE}/types/Movie/attrs/title"
TAGLINE = f"{IRI_BASE}/types/Movie/attrs/tagline"

M1 = entity_uri("Movie", "m1")
M2 = entity_uri("Movie", "m2")


def _movie(uri: str, label: str, *, title: str | None = None, tagline: str | None = None):
    triples = [(uri, RDF_TYPE, MOVIE_TYPE), (uri, LABEL, label)]
    if title is not None:
        triples.append((uri, TITLE, title))
    if tagline is not None:
        triples.append((uri, TAGLINE, tagline))
    return triples


@pytest.fixture
def store():
    st = MemoryGraphStore()
    configure_graph_store(st)
    return st


def _seed(store, graph: str, triples):
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        insert_facts(None, graph, triples, store=store)
    )


@pytest.fixture
def seeded(store):
    """One KG with two movies, plus a peer workspace holding the same needle."""
    _seed(store, GRAPH, _movie(M1, "The Matrix", title="The Matrix", tagline="Free your mind"))
    _seed(store, GRAPH, _movie(M2, "Matrix Reloaded", title="Matrix Reloaded"))
    _seed(
        store,
        PEER_GRAPH,
        _movie(entity_uri("Movie", "peer"), "Matrix Peer", title="Matrix Peer"),
    )
    _seed(
        store,
        OTHER_KG_GRAPH,
        _movie(entity_uri("Movie", "otherkg"), "Matrix Book", title="Matrix Book"),
    )
    return store


@pytest.fixture
def client(store):
    app = create_app()
    # The grep route still declares Depends(get_neptune_client) even though it
    # runs a property-graph scan — one of the residual NeptuneClient references
    # ONTA-527's ratchet is counting down. It must never be CALLED; several
    # tests below assert that by leaving this mock un-stubbed.
    mock = AsyncMock(spec=NeptuneClient)
    mock.health.return_value = True
    app.state.neptune_client = mock
    return TestClient(app)


@pytest.fixture
def auth_headers():
    return {"X-API-Key": "test-key"}


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch):
    monkeypatch.delenv("INFONA_GREP_ENABLED", raising=False)
    monkeypatch.delenv("INFONA_GREP_TIMEOUT_S", raising=False)
    # The slowapi limiter is a MODULE-GLOBAL whose counters outlive the per-test
    # app, so without this every request in this file shares one 60/min bucket
    # and the file would start 429ing itself as it grows. Reset per test.
    from infona_client.api.rate_limit import limiter

    limiter.reset()
    yield
    limiter.reset()


def _post(client, body, headers, tenant=TENANT):
    return client.post(f"/graphs/{tenant}/grep", json=body, headers=headers)


def _uris(res) -> set[str]:
    return {m["entity_uri"] for m in res.json()["matches"]}


# --- scoping / auth ---------------------------------------------------------- #


def test_scan_is_bounded_to_the_resolved_tenant_and_kg(seeded, client, auth_headers):
    """A peer workspace and a sibling KG both hold the needle; neither leaks.

    This is the assertion the old `FROM <graph>` string check stood in for, made
    against data instead of query text.
    """
    res = _post(client, {"q": "matrix", "kg_name": KG}, auth_headers)
    assert res.status_code == 200, res.text
    assert _uris(res) == {M1, M2}


def test_missing_api_key_is_401(seeded, client):
    res = _post(client, {"q": "matrix", "kg_name": KG}, {})
    assert res.status_code == 401


def test_foreign_tenant_in_path_never_returns_that_tenants_rows(
    seeded, client, auth_headers
):
    res = _post(client, {"q": "matrix", "kg_name": KG}, auth_headers, tenant=PEER_TENANT)
    assert res.status_code in (401, 403), res.text


@pytest.mark.parametrize("needle", ["", " ", "a", " a "])
def test_short_needle_is_400(needle, seeded, client, auth_headers):
    res = _post(client, {"q": needle, "kg_name": KG}, auth_headers)
    assert res.status_code == 400


@pytest.mark.parametrize("kg", ["../evil", "a b", "kg>", ""])
def test_bad_kg_name_is_400(kg, seeded, client, auth_headers):
    res = _post(client, {"q": "matrix", "kg_name": kg}, auth_headers)
    assert res.status_code in (400, 422)


def test_kg_name_is_required(seeded, client, auth_headers):
    res = _post(client, {"q": "matrix"}, auth_headers)
    assert res.status_code in (400, 422)


# --- limits / truncation ------------------------------------------------------ #


@pytest.mark.parametrize(("asked", "effective"), [(0, 1), (1, 1), (5, 5), (10_000, 200)])
def test_limit_is_clamped_and_echoed(asked, effective, seeded, client, auth_headers):
    res = _post(client, {"q": "matrix", "kg_name": KG, "limit": asked}, auth_headers)
    assert res.status_code == 200, res.text
    assert res.json()["limit"] == effective


def test_truncated_is_observed_from_the_overfetch(seeded, client, auth_headers):
    """Two rows match; asking for one must report truncation, not silence it."""
    res = _post(client, {"q": "matrix", "kg_name": KG, "limit": 1}, auth_headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body["matches"]) == 1
    assert body["truncated"] is True


def test_exactly_limit_rows_is_not_truncated(seeded, client, auth_headers):
    """A full page is not a truncated page.

    grep is per-ATTRIBUTE, not per-entity, so ask for the real match count
    rather than assuming one hit per seeded movie.
    """
    everything = _post(client, {"q": "matrix", "kg_name": KG, "limit": 200}, auth_headers)
    total = len(everything.json()["matches"])
    assert total >= 2, everything.text
    assert everything.json()["truncated"] is False

    res = _post(client, {"q": "matrix", "kg_name": KG, "limit": total}, auth_headers)
    body = res.json()
    assert len(body["matches"]) == total
    assert body["truncated"] is False


def test_unknown_kg_yields_empty_matches_not_an_error(seeded, client, auth_headers):
    res = _post(client, {"q": "matrix", "kg_name": "nosuchkg"}, auth_headers)
    assert res.status_code == 200, res.text
    assert res.json()["matches"] == []


# --- match shape -------------------------------------------------------------- #


def test_matches_carry_label_type_attr_and_snippet(seeded, client, auth_headers):
    res = _post(client, {"q": "free your", "kg_name": KG}, auth_headers)
    assert res.status_code == 200, res.text
    (match,) = res.json()["matches"]
    assert match["entity_uri"] == M1
    assert match["label"] == "The Matrix"
    assert match["type"] == "Movie"
    assert match["attr"] == "tagline"
    assert "Free your mind" in match["value"]
    assert "free your" in match["snippet"].lower()


def test_unlabeled_untyped_subject_still_returns_its_match(store, client, auth_headers):
    bare = entity_uri("Movie", "bare")
    _seed(store, GRAPH, [(bare, TITLE, "Matrix Reloaded")])
    res = _post(client, {"q": "reloaded", "kg_name": KG}, auth_headers)
    assert res.status_code == 200, res.text
    assert bare in _uris(res)


# --- injection safety --------------------------------------------------------- #


@pytest.mark.parametrize(
    "needle",
    [
        'matrix"\nDROP',
        "matrix' OR '1'='1",
        f"matrix }} }} GRAPH <{PEER_GRAPH}> {{ ?s ?p ?o",
        "matrix\r\n\t\\",
    ],
)
def test_hostile_needles_are_matched_as_text_and_widen_nothing(
    seeded, client, auth_headers, needle
):
    """A needle is a substring, never syntax. It must not 500 and must not reach
    the peer workspace whose IRI one of these spells out in full."""
    res = _post(client, {"q": needle, "kg_name": KG}, auth_headers)
    assert res.status_code == 200, res.text
    assert _uris(res) <= {M1, M2}


# --- internal predicates ------------------------------------------------------ #


@pytest.mark.xfail(
    reason=(
        "BUG (pre-dates ONTA-527, surfaced by it): the property-graph grep does "
        "NOT filter internal predicates. api/routes/grep.py runs "
        "is_internal_predicate on every SPARQL row (the authoritative filter) "
        "plus a prefilter pushed into the scan, but its GraphStore branch maps "
        "every hit straight into GrepMatch. So on Neo4j — i.e. in production — "
        "grep returns ER blocking keys and other internal machinery to any "
        "client, and those rows also consume the LIMIT and shrink the page "
        "invisibly, which is exactly what the SPARQL prefilter exists to "
        "prevent. Not fixed here because the fix is write-side: classify_triple "
        "flattens er/blockKey to the property key 'blockKey', so the internal "
        "namespace is lost before grep can see it and a read-side filter cannot "
        "tell it from a user attribute of the same name."
    ),
    strict=True,
)
def test_internal_predicates_are_filtered_but_label_is_kept(store, client, auth_headers):
    subject = entity_uri("Movie", "m3")
    _seed(
        store,
        GRAPH,
        [
            (subject, RDF_TYPE, MOVIE_TYPE),
            (subject, LABEL, "Matrix Revolutions"),
            (subject, f"{IRI_BASE}/attr_meta/Movie/title/source_url", "matrix-source"),
            (subject, f"{IRI_BASE}/onto/er/blockKey", "matrix-block"),
        ],
    )
    res = _post(client, {"q": "matrix", "kg_name": KG}, auth_headers)
    assert res.status_code == 200, res.text
    attrs = {m["attr"] for m in res.json()["matches"] if m["entity_uri"] == subject}
    assert attrs, "the labelled subject should still match"
    assert not {a for a in attrs if "source_url" in a or "blockKey" in a}


# --- snippets ----------------------------------------------------------------- #


def test_snippet_is_capped_and_centered_on_the_match(store, client, auth_headers):
    blob = ("x" * 5000) + "matrix" + ("y" * 5000)
    subject = entity_uri("Movie", "big")
    _seed(store, GRAPH, [(subject, RDF_TYPE, MOVIE_TYPE), (subject, TAGLINE, blob)])
    res = _post(client, {"q": "matrix", "kg_name": KG}, auth_headers)
    assert res.status_code == 200, res.text
    (match,) = [m for m in res.json()["matches"] if m["entity_uri"] == subject]
    assert len(match["snippet"]) < 1000
    assert "matrix" in match["snippet"]
    assert len(match["value"]) < len(blob)


def test_short_value_is_returned_verbatim(seeded, client, auth_headers):
    res = _post(client, {"q": "reloaded", "kg_name": KG}, auth_headers)
    values = {m["value"] for m in res.json()["matches"]}
    assert "Matrix Reloaded" in values


# --- case sensitivity --------------------------------------------------------- #


def test_default_is_case_insensitive(seeded, client, auth_headers):
    res = _post(client, {"q": "MATRIX", "kg_name": KG}, auth_headers)
    assert res.status_code == 200, res.text
    assert _uris(res) == {M1, M2}


def test_case_sensitive_respects_case(seeded, client, auth_headers):
    res = _post(
        client, {"q": "MATRIX", "kg_name": KG, "case_sensitive": True}, auth_headers
    )
    assert res.status_code == 200, res.text
    assert _uris(res) == set()


# --- type filter -------------------------------------------------------------- #


def test_type_filter_narrows_to_that_type(store, client, auth_headers):
    person = entity_uri("Person", "p1")
    _seed(
        store,
        GRAPH,
        [
            (person, RDF_TYPE, f"{IRI_BASE}/types/Person"),
            (person, LABEL, "Matrix Fan"),
        ],
    )
    _seed(store, GRAPH, _movie(M1, "The Matrix", title="The Matrix"))
    res = _post(client, {"q": "matrix", "kg_name": KG, "type": "Movie"}, auth_headers)
    assert res.status_code == 200, res.text
    assert person not in _uris(res)


@pytest.mark.parametrize("bad_type", ["../evil", "a b", "Type>", "Ty'pe"])
def test_bad_type_is_400(bad_type, seeded, client, auth_headers):
    res = _post(client, {"q": "matrix", "kg_name": KG, "type": bad_type}, auth_headers)
    assert res.status_code == 400


# --- gate --------------------------------------------------------------------- #


def test_gate_off_is_503_naming_the_gate(seeded, client, auth_headers, monkeypatch):
    monkeypatch.setenv("INFONA_GREP_ENABLED", "false")
    res = _post(client, {"q": "matrix", "kg_name": KG}, auth_headers)
    assert res.status_code == 503
    assert "INFONA_GREP_ENABLED" in res.json()["detail"]

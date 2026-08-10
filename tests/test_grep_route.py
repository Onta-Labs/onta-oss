"""Route-level tests for the index-free literal grep (ONTA-416).

``POST /graphs/{tenant}/grep`` is the ONE literal-scan surface every client
(MCP / SDK / CLI / webapp) rides, so this file locks its HTTP contract AND the
SPARQL it emits against a mocked store:

* **scoping** — the scanned graph URI is built from the RESOLVED tenant + a
  charset-validated kg_name, never from caller text (the tenant-isolation
  property); a foreign tenant in the path is a 403/key-scoped read.
* **validation** — needle under 2 non-whitespace chars → 400; bad kg_name → 400;
  limit clamped to [1, 200] and echoed.
* **honest truncation** — the scan asks for ``LIMIT limit + 1`` and reports
  ``truncated`` from the over-fetch, never from a full page.
* **escaping** — a needle containing a newline must NOT emit an unterminated
  SPARQL literal (the pre-fix 500; the whole reason ``sparql_string_literal``
  was promoted).
* **internal predicates** — attr_meta companions / ER signals / ingest markers
  are filtered out, while ``rdfs:label`` is deliberately KEPT (grep's commonest
  use is finding a thing by its displayed name).
* **snippet capping** — a huge literal yields a bounded window centered on the
  match, so MCP context can't be blown by one row.
* **gate** — ``INFONA_GREP_ENABLED=false`` → 503 naming the gate; default on.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

os.environ["INFONA_API_KEYS"] = '{"test-key": "test-tenant"}'
os.environ["INFONA_NEPTUNE_ENDPOINT"] = "http://fake-neptune:8182"

from infona_client.api.app import create_app
from infona_client.graph.client import NeptuneClient

TENANT = "test-tenant"
KG = "movies"
GRAPH = f"https://graph.infona.ai/graphs/{TENANT}/kg/{KG}"

ENTITIES = "https://graph.infona.ai/entities/"
ONTO = "https://graph.infona.ai/onto/"
TYPES = "https://graph.infona.ai/types/"
LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"

E1 = ENTITIES + "Movie/m1"
E2 = ENTITIES + "Movie/m2"
TITLE = ONTO + "title"
TAGLINE = ONTO + "tagline"


def _rows(*binding_dicts):
    variables: list[str] = []
    for b in binding_dicts:
        for k in b:
            if k not in variables:
                variables.append(k)
    return {
        "head": {"vars": variables},
        "results": {
            "bindings": [
                {k: {"value": v} for k, v in b.items()} for b in binding_dicts
            ]
        },
    }


def _empty():
    return {"head": {"vars": []}, "results": {"bindings": []}}


class _Store:
    """Records every SPARQL string and answers scan vs decorate separately."""

    def __init__(self, scan_rows=None, decorate_rows=None):
        self.queries: list[str] = []
        self.timeouts: list[float | None] = []
        self._scan = scan_rows if scan_rows is not None else _empty()
        self._decorate = decorate_rows if decorate_rows is not None else _empty()

    async def query(self, sparql: str, *, timeout: float | None = None):
        self.queries.append(sparql)
        self.timeouts.append(timeout)
        # The decoration query is the one with a VALUES ?s block.
        if "VALUES ?s {" in sparql:
            return self._decorate
        return self._scan

    @property
    def scan_query(self) -> str:
        return self.queries[0]


@pytest.fixture
def store():
    return _Store()


@pytest.fixture
def make_client():
    def _make(st: _Store) -> TestClient:
        mock = AsyncMock(spec=NeptuneClient)
        mock.health.return_value = True
        mock.query.side_effect = st.query
        app = create_app()
        app.state.neptune_client = mock
        return TestClient(app)

    return _make


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


# --- scoping / auth ---------------------------------------------------------- #


def test_scan_is_bounded_to_the_resolved_tenant_and_kg_graph(
    store, make_client, auth_headers
):
    """The graph URI comes from kg_graph_uri(resolved tenant, kg_name) only."""
    client = make_client(store)
    res = _post(client, {"q": "matrix", "kg_name": KG}, auth_headers)
    assert res.status_code == 200
    assert f"FROM <{GRAPH}>" in store.scan_query
    # ONE graph in the FROM clause: a grep can never fan out across KGs.
    assert store.scan_query.count("FROM <") == 1


def test_missing_api_key_is_401(store, make_client):
    client = make_client(store)
    res = _post(client, {"q": "matrix", "kg_name": KG}, {})
    assert res.status_code == 401
    assert store.queries == []


def test_foreign_tenant_in_path_never_scans_that_tenants_graph(
    store, make_client, auth_headers
):
    """A static key must not be able to grep another tenant's graph.

    Foreign path tenant is 403; the load-bearing assertion is that no SPARQL
    ran against the path tenant (or anything else) on the rejected request.
    """
    client = make_client(store)
    res = _post(client, {"q": "matrix", "kg_name": KG}, auth_headers, tenant="other")
    assert res.status_code == 403
    assert store.queries == []


# --- validation -------------------------------------------------------------- #


@pytest.mark.parametrize("needle", ["", " ", "a", "  a  ", "\n"])
def test_short_needle_is_400_and_never_scans(needle, store, make_client, auth_headers):
    client = make_client(store)
    res = _post(client, {"q": needle, "kg_name": KG}, auth_headers)
    assert res.status_code == 400
    assert "non-whitespace" in res.json()["detail"]
    assert store.queries == []


@pytest.mark.parametrize("kg", ["", "has space", "a/b", "../other", "x'y"])
def test_bad_kg_name_is_400_and_never_scans(kg, store, make_client, auth_headers):
    client = make_client(store)
    res = _post(client, {"q": "matrix", "kg_name": kg}, auth_headers)
    assert res.status_code == 400
    assert store.queries == []


def test_kg_name_is_required(store, make_client, auth_headers):
    """Unlike /search, kg_name is not optional: it is the primary cost control."""
    client = make_client(store)
    res = _post(client, {"q": "matrix"}, auth_headers)
    assert res.status_code == 422
    assert store.queries == []


@pytest.mark.parametrize("asked,effective", [(0, 1), (-5, 1), (5000, 200), (7, 7)])
def test_limit_is_clamped_and_echoed(asked, effective, store, make_client, auth_headers):
    client = make_client(store)
    res = _post(
        client, {"q": "matrix", "kg_name": KG, "limit": asked}, auth_headers
    )
    assert res.status_code == 200
    assert res.json()["limit"] == effective
    # The scan over-fetches by exactly one row (honest `truncated`).
    assert f"LIMIT {effective + 1}" in store.scan_query


# --- escaping (the ONTA-416 prerequisite) ------------------------------------ #


@pytest.mark.parametrize(
    "needle", ['multi\nline', 'carriage\rreturn', 'tab\tsep', 'quote" and \\ slash']
)
def test_control_chars_in_needle_do_not_break_the_literal(
    needle, store, make_client, auth_headers
):
    """A raw newline inside a SPARQL "..." literal is a PARSE ERROR (a 500 dressed
    up as a user typo). Every control char must arrive escaped."""
    client = make_client(store)
    res = _post(client, {"q": needle, "kg_name": KG}, auth_headers)
    assert res.status_code == 200
    q = store.scan_query
    # No raw control character survives into the emitted query...
    for raw in ("\n", "\r", "\t"):
        if raw in needle:
            # ...inside the CONTAINS literal. Split on the literal to check only
            # the interpolated part, since the query itself is multi-line.
            literal = q.split('CONTAINS(LCASE(STR(?o)), "', 1)[1].split('")', 1)[0]
            assert raw not in literal
    # Every literal terminates: after dropping the ESCAPED backslashes and quotes
    # (in that order), the remaining quotes pair up.
    unescaped = q.replace("\\\\", "").replace('\\"', "")
    assert unescaped.count('"') % 2 == 0


def test_needle_cannot_inject_a_second_graph(store, make_client, auth_headers):
    """A quote-and-brace payload stays INSIDE the literal, no new GRAPH block."""
    client = make_client(store)
    payload = '") } GRAPH <https://graph.infona.ai/graphs/victim> { ?s ?p ?o FILTER("'
    res = _post(client, {"q": payload, "kg_name": KG}, auth_headers)
    assert res.status_code == 200
    q = store.scan_query
    # The payload's quotes arrive ESCAPED, so it stays one literal...
    assert '\\") }} GRAPH' in q or '\\")' in q
    # ...and the scan still names exactly ONE graph: the caller's KG.
    assert q.count("FROM <") == 1
    assert f"FROM <{GRAPH}>" in q
    # The victim URI only ever appears INSIDE the literal, never as a GRAPH term.
    literal = q.split('CONTAINS(LCASE(STR(?o)), "', 1)[1].rsplit('")', 1)[0]
    assert "graphs/victim" in literal
    assert "graphs/victim" not in q.replace(literal, "")


# --- results ----------------------------------------------------------------- #


def test_matches_carry_label_type_attr_and_snippet(make_client, auth_headers):
    st = _Store(
        scan_rows=_rows(
            {"s": E1, "p": TITLE, "o": "The Matrix"},
            {"s": E1, "p": TAGLINE, "o": "Welcome to the Matrix"},
        ),
        decorate_rows=_rows(
            {"s": E1, "label": "The Matrix", "type": TYPES + "Movie"},
        ),
    )
    client = make_client(st)
    res = _post(client, {"q": "matrix", "kg_name": KG}, auth_headers)
    assert res.status_code == 200
    body = res.json()
    assert body["count"] == 2
    assert body["truncated"] is False
    first = body["matches"][0]
    assert first["entity_uri"] == E1
    assert first["label"] == "The Matrix"
    assert first["type"] == "Movie"
    assert first["predicate"] == TITLE
    assert first["attr"] == "title"
    assert first["value"] == "The Matrix"
    assert first["snippet"] == "The Matrix"
    # The unit of a hit is a TRIPLE: the same entity appears once per attribute.
    assert {m["attr"] for m in body["matches"]} == {"title", "tagline"}


def test_unlabeled_untyped_subject_still_returns_its_match(make_client, auth_headers):
    st = _Store(
        scan_rows=_rows({"s": E2, "p": TITLE, "o": "Matrix Reloaded"}),
        decorate_rows=_empty(),
    )
    client = make_client(st)
    res = _post(client, {"q": "matrix", "kg_name": KG}, auth_headers)
    body = res.json()
    assert body["count"] == 1
    assert body["matches"][0]["entity_uri"] == E2
    assert body["matches"][0]["label"] == ""
    assert body["matches"][0]["type"] == ""


def test_decoration_failure_degrades_to_bare_uris(make_client, auth_headers):
    """A failed second query must never lose matches the scan already paid for."""

    class _Flaky(_Store):
        async def query(self, sparql: str, *, timeout: float | None = None):
            self.queries.append(sparql)
            if "VALUES ?s {" in sparql:
                raise RuntimeError("decoration blew up")
            return self._scan

    st = _Flaky(scan_rows=_rows({"s": E1, "p": TITLE, "o": "The Matrix"}))
    client = make_client(st)
    res = _post(client, {"q": "matrix", "kg_name": KG}, auth_headers)
    assert res.status_code == 200
    assert res.json()["count"] == 1
    assert res.json()["matches"][0]["label"] == ""


def test_truncated_is_observed_from_the_overfetch(make_client, auth_headers):
    """limit=2 with 3 scan rows → 2 matches + truncated (the 3rd is never shown)."""
    st = _Store(
        scan_rows=_rows(
            {"s": E1, "p": TITLE, "o": "Matrix 1"},
            {"s": E1, "p": TAGLINE, "o": "Matrix 2"},
            {"s": E2, "p": TITLE, "o": "Matrix 3"},
        )
    )
    client = make_client(st)
    res = _post(client, {"q": "matrix", "kg_name": KG, "limit": 2}, auth_headers)
    body = res.json()
    assert body["count"] == 2
    assert body["truncated"] is True
    assert "Matrix 3" not in [m["value"] for m in body["matches"]]


def test_exactly_limit_rows_is_not_truncated(make_client, auth_headers):
    st = _Store(
        scan_rows=_rows(
            {"s": E1, "p": TITLE, "o": "Matrix 1"},
            {"s": E1, "p": TAGLINE, "o": "Matrix 2"},
        )
    )
    client = make_client(st)
    res = _post(client, {"q": "matrix", "kg_name": KG, "limit": 2}, auth_headers)
    assert res.json()["truncated"] is False


def test_unknown_kg_yields_empty_matches_not_an_error(store, make_client, auth_headers):
    client = make_client(store)
    res = _post(client, {"q": "matrix", "kg_name": "nosuchkg"}, auth_headers)
    assert res.status_code == 200
    assert res.json() == {"matches": [], "count": 0, "limit": 50, "truncated": False}


# --- internal predicates ------------------------------------------------------ #


def test_internal_predicates_are_filtered_but_label_is_kept(make_client, auth_headers):
    st = _Store(
        scan_rows=_rows(
            {"s": E1, "p": LABEL, "o": "The Matrix"},
            {"s": E1, "p": ONTO + "source", "o": "matrix.csv"},
            {"s": E1, "p": ONTO + "batch_id", "o": "matrix-batch"},
            {
                "s": E1,
                "p": "https://graph.infona.ai/attr_meta/Movie/title/source_url",
                "o": "https://example.test/matrix",
            },
            {"s": E1, "p": "https://graph.infona.ai/er/blockKey", "o": "matrix"},
            {"s": E1, "p": TITLE, "o": "The Matrix"},
        ),
        decorate_rows=_empty(),
    )
    client = make_client(st)
    res = _post(client, {"q": "matrix", "kg_name": KG}, auth_headers)
    preds = [m["predicate"] for m in res.json()["matches"]]
    # rdfs:label survives on purpose: finding a thing by its NAME is the point.
    assert LABEL in preds
    assert TITLE in preds
    for internal in (ONTO + "source", ONTO + "batch_id"):
        assert internal not in preds
    assert not any("attr_meta" in p or "/er/" in p for p in preds)


def test_internal_namespaces_are_also_excluded_in_the_scan_query(
    store, make_client, auth_headers
):
    """Prefiltered in SPARQL too, so internal triples can't eat the LIMIT."""
    client = make_client(store)
    _post(client, {"q": "matrix", "kg_name": KG}, auth_headers)
    q = store.scan_query
    for ns in (
        "https://graph.infona.ai/er/",
        "https://graph.infona.ai/attr_meta/",
        "https://graph.infona.ai/onto/norm/",
    ):
        assert f'!STRSTARTS(STR(?p), "{ns}")' in q


# --- snippet ------------------------------------------------------------------ #


def test_snippet_is_capped_and_centered_on_the_match(make_client, auth_headers):
    blob = ("x" * 5000) + "NEEDLE" + ("y" * 5000)
    st = _Store(scan_rows=_rows({"s": E1, "p": TAGLINE, "o": blob}))
    client = make_client(st)
    res = _post(client, {"q": "needle", "kg_name": KG}, auth_headers)
    m = res.json()["matches"][0]
    assert len(m["snippet"]) <= 210  # 200 + the two elision markers
    assert "NEEDLE" in m["snippet"]
    assert m["snippet"].startswith("…") and m["snippet"].endswith("…")
    # The raw value is capped too, so one row can't blow an MCP context window.
    assert len(m["value"]) <= 510


def test_short_value_is_returned_verbatim(make_client, auth_headers):
    st = _Store(scan_rows=_rows({"s": E1, "p": TITLE, "o": "The Matrix"}))
    client = make_client(st)
    res = _post(client, {"q": "matrix", "kg_name": KG}, auth_headers)
    m = res.json()["matches"][0]
    assert m["snippet"] == "The Matrix" and "…" not in m["snippet"]


# --- filters ------------------------------------------------------------------ #


def test_case_sensitive_drops_the_lcase_wrapper(store, make_client, auth_headers):
    client = make_client(store)
    _post(
        client,
        {"q": "Matrix", "kg_name": KG, "case_sensitive": True},
        auth_headers,
    )
    q = store.scan_query
    assert 'CONTAINS(STR(?o), "Matrix")' in q
    assert "LCASE" not in q


def test_default_is_case_insensitive_and_lowercases_the_needle(
    store, make_client, auth_headers
):
    client = make_client(store)
    _post(client, {"q": "MaTrIx", "kg_name": KG}, auth_headers)
    assert 'CONTAINS(LCASE(STR(?o)), "matrix")' in store.scan_query


def test_type_filter_enumerates_every_layer_namespace(store, make_client, auth_headers):
    client = make_client(store)
    _post(client, {"q": "matrix", "kg_name": KG, "type": "Movie"}, auth_headers)
    q = store.scan_query
    assert f"<{RDF_TYPE}> ?t" in q
    for ns in ("types/Movie", "types/x/Movie", "types/public/Movie"):
        assert f"https://graph.infona.ai/{ns}>" in q
    # The rdf:type join can bind ?t twice for a cross-layer-typed entity, which
    # would emit the same triple twice and burn the caller's limit.
    assert q.startswith("SELECT DISTINCT ?s ?p ?o")


@pytest.mark.parametrize(
    "bad_type",
    [
        "Some Type",  # an ordinary space: an ILLEGAL IRIREF, a store parse error
        "Movie>",  # closes the <…> early
        "a> ?x ?y . <b",  # closes it and appends a graph pattern
        "Mo\nvie",
        'Mo"vie',
        "Movie/Sub",
    ],
)
def test_bad_type_is_400_and_never_scans(bad_type, store, make_client, auth_headers):
    """`type` is interpolated into a type IRI and wrapped in <…>, so it needs the
    same charset gate as kg_name. Unvalidated, a space emitted an illegal IRIREF
    (an opaque 500) and a `>` closed the IRI early."""
    client = make_client(store)
    res = _post(
        client, {"q": "matrix", "kg_name": KG, "type": bad_type}, auth_headers
    )
    assert res.status_code == 400
    assert "type" in res.json()["detail"]
    assert store.queries == []


def test_every_emitted_iri_is_well_formed_under_a_type_filter(
    store, make_client, auth_headers
):
    """No caller value can leave a dangling/extra angle bracket in the query."""
    client = make_client(store)
    _post(client, {"q": "matrix", "kg_name": KG, "type": "Movie"}, auth_headers)
    q = store.scan_query
    assert q.count("<") == q.count(">")


def test_untyped_scan_skips_distinct(store, make_client, auth_headers):
    """A triple is unique without the type join, so DISTINCT would be pure
    overhead on the expensive path."""
    client = make_client(store)
    _post(client, {"q": "matrix", "kg_name": KG}, auth_headers)
    assert store.scan_query.startswith("SELECT ?s ?p ?o")


def test_predicate_filter_full_uri_binds_exactly(store, make_client, auth_headers):
    client = make_client(store)
    _post(
        client, {"q": "matrix", "kg_name": KG, "predicate": TITLE}, auth_headers
    )
    assert f"VALUES ?p {{ <{TITLE}> }}" in store.scan_query


def test_predicate_filter_leaf_name_matches_the_tail(store, make_client, auth_headers):
    client = make_client(store)
    _post(client, {"q": "matrix", "kg_name": KG, "predicate": "title"}, auth_headers)
    assert 'STRENDS(STR(?p), "/title")' in store.scan_query


def test_predicate_uri_with_an_iri_breaking_char_is_400(
    store, make_client, auth_headers
):
    client = make_client(store)
    res = _post(
        client,
        {"q": "matrix", "kg_name": KG, "predicate": "https://evil.test/a> <b"},
        auth_headers,
    )
    assert res.status_code == 400
    assert store.queries == []


# --- cost guardrails ---------------------------------------------------------- #


def test_scan_uses_the_dedicated_short_timeout(store, make_client, auth_headers):
    """NOT the client-wide 120s: an interactive scan fails fast or not at all."""
    client = make_client(store)
    _post(client, {"q": "matrix", "kg_name": KG}, auth_headers)
    assert store.timeouts[0] == 15.0


def test_timeout_is_env_tunable(store, make_client, auth_headers, monkeypatch):
    monkeypatch.setenv("INFONA_GREP_TIMEOUT_S", "3")
    client = make_client(store)
    _post(client, {"q": "matrix", "kg_name": KG}, auth_headers)
    assert store.timeouts[0] == 3.0


def test_gate_off_is_503_naming_the_env_var(store, make_client, auth_headers, monkeypatch):
    monkeypatch.setenv("INFONA_GREP_ENABLED", "false")
    client = make_client(store)
    res = _post(client, {"q": "matrix", "kg_name": KG}, auth_headers)
    assert res.status_code == 503
    assert "INFONA_GREP_ENABLED" in res.json()["detail"]
    assert store.queries == []


def test_rate_limited_per_api_key(store, make_client, auth_headers):
    """60/minute per key. LIMIT bounds the RESULT, not the scan, so the request
    rate is the only thing bounding aggregate scan cost."""
    client = make_client(store)
    body = {"q": "matrix", "kg_name": KG}
    statuses = [_post(client, body, auth_headers).status_code for _ in range(61)]
    assert statuses[:60] == [200] * 60
    assert statuses[60] == 429


def test_gate_defaults_on(store, make_client, auth_headers):
    """Opt-OUT, unlike the semantic index's opt-IN gate: grep costs nothing until
    it is called, and it is the surface users asked for."""
    client = make_client(store)
    assert _post(client, {"q": "matrix", "kg_name": KG}, auth_headers).status_code == 200

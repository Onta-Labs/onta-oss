"""Cross-tenant confinement for the raw SPARQL passthrough routes (ONTA-412).

The regression these lock down: ``POST /graphs/{tenant}/query`` authorized the
tenant in the path and then executed the caller's SPARQL verbatim. On Neptune
the default graph is the union of all named graphs, so a query needed no
``FROM`` clause at all to read every other workspace's data.

Every rejection case below asserts the store was NEVER CALLED, not merely that
the response looked empty. An assertion on the response body alone would pass
against a mock that returns nothing while the real Neptune returned the victim's
rows.
"""

from unittest.mock import AsyncMock

import pytest

from cograph_client.auth.api_keys import TenantContext, get_tenant
from cograph_client.graph.sparql_scope import (
    TenantScopeError,
    enforce_query_scope,
    tenant_owns_graph,
)

TENANT = "test-tenant"
OWN_GRAPH = f"https://cograph.tech/graphs/{TENANT}"
VICTIM_GRAPH = "https://cograph.tech/graphs/victim-tenant"


def _post_query(client, auth_headers, query: str):
    return client.post(
        f"/graphs/{TENANT}/query", headers=auth_headers, json={"query": query}
    )


# ---------------------------------------------------------------------------
# The hole itself
# ---------------------------------------------------------------------------


def test_cross_tenant_from_clause_is_rejected(client, auth_headers, mock_neptune):
    """The reported attack: name another workspace's graph in FROM."""
    res = _post_query(
        client,
        auth_headers,
        f"SELECT ?s ?p ?o FROM <{VICTIM_GRAPH}> WHERE {{ ?s ?p ?o }}",
    )
    assert res.status_code == 403
    assert "victim-tenant" in res.json()["detail"]
    mock_neptune.query.assert_not_called()


def test_unscoped_query_is_rejected(client, auth_headers, mock_neptune):
    """The BIGGER hole: no dataset clause at all.

    Neptune's default graph is the union of every named graph, so this query
    read all tenants without naming a single graph. Any guard shaped as "reject
    clauses pointing at foreign graphs" would have waved it straight through.
    """
    res = _post_query(client, auth_headers, "SELECT ?s ?p ?o WHERE { ?s ?p ?o }")
    assert res.status_code == 400
    assert "dataset clause" in res.json()["detail"]
    mock_neptune.query.assert_not_called()


@pytest.mark.parametrize(
    "query",
    [
        # Foreign graph reached through GRAPH rather than FROM.
        f"SELECT ?s FROM <{OWN_GRAPH}> WHERE {{ GRAPH <{VICTIM_GRAPH}> {{ ?s ?p ?o }} }}",
        # ... and through FROM NAMED + GRAPH.
        f"SELECT ?s FROM <{OWN_GRAPH}> FROM NAMED <{VICTIM_GRAPH}> WHERE {{ ?s ?p ?o }}",
        # A relative IRI resolved against a BASE pointing at the victim.
        f"BASE <{VICTIM_GRAPH}> SELECT ?s FROM <> WHERE {{ ?s ?p ?o }}",
        f"SELECT ?s FROM <../victim-tenant> WHERE {{ ?s ?p ?o }}",
        # A prefixed name, which would expand outside anything we can read here.
        "PREFIX g: <https://cograph.tech/graphs/> "
        "SELECT ?s FROM g:victim-tenant WHERE { ?s ?p ?o }",
        # A neighbouring tenant id that merely shares our prefix.
        f"SELECT ?s FROM <{OWN_GRAPH}-evil> WHERE {{ ?s ?p ?o }}",
        # Neptune's shared fallback graph, which is nobody's tenant graph.
        "SELECT ?s FROM <http://aws.amazon.com/neptune/vocab/v01/DefaultNamedGraph> "
        "WHERE { ?s ?p ?o }",
        # Federation: an outbound channel for the rows we do allow.
        f"SELECT ?s FROM <{OWN_GRAPH}> WHERE "
        "{ SERVICE <http://attacker.example/sparql> { ?s ?p ?o } }",
    ],
)
def test_escape_routes_are_rejected(client, auth_headers, mock_neptune, query):
    res = _post_query(client, auth_headers, query)
    assert res.status_code in (400, 403), res.text
    mock_neptune.query.assert_not_called()


def test_from_hidden_in_a_string_literal_does_not_satisfy_the_gate(
    client, auth_headers, mock_neptune
):
    """A literal containing "FROM <...>" must not count as a dataset clause.

    This is the direction where a sloppy scan fails OPEN: mistake the text
    inside a literal for a real clause and an otherwise unscoped query gets
    executed against the union of every graph.
    """
    res = _post_query(
        client,
        auth_headers,
        'SELECT ?s WHERE { ?s ?p "FROM <%s>" }' % OWN_GRAPH,
    )
    assert res.status_code == 400
    mock_neptune.query.assert_not_called()


@pytest.mark.parametrize(
    "query",
    [
        # An unterminated literal, i.e. text our reading of it and the store's
        # could plausibly disagree about.
        f'SELECT * WHERE {{ ?s ?p "abc }} FROM <{VICTIM_GRAPH}>',
        f'SELECT * FROM <{OWN_GRAPH}> WHERE {{ ?s ?p "x GRAPH <{VICTIM_GRAPH}> }}',
        f'SELECT * FROM <{OWN_GRAPH}> WHERE {{ ?s ?p """a FROM <{VICTIM_GRAPH}>""" }}',
    ],
)
def test_raw_text_scan_catches_what_the_parse_might_not(
    client, auth_headers, mock_neptune, query
):
    """The belt, exercised without its suspenders.

    The foreign-IRI scan reads untouched text, so naming another workspace's
    graph is caught no matter how the surrounding syntax parses, and a
    divergence between our parser and the store's cannot blind it.
    """
    res = _post_query(client, auth_headers, query)
    assert res.status_code in (400, 403), res.text
    mock_neptune.query.assert_not_called()


# ---------------------------------------------------------------------------
# Token-boundary bypasses: the reason rule A uses a parser, not a keyword scan
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "query"),
    [
        # SPARQL's BLANK_NODE_LABEL and PN_LOCAL both allow "-", "." and a long
        # tail of PN_CHARS INSIDE a name. Each decoy below is ONE token to the
        # store, so the store sees NO dataset clause and falls back to the union
        # of every graph, while a keyword scanner reads a standalone FROM
        # followed by an owned IRI and calls the query scoped. The decoy sits in
        # an OPTIONAL / MINUS so it constrains nothing and `?s ?p ?o` returns
        # everything. This is a full reinstatement of the original bug, and it
        # names no foreign IRI, so rule B cannot catch it either.
        (
            "blank node label",
            "SELECT ?s ?p ?o WHERE { ?s ?p ?o OPTIONAL { _:b-FROM "
            f"<{OWN_GRAPH}> ?z }} }}",
        ),
        (
            "prefixed name with hyphen",
            "PREFIX ex: <http://e/> SELECT ?s ?p ?o WHERE { ?s ?p ?o "
            f"OPTIONAL {{ ?x ex:p-FROM <{OWN_GRAPH}> }} }}",
        ),
        (
            "prefixed name with dot",
            "PREFIX ex: <http://e/> SELECT ?s ?p ?o WHERE { ?s ?p ?o "
            f"MINUS {{ ?x ex:a.FROM <{OWN_GRAPH}> }} }}",
        ),
        # U+00B7 MIDDLE DOT is a legal PN_CHARS codepoint that is neither a word
        # character nor "-"/"."; it defeats the tightened lookbehind that a
        # regex-based fix would have reached for.
        (
            "prefixed name with middle dot",
            "PREFIX ex: <http://e/> SELECT ?s ?p ?o WHERE { ?s ?p ?o "
            f"OPTIONAL {{ ?x ex:p·FROM <{OWN_GRAPH}> }} }}",
        ),
    ],
)
def test_keyword_lookalike_tokens_cannot_fake_a_dataset_clause(
    client, auth_headers, mock_neptune, label, query
):
    res = _post_query(client, auth_headers, query)
    assert res.status_code == 400, f"{label}: {res.text}"
    mock_neptune.query.assert_not_called()


@pytest.mark.parametrize(
    "graph",
    [
        f"{OWN_GRAPH}/../victim-tenant",
        f"{OWN_GRAPH}/kg/../../victim-tenant",
        f"{OWN_GRAPH}/./../victim-tenant",
        f"{OWN_GRAPH}/%2e%2e/victim-tenant",
        f"{OWN_GRAPH}/..\\victim-tenant",
    ],
)
def test_dot_segments_under_an_owned_prefix_are_rejected(
    client, auth_headers, mock_neptune, graph
):
    """An owned PREFIX is not an owned TARGET.

    RFC 3986 section 5.2.2 removes dot segments when resolving a reference even
    when it carries a scheme, and Jena's resolver does exactly that, so these
    start inside the workspace's namespace and land outside it.
    """
    res = _post_query(
        client, auth_headers, f"SELECT * FROM <{graph}> WHERE {{ ?s ?p ?o }}"
    )
    # 403 from the ownership check, or 400 when the IRI is malformed enough that
    # the parser refuses it first (a backslash is not legal in an IRIREF). Both
    # are refusals; `tenant_owns_graph` is pinned directly on all of these below.
    assert res.status_code in (400, 403), res.text
    mock_neptune.query.assert_not_called()


def test_comment_cannot_smuggle_a_dataset_clause(client, auth_headers, mock_neptune):
    res = _post_query(
        client,
        auth_headers,
        f"# FROM <{OWN_GRAPH}>\nSELECT ?s WHERE {{ ?s ?p ?o }}",
    )
    assert res.status_code == 400
    mock_neptune.query.assert_not_called()


# ---------------------------------------------------------------------------
# ... without breaking the route
# ---------------------------------------------------------------------------


def test_same_tenant_query_still_works(client, auth_headers, mock_neptune):
    mock_neptune.query.return_value = {
        "head": {"vars": ["name"]},
        "results": {
            "bindings": [{"name": {"type": "literal", "value": "Central Park"}}]
        },
    }
    res = _post_query(
        client,
        auth_headers,
        f"SELECT ?name FROM <{OWN_GRAPH}> WHERE "
        "{ ?s <https://schema.org/name> ?name }",
    )
    assert res.status_code == 200
    assert res.json()["bindings"][0]["name"] == "Central Park"
    mock_neptune.query.assert_called_once()


def test_published_cli_clear_loop_still_works(client, auth_headers, mock_neptune):
    """The exact query shape ``cograph clear`` sends (packages/cograph/src/cli.ts).

    It is the only first-party client of this route, and it already scoped
    itself, so the guard must not break already-published CLI versions.
    """
    res = _post_query(
        client,
        auth_headers,
        f"SELECT ?s ?p ?o FROM <{OWN_GRAPH}> WHERE {{ ?s ?p ?o . "
        "FILTER(CONTAINS(STR(?s), '/entities/') || CONTAINS(STR(?s), '/onto/') "
        "|| CONTAINS(STR(?s), '/kgs/')) } LIMIT 1000",
    )
    assert res.status_code == 200
    mock_neptune.query.assert_called_once()


def test_eval_diagnosis_probe_shapes_still_work(client, auth_headers, mock_neptune):
    """The two probes in cograph_client/eval_diagnosis.py, unchanged."""
    kg_graph = f"{OWN_GRAPH}/kg/imdb"
    for query in (
        f"SELECT (COUNT(?v) AS ?cnt) FROM <{kg_graph}> "
        'WHERE { ?s ?p ?v . FILTER(CONTAINS(STR(?v), "|")) } LIMIT 1',
        f"ASK FROM <{OWN_GRAPH}> WHERE "
        "{ <https://cograph.tech/onto/directedBy> ?p ?o }",
    ):
        mock_neptune.query.reset_mock()
        assert _post_query(client, auth_headers, query).status_code == 200, query
        mock_neptune.query.assert_called_once()


def test_iris_with_a_hash_are_not_read_as_comments(client, auth_headers):
    """rdf:type spelled out ends in "#type"; the mask must keep the IRI whole."""
    res = _post_query(
        client,
        auth_headers,
        f"SELECT ?s FROM <{OWN_GRAPH}> WHERE "
        "{ ?s <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> ?o }",
    )
    assert res.status_code == 200


def test_tenant_owned_companion_graphs_are_allowed(client, auth_headers):
    for graph in (
        f"{OWN_GRAPH}/kg/imdb",
        f"{OWN_GRAPH}/kg/imdb/provenance",
        OWN_GRAPH,
    ):
        res = _post_query(
            client, auth_headers, f"SELECT ?s FROM <{graph}> WHERE {{ ?s ?p ?o }}"
        )
        assert res.status_code == 200, graph


def test_multiple_owned_clauses_and_graph_variable_are_allowed(client, auth_headers):
    res = _post_query(
        client,
        auth_headers,
        f"SELECT ?g ?s FROM <{OWN_GRAPH}> FROM NAMED <{OWN_GRAPH}/kg/imdb> "
        "WHERE { GRAPH ?g { ?s ?p ?o } }",
    )
    assert res.status_code == 200


# ---------------------------------------------------------------------------
# Unit-level rules
# ---------------------------------------------------------------------------


def test_tenant_owns_graph_does_not_leak_across_a_shared_prefix():
    assert tenant_owns_graph(OWN_GRAPH, TENANT)
    assert tenant_owns_graph(f"{OWN_GRAPH}/kg/x", TENANT)
    assert tenant_owns_graph(f"{OWN_GRAPH}/kg/x/provenance", TENANT)
    assert not tenant_owns_graph(f"{OWN_GRAPH}-evil", TENANT)
    assert not tenant_owns_graph(f"{OWN_GRAPH}evil", TENANT)
    assert not tenant_owns_graph(VICTIM_GRAPH, TENANT)
    assert not tenant_owns_graph("https://cograph.tech/graphs/", TENANT)


def test_tenant_owns_graph_rejects_paths_that_escape_the_owned_prefix():
    """Owned prefix, unowned target. See RFC 3986 section 5.2.2."""
    assert not tenant_owns_graph(f"{OWN_GRAPH}/../victim-tenant", TENANT)
    assert not tenant_owns_graph(f"{OWN_GRAPH}/kg/../../victim-tenant", TENANT)
    assert not tenant_owns_graph(f"{OWN_GRAPH}/./x", TENANT)
    assert not tenant_owns_graph(f"{OWN_GRAPH}/%2e%2e/victim-tenant", TENANT)
    assert not tenant_owns_graph(f"{OWN_GRAPH}/..\\victim-tenant", TENANT)
    assert not tenant_owns_graph(f"{OWN_GRAPH}//victim-tenant", TENANT)
    assert not tenant_owns_graph(f"{OWN_GRAPH}/", TENANT)


def test_scope_error_carries_the_status_the_route_should_return():
    with pytest.raises(TenantScopeError) as unscoped:
        enforce_query_scope("SELECT * WHERE { ?s ?p ?o }", TENANT)
    assert unscoped.value.status_code == 400

    with pytest.raises(TenantScopeError) as foreign:
        enforce_query_scope(
            f"SELECT * FROM <{VICTIM_GRAPH}> WHERE {{ ?s ?p ?o }}", TENANT
        )
    assert foreign.value.status_code == 403


@pytest.mark.parametrize(
    "query",
    [
        f"SELECT ?from FROM <{OWN_GRAPH}> WHERE {{ ?s ?p ?from }}",
        f"SELECT ?s FROM <{OWN_GRAPH}> WHERE {{ ?s <http://example.com/from> ?o }}",
        "PREFIX ex: <http://example.com/> "
        f"SELECT ?s FROM <{OWN_GRAPH}> WHERE {{ ?s ex:from ?o }}",
        # Hyphenated local names are ordinary in third-party vocabularies, and a
        # keyword scanner rejected every one of these: "*-service" as banned
        # federation, "*-from" as a malformed dataset clause, and the blank-node
        # form as cross-tenant access on a query naming no foreign graph at all.
        "PREFIX ex: <http://example.com/> "
        f"SELECT ?s FROM <{OWN_GRAPH}> WHERE {{ ?s ex:web-service ?o }}",
        "PREFIX ex: <http://example.com/> "
        f"SELECT ?s FROM <{OWN_GRAPH}> WHERE {{ ?s ex:date-from ?o }}",
        f"SELECT ?o FROM <{OWN_GRAPH}> WHERE {{ _:node-from <https://p/n> ?o }}",
    ],
)
def test_keyword_lookalikes_are_not_mistaken_for_clauses(query):
    """A name that merely CONTAINS a keyword is one token, not a clause.

    Each query here IS properly scoped, so the only way to fail is to see a
    phantom keyword and reject legitimate work.
    """
    enforce_query_scope(query, TENANT)


def test_from_named_alone_is_a_dataset_clause():
    """FROM NAMED with no plain FROM still declares a dataset, so it is accepted.

    Pinned because it is the one accepted shape where the default graph ends up
    EMPTY rather than tenant-scoped, which is safe but easy to break by mistake.
    """
    enforce_query_scope(
        f"SELECT ?g FROM NAMED <{OWN_GRAPH}/kg/imdb> WHERE "
        "{ GRAPH ?g { ?s ?p ?o } }",
        TENANT,
    )


#: Every query this module asserts the guard ACCEPTS. Kept in one place so the
#: differential invariant below covers all of them, not a hand-picked subset.
ACCEPTED_QUERIES = [
    f"SELECT ?name FROM <{OWN_GRAPH}> WHERE {{ ?s <https://schema.org/name> ?name }}",
    f"SELECT ?s ?p ?o FROM <{OWN_GRAPH}> WHERE {{ ?s ?p ?o }} LIMIT 1000",
    f"SELECT (COUNT(?v) AS ?cnt) FROM <{OWN_GRAPH}/kg/imdb> "
    'WHERE { ?s ?p ?v . FILTER(CONTAINS(STR(?v), "|")) } LIMIT 1',
    f"ASK FROM <{OWN_GRAPH}> WHERE {{ <https://cograph.tech/onto/x> ?p ?o }}",
    f"SELECT ?s FROM <{OWN_GRAPH}/kg/imdb/provenance> WHERE {{ ?s ?p ?o }}",
    f"SELECT ?g ?s FROM <{OWN_GRAPH}> FROM NAMED <{OWN_GRAPH}/kg/imdb> "
    "WHERE { GRAPH ?g { ?s ?p ?o } }",
    f"SELECT ?g FROM NAMED <{OWN_GRAPH}/kg/imdb> WHERE {{ GRAPH ?g {{ ?s ?p ?o }} }}",
    "PREFIX ex: <http://example.com/> "
    f"SELECT ?s FROM <{OWN_GRAPH}> WHERE {{ ?s ex:web-service ?o }}",
    f"SELECT ?from FROM <{OWN_GRAPH}> WHERE {{ ?s ?p ?from }}",
    f"CONSTRUCT {{ ?s ?p ?o }} FROM <{OWN_GRAPH}> WHERE {{ ?s ?p ?o }}",
    f"DESCRIBE ?s FROM <{OWN_GRAPH}> WHERE {{ ?s ?p ?o }}",
]


@pytest.mark.parametrize("query", ACCEPTED_QUERIES)
def test_every_accepted_query_really_carries_an_owned_dataset_clause(query):
    """The invariant the guard exists to establish, checked independently.

    Asserting a 200 only proves the guard said yes. It does NOT prove the store
    will confine the query, and that gap is precisely where a token-boundary
    bypass lives: the guard sees a dataset clause, the store sees none, and the
    query runs against the union of every graph. So re-derive the dataset from
    the parse tree and require it to be non-empty and entirely tenant-owned.
    """
    from rdflib.plugins.sparql.parser import parseQuery

    from cograph_client.graph.sparql_scope import dataset_graphs, tenant_owns_graph

    enforce_query_scope(query, TENANT)

    graphs = dataset_graphs(parseQuery(query)[1])
    assert graphs, f"accepted a query the parser reports as unscoped: {query}"
    for graph_uri in graphs:
        assert tenant_owns_graph(graph_uri, TENANT), (query, graph_uri)


def test_unparseable_sparql_is_rejected_rather_than_forwarded():
    with pytest.raises(TenantScopeError) as err:
        enforce_query_scope("this is not sparql at all", TENANT)
    assert err.value.status_code == 400


# ---------------------------------------------------------------------------
# The write passthrough
# ---------------------------------------------------------------------------


def _post_update(client, headers, update: str):
    return client.post(
        f"/graphs/{TENANT}/update", headers=headers, json={"update": update}
    )


def test_update_is_operator_only(client, auth_headers, mock_neptune):
    res = _post_update(
        client, auth_headers, f"DROP SILENT GRAPH <{VICTIM_GRAPH}>"
    )
    assert res.status_code == 403
    mock_neptune.update.assert_not_called()


def test_update_rejects_a_non_operator_even_for_its_own_graph(
    client, auth_headers, mock_neptune
):
    """No same-tenant carve-out: DELETE WHERE { ?s ?p ?o } names no graph yet
    acts on Neptune's union default, so "it looks tenant-scoped" is not a thing
    the route can check."""
    res = _post_update(
        client, auth_headers, f"DROP SILENT GRAPH <{OWN_GRAPH}/kg/x>"
    )
    assert res.status_code == 403
    mock_neptune.update.assert_not_called()


def test_update_allows_an_operator(app, client, auth_headers, mock_neptune):
    app.dependency_overrides[get_tenant] = lambda: TenantContext(
        tenant_id=TENANT, api_key="k", is_operator=True
    )
    try:
        res = _post_update(
            client, auth_headers, f"DROP SILENT GRAPH <{OWN_GRAPH}/kg/x>"
        )
        assert res.status_code == 200
        mock_neptune.update.assert_awaited_once()
    finally:
        app.dependency_overrides.pop(get_tenant, None)


# ---------------------------------------------------------------------------
# Ordering and open-access mode
# ---------------------------------------------------------------------------


def test_unauthenticated_is_401_not_a_scope_error(client, mock_neptune):
    """Auth resolves first: an anonymous caller learns nothing about scoping."""
    for path in (f"/graphs/{TENANT}/query", f"/graphs/{TENANT}/update"):
        assert client.post(path, json={"query": "x", "update": "x"}).status_code == 401
    mock_neptune.query.assert_not_called()
    mock_neptune.update.assert_not_called()


def test_open_access_self_host_keeps_the_update_escape_hatch(
    monkeypatch, app, client, mock_neptune
):
    """With no auth configured there is no tenant boundary to protect."""
    from cograph_client.auth import api_keys

    monkeypatch.setattr(api_keys, "_has_static_keys", lambda: False)
    monkeypatch.setattr(api_keys, "_external_verifier", None)
    res = client.post(
        f"/graphs/{TENANT}/update", json={"update": f"DROP GRAPH <{OWN_GRAPH}>"}
    )
    assert res.status_code == 200
    mock_neptune.update.assert_awaited_once()


def test_open_access_self_host_still_scopes_reads(monkeypatch, client, mock_neptune):
    """The READ guard is not conditional on auth: it is a correctness rule about
    which graphs a query names, and staying on in open-access mode keeps a
    self-hosted multi-workspace install honest."""
    from cograph_client.auth import api_keys

    monkeypatch.setattr(api_keys, "_has_static_keys", lambda: False)
    monkeypatch.setattr(api_keys, "_external_verifier", None)
    res = client.post(
        f"/graphs/{TENANT}/query", json={"query": f"SELECT * FROM <{VICTIM_GRAPH}> WHERE {{ ?s ?p ?o }}"}
    )
    assert res.status_code == 403
    mock_neptune.query.assert_not_called()


def test_every_route_on_the_passthrough_router_is_guarded():
    """Structural guard, mirroring the operator router's own test.

    A new route added to this router that forgot its guard would be a raw
    passthrough with nothing but path-level tenant auth in front of it, which is
    exactly the bug this ticket fixed. Fail in CI rather than in review.
    """
    import inspect

    from cograph_client.api.routes import query as query_routes

    for route in query_routes.router.routes:
        source = inspect.getsource(route.endpoint)
        guarded = (
            "enforce_query_scope" in source
            or "require_raw_update_access" in source
        )
        assert guarded, f"{route.path} has no tenant-confinement guard"

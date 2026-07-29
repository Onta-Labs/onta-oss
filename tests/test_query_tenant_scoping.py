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
        # An unterminated literal: the mask cannot tell where the literal ends,
        # so clause detection is unreliable here by construction.
        f'SELECT * WHERE {{ ?s ?p "abc }} FROM <{VICTIM_GRAPH}>',
        f'SELECT * FROM <{OWN_GRAPH}> WHERE {{ ?s ?p "x GRAPH <{VICTIM_GRAPH}> }}',
        f'SELECT * FROM <{OWN_GRAPH}> WHERE {{ ?s ?p """a FROM <{VICTIM_GRAPH}>""" }}',
    ],
)
def test_raw_text_scan_catches_what_the_mask_cannot(
    client, auth_headers, mock_neptune, query
):
    """The belt, exercised without its suspenders.

    Clause detection reads a masked copy and can be confused by a malformed
    literal. The foreign-IRI scan reads the untouched text, so naming another
    workspace's graph is caught no matter how the surrounding syntax parses.
    """
    res = _post_query(client, auth_headers, query)
    assert res.status_code == 403, res.text
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
    assert not tenant_owns_graph(f"{OWN_GRAPH}-evil", TENANT)
    assert not tenant_owns_graph(f"{OWN_GRAPH}evil", TENANT)
    assert not tenant_owns_graph(VICTIM_GRAPH, TENANT)
    assert not tenant_owns_graph("https://cograph.tech/graphs/", TENANT)


def test_scope_error_carries_the_status_the_route_should_return():
    with pytest.raises(TenantScopeError) as unscoped:
        enforce_query_scope("SELECT * WHERE { ?s ?p ?o }", TENANT)
    assert unscoped.value.status_code == 400

    with pytest.raises(TenantScopeError) as foreign:
        enforce_query_scope(
            f"SELECT * FROM <{VICTIM_GRAPH}> WHERE {{ ?s ?p ?o }}", TENANT
        )
    assert foreign.value.status_code == 403


def test_keyword_lookalikes_are_not_mistaken_for_clauses():
    """?from / ex:from / a path segment named "from" are not dataset clauses.

    Each of these queries IS properly scoped, so the only way to fail is for the
    scanner to see a phantom extra FROM keyword and reject a legitimate query.
    """
    for query in (
        f"SELECT ?from FROM <{OWN_GRAPH}> WHERE {{ ?s ?p ?from }}",
        f"SELECT ?s FROM <{OWN_GRAPH}> WHERE {{ ?s <http://example.com/from> ?o }}",
        "PREFIX ex: <http://example.com/> "
        f"SELECT ?s FROM <{OWN_GRAPH}> WHERE {{ ?s ex:from ?o }}",
    ):
        enforce_query_scope(query, TENANT)


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

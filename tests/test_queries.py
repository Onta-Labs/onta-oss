import pytest

from infona_client.graph.queries import (
    tenant_graph_uri,
    insert_triples,
    delete_triples,
    select_triples,
    register_function_triple,
    list_functions_query,
)


def test_tenant_graph_uri():
    assert tenant_graph_uri("acme") == "https://graph.infona.ai/graphs/acme"


def test_insert_single_triple():
    sparql = insert_triples(
        "https://graph.infona.ai/graphs/t1",
        [("https://example.com/place/1", "https://schema.org/name", "Central Park")],
    )
    assert "INSERT DATA" in sparql
    assert "GRAPH <https://graph.infona.ai/graphs/t1>" in sparql
    assert "<https://example.com/place/1>" in sparql
    assert '"Central Park"' in sparql


def test_insert_multiple_triples():
    sparql = insert_triples(
        "https://graph.infona.ai/graphs/t1",
        [
            ("https://example.com/place/1", "https://schema.org/name", "Central Park"),
            ("https://example.com/place/1", "https://schema.org/type", "https://schema.org/Park"),
        ],
    )
    assert sparql.count("<https://example.com/place/1>") == 2


def test_delete_triples():
    sparql = delete_triples(
        "https://graph.infona.ai/graphs/t1",
        [("https://example.com/place/1", "https://schema.org/name", "Central Park")],
    )
    assert "DELETE DATA" in sparql
    assert "GRAPH <https://graph.infona.ai/graphs/t1>" in sparql


def test_select_all_triples():
    sparql = select_triples("https://graph.infona.ai/graphs/t1")
    assert "SELECT ?s ?p ?o" in sparql
    assert "FROM <https://graph.infona.ai/graphs/t1>" in sparql
    assert "LIMIT 100" in sparql


def test_select_with_subject_filter():
    sparql = select_triples(
        "https://graph.infona.ai/graphs/t1",
        subject="https://example.com/place/1",
    )
    assert "<https://example.com/place/1>" in sparql


def test_select_custom_limit():
    sparql = select_triples("https://graph.infona.ai/graphs/t1", limit=50)
    assert "LIMIT 50" in sparql


def test_register_function_triple():
    sparql = register_function_triple(
        "https://graph.infona.ai/graphs/t1",
        entity_type="Place",
        function_name="calculate_distance",
        endpoint_url="https://api.example.com/distance",
        description="Calculate distance between places",
    )
    assert "INSERT DATA" in sparql
    assert "graph.infona.ai/functions/calculate_distance" in sparql
    assert "graph.infona.ai/types/Place" in sparql
    assert "https://api.example.com/distance" in sparql


def test_register_function_triple_enhanced_layer_qualified():
    from infona_client.graph.layers import Layer, enhanced_graph_uri, layer_type_uri

    sparql = register_function_triple(
        "https://graph.infona.ai/graphs/t1",
        entity_type="Organization",
        function_name="lookup_lei",
        endpoint_url="https://api.example.com/lei",
        layer=Layer.ENHANCED,
    )
    assert layer_type_uri(Layer.ENHANCED, "Organization") in sparql
    assert enhanced_graph_uri() in sparql


def test_list_functions_query_all():
    sparql = list_functions_query("https://graph.infona.ai/graphs/t1")
    assert "SELECT" in sparql
    assert "?name" in sparql
    assert "FILTER" not in sparql


def test_list_functions_query_by_type():
    sparql = list_functions_query("https://graph.infona.ai/graphs/t1", entity_type="Place")
    assert "FILTER" in sparql
    assert "graph.infona.ai/types/Place" in sparql


def test_list_functions_query_by_enhanced_type():
    from infona_client.graph.layers import Layer, layer_type_uri

    sparql = list_functions_query(
        "https://graph.infona.ai/graphs/global/enhanced",
        entity_type="Organization",
        layer=Layer.ENHANCED,
    )
    assert "FILTER" in sparql
    assert layer_type_uri(Layer.ENHANCED, "Organization") in sparql


# --------------------------------------------------------------------------- #
# ONTA-414: kg_name can never break out of the graph IRI
# --------------------------------------------------------------------------- #
def test_kg_graph_uri_accepts_a_legal_name():
    from infona_client.graph.queries import kg_graph_uri

    assert (
        kg_graph_uri("acme", "imdb-movies_2")
        == "https://graph.infona.ai/graphs/acme/kg/imdb-movies_2"
    )


@pytest.mark.parametrize(
    "bad",
    [
        # The tenant-isolation break: ">" closes the <...> wrapper so a second
        # FROM naming ANOTHER tenant's graph can be appended to the query.
        "kg> FROM <https://graph.infona.ai/graphs/victim",
        "kg name",
        "kg\nname",
        # TRAILING newline specifically: Python's "$" matches before a final
        # newline, so an "^...$" + .match() guard accepts this. pydantic's Rust
        # "$" does not, so accepting it here would break the stated invariant
        # that this is the same pattern create enforces. Reachable via a path or
        # query param carrying %0A.
        "kg\n",
        "kg\r\n",
        "kg ",
        'kg"name',
        "kg/sub",
        "kg{}",
        "",
        None,
    ],
)
def test_kg_graph_uri_rejects_iri_breaking_names(bad):
    from infona_client.graph.queries import InvalidKGName, kg_graph_uri

    with pytest.raises(InvalidKGName):
        kg_graph_uri("acme", bad)


def test_invalid_kg_name_is_a_value_error():
    """Subclassing ValueError keeps any pre-existing `except ValueError` intact."""
    from infona_client.graph.queries import InvalidKGName

    assert issubclass(InvalidKGName, ValueError)


def test_kg_name_guard_agrees_with_the_create_pattern():
    """The guard must accept exactly what `KGCreate.name` accepts, no more.

    `KGCreate` compiles its pattern with pydantic (Rust regex, strict
    end-of-text). The guard uses Python `re`, whose `$` also matches before a
    final newline. Pinning them against each other is what keeps "the pattern
    create already enforces" a true statement rather than an approximate one.
    """
    import pydantic

    from infona_client.api.routes.knowledge_graphs import KGCreate
    from infona_client.graph.queries import is_valid_kg_name

    for candidate in ["imdb", "a-b_c", "kg\n", "kg ", "kg\r\n", "a/b", "x>y", ""]:
        try:
            KGCreate(name=candidate)
            create_accepts = True
        except pydantic.ValidationError:
            create_accepts = False
        assert is_valid_kg_name(candidate) == create_accepts, candidate


def test_kg_writer_uses_the_shared_guard_not_a_local_copy():
    """The drifted second copy in kg_writer is gone (it accepted "kg\\n")."""
    import infona_client.graph.kg_writer as kw

    assert not hasattr(kw, "_KG_NAME_RE")


def test_kg_graph_uri_round_trips_through_parse():
    from infona_client.graph.queries import kg_graph_uri, parse_kg_graph_uri

    assert parse_kg_graph_uri(kg_graph_uri("acme", "imdb")) == ("acme", "imdb")


def test_tenant_graph_uri_round_trips_through_parse():
    """ONTA-529: bare tenant ontology graph is distinct from per-KG URIs."""
    from infona_client.graph.queries import (
        kg_graph_uri,
        parse_kg_graph_uri,
        parse_tenant_graph_uri,
        tenant_graph_uri,
    )

    assert parse_tenant_graph_uri(tenant_graph_uri("acme")) == "acme"
    # Per-KG and companion URIs must NOT parse as tenant catalog graphs.
    assert parse_tenant_graph_uri(kg_graph_uri("acme", "imdb")) is None
    assert parse_tenant_graph_uri(tenant_graph_uri("acme") + "/versions") is None
    assert parse_kg_graph_uri(tenant_graph_uri("acme")) is None

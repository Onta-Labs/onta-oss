import pytest

from cograph_client.graph.queries import (
    tenant_graph_uri,
    insert_triples,
    delete_triples,
    select_triples,
    register_function_triple,
    list_functions_query,
)


def test_tenant_graph_uri():
    assert tenant_graph_uri("acme") == "https://cograph.tech/graphs/acme"


def test_insert_single_triple():
    sparql = insert_triples(
        "https://cograph.tech/graphs/t1",
        [("https://example.com/place/1", "https://schema.org/name", "Central Park")],
    )
    assert "INSERT DATA" in sparql
    assert "GRAPH <https://cograph.tech/graphs/t1>" in sparql
    assert "<https://example.com/place/1>" in sparql
    assert '"Central Park"' in sparql


def test_insert_multiple_triples():
    sparql = insert_triples(
        "https://cograph.tech/graphs/t1",
        [
            ("https://example.com/place/1", "https://schema.org/name", "Central Park"),
            ("https://example.com/place/1", "https://schema.org/type", "https://schema.org/Park"),
        ],
    )
    assert sparql.count("<https://example.com/place/1>") == 2


def test_delete_triples():
    sparql = delete_triples(
        "https://cograph.tech/graphs/t1",
        [("https://example.com/place/1", "https://schema.org/name", "Central Park")],
    )
    assert "DELETE DATA" in sparql
    assert "GRAPH <https://cograph.tech/graphs/t1>" in sparql


def test_select_all_triples():
    sparql = select_triples("https://cograph.tech/graphs/t1")
    assert "SELECT ?s ?p ?o" in sparql
    assert "FROM <https://cograph.tech/graphs/t1>" in sparql
    assert "LIMIT 100" in sparql


def test_select_with_subject_filter():
    sparql = select_triples(
        "https://cograph.tech/graphs/t1",
        subject="https://example.com/place/1",
    )
    assert "<https://example.com/place/1>" in sparql


def test_select_custom_limit():
    sparql = select_triples("https://cograph.tech/graphs/t1", limit=50)
    assert "LIMIT 50" in sparql


def test_register_function_triple():
    sparql = register_function_triple(
        "https://cograph.tech/graphs/t1",
        entity_type="Place",
        function_name="calculate_distance",
        endpoint_url="https://api.example.com/distance",
        description="Calculate distance between places",
    )
    assert "INSERT DATA" in sparql
    assert "cograph.tech/functions/calculate_distance" in sparql
    assert "cograph.tech/types/Place" in sparql
    assert "https://api.example.com/distance" in sparql


def test_register_function_triple_enhanced_layer_qualified():
    from cograph_client.graph.layers import Layer, enhanced_graph_uri, layer_type_uri

    sparql = register_function_triple(
        "https://cograph.tech/graphs/t1",
        entity_type="Organization",
        function_name="lookup_lei",
        endpoint_url="https://api.example.com/lei",
        layer=Layer.ENHANCED,
    )
    assert layer_type_uri(Layer.ENHANCED, "Organization") in sparql
    assert enhanced_graph_uri() in sparql


def test_list_functions_query_all():
    sparql = list_functions_query("https://cograph.tech/graphs/t1")
    assert "SELECT" in sparql
    assert "?name" in sparql
    assert "FILTER" not in sparql


def test_list_functions_query_by_type():
    sparql = list_functions_query("https://cograph.tech/graphs/t1", entity_type="Place")
    assert "FILTER" in sparql
    assert "cograph.tech/types/Place" in sparql


def test_list_functions_query_by_enhanced_type():
    from cograph_client.graph.layers import Layer, layer_type_uri

    sparql = list_functions_query(
        "https://cograph.tech/graphs/global/enhanced",
        entity_type="Organization",
        layer=Layer.ENHANCED,
    )
    assert "FILTER" in sparql
    assert layer_type_uri(Layer.ENHANCED, "Organization") in sparql


# --------------------------------------------------------------------------- #
# ONTA-414: kg_name can never break out of the graph IRI
# --------------------------------------------------------------------------- #
def test_kg_graph_uri_accepts_a_legal_name():
    from cograph_client.graph.queries import kg_graph_uri

    assert (
        kg_graph_uri("acme", "imdb-movies_2")
        == "https://cograph.tech/graphs/acme/kg/imdb-movies_2"
    )


@pytest.mark.parametrize(
    "bad",
    [
        # The tenant-isolation break: ">" closes the <...> wrapper so a second
        # FROM naming ANOTHER tenant's graph can be appended to the query.
        "kg> FROM <https://cograph.tech/graphs/victim",
        "kg name",
        "kg\nname",
        'kg"name',
        "kg/sub",
        "kg{}",
        "",
        None,
    ],
)
def test_kg_graph_uri_rejects_iri_breaking_names(bad):
    from cograph_client.graph.queries import InvalidKGName, kg_graph_uri

    with pytest.raises(InvalidKGName):
        kg_graph_uri("acme", bad)


def test_invalid_kg_name_is_a_value_error():
    """Subclassing ValueError keeps any pre-existing `except ValueError` intact."""
    from cograph_client.graph.queries import InvalidKGName

    assert issubclass(InvalidKGName, ValueError)


def test_kg_graph_uri_round_trips_through_parse():
    from cograph_client.graph.queries import kg_graph_uri, parse_kg_graph_uri

    assert parse_kg_graph_uri(kg_graph_uri("acme", "imdb")) == ("acme", "imdb")

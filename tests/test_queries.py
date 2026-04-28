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


def test_list_functions_query_all():
    sparql = list_functions_query("https://cograph.tech/graphs/t1")
    assert "SELECT" in sparql
    assert "?name" in sparql
    assert "FILTER" not in sparql


def test_list_functions_query_by_type():
    sparql = list_functions_query("https://cograph.tech/graphs/t1", entity_type="Place")
    assert "FILTER" in sparql
    assert "cograph.tech/types/Place" in sparql

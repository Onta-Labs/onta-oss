from cograph_client.graph.ontology_queries import (
    insert_type,
    insert_attribute,
    insert_subtype,
    list_types_query,
    get_type_detail_query,
    get_type_attributes_query,
    get_subtypes_query,
    get_type_functions_query,
    get_full_ontology_query,
    type_uri,
    attr_uri,
)

GRAPH = "https://cograph.tech/graphs/test"


def test_type_uri():
    assert type_uri("Place") == "https://cograph.tech/types/Place"


def test_attr_uri():
    assert attr_uri("Place", "name") == "https://cograph.tech/types/Place/attrs/name"


def test_insert_type_basic():
    sparql = insert_type(GRAPH, "Place")
    assert "INSERT DATA" in sparql
    assert "GRAPH <https://cograph.tech/graphs/test>" in sparql
    assert "cograph.tech/types/Place" in sparql
    assert "Class" in sparql
    assert '"Place"' in sparql


def test_insert_type_with_description():
    sparql = insert_type(GRAPH, "Place", description="A geographic location")
    assert "A geographic location" in sparql


def test_insert_type_with_parent():
    sparql = insert_type(GRAPH, "Park", parent_type="Place")
    assert "subClassOf" in sparql
    assert "cograph.tech/types/Place" in sparql


def test_insert_attribute():
    sparql = insert_attribute(GRAPH, "Place", "name", "The name", "string")
    assert "INSERT DATA" in sparql
    assert "cograph.tech/types/Place/attrs/name" in sparql
    assert "Property" in sparql
    assert "domain" in sparql
    assert "range" in sparql
    assert "string" in sparql


def test_insert_attribute_datetime():
    sparql = insert_attribute(GRAPH, "Event", "startDate", datatype="datetime")
    assert "dateTime" in sparql


def test_insert_subtype():
    sparql = insert_subtype(GRAPH, "Place", "Park")
    assert "subClassOf" in sparql
    assert "cograph.tech/types/Park" in sparql
    assert "cograph.tech/types/Place" in sparql


def test_list_types_query():
    sparql = list_types_query(GRAPH)
    assert "SELECT" in sparql
    assert "Class" in sparql
    assert "FROM <https://cograph.tech/graphs/test>" in sparql


def test_get_type_detail_query():
    sparql = get_type_detail_query(GRAPH, "Place")
    assert "cograph.tech/types/Place" in sparql
    assert "label" in sparql


def test_get_type_attributes_query():
    sparql = get_type_attributes_query(GRAPH, "Place")
    assert "domain" in sparql
    assert "cograph.tech/types/Place" in sparql


def test_get_subtypes_query():
    sparql = get_subtypes_query(GRAPH, "Place")
    assert "subClassOf" in sparql


def test_get_type_functions_query():
    sparql = get_type_functions_query(GRAPH, "Place")
    assert "attachedTo" in sparql
    assert "cograph.tech/types/Place" in sparql


def test_get_full_ontology_query():
    sparql = get_full_ontology_query(GRAPH)
    assert "Class" in sparql
    assert "domain" in sparql
    assert "attachedTo" in sparql

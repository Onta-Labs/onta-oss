"""COG-82: atomic-upsert ontology builders.

Pure string-level assertions — no Neptune, no network. These guard that
``upsert_type`` / ``upsert_attribute`` REPLACE single-valued predicates
(rdfs:comment, rdfs:subClassOf, rdfs:range) via DELETE/INSERT/WHERE rather than
blindly appending like the legacy ``insert_*`` builders.
"""

from cograph_client.graph.ontology_queries import (
    upsert_type,
    upsert_attribute,
    type_uri,
    attr_uri,
    XSD,
)

GRAPH = "https://cograph.tech/graphs/test"


def test_upsert_type_description_replaces_not_appends():
    # A description must produce a DELETE clause for the single-valued
    # rdfs:comment AND an INSERT carrying the new comment, so re-asserting a
    # changed description replaces rather than appends.
    sparql = upsert_type(GRAPH, "Place", description="A geographic location")
    uri = type_uri("Place")
    assert "DELETE" in sparql
    assert f"<{uri}> <http://www.w3.org/2000/01/rdf-schema#comment> ?c" in sparql
    assert "INSERT" in sparql
    assert '"A geographic location"' in sparql
    # The DELETE for comment must precede / accompany the matching INSERT.
    assert "comment" in sparql


def test_upsert_type_changing_description_replaces():
    # Two calls with different descriptions both emit a DELETE for the
    # single-valued predicate — i.e. the second write would replace, not stack.
    first = upsert_type(GRAPH, "Place", description="old text")
    second = upsert_type(GRAPH, "Place", description="new text")
    for sparql in (first, second):
        assert "DELETE { GRAPH <%s> { <%s> <http://www.w3.org/2000/01/rdf-schema#comment> ?c } }" % (
            GRAPH,
            type_uri("Place"),
        ) in sparql
    assert '"old text"' in first and '"old text"' not in second
    assert '"new text"' in second


def test_upsert_type_changing_parent_replaces():
    # Re-parenting must DELETE the old subClassOf, not append a second parent.
    sparql = upsert_type(GRAPH, "Park", parent_type="Place")
    uri = type_uri("Park")
    assert f"DELETE {{ GRAPH <{GRAPH}> {{ <{uri}> <http://www.w3.org/2000/01/rdf-schema#subClassOf> ?p }} }}" in sparql
    assert "INSERT" in sparql
    assert type_uri("Place") in sparql
    assert "subClassOf" in sparql


def test_upsert_type_empty_description_clears_no_insert():
    # Authoritative-upsert choice: no description still DELETEs any existing
    # comment (clearing stale state) but inserts no new comment value.
    sparql = upsert_type(GRAPH, "Place")
    uri = type_uri("Place")
    assert f"DELETE {{ GRAPH <{GRAPH}> {{ <{uri}> <http://www.w3.org/2000/01/rdf-schema#comment> ?c }} }}" in sparql
    # No INSERT of a comment literal when description is empty.
    assert "#comment> \"" not in sparql


def test_upsert_type_none_parent_clears_no_insert():
    # No parent still DELETEs any existing subClassOf (un-roots the type) but
    # inserts no new parent edge.
    sparql = upsert_type(GRAPH, "Place")
    uri = type_uri("Place")
    assert f"DELETE {{ GRAPH <{GRAPH}> {{ <{uri}> <http://www.w3.org/2000/01/rdf-schema#subClassOf> ?p }} }}" in sparql
    assert "#subClassOf> <" not in sparql


def test_upsert_type_label_and_class_are_plain_inserts():
    # Idempotence sanity: rdf:type rdfs:Class and rdfs:label are plain
    # INSERT DATA (no DELETE for those triples).
    sparql = upsert_type(GRAPH, "Place")
    uri = type_uri("Place")
    assert "INSERT DATA" in sparql
    assert f'<{uri}> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2000/01/rdf-schema#Class> .' in sparql
    assert f'<{uri}> <http://www.w3.org/2000/01/rdf-schema#label> "Place" .' in sparql


def test_upsert_attribute_range_replaced():
    # rdfs:range is single-valued — must be DELETE/INSERT, always with a fresh
    # value (primitive XSD here).
    sparql = upsert_attribute(GRAPH, "Place", "name", "The name", "string")
    a_uri = attr_uri("Place", "name")
    assert f"DELETE {{ GRAPH <{GRAPH}> {{ <{a_uri}> <http://www.w3.org/2000/01/rdf-schema#range> ?r }} }}" in sparql
    assert f"<{XSD}#string>" in sparql
    assert "INSERT" in sparql


def test_upsert_attribute_range_to_type_uri_replaces_old():
    # When datatype is another type name (the attribute became a relationship),
    # rdfs:range must hold a types/ URI and the old range is DELETEd.
    sparql = upsert_attribute(GRAPH, "Person", "employer", datatype="Company")
    a_uri = attr_uri("Person", "employer")
    assert f"DELETE {{ GRAPH <{GRAPH}> {{ <{a_uri}> <http://www.w3.org/2000/01/rdf-schema#range> ?r }} }}" in sparql
    assert type_uri("Company") in sparql
    # The XSD primitive form must NOT appear — range points at the type URI.
    assert f"<{XSD}#string>" not in sparql


def test_upsert_attribute_comment_replaced():
    sparql = upsert_attribute(GRAPH, "Place", "name", description="The display name")
    a_uri = attr_uri("Place", "name")
    assert f"DELETE {{ GRAPH <{GRAPH}> {{ <{a_uri}> <http://www.w3.org/2000/01/rdf-schema#comment> ?c }} }}" in sparql
    assert '"The display name"' in sparql


def test_upsert_attribute_empty_comment_clears_no_insert():
    sparql = upsert_attribute(GRAPH, "Place", "name")
    a_uri = attr_uri("Place", "name")
    assert f"DELETE {{ GRAPH <{GRAPH}> {{ <{a_uri}> <http://www.w3.org/2000/01/rdf-schema#comment> ?c }} }}" in sparql
    assert "#comment> \"" not in sparql


def test_upsert_attribute_property_domain_label_plain_inserts():
    # Idempotence sanity: rdf:type rdf:Property, rdfs:label, rdfs:domain are
    # plain INSERT DATA triples.
    sparql = upsert_attribute(GRAPH, "Place", "name")
    a_uri = attr_uri("Place", "name")
    t_uri = type_uri("Place")
    assert "INSERT DATA" in sparql
    assert f'<{a_uri}> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/1999/02/22-rdf-syntax-ns#Property> .' in sparql
    assert f'<{a_uri}> <http://www.w3.org/2000/01/rdf-schema#label> "name" .' in sparql
    assert f'<{a_uri}> <http://www.w3.org/2000/01/rdf-schema#domain> <{t_uri}> .' in sparql


def test_upsert_multi_operation_separator():
    # Multiple DELETE/INSERT/WHERE operations are joined with `;` per SPARQL.
    sparql = upsert_type(GRAPH, "Place", description="d", parent_type="Thing")
    assert " ;\n" in sparql

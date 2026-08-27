"""INF-599: planner bind for subclass attribute predicates (ADR 0001).

Instance triples stay on the asserted leaf (`types/Contact/attrs/first_name`).
A Person ask that only binds `types/Person/attrs/first_name` returns empty
names. These tests pin the bind + SPARQL pattern — no live /ask, no LLM.
"""

from __future__ import annotations

from infona_client.graph.ontology_queries import (
    attr_uri,
    rewrite_parent_attr_to_subclass_predicates,
    rewrite_type_predicate_to_closure,
)
from infona_client.graph.rdfs_helpers import (
    bind_subclass_attribute,
    subclass_attribute_predicates,
    type_names_with_subclasses,
)
from infona_client.nlp.cypher_stub_basic import try_stub_count_query
from infona_client.nlp.pipeline_sparql_fix import PipelineSparqlFixMixin

CHILD_TO_PARENT = {"Contact": "Person", "Staff": "Person"}
ONTOLOGY = """\
Type: Person [no instances]
  parent:
  - first_name: string (literal, key=first_name) [no instances]
Type: Contact (20 entities)
  parent: Person
  - first_name: string (literal, key=first_name)
  - spent: float (literal, key=spent)
Type: Staff (4 entities)
  parent: Person
  - first_name: string (literal, key=first_name)
"""

PERSON_FIRST = attr_uri("Person", "first_name")
CONTACT_FIRST = attr_uri("Contact", "first_name")
STAFF_FIRST = attr_uri("Staff", "first_name")


def test_how_many_people_binds_contact_and_staff():
    expanded = type_names_with_subclasses(
        "Person", child_to_parent=CHILD_TO_PARENT
    )
    assert "Contact" in expanded
    assert "Staff" in expanded
    counts = {"Contact": 20, "Staff": 4, "Person": 0}
    # Synthetic: subclass closure counts contacts+staff; Person instances stay 0.
    assert sum(counts[t] for t in expanded) == 24
    assert counts["Person"] == 0

    q = (
        "SELECT (COUNT(?x) AS ?n) WHERE { "
        "?x a <https://graph.infona.ai/types/Person> }"
    )
    closed = rewrite_type_predicate_to_closure(q)
    assert "subClassOf>" in closed

    fixture = try_stub_count_query(
        "how many people?", ONTOLOGY, type_names=["Person", "Contact", "Staff"]
    )
    assert fixture is not None
    assert fixture["params"]["type_names"]
    assert set(fixture["params"]["type_names"]) >= {"Contact", "Staff"}
    assert "Person" in fixture["params"]["type_names"]


def test_list_first_names_binds_child_predicates_not_only_person():
    bind = bind_subclass_attribute(
        "Person", "first_name", child_to_parent=CHILD_TO_PARENT
    )
    predicates = bind["predicates"]
    assert CONTACT_FIRST in predicates
    assert STAFF_FIRST in predicates
    assert predicates != [PERSON_FIRST]
    assert bind["prop_key"] == "first_name"
    assert set(bind["type_names"]) >= {"Contact", "Staff", "Person"}

    only_parent = subclass_attribute_predicates(
        "Person", "first_name", child_to_parent={}
    )
    # No subclass map → parent-only (the empty-names failure mode).
    assert only_parent == [PERSON_FIRST]


def test_sparql_person_first_name_rewrites_to_contact_staff_values():
    q = (
        "SELECT ?n WHERE { "
        "?x a <https://graph.infona.ai/types/Person> . "
        f"?x <{PERSON_FIRST}> ?n ."
        "}"
    )
    out = rewrite_parent_attr_to_subclass_predicates(q, CHILD_TO_PARENT)
    assert CONTACT_FIRST in out
    assert STAFF_FIRST in out
    assert "?_subattr_first_name" in out
    # Parent IRI may still appear in VALUES; the triple must not use it alone.
    assert f"?x <{PERSON_FIRST}>" not in out
    assert "?x ?_subattr_first_name" in out
    assert "VALUES ?_subattr_first_name" in out


def test_sparql_contact_ask_does_not_expand_to_staff():
    q = (
        "SELECT ?n WHERE { "
        "?x a <https://graph.infona.ai/types/Contact> . "
        f"?x <{CONTACT_FIRST}> ?n ."
        "}"
    )
    out = rewrite_parent_attr_to_subclass_predicates(q, CHILD_TO_PARENT)
    assert out == q
    assert STAFF_FIRST not in out


def test_pipeline_fix_uses_ontology_parent_map():
    q = (
        "SELECT ?n WHERE { "
        "?x a <https://graph.infona.ai/types/Person> . "
        f"?x <{PERSON_FIRST}> ?n ."
        "}"
    )
    out = PipelineSparqlFixMixin._fix_common_sparql_issues(q, ONTOLOGY)
    assert CONTACT_FIRST in out
    assert STAFF_FIRST in out


def test_rewrite_idempotent():
    q = (
        "SELECT ?n WHERE { "
        "?x a <https://graph.infona.ai/types/Person> . "
        f"?x <{PERSON_FIRST}> ?n ."
        "}"
    )
    once = rewrite_parent_attr_to_subclass_predicates(q, CHILD_TO_PARENT)
    twice = rewrite_parent_attr_to_subclass_predicates(once, CHILD_TO_PARENT)
    assert once == twice

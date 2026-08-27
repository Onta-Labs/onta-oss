"""INF-599: live Cypher /ask bind for subclass types + child attr predicates.

Instance triples stay on the asserted leaf (`types/Contact/attrs/first_name`).
Staff in the CRM fixture declares ``name``, not ``first_name`` — do not claim
Staff.first_name. Product NL is LLM Cypher; SPARQL rewrite is not this path.
"""

from __future__ import annotations

import inspect

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
from infona_client.nlp.cypher_subclass_bind import expand_cypher_subclass_binds
from infona_client.nlp.empty_type_guard import honest_empty_targets
from infona_client.nlp.pipeline_ask import PipelineAskMixin
from infona_client.nlp.pipeline_sparql_fix import PipelineSparqlFixMixin

CHILD_TO_PARENT = {"Contact": "Person", "Staff": "Person"}
# Staff has name/email, NOT first_name (CRM staff.csv).
ONTOLOGY = """\
Type: Person [no instances]
  - first_name: string (literal, key=first_name) [no instances]
Type: Contact (20 entities)
  parent: Person
  - first_name: string (literal, key=first_name)
  - spent: float (literal, key=spent)
Type: Staff (4 entities)
  parent: Person
  - name: string (literal, key=name)
  - email: string (literal, key=email)
Type: Sprocket [no instances]
"""

PERSON_FIRST = attr_uri("Person", "first_name")
CONTACT_FIRST = attr_uri("Contact", "first_name")
STAFF_FIRST = attr_uri("Staff", "first_name")

PERSON_ONLY_COUNT = (
    "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->"
    "(c:Class {tenant_id: $tenant_id, kg: $kg}) "
    "WHERE c.name IN ['Person'] "
    "RETURN count(DISTINCT e) AS n"
)
PERSON_ONLY_FIRST_NAME = (
    "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->"
    "(c:Class {tenant_id: $tenant_id, kg: $kg}) "
    "WHERE c.name IN $type_names "
    "OPTIONAL MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg, subject_id: e.id})"
    "-[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg}) "
    "WHERE p.name = $prop_key "
    "RETURN e.name AS name, a.literal_value AS first_name"
)


def test_how_many_people_binds_contact_and_staff():
    expanded = type_names_with_subclasses(
        "Person", child_to_parent=CHILD_TO_PARENT
    )
    assert "Contact" in expanded
    assert "Staff" in expanded

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
    assert set(fixture["params"]["type_names"]) >= {"Contact", "Staff"}


def test_live_cypher_count_expands_person_only_type_names():
    """Goes red if expand_cypher_subclass_binds is not applied to /ask params."""
    cypher, params = expand_cypher_subclass_binds(
        PERSON_ONLY_COUNT,
        {"type_names": ["Person"]},
        ONTOLOGY,
    )
    assert set(params["type_names"]) >= {"Contact", "Staff", "Person"}
    assert "Contact" in cypher and "Staff" in cypher
    assert "IN ['Person']" not in cypher or "Contact" in cypher


def test_live_cypher_first_name_is_not_person_iri_only():
    """List-names: Contact.first_name bound; Staff.first_name NOT claimed."""
    cypher, params = expand_cypher_subclass_binds(
        PERSON_ONLY_FIRST_NAME,
        {"type_names": ["Person"], "prop_key": "first_name"},
        ONTOLOGY,
    )
    assert set(params["type_names"]) >= {"Contact", "Staff"}
    predicates = params.get("predicates") or []
    assert CONTACT_FIRST in predicates
    assert STAFF_FIRST not in predicates
    assert predicates != [PERSON_FIRST]
    assert params["prop_key"] == "first_name"
    # Plan is Cypher leaf + subclass types, not a Person-only attr IRI.
    assert "types/Person/attrs/first_name" not in cypher or CONTACT_FIRST in str(
        predicates
    )


def test_pipeline_ask_wires_cypher_subclass_bind():
    """Drift: deleting the /ask call must go red (SPARQL-only is theater)."""
    src = inspect.getsource(PipelineAskMixin._ask_cypher)
    assert "expand_cypher_subclass_binds" in src


def test_list_first_names_binds_contact_not_undeclared_staff():
    bind = bind_subclass_attribute(
        "Person",
        "first_name",
        child_to_parent=CHILD_TO_PARENT,
        ontology_summary=ONTOLOGY,
    )
    predicates = bind["predicates"]
    assert CONTACT_FIRST in predicates
    assert STAFF_FIRST not in predicates
    assert predicates != [PERSON_FIRST]
    assert set(bind["type_names"]) >= {"Contact", "Staff", "Person"}

    only_parent = subclass_attribute_predicates(
        "Person", "first_name", child_to_parent={}
    )
    assert only_parent == [PERSON_FIRST]


def test_sparql_person_first_name_rewrites_to_contact_values():
    q = (
        "SELECT ?n WHERE { "
        "?x a <https://graph.infona.ai/types/Person> . "
        f"?x <{PERSON_FIRST}> ?n ."
        "}"
    )
    out = rewrite_parent_attr_to_subclass_predicates(
        q, CHILD_TO_PARENT, ontology_summary=ONTOLOGY
    )
    assert CONTACT_FIRST in out
    assert STAFF_FIRST not in out
    assert "?_subattr_first_name" in out
    assert f"?x <{PERSON_FIRST}>" not in out
    assert "VALUES ?_subattr_first_name" in out


def test_sparql_contact_ask_does_not_expand_to_staff():
    q = (
        "SELECT ?n WHERE { "
        "?x a <https://graph.infona.ai/types/Contact> . "
        f"?x <{CONTACT_FIRST}> ?n ."
        "}"
    )
    out = rewrite_parent_attr_to_subclass_predicates(
        q, CHILD_TO_PARENT, ontology_summary=ONTOLOGY
    )
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
    assert STAFF_FIRST not in out


def test_rewrite_idempotent():
    q = (
        "SELECT ?n WHERE { "
        "?x a <https://graph.infona.ai/types/Person> . "
        f"?x <{PERSON_FIRST}> ?n ."
        "}"
    )
    once = rewrite_parent_attr_to_subclass_predicates(
        q, CHILD_TO_PARENT, ontology_summary=ONTOLOGY
    )
    twice = rewrite_parent_attr_to_subclass_predicates(
        once, CHILD_TO_PARENT, ontology_summary=ONTOLOGY
    )
    assert once == twice


def test_empty_type_guard_does_not_freeze_person_when_subclasses_have_rows():
    """Person [no instances] + Contact/Staff rows is NOT an honest 0-people."""
    person_cypher = PERSON_ONLY_COUNT
    assert (
        honest_empty_targets(
            "how many Person records are there",
            person_cypher,
            ONTOLOGY,
            params={"type_names": ["Person"]},
        )
        == set()
    )
    sprocket_q = (
        "MATCH (e:Entity)-[:INSTANCE_OF]->(c:Class) "
        "WHERE c.name IN ['Sprocket'] RETURN count(DISTINCT e) AS n"
    )
    assert honest_empty_targets(
        "list all Sprockets",
        sprocket_q,
        ONTOLOGY,
        params={"type_names": ["Sprocket"]},
    ) == {"Sprocket"}

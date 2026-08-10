"""ADR 0013 semantic helpers — hermetic unit tests (no live Neo4j)."""

from __future__ import annotations

import pytest

from cograph_client.graph.iri import IRI_BASE
from cograph_client.graph.memory_store import MemoryGraphStore
from cograph_client.graph.ontology_catalog import upsert_type_pg
from cograph_client.graph.rdfs_helpers import (
    TEMPLATE_ENTITIES_OF_TYPE,
    TEMPLATE_ENTITIES_OF_TYPE_COUNT,
    TEMPLATE_LITERAL_VALUES,
    TEMPLATE_RELATED_ENTITIES,
    descendants_of,
    extract_subclass_map_from_ontology,
    semantic_templates,
    type_names_with_subclasses,
)
from cograph_client.graph.schema_bootstrap import TEMPLATES, get_template
from cograph_client.graph.scope import GraphScope
from cograph_client.nlp.cypher_generate import (
    try_deterministic_cypher,
    try_list_query,
    try_stub_count_query,
)


def test_semantic_templates_registered_in_bootstrap():
    names = set(semantic_templates())
    for n in names:
        assert n in TEMPLATES
        tmpl = get_template(n)
        assert tmpl.writing is False
        assert "$tenant_id" in tmpl.cypher
        assert "$kg" in tmpl.cypher


def test_descendants_of_transitive():
    child_to_parent = {"Dog": "Mammal", "Cat": "Mammal", "Mammal": "Animal"}
    assert descendants_of("Animal", child_to_parent) == [
        "Animal",
        "Cat",
        "Dog",
        "Mammal",
    ]
    assert descendants_of("Mammal", child_to_parent) == ["Mammal", "Cat", "Dog"]
    assert descendants_of("Dog", child_to_parent) == ["Dog"]


def test_extract_subclass_map_from_ontology_summary():
    text = (
        "Type: Animal (1 entities)\n"
        "Type: Dog\n"
        "  parent: Animal\n"
        "Type: Cat\n"
        "  parent: Animal\n"
        "  - name: string\n"
    )
    m = extract_subclass_map_from_ontology(text)
    assert m == {"Dog": "Animal", "Cat": "Animal"}
    expanded = type_names_with_subclasses("Animal", ontology_summary=text)
    assert expanded == ["Animal", "Cat", "Dog"]


def test_count_fixture_uses_entities_of_type_count_template():
    onto = "Type: Book\n  - title"
    payload = try_stub_count_query("How many books?", onto)
    assert payload is not None
    assert payload["template"] == TEMPLATE_ENTITIES_OF_TYPE_COUNT
    assert payload["params"]["type_names"] == ["Book"]
    assert "primary_type" not in payload["params"]


def test_list_fixture_uses_entities_of_type_with_subclass_expansion():
    onto = (
        "Type: Animal\n"
        "Type: Dog\n"
        "  parent: Animal\n"
        "Type: Cat\n"
        "  parent: Animal\n"
    )
    payload = try_list_query("list all animals", onto)
    assert payload is not None
    assert payload["template"] == TEMPLATE_ENTITIES_OF_TYPE
    assert set(payload["params"]["type_names"]) == {"Animal", "Dog", "Cat"}
    assert payload["params"]["type_names"][0] == "Animal"


def test_filter_and_hop_use_semantic_template_names():
    onto = "Type: Book\nType: Author\n  - name"
    filt = try_deterministic_cypher("books where name is Dune", onto)
    assert filt is not None
    assert filt["template"] == TEMPLATE_LITERAL_VALUES
    assert filt["params"]["type_names"] == ["Book"]

    hop = try_deterministic_cypher("authors of books", onto)
    assert hop is not None
    assert hop["template"] == TEMPLATE_RELATED_ENTITIES
    assert hop["params"]["from_types"] == ["Book"]
    assert hop["params"]["to_types"] == ["Author"]


@pytest.mark.asyncio
async def test_entities_of_type_count_with_subclass_e2e_memory():
    """Type + subclass semantic template returns parent∪descendant entities.

    Membership is via INSTANCE_OF → Class (ADR 0013), not primary_type alone.
    """
    from cograph_client.graph.ontology_queries import type_uri
    from cograph_client.graph.rdf_model import AssertionFact, assert_fact, set_subclass_of

    store = MemoryGraphStore()
    cat = store.session(
        GraphScope.for_catalog(layer="tenant", tenant_id="demo-tenant")
    )
    await upsert_type_pg(cat, name="Animal", description="root")
    await upsert_type_pg(cat, name="Dog", description="bark", parent_type="Animal")
    scope = GraphScope.for_instance("demo-tenant", "zoo")
    session = store.session(scope)

    animal_id = type_uri("Animal")
    dog_id = type_uri("Dog")
    await set_subclass_of(session, dog_id, animal_id)

    a1 = f"{IRI_BASE}/entities/Animal/a1"
    d1 = f"{IRI_BASE}/entities/Dog/d1"
    d2 = f"{IRI_BASE}/entities/Dog/d2"
    for eid, tleaf, name in (
        (a1, "Animal", "Generic"),
        (d1, "Dog", "Fido"),
        (d2, "Dog", "Rex"),
    ):
        await session.write_merge_entity(
            id=eid, primary_type=tleaf, name=name, source="test"
        )
        await assert_fact(
            session,
            AssertionFact(subject_id=eid, kind="type", value=tleaf),
            dual_write_cache=True,
        )

    onto = (
        "Type: Animal\n"
        "Type: Dog\n"
        "  parent: Animal\n"
    )
    payload = try_stub_count_query("How many animals?", onto)
    assert payload is not None
    assert payload["template"] == TEMPLATE_ENTITIES_OF_TYPE_COUNT
    assert set(payload["params"]["type_names"]) == {"Animal", "Dog"}

    rows = await session.execute_template(
        TEMPLATE_ENTITIES_OF_TYPE_COUNT,
        {"type_names": payload["params"]["type_names"]},
    )
    assert rows[0].get("n") == 3

    listed = await session.execute_template(
        TEMPLATE_ENTITIES_OF_TYPE,
        {
            "type_names": payload["params"]["type_names"],
            "after_id": None,
            "limit": 25,
        },
    )
    names = {r.get("name") for r in listed}
    assert names == {"Generic", "Fido", "Rex"}

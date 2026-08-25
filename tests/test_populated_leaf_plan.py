"""Assertion SoT + honest inventory for /ask planning (Stream B).

Generic Event / Person / Company fixtures only. No founder questions, no
industry vocab. Proves:

1. Empty alias ``title`` is marked ``[no instances]`` when ``event_title``
   holds the literals.
2. A "most recent event" plan that projected ``e.title`` / ``e.date_end`` is
   remapped onto populated siblings / rejected as empty Entity cache.
3. Kind-filter sketch prefers a populated typed enum on the asked type over
   unstructured ``category`` on a related type.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from infona_client.graph.iri import IRI_BASE
from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_catalog import upsert_attribute, upsert_type
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.store import reset_graph_store_for_tests
from infona_client.nlp.cypher_generate import ontology_from_graph_store
from infona_client.nlp.planning_schema import (
    NO_INSTANCES_MARK,
    PlanningSlot,
    PlanningType,
    format_planning_ontology,
)
from infona_client.nlp.populated_leaf_plan import (
    classify_literals_by_type,
    empty_entity_cache_hits,
    format_leaf_grounding_notes,
    populated_sibling_leaf,
    prefer_kind_filter_leaf,
    remap_empty_cache_projections,
)
from infona_client.nlp.schema_valid_cypher import (
    STRUCTURAL_PROP_KEYS,
    check_schema_valid_cypher,
)

TENANT = "leaf-plan-tenant"
KG = "leaf-plan-kg"
GRAPH = f"{IRI_BASE}/graphs/{TENANT}/kg/{KG}"

TYPE_EVENT = "Event"
TYPE_PERSON = "Person"
TYPE_COMPANY = "Company"

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"

NLP_ROOT = Path(__file__).resolve().parents[1] / "infona_client" / "nlp"
_BANNED_VOCAB = ("Venture capital", "vc_categories", "meetings-notion")


def _event_ontology_text() -> str:
    return format_planning_ontology(
        [
            PlanningType(
                name=TYPE_EVENT,
                entity_count=3,
                slots=(
                    PlanningSlot(
                        name="event_title",
                        kind="literal",
                        datatype="string",
                        populated=True,
                        count=3,
                    ),
                    PlanningSlot(
                        name="event_date",
                        kind="literal",
                        datatype="datetime",
                        populated=True,
                        count=3,
                    ),
                    PlanningSlot(
                        name="event_kind",
                        kind="literal",
                        datatype="enum",
                        populated=True,
                        count=3,
                    ),
                    PlanningSlot(
                        name="title",
                        kind="literal",
                        datatype="string",
                        populated=False,
                        count=0,
                    ),
                    PlanningSlot(
                        name="date_end",
                        kind="literal",
                        datatype="datetime",
                        populated=False,
                        count=0,
                    ),
                ),
            ),
            PlanningType(
                name=TYPE_COMPANY,
                entity_count=2,
                slots=(
                    PlanningSlot(
                        name="company_name",
                        kind="literal",
                        datatype="string",
                        populated=True,
                        count=2,
                    ),
                    PlanningSlot(
                        name="category",
                        kind="literal",
                        datatype="string",
                        populated=True,
                        count=2,
                    ),
                ),
            ),
            PlanningType(
                name=TYPE_PERSON,
                entity_count=2,
                slots=(
                    PlanningSlot(
                        name="person_name",
                        kind="literal",
                        datatype="string",
                        populated=True,
                        count=2,
                    ),
                ),
            ),
        ]
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_inventory_marks_empty_title_alias():
    text = _event_ontology_text()
    by = classify_literals_by_type(text)
    populated, empty = by[TYPE_EVENT]
    assert "event_title" in populated
    assert "event_date" in populated
    assert "title" in empty
    assert "date_end" in empty
    event_block = text[text.index(f"Type: {TYPE_EVENT}") :]
    title_line = next(ln for ln in event_block.splitlines() if ln.strip().startswith("- title:"))
    event_title_line = next(
        ln for ln in event_block.splitlines() if "event_title:" in ln
    )
    assert NO_INSTANCES_MARK in title_line
    assert NO_INSTANCES_MARK not in event_title_line


def test_populated_sibling_title_and_date():
    pop = ("event_title", "event_date")
    empty = ("title", "date_end")
    assert (
        populated_sibling_leaf(
            "title", populated=pop, empty=empty, type_hint=TYPE_EVENT
        )
        == "event_title"
    )
    assert (
        populated_sibling_leaf(
            "date_end", populated=pop, empty=empty, type_hint=TYPE_EVENT
        )
        == "event_date"
    )
    assert (
        populated_sibling_leaf("event_title", populated=pop, empty=empty) is None
    )


def test_most_recent_event_plan_projects_event_title_not_e_title():
    """Production failure class: RETURN e.title / e.date_end on empty cache."""
    onto = _event_ontology_text()
    cypher = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names
RETURN e.title AS title, e.date_end AS date_end
ORDER BY e.date_end DESC LIMIT 1
""".strip()
    params = {"type_names": [TYPE_EVENT]}
    hits = empty_entity_cache_hits(cypher, onto, params=params)
    empty_leaves = {h[1].lower() for h in hits}
    suggested = {h[2] for h in hits}
    assert "title" in empty_leaves
    assert "date_end" in empty_leaves
    assert "event_title" in suggested
    assert "event_date" in suggested

    rewritten = remap_empty_cache_projections(cypher, onto, params=params)
    assert "e.event_title" in rewritten
    assert "e.event_date" in rewritten
    assert "e.title" not in rewritten
    assert "e.date_end" not in rewritten
    assert "Assertion" not in rewritten or "event_title" in rewritten


def test_schema_valid_rejects_empty_title_cache():
    onto = _event_ontology_text()
    cypher = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names
RETURN e.title AS title, e.date_end AS date_end
""".strip()
    r = check_schema_valid_cypher(
        cypher, onto, params={"type_names": [TYPE_EVENT]}
    )
    assert not r.ok, r.reason
    assert "title" in (r.reason or "").lower()
    assert "event_title" in (r.reason or "")
    assert "literal_value" in (r.reason or "")
    assert "title" not in STRUCTURAL_PROP_KEYS


def test_schema_valid_accepts_event_title_and_assertion():
    onto = _event_ontology_text()
    cypher = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names
MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg, subject_id: e.id})-[:PREDICATE]->(p:Property {
  tenant_id: $tenant_id, kg: $kg
})
WHERE p.name = $prop_key
RETURN a.literal_value AS event_title
ORDER BY a.literal_value DESC LIMIT 1
""".strip()
    r = check_schema_valid_cypher(
        cypher,
        onto,
        params={"type_names": [TYPE_EVENT], "prop_key": "event_title"},
    )
    assert r.ok, r.reason


def test_template_does_not_waive_empty_entity_cache():
    """template=related_entities must not skip empty-cache on executing Cypher.

    Assertion-shaped body (no invented rels) may execute_read even when a
    template name is set. Projecting the empty ``title`` alias while
    ``event_title`` is populated must fail schema-valid — not ``template
    schema ok``.
    """
    onto = _event_ontology_text()
    cypher = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names
MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg})-[:SUBJECT]->(e)
MATCH (a)-[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg})
WHERE p.name = $prop_key
RETURN e.title AS title
""".strip()
    r = check_schema_valid_cypher(
        cypher,
        onto,
        params={"type_names": [TYPE_EVENT], "prop_key": "event_title"},
        template="related_entities",
    )
    assert not r.ok, r.reason
    assert (r.reason or "") != "template schema ok"
    assert "title" in (r.reason or "").lower()
    assert "event_title" in (r.reason or "")
    rewritten = remap_empty_cache_projections(
        cypher, onto, params={"type_names": [TYPE_EVENT]}
    )
    assert "e.event_title" in rewritten
    assert "e.title" not in rewritten


def test_invented_rel_template_rescue_skips_empty_cache_on_unexecuted_cypher():
    """Lowercase typed hop + template stays schema-ok (execute-time rescue)."""
    onto = _event_ontology_text() + "\n  Relationships: hosts → Person\n"
    cypher = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names
MATCH (e)-[:hosts]->(p:Entity {tenant_id: $tenant_id, kg: $kg})
RETURN e.title AS title
""".strip()
    r = check_schema_valid_cypher(
        cypher,
        onto,
        params={"type_names": [TYPE_EVENT]},
        template="related_entities",
    )
    assert r.ok, r.reason
    assert r.reason == "template schema ok"


@dataclass
class _Slot:
    name: str
    kind: str = "literal"
    datatype: str | None = "string"
    populated: bool = False
    range_type: str | None = None
    enum_values: tuple[str, ...] = ()


def test_kind_filter_prefers_asked_enum_over_related_category():
    slots_by_type = {
        TYPE_EVENT: (
            _Slot(
                name="event_kind",
                datatype="enum",
                populated=True,
                enum_values=("Talk", "Workshop"),
            ),
            _Slot(name="event_title", populated=True),
        ),
        TYPE_COMPANY: (
            _Slot(name="category", populated=True),
            _Slot(name="company_name", populated=True),
        ),
        TYPE_PERSON: (_Slot(name="person_name", populated=True),),
    }
    got = prefer_kind_filter_leaf([TYPE_EVENT], slots_by_type)
    assert got == f"{TYPE_EVENT}.event_kind"
    notes = format_leaf_grounding_notes(
        [
            PlanningType(
                name=TYPE_EVENT,
                entity_count=3,
                slots=(
                    PlanningSlot(
                        name="event_kind",
                        kind="literal",
                        datatype="enum",
                        populated=True,
                        count=3,
                    ),
                    PlanningSlot(
                        name="event_title",
                        kind="literal",
                        datatype="string",
                        populated=True,
                        count=3,
                    ),
                ),
            ),
            PlanningType(
                name=TYPE_COMPANY,
                entity_count=2,
                slots=(
                    PlanningSlot(
                        name="category",
                        kind="literal",
                        datatype="string",
                        populated=True,
                        count=2,
                    ),
                ),
            ),
        ],
        asked_types=[TYPE_EVENT],
    )
    assert "event_kind" in notes
    assert "category" in notes
    assert "typed enum" in notes.lower() or "enum/select" in notes.lower()


def test_no_industry_vocab_in_planning_code():
    files = (
        "populated_leaf_plan.py",
        "prompts.py",
        "schema_valid_cypher.py",
        "query_build.py",
        "planning_schema.py",
        "cypher_schema.py",
    )
    for name in files:
        text = (NLP_ROOT / name).read_text(encoding="utf-8")
        for banned in _BANNED_VOCAB:
            assert banned not in text, f"{name} contains {banned!r}"


# ---------------------------------------------------------------------------
# GraphStore inventory
# ---------------------------------------------------------------------------


async def _seed_event_person_company(store: MemoryGraphStore) -> None:
    await upsert_type(
        store=store, name=TYPE_EVENT, description="synthetic event",
        layer="tenant", tenant_id=TENANT,
    )
    await upsert_type(
        store=store, name=TYPE_PERSON, description="synthetic person",
        layer="tenant", tenant_id=TENANT,
    )
    await upsert_type(
        store=store, name=TYPE_COMPANY, description="synthetic company",
        layer="tenant", tenant_id=TENANT,
    )
    for attr, dt in (
        ("title", "string"),
        ("event_title", "string"),
        ("date_end", "datetime"),
        ("event_date", "datetime"),
        ("event_kind", "string"),
    ):
        await upsert_attribute(
            store=store, type_name=TYPE_EVENT, attr_name=attr,
            datatype=dt, layer="tenant", tenant_id=TENANT,
        )
    await upsert_attribute(
        store=store, type_name=TYPE_PERSON, attr_name="person_name",
        datatype="string", layer="tenant", tenant_id=TENANT,
    )
    await upsert_attribute(
        store=store, type_name=TYPE_COMPANY, attr_name="company_name",
        datatype="string", layer="tenant", tenant_id=TENANT,
    )
    await upsert_attribute(
        store=store, type_name=TYPE_COMPANY, attr_name="category",
        datatype="string", layer="tenant", tenant_id=TENANT,
    )
    e1 = entity_uri(TYPE_EVENT, "e1")
    p1 = entity_uri(TYPE_PERSON, "p1")
    c1 = entity_uri(TYPE_COMPANY, "c1")
    triples = [
        (e1, RDF_TYPE, f"{IRI_BASE}/types/{TYPE_EVENT}"),
        (e1, RDFS_LABEL, "Keynote One"),
        (e1, f"{IRI_BASE}/types/{TYPE_EVENT}/attrs/event_title", "Keynote One"),
        (e1, f"{IRI_BASE}/types/{TYPE_EVENT}/attrs/event_date", "2026-04-01"),
        (e1, f"{IRI_BASE}/types/{TYPE_EVENT}/attrs/event_kind", "Talk"),
        (p1, RDF_TYPE, f"{IRI_BASE}/types/{TYPE_PERSON}"),
        (p1, RDFS_LABEL, "Ada Lovelace"),
        (p1, f"{IRI_BASE}/types/{TYPE_PERSON}/attrs/person_name", "Ada Lovelace"),
        (c1, RDF_TYPE, f"{IRI_BASE}/types/{TYPE_COMPANY}"),
        (c1, RDFS_LABEL, "Acme Labs"),
        (c1, f"{IRI_BASE}/types/{TYPE_COMPANY}/attrs/company_name", "Acme Labs"),
        (c1, f"{IRI_BASE}/types/{TYPE_COMPANY}/attrs/category", "software"),
    ]
    await insert_facts(None, GRAPH, triples, store=store)


@pytest.mark.asyncio
async def test_graphstore_inventory_marks_empty_title_alias():
    store = MemoryGraphStore()
    try:
        await _seed_event_person_company(store)
        text, names = await ontology_from_graph_store(
            store, tenant_id=TENANT, kg=KG, prefer_populated=True
        )
        assert text
        assert TYPE_EVENT in names
        assert "event_title" in text
        start = text.index(f"Type: {TYPE_EVENT}")
        rest = text[start:]
        nxt = rest.find("\nType: ", 1)
        block = rest if nxt < 0 else rest[:nxt]
        title_line = next(
            ln for ln in block.splitlines() if "- title:" in ln or ln.strip().startswith("- title:")
        )
        event_title_line = next(ln for ln in block.splitlines() if "event_title" in ln)
        assert NO_INSTANCES_MARK in title_line
        assert NO_INSTANCES_MARK not in event_title_line
        date_end_line = next(ln for ln in block.splitlines() if "date_end" in ln)
        assert NO_INSTANCES_MARK in date_end_line
        assert "event_title" in text
        assert "Empty aliases" in text or "event_title" in text
        cypher = (
            "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
            "RETURN e.title AS title ORDER BY e.date_end DESC LIMIT 1"
        )
        rewritten = remap_empty_cache_projections(
            cypher, text, params={"type_names": [TYPE_EVENT]}
        )
        assert "e.event_title" in rewritten
        assert "e.title" not in rewritten
    finally:
        await store.close()
        reset_graph_store_for_tests()

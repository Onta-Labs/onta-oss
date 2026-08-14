"""Schema-valid allowlist prefers GraphStore / populated inventory over sparse text.

Persona b81a4a / 90ae74: free-form Cypher using leaves that vis + export +
GraphStore show after ingest (``unit_cost``, ``located_at``, ``has_status``, …)
was rejected because the allowlist was built only from partial ontology text.

Anti-overfit: Synth* types / leaves only — no persona CSV gold hardcodes.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from infona_client.graph.iri import IRI_BASE
from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_catalog import (
    OntoAttrRecord,
    SchemaTypeSummary,
    upsert_attribute,
    upsert_type,
)
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.store import reset_graph_store_for_tests
from infona_client.nlp.planning_schema import (
    PlanningSlot,
    PlanningType,
    planning_types_from_schema_and_summaries,
)
from infona_client.nlp.schema_valid_cypher import (
    OntologyLeafInventory,
    check_schema_valid_cypher,
    inventory_from_graph_store,
)

TENANT = "schema-valid-gs-tenant"
KG = "schema-valid-gs-kg"
GRAPH = f"{IRI_BASE}/graphs/{TENANT}/kg/{KG}"

TYPE_WIDGET = "SynthWidget"
TYPE_LOC = "SynthLocation"
TYPE_STATUS = "SynthStatus"

# Instance-populated leaves (may be missing from sparse ontology text).
LEAF_UNIT_COST = "unit_cost"
LEAF_LOCATED_AT = "located_at"
LEAF_HAS_STATUS = "has_status"
LEAF_ASSAY_COST = "assay_cost"
LEAF_IN_STOCK = "in_stock"
LEAF_HAS_STYLE = "has_style"

# Truly invented — must still fail.
INVENTED_REL = "HAS_NOTAREALTHING"
INVENTED_PROP = "not_a_real_attr_xyz"


# Sparse ontology text: type present, but missing the live inventory leaves.
SPARSE_ONTO = (
    f"Type: {TYPE_WIDGET}\n"
    "  - sku: string (literal, key=sku)\n"
    f"Type: {TYPE_LOC}\n"
    "  - code: string (literal, key=code)\n"
)

CYPHER_UNIT_COST = f"""
MATCH (e:Entity {{tenant_id: $tenant_id, kg: $kg}})-[:INSTANCE_OF]->(c:Class {{
  tenant_id: $tenant_id, kg: $kg
}})
WHERE c.name IN $type_names
MATCH (a:Assertion {{tenant_id: $tenant_id, kg: $kg}})-[:SUBJECT]->(e)
MATCH (a)-[:PREDICATE]->(p:Property {{tenant_id: $tenant_id, kg: $kg}})
WHERE p.name = '{LEAF_UNIT_COST}'
RETURN e.id AS id, a.literal_value AS cost LIMIT 10
""".strip()

CYPHER_LOCATED_AT = f"""
MATCH (e:Entity {{tenant_id: $tenant_id, kg: $kg}})-[:INSTANCE_OF]->(c:Class {{
  tenant_id: $tenant_id, kg: $kg
}})
WHERE c.name IN $type_names
MATCH (e)-[:LOCATED_AT]->(loc:Entity {{tenant_id: $tenant_id, kg: $kg}})
RETURN e.id AS id, loc.name AS place LIMIT 10
""".strip()

CYPHER_HAS_STATUS = f"""
MATCH (e:Entity {{tenant_id: $tenant_id, kg: $kg}})-[:INSTANCE_OF]->(c:Class {{
  tenant_id: $tenant_id, kg: $kg
}})
WHERE c.name IN $type_names
MATCH (e)-[:HAS_STATUS]->(s:Entity {{tenant_id: $tenant_id, kg: $kg}})
RETURN e.id AS id LIMIT 10
""".strip()

CYPHER_INVENTED = f"""
MATCH (e:Entity {{tenant_id: $tenant_id, kg: $kg}})-[:INSTANCE_OF]->(c:Class {{
  tenant_id: $tenant_id, kg: $kg
}})
WHERE c.name IN $type_names
MATCH (e)-[:{INVENTED_REL}]->(x:Entity {{tenant_id: $tenant_id, kg: $kg}})
WHERE e.{INVENTED_PROP} = $needle
RETURN count(e) AS n
""".strip()

CYPHER_ENTITY_PROP_COMPARE = f"""
MATCH (e:Entity {{tenant_id: $tenant_id, kg: $kg}})-[:INSTANCE_OF]->(c:Class {{
  tenant_id: $tenant_id, kg: $kg
}})
WHERE c.name IN $type_names AND e.{LEAF_UNIT_COST} < $threshold
RETURN e.id AS id, e.{LEAF_UNIT_COST} AS cost LIMIT 10
""".strip()


# ---------------------------------------------------------------------------
# Pure: inventory from explicit leaves / planning types
# ---------------------------------------------------------------------------


def test_from_leaves_allows_inventory_even_when_ontology_text_sparse():
    """unit_cost / located_at pass schema-valid via inventory; sparse text alone would miss them."""
    inv = OntologyLeafInventory.from_leaves(
        relationship_leaves=[LEAF_LOCATED_AT, LEAF_HAS_STATUS, LEAF_HAS_STYLE],
        attribute_leaves=[LEAF_UNIT_COST, LEAF_ASSAY_COST, LEAF_IN_STOCK, "sku"],
        type_names=[TYPE_WIDGET, TYPE_LOC, TYPE_STATUS],
        source="graph_store",
    )
    assert not inv.empty
    assert "LOCATED_AT" in inv.allowed_rel_tokens
    assert "HAS_STATUS" in inv.allowed_rel_tokens
    assert "HAS_STYLE" in inv.allowed_rel_tokens
    assert LEAF_UNIT_COST in inv.allowed_prop_keys
    assert LEAF_ASSAY_COST in inv.allowed_prop_keys
    assert LEAF_IN_STOCK in inv.allowed_prop_keys
    # Invented dual-write for bare leaf must not appear.
    assert "HAS_LOCATED_AT" not in inv.allowed_rel_tokens

    # Sparse ontology text alone rejects these.
    sparse = check_schema_valid_cypher(
        CYPHER_UNIT_COST,
        SPARSE_ONTO,
        params={"type_names": [TYPE_WIDGET]},
    )
    assert not sparse.ok or LEAF_UNIT_COST not in (
        sparse.inventory.attribute_leaves if sparse.inventory else ()
    )
    # With live inventory, free Cypher using unit_cost passes.
    r_cost = check_schema_valid_cypher(
        CYPHER_UNIT_COST,
        SPARSE_ONTO,
        params={"type_names": [TYPE_WIDGET]},
        inventory=inv,
    )
    assert r_cost.ok, r_cost.reason

    r_loc = check_schema_valid_cypher(
        CYPHER_LOCATED_AT,
        SPARSE_ONTO,
        params={"type_names": [TYPE_WIDGET]},
        inventory=inv,
    )
    assert r_loc.ok, r_loc.reason

    r_status = check_schema_valid_cypher(
        CYPHER_HAS_STATUS,
        SPARSE_ONTO,
        params={"type_names": [TYPE_WIDGET]},
        inventory=inv,
    )
    assert r_status.ok, r_status.reason

    r_prop = check_schema_valid_cypher(
        CYPHER_ENTITY_PROP_COMPARE,
        SPARSE_ONTO,
        params={"type_names": [TYPE_WIDGET], "threshold": 10.0},
        inventory=inv,
    )
    assert r_prop.ok, r_prop.reason


def test_invented_has_notarealthing_still_fails_with_inventory():
    """Truly invented HAS_NOTAREALTHING / free-form prop still rejected."""
    inv = OntologyLeafInventory.from_leaves(
        relationship_leaves=[LEAF_LOCATED_AT, LEAF_HAS_STATUS],
        attribute_leaves=[LEAF_UNIT_COST, "sku"],
        type_names=[TYPE_WIDGET],
        source="graph_store",
    )
    r = check_schema_valid_cypher(
        CYPHER_INVENTED,
        SPARSE_ONTO,
        params={"type_names": [TYPE_WIDGET], "needle": "x"},
        inventory=inv,
    )
    assert not r.ok
    assert any(x.upper() == INVENTED_REL for x in r.invented_rel_types)
    # Prop invent may also be reported.
    assert any(INVENTED_PROP.lower() == p.lower() for p in r.invented_prop_keys) or any(
        x.upper() == INVENTED_REL for x in r.invented_rel_types
    )


def test_from_planning_types_surfaces_instance_only_slots():
    """Planning overlay: declared empty + instance-only → both allowed."""
    planning = [
        PlanningType(
            name=TYPE_WIDGET,
            entity_count=5,
            slots=(
                PlanningSlot(
                    name=LEAF_UNIT_COST,
                    kind="literal",
                    datatype="float",
                    populated=True,
                    count=5,
                ),
                PlanningSlot(
                    name=LEAF_LOCATED_AT,
                    kind="relationship",
                    range_type=TYPE_LOC,
                    populated=True,
                    count=4,
                ),
                PlanningSlot(
                    name="legacy_declared_empty",
                    kind="relationship",
                    range_type="SynthGone",
                    populated=False,
                    count=0,
                ),
            ),
        )
    ]
    inv = OntologyLeafInventory.from_planning_types(planning, source="planning")
    assert LEAF_UNIT_COST in inv.allowed_prop_keys
    assert "LOCATED_AT" in inv.allowed_rel_tokens
    assert "LEGACY_DECLARED_EMPTY" in inv.allowed_rel_tokens  # declared still allowed


def test_merge_unions_leaves():
    a = OntologyLeafInventory.from_leaves(
        relationship_leaves=[LEAF_LOCATED_AT],
        attribute_leaves=[LEAF_UNIT_COST],
        type_names=[TYPE_WIDGET],
    )
    b = OntologyLeafInventory.from_leaves(
        relationship_leaves=[LEAF_HAS_STATUS],
        attribute_leaves=[LEAF_IN_STOCK],
        type_names=[TYPE_STATUS],
    )
    m = a.merge(b)
    assert LEAF_UNIT_COST in m.allowed_prop_keys
    assert LEAF_IN_STOCK in m.allowed_prop_keys
    assert "LOCATED_AT" in m.allowed_rel_tokens
    assert "HAS_STATUS" in m.allowed_rel_tokens
    assert m.source == "merged"


def test_planning_types_from_summaries_feeds_schema_valid():
    """Duck-typed schema + type_summary → inventory that validates free Cypher."""
    schema = [
        SchemaTypeSummary(
            name=TYPE_WIDGET,
            layer="tenant",
            entity_count=2,
            attributes=(
                OntoAttrRecord(
                    name="sku",
                    domain=TYPE_WIDGET,
                    layer="tenant",
                    tenant_id=TENANT,
                    kg="__ontology__",
                    kind="literal",
                    datatype="string",
                ),
            ),
        ),
    ]

    @dataclass
    class _Attr:
        name: str
        count: int = 1
        datatype: str = "float"

    @dataclass
    class _Rel:
        name: str
        target_type: str | None = None
        count: int = 1

    @dataclass
    class _Sum:
        name: str
        entity_count: int = 2
        attributes: tuple = ()
        relationships: tuple = ()

    summaries = {
        TYPE_WIDGET: _Sum(
            name=TYPE_WIDGET,
            entity_count=2,
            attributes=(
                _Attr(name=LEAF_UNIT_COST, count=2, datatype="float"),
                _Attr(name=LEAF_IN_STOCK, count=2, datatype="boolean"),
            ),
            relationships=(
                _Rel(name=LEAF_LOCATED_AT, target_type=TYPE_LOC, count=2),
                _Rel(name=LEAF_HAS_STATUS, target_type=TYPE_STATUS, count=1),
            ),
        )
    }
    planning = planning_types_from_schema_and_summaries(
        schema, summaries, inventory_probed=True
    )
    inv = OntologyLeafInventory.from_planning_types(planning, source="graph_store")
    r = check_schema_valid_cypher(
        CYPHER_LOCATED_AT,
        SPARSE_ONTO,  # sparse text alone insufficient
        params={"type_names": [TYPE_WIDGET]},
        inventory=inv,
    )
    assert r.ok, r.reason
    r2 = check_schema_valid_cypher(
        CYPHER_INVENTED,
        SPARSE_ONTO,
        params={"type_names": [TYPE_WIDGET], "needle": "x"},
        inventory=inv,
    )
    assert not r2.ok


def test_sparse_ontology_text_rejects_unit_cost_without_inventory():
    """Regression: text-only path still fails closed on leaves not in text."""
    r = check_schema_valid_cypher(
        CYPHER_ENTITY_PROP_COMPARE,
        SPARSE_ONTO,
        params={"type_names": [TYPE_WIDGET], "threshold": 5.0},
    )
    assert not r.ok
    assert any(LEAF_UNIT_COST.lower() == p.lower() for p in r.invented_prop_keys)


# ---------------------------------------------------------------------------
# GraphStore integration (MemoryGraphStore hermetic)
# ---------------------------------------------------------------------------


async def _seed_widget_inventory(store: MemoryGraphStore) -> None:
    """Sparse catalog declarations + richer instance inventory leaves."""
    w1 = entity_uri(TYPE_WIDGET, "w1")
    loc1 = entity_uri(TYPE_LOC, "loc1")
    st1 = entity_uri(TYPE_STATUS, "active")
    triples = [
        (w1, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/{TYPE_WIDGET}"),
        (w1, "http://www.w3.org/2000/01/rdf-schema#label", "Widget One"),
        (w1, f"{IRI_BASE}/types/{TYPE_WIDGET}/attrs/{LEAF_UNIT_COST}", 12.5),
        (w1, f"{IRI_BASE}/types/{TYPE_WIDGET}/attrs/{LEAF_IN_STOCK}", True),
        (w1, f"{IRI_BASE}/types/{TYPE_WIDGET}/attrs/{LEAF_ASSAY_COST}", 3.0),
        (w1, f"{IRI_BASE}/onto/{LEAF_LOCATED_AT}", loc1),
        (w1, f"{IRI_BASE}/onto/{LEAF_HAS_STATUS}", st1),
        (w1, f"{IRI_BASE}/onto/{LEAF_HAS_STYLE}", entity_uri("SynthStyle", "classic")),
        (loc1, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/{TYPE_LOC}"),
        (loc1, "http://www.w3.org/2000/01/rdf-schema#label", "Bay A"),
        (st1, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/{TYPE_STATUS}"),
        (st1, "http://www.w3.org/2000/01/rdf-schema#label", "Active"),
    ]
    await insert_facts(None, GRAPH, triples, store=store)

    await upsert_type(
        store=store, name=TYPE_WIDGET, description="synthetic widget",
        layer="tenant", tenant_id=TENANT,
    )
    await upsert_type(
        store=store, name=TYPE_LOC, description="synthetic location",
        layer="tenant", tenant_id=TENANT,
    )
    await upsert_type(
        store=store, name=TYPE_STATUS, description="synthetic status",
        layer="tenant", tenant_id=TENANT,
    )
    # Only declare sku/code — leave unit_cost / located_at / has_* as
    # instance-only so catalog text is sparse vs live inventory (the bug class).
    await upsert_attribute(
        store=store, type_name=TYPE_WIDGET, attr_name="sku",
        datatype="string", layer="tenant", tenant_id=TENANT,
    )
    await upsert_attribute(
        store=store, type_name=TYPE_LOC, attr_name="code",
        datatype="string", layer="tenant", tenant_id=TENANT,
    )


@pytest.mark.asyncio
async def test_inventory_from_graph_store_includes_instance_leaves():
    store = MemoryGraphStore()
    try:
        await _seed_widget_inventory(store)
        inv = await inventory_from_graph_store(
            store, tenant_id=TENANT, kg=KG
        )
        assert inv is not None and not inv.empty
        assert inv.source == "graph_store"
        # Instance-populated leaves must be allowlisted.
        for leaf in (
            LEAF_UNIT_COST,
            LEAF_ASSAY_COST,
            LEAF_IN_STOCK,
            LEAF_LOCATED_AT,
            LEAF_HAS_STATUS,
            LEAF_HAS_STYLE,
        ):
            assert (
                leaf.lower() in inv.allowed_prop_keys
                or leaf.upper() in inv.allowed_rel_tokens
                or leaf.lower()
                in {x.lower() for x in inv.relationship_leaves}
            ), f"expected {leaf} in graph_store inventory"

        # Free Cypher using inventory leaves passes even with sparse ontology text.
        r = check_schema_valid_cypher(
            CYPHER_UNIT_COST,
            SPARSE_ONTO,
            params={"type_names": [TYPE_WIDGET]},
            inventory=inv,
        )
        assert r.ok, r.reason
        r2 = check_schema_valid_cypher(
            CYPHER_LOCATED_AT,
            SPARSE_ONTO,
            params={"type_names": [TYPE_WIDGET]},
            inventory=inv,
        )
        assert r2.ok, r2.reason
        r3 = check_schema_valid_cypher(
            CYPHER_HAS_STATUS,
            SPARSE_ONTO,
            params={"type_names": [TYPE_WIDGET]},
            inventory=inv,
        )
        assert r3.ok, r3.reason

        # Invented still fails.
        bad = check_schema_valid_cypher(
            CYPHER_INVENTED,
            SPARSE_ONTO,
            params={"type_names": [TYPE_WIDGET], "needle": "x"},
            inventory=inv,
        )
        assert not bad.ok
        assert any(x.upper() == INVENTED_REL for x in bad.invented_rel_types)
    finally:
        await store.close()
        reset_graph_store_for_tests()


@pytest.mark.asyncio
async def test_inventory_from_graph_store_none_when_empty():
    store = MemoryGraphStore()
    try:
        inv = await inventory_from_graph_store(
            store, tenant_id="empty-tenant", kg="empty-kg"
        )
        # No catalog rows → None (caller falls back to ontology text).
        assert inv is None or inv.empty
    finally:
        await store.close()
        reset_graph_store_for_tests()


def test_anti_overfit_no_persona_gold_in_schema_valid_module():
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "infona_client/nlp/schema_valid_cypher.py"
    )
    text = src.read_text(encoding="utf-8")
    for banned in ("Fall", "CourseOffering", "bookstore", "seats_remaining"):
        assert banned not in text

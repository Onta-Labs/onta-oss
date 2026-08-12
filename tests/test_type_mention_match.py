"""General NL type-mention matching (anti-overfit: synthetic types only).

Defect class: COUNT / list fixtures invent non-existent type leaves when the
user says a domain synonym or CamelCase head-noun plural, producing silent 0.
"""

from __future__ import annotations

import pytest

from infona_client.nlp.cypher_generate import (
    TEMPLATE_COUNT_BY_TYPE,
    match_type_name,
    resolve_type_name,
    try_deterministic_cypher,
    try_related_name_filter_query,
    try_stub_count_query,
)


SYNTH_TYPES = ["Widget", "SensorHub", "TrialRun", "InventorySKU"]


def test_match_exact_and_simple_plural():
    assert match_type_name("widgets", SYNTH_TYPES) == "Widget"
    assert match_type_name("Widget", SYNTH_TYPES) == "Widget"


def test_match_camelcase_head_noun_plural():
    """Compound type leaves match their final CamelCase segment plural."""
    assert match_type_name("runs", SYNTH_TYPES) == "TrialRun"
    assert match_type_name("hubs", SYNTH_TYPES) == "SensorHub"
    assert match_type_name("skus", SYNTH_TYPES) == "InventorySKU"


def test_match_multiword_phrase():
    assert match_type_name("sensor hubs", SYNTH_TYPES) == "SensorHub"
    assert match_type_name("trial runs", SYNTH_TYPES) == "TrialRun"


def test_match_or_alternatives_picks_known_leaf():
    # Only InventorySKU is present among the alternatives' hits.
    assert (
        match_type_name("inventory items or SKUs", ["InventorySKU", "Widget"])
        == "InventorySKU"
    )


def test_no_invent_when_ontology_known():
    assert resolve_type_name("gadgets", SYNTH_TYPES) is None
    assert try_stub_count_query(
        "How many gadgets?", type_names=SYNTH_TYPES
    ) is None


def test_guess_only_when_ontology_empty():
    # type_names is None AND no summary types → bootstrap invent
    assert resolve_type_name("widgets", None, "") == "Widget"
    # Explicit empty candidate list still falls through to invent only when
    # no ontology summary is available (same as None for hermetic tests).
    assert resolve_type_name("widgets", None, "Type: SensorHub") is None


def test_count_fixture_head_noun():
    payload = try_stub_count_query(
        "How many runs?", type_names=SYNTH_TYPES
    )
    assert payload is not None
    assert payload["template"] == TEMPLATE_COUNT_BY_TYPE
    assert payload["params"]["type_names"] == ["TrialRun"]


def test_count_fixture_simple_plural():
    payload = try_stub_count_query(
        "How many widgets are there?", type_names=SYNTH_TYPES
    )
    assert payload is not None
    assert payload["params"]["type_names"] == ["Widget"]


def test_count_does_not_silent_zero_on_unknown():
    """Miss must fall through (None), not invent Trial/SKU and count 0."""
    assert (
        try_stub_count_query(
            "How many inventory items or SKUs are there?",
            type_names=["Widget", "SensorHub"],
        )
        is None
    )


def test_containment_false_positives_short_types():
    """Short accidental substrings must not bind (ONTA-450 class)."""
    types = ["Age", "Cat", "Ion", "Category"]
    # "categories" → Category via plural, not Age/Cat/Ion
    assert match_type_name("categories", types) == "Category"
    assert match_type_name("medication", types) is None


def test_related_filter_resolves_phase_from_ontology():
    onto = (
        "Type: TrialRun\n"
        "  - enrollment: integer (literal)\n"
        "  - has_phase: relationship → Phase\n"
        "Type: Phase\n"
    )
    payload = try_related_name_filter_query(
        "which trial runs have phase 2",
        onto,
        type_names=["TrialRun", "Phase"],
    )
    assert payload is not None
    assert payload["template"] == "related_entity_name_filter"
    assert payload["params"]["rel_attr"] == "has_phase"
    assert payload["params"]["target_name"] == "2"
    assert "TrialRun" in payload["params"]["type_names"]


def test_related_filter_resolves_supplier_leaf():
    onto = (
        "Type: Widget\n"
        "  - unit_cost: float (literal)\n"
        "  - supplied_by: relationship → Vendor\n"
        "Type: Vendor\n"
    )
    payload = try_deterministic_cypher(
        "widgets supplied by Northwind",
        onto,
        type_names=["Widget", "Vendor"],
    )
    assert payload is not None
    assert payload["params"].get("rel_attr") == "supplied_by"
    assert "Northwind" in str(payload["params"].get("target_name", ""))


def test_equality_routes_relationship_to_related_filter():
    onto = (
        "Type: Widget\n"
        "  - has_status: relationship → Status\n"
        "Type: Status\n"
    )
    payload = try_deterministic_cypher(
        "widgets where has_status is backorder",
        onto,
        type_names=["Widget", "Status"],
    )
    assert payload is not None
    assert payload["template"] == "related_entity_name_filter"
    assert payload["params"]["rel_attr"] == "has_status"
    assert payload["params"]["target_name"] == "backorder"


def test_short_type_not_substring_of_unrelated_word():
    assert match_type_name("medication", ["Ion"]) is None
    assert match_type_name("information", ["Form"]) is None


def test_related_filter_rejects_literal_attrs():
    """Broadened regex must not steal literal equality shapes."""
    onto = (
        "Type: Widget\n"
        "  - unit_cost: float (literal, key=unit_cost)\n"
        "  - title: string (literal, key=title)\n"
        "  - has_phase -> Phase (relationship, key=has_phase)\n"
        "Type: Phase\n"
    )
    # Literals — fall through (None) so equality/LLM can handle.
    assert try_related_name_filter_query(
        "widgets with title Alpha", onto, type_names=["Widget", "Phase"]
    ) is None
    assert try_related_name_filter_query(
        "widgets with unit_cost 9.99", onto, type_names=["Widget", "Phase"]
    ) is None
    # Equality on literals stays literal_values
    payload = try_deterministic_cypher(
        "widgets where title is Alpha",
        onto,
        type_names=["Widget", "Phase"],
    )
    assert payload is not None
    assert payload["template"] == "literal_values"


def test_related_filter_production_arrow_format():
    onto = (
        "Type: Widget\n"
        "  - unit_cost: float (literal, key=unit_cost)\n"
        "  - has_phase -> Phase (relationship, key=has_phase)\n"
        "  - supplied_by -> Vendor (relationship, key=supplied_by)\n"
        "Type: Phase\n"
        "Type: Vendor\n"
    )
    phase = try_related_name_filter_query(
        "widgets with phase 2", onto, type_names=["Widget", "Phase", "Vendor"]
    )
    assert phase is not None
    assert phase["params"]["rel_attr"] == "has_phase"
    assert phase["params"]["target_name"] == "2"

    # Equality on relationship attr routes to related filter
    eq = try_deterministic_cypher(
        "widgets where has_phase is 2",
        onto,
        type_names=["Widget", "Phase", "Vendor"],
    )
    assert eq is not None
    assert eq["template"] == "related_entity_name_filter"
    assert eq["params"]["rel_attr"] == "has_phase"


def test_author_does_not_bind_has_authority():
    onto = (
        "Type: Widget\n"
        "  - has_author -> Person (relationship, key=has_author)\n"
        "  - has_authority -> Org (relationship, key=has_authority)\n"
        "Type: Person\n"
        "Type: Org\n"
    )
    from infona_client.nlp.cypher_generate import _resolve_relationship_attr
    assert (
        _resolve_relationship_attr(
            "author", type_name="Widget", ontology_summary=onto
        )
        == "has_author"
    )


def test_count_fixture_refuses_filtered_how_many():
    """Silent-wrong class: must not answer filtered count with unfiltered total."""
    onto = "Type: Widget\n  - wing: string (literal)\n"
    assert try_stub_count_query(
        "How many widgets have wing East?",
        onto,
        type_names=["Widget"],
    ) is None
    assert try_stub_count_query(
        "How many widgets are there?",
        onto,
        type_names=["Widget"],
    ) is not None


def test_less_than_routes_to_numeric_not_equality():
    onto = "Type: Widget\n  - unit_cost: float (literal)\n"
    payload = try_deterministic_cypher(
        "widgets where unit_cost is less than 15",
        onto,
        type_names=["Widget"],
    )
    assert payload is not None
    assert payload["template"] == "literal_compare"
    assert payload["params"]["op"] == "lt"
    assert payload["params"]["threshold"] == 15.0

def test_count_fixture_refuses_are_at_scope():
    onto = "Type: Widget\n  - site: string (literal)\n"
    assert try_stub_count_query(
        "How many widgets are at Plant-A?",
        onto,
        type_names=["Widget"],
    ) is None



def test_total_number_of_stays_count_not_aggregate():
    from infona_client.nlp.cypher_generate import try_deterministic_cypher

    onto = "Type: Widget\n  - unit_cost: float (literal)\n"
    p = try_deterministic_cypher(
        "total number of widgets", onto, type_names=["Widget"]
    )
    assert p is not None
    assert p["template"] == "entities_of_type_count"


def test_forbidden_has_assertion_detector():
    from infona_client.nlp.pipeline import _cypher_uses_forbidden_shapes

    bad = "MATCH (e)-[:HAS_ASSERTION]->(a) RETURN sum(a.literal_value)"
    assert _cypher_uses_forbidden_shapes(bad)
    good = (
        "MATCH (e:Entity)-[:INSTANCE_OF]->(c:Class) "
        "OPTIONAL MATCH (a:Assertion {subject_id:e.id})-[:PREDICATE]->(p:Property) "
        "RETURN sum(toFloat(a.literal_value))"
    )
    assert _cypher_uses_forbidden_shapes(good) is None

def test_aggregate_sum_avg_on_synthetic_type():
    """SUM/AVG use allowlisted literal_aggregate — never HAS_ASSERTION."""
    from infona_client.nlp.cypher_generate import try_deterministic_cypher

    onto = (
        "Type: Widget\n"
        "  - unit_cost: float (literal, key=unit_cost)\n"
        "  - qty: integer (literal, key=qty)\n"
    )
    types = ["Widget"]
    total = try_deterministic_cypher(
        "total unit_cost of widgets", onto, type_names=types
    )
    assert total is not None
    assert total["template"] == "literal_aggregate"
    assert "HAS_ASSERTION" not in total["cypher"]
    assert total["params"]["prop_key"] == "unit_cost"
    assert total["params"]["agg_op"] == "sum"
    assert total["params"]["type_names"] == ["Widget"]

    # amount absent → remap to first numeric candidate present (unit_cost)
    remapped = try_deterministic_cypher(
        "total amount of widgets", onto, type_names=types
    )
    assert remapped is not None
    assert remapped["params"]["prop_key"] == "unit_cost"

    avg = try_deterministic_cypher(
        "average amount of widgets",
        "Type: Widget\n  - amount: integer (literal, key=amount)\n",
        type_names=types,
    )
    assert avg is not None
    assert avg["template"] == "literal_aggregate"
    assert avg["params"]["agg_op"] == "avg"
    assert avg["params"]["prop_key"] == "amount"

import pytest


@pytest.mark.asyncio
async def test_literal_aggregate_e2e_memory():
    """SUM over seeded literals returns non-zero without HAS_ASSERTION."""
    from infona_client.graph.iri import IRI_BASE
    from infona_client.graph.memory_store import MemoryGraphStore
    from infona_client.graph.ontology_catalog import upsert_type_pg
    from infona_client.graph.rdf_model import AssertionFact, assert_fact
    from infona_client.graph.scope import GraphScope
    from infona_client.nlp.cypher_generate import try_deterministic_cypher

    store = MemoryGraphStore()
    cat = store.session(
        GraphScope.for_catalog(layer="tenant", tenant_id="demo-tenant")
    )
    await upsert_type_pg(cat, name="Widget", description="w")
    scope = GraphScope.for_instance("demo-tenant", "agg-demo")
    session = store.session(scope)
    for i, cost in enumerate((10.0, 20.0, 30.0), start=1):
        eid = f"{IRI_BASE}/entities/Widget/w{i}"
        await session.write_merge_entity(
            id=eid, primary_type="Widget", name=f"W{i}", source="test"
        )
        await assert_fact(
            session,
            AssertionFact(subject_id=eid, kind="type", value="Widget"),
            dual_write_cache=True,
        )
        await assert_fact(
            session,
            AssertionFact(
                subject_id=eid,
                kind="literal",
                property_leaf="unit_cost",
                value=cost,
            ),
            dual_write_cache=True,
        )

    payload = try_deterministic_cypher(
        "total unit_cost of widgets",
        "Type: Widget\n  - unit_cost: float (literal)\n",
        type_names=["Widget"],
    )
    assert payload and payload["template"] == "literal_aggregate"
    rows = await session.execute_template(
        payload["template"], payload["params"]
    )
    assert rows, rows
    row0 = rows[0]
    val = row0.get("value") if hasattr(row0, "get") else row0.data.get("value")
    assert val == 60.0, val


@pytest.mark.asyncio
async def test_literal_aggregate_duplicates_sum():
    """Duplicate values must sum per entity (100+100+100=300), not DISTINCT values."""
    from infona_client.graph.iri import IRI_BASE
    from infona_client.graph.memory_store import MemoryGraphStore
    from infona_client.graph.ontology_catalog import upsert_type_pg
    from infona_client.graph.rdf_model import AssertionFact, assert_fact
    from infona_client.graph.scope import GraphScope
    from infona_client.nlp.cypher_generate import try_deterministic_cypher
    from infona_client.graph.rdfs_helpers import LITERAL_AGGREGATE_CYPHER

    assert "collect(DISTINCT num)" not in LITERAL_AGGREGATE_CYPHER

    store = MemoryGraphStore()
    cat = store.session(
        GraphScope.for_catalog(layer="tenant", tenant_id="demo-tenant")
    )
    await upsert_type_pg(cat, name="Widget", description="w")
    scope = GraphScope.for_instance("demo-tenant", "agg-dup")
    session = store.session(scope)
    for i in range(1, 4):
        eid = f"{IRI_BASE}/entities/Widget/d{i}"
        await session.write_merge_entity(
            id=eid, primary_type="Widget", name=f"D{i}", source="test"
        )
        await assert_fact(
            session,
            AssertionFact(subject_id=eid, kind="type", value="Widget"),
            dual_write_cache=True,
        )
        await assert_fact(
            session,
            AssertionFact(
                subject_id=eid,
                kind="literal",
                property_leaf="unit_cost",
                value=100.0,
            ),
            dual_write_cache=True,
        )

    payload = try_deterministic_cypher(
        "total unit_cost of widgets",
        "Type: Widget\n  - unit_cost: float (literal)\n",
        type_names=["Widget"],
    )
    rows = await session.execute_template(
        payload["template"], payload["params"]
    )
    val = rows[0].get("value") if hasattr(rows[0], "get") else rows[0].data.get("value")
    assert val == 300.0, val

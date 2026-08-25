"""Hermetic group-by SUM top-1 (argmax-by-dim). Synthetic gadgets only."""

from __future__ import annotations

import pytest

from infona_client.graph.iri import IRI_BASE
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_catalog import upsert_type_pg
from infona_client.graph.rdf_model import AssertionFact, assert_fact
from infona_client.graph.schema_bootstrap import TEMPLATES, get_template
from infona_client.graph.scope import GraphScope
from infona_client.nlp.cypher_example_seeds import CYPHER_SEEDS, SHAPE_ARGMAX
from infona_client.nlp.prompts import CYPHER_GENERATION_SYSTEM
from infona_client.nlp.query_constraint_coverage_check import check_constraint_coverage
from infona_client.nlp.query_constraint_coverage_count import count_vs_list_fail_closed
from infona_client.nlp.query_intent import sketch_query_intent

_ARGMAX_Q = "Which vendor_code group has the highest total unit_qty?"
_COUNT_Q = "How many gadgets have status_label 'open'?"
_LIST_Q = "List gadgets with status_label 'open'"
_ENTITY_MAX_Q = "What is the highest unit_qty?"

_PARAMS = {
    "type_names": ["Gadget"],
    "group_key": "vendor_code",
    "prop_key": "unit_qty",
}


def test_argmax_intent_is_narrow():
    sk = sketch_query_intent(_ARGMAX_Q)
    assert sk.has_argmax_intent
    assert "count" not in sk.aggregate_ops
    assert not sketch_query_intent(_COUNT_Q).has_argmax_intent
    assert not sketch_query_intent(_LIST_Q).has_argmax_intent
    assert not sketch_query_intent(_ENTITY_MAX_Q).has_argmax_intent


def test_count_gate_still_ignores_argmax():
    sk = sketch_query_intent(_ARGMAX_Q)
    assert count_vs_list_fail_closed(sk, "literal_values") is None


def test_list_template_on_argmax_fail_closes():
    r = check_constraint_coverage(
        _ARGMAX_Q,
        TEMPLATES["literal_values"].cypher,
        params={"type_names": ["Gadget"], "prop_key": "status_label", "prop_value": "open"},
        template="literal_values",
    )
    assert not r.ok
    assert r.fail_closed
    reason = (r.reason or "").lower()
    assert "argmax" in reason
    assert "literal_argmax_by_dim" in reason
    assert "open" not in reason


def test_count_twins_on_argmax_fail_close():
    for tmpl in ("literal_values_count", "entities_of_type_count"):
        r = check_constraint_coverage(
            _ARGMAX_Q,
            TEMPLATES[tmpl].cypher,
            params={"type_names": ["Gadget"], "prop_key": "status_label", "prop_value": "open"},
            template=tmpl,
        )
        assert not r.ok
        assert r.fail_closed


def test_seed_question_passes_on_argmax_template():
    q = next(s["question"] for s in CYPHER_SEEDS if s["shape"] == SHAPE_ARGMAX)
    r = check_constraint_coverage(
        q,
        TEMPLATES["literal_argmax_by_dim"].cypher,
        params={
            "type_names": ["Product"],
            "group_key": "region_code",
            "prop_key": "price",
        },
        template="literal_argmax_by_dim",
    )
    assert r.ok
    assert not r.fail_closed


def test_bare_sum_template_on_argmax_fail_closes():
    r = check_constraint_coverage(
        _ARGMAX_Q,
        TEMPLATES["literal_aggregate"].cypher,
        params={"type_names": ["Gadget"], "prop_key": "unit_qty", "agg_op": "sum"},
        template="literal_aggregate",
    )
    assert not r.ok
    assert r.fail_closed


def test_argmax_template_passes():
    r = check_constraint_coverage(
        _ARGMAX_Q,
        TEMPLATES["literal_argmax_by_dim"].cypher,
        params=_PARAMS,
        template="literal_argmax_by_dim",
    )
    assert r.ok
    assert not r.fail_closed


def test_list_question_still_uses_list_helper():
    r = check_constraint_coverage(
        _LIST_Q,
        TEMPLATES["literal_values"].cypher,
        params={"type_names": ["Gadget"], "prop_key": "status_label", "prop_value": "open"},
        template="literal_values",
    )
    assert r.ok
    assert not r.fail_closed


def test_how_many_still_uses_count_twin():
    r = check_constraint_coverage(
        _COUNT_Q,
        TEMPLATES["literal_values"].cypher,
        params={"type_names": ["Gadget"], "prop_key": "status_label", "prop_value": "open"},
        template="literal_values",
    )
    assert not r.ok
    assert r.fail_closed
    assert "literal_values_count" in (r.reason or "")


def test_template_registered_and_prompt_teaches_it():
    t = get_template("literal_argmax_by_dim")
    assert "$group_key" in t.cypher
    assert "sum(num)" in t.cypher.replace(" ", "").lower() or "sum(num)" in t.cypher
    assert "LIMIT 1" in t.cypher
    assert "sponsor" not in t.cypher.lower()
    assert "enrollment" not in t.cypher.lower()
    s = CYPHER_GENERATION_SYSTEM.lower()
    assert "literal_argmax_by_dim" in s
    assert "bristol" not in s
    assert "enrollment" not in s


def test_seed_is_abstract_group_by():
    rows = [s for s in CYPHER_SEEDS if s["shape"] == SHAPE_ARGMAX]
    assert rows
    blob = " ".join(s["question"].lower() for s in rows)
    assert "sponsor" not in blob
    assert "enrollment" not in blob
    assert "trial" not in blob
    assert "highest total" in blob


@pytest.mark.asyncio
async def test_memory_grouped_sum_picks_higher_vendor():
    store = MemoryGraphStore()
    cat = store.session(GraphScope.for_catalog(layer="tenant", tenant_id="demo-tenant"))
    await upsert_type_pg(cat, name="Gadget", description="widget")
    session = store.session(GraphScope.for_instance("demo-tenant", "lab"))

    async def _gadget(eid: str, vendor: str, qty: float) -> None:
        uri = f"{IRI_BASE}/entities/Gadget/{eid}"
        await session.write_merge_entity(
            id=uri, primary_type="Gadget", name=eid, source="test"
        )
        await assert_fact(
            session, AssertionFact(subject_id=uri, kind="type", value="Gadget"),
            dual_write_cache=True,
        )
        await assert_fact(
            session,
            AssertionFact(
                subject_id=uri, kind="literal", property_leaf="vendor_code", value=vendor
            ),
            dual_write_cache=True,
        )
        await assert_fact(
            session,
            AssertionFact(
                subject_id=uri, kind="literal", property_leaf="unit_qty", value=qty
            ),
            dual_write_cache=True,
        )

    await _gadget("a1", "VendA", 10)
    await _gadget("a2", "VendA", 20)
    await _gadget("b1", "VendB", 25)
    rows = await session.execute_template("literal_argmax_by_dim", _PARAMS)
    assert len(rows) == 1
    assert rows[0].get("name") == "VendA"
    assert rows[0].get("value") == 30.0

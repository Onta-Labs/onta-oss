"""Hermetic list-vs-count: how-many must not execute a row helper.

Synthetic gadgets / status ``open`` only — no eval-set strings.
"""

from __future__ import annotations

import pytest

from infona_client.graph.iri import IRI_BASE
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_catalog import upsert_type_pg
from infona_client.graph.rdf_model import AssertionFact, assert_fact
from infona_client.graph.schema_bootstrap import TEMPLATES, get_template
from infona_client.graph.scope import GraphScope
from infona_client.nlp.prompts import CYPHER_GENERATION_SYSTEM
from infona_client.nlp.query_constraint_coverage_check import check_constraint_coverage
from infona_client.nlp.query_constraint_coverage_feedback import coverage_feedback
from infona_client.nlp.query_intent import sketch_query_intent

_COUNT_Q = "How many gadgets have status_label 'open'?"
_LIST_Q = "List gadgets with status_label 'open'"
_ARGMAX_Q = "Which gadget has the highest total unit_qty?"

_LIST_CYPHER = TEMPLATES["literal_values"].cypher
_COUNT_CYPHER = TEMPLATES["literal_values_count"].cypher
_PARAMS = {
    "type_names": ["Gadget"],
    "prop_key": "status_label",
    "prop_value": "open",
}


def test_how_many_is_count_intent_not_list():
    sk = sketch_query_intent(_COUNT_Q)
    assert "count" in sk.aggregate_ops
    sk_list = sketch_query_intent(_LIST_Q)
    assert "count" not in sk_list.aggregate_ops
    sk_top = sketch_query_intent(_ARGMAX_Q)
    assert "count" not in sk_top.aggregate_ops


def test_list_template_on_how_many_fail_closes():
    r = check_constraint_coverage(
        _COUNT_Q, _LIST_CYPHER, params=_PARAMS, template="literal_values"
    )
    assert not r.ok
    assert r.fail_closed
    assert r.confidence == "low"
    reason = (r.reason or "").lower()
    assert "count" in reason
    assert "literal_values_count" in reason
    assert "open" not in reason


def test_count_template_on_how_many_passes():
    r = check_constraint_coverage(
        _COUNT_Q, _COUNT_CYPHER, params=_PARAMS, template="literal_values_count"
    )
    assert r.ok
    assert not r.fail_closed
    assert r.confidence == "high"


def test_list_template_on_list_question_still_ok():
    r = check_constraint_coverage(
        _LIST_Q, _LIST_CYPHER, params=_PARAMS, template="literal_values"
    )
    assert r.ok
    assert not r.fail_closed


def test_argmax_list_template_not_caught_by_count_gate():
    """Highest-total is not how-many; the count twin must stay silent."""
    from infona_client.nlp.query_constraint_coverage_count import (
        count_vs_list_fail_closed,
    )
    from infona_client.nlp.query_intent import sketch_query_intent

    sk = sketch_query_intent(_ARGMAX_Q)
    assert "count" not in sk.aggregate_ops
    assert count_vs_list_fail_closed(sk, "literal_values") is None


def test_entities_of_type_on_how_many_fail_closes():
    r = check_constraint_coverage(
        "How many gadgets are there?",
        TEMPLATES["entities_of_type"].cypher,
        params={"type_names": ["Gadget"], "limit": 50},
        template="entities_of_type",
    )
    assert not r.ok
    assert r.fail_closed
    assert "entities_of_type_count" in (r.reason or "")


def test_count_twin_registered_and_returns_n():
    t = get_template("literal_values_count")
    assert "count(DISTINCT e)" in t.cypher
    assert "$limit" not in t.cypher
    assert t.cypher != get_template("literal_values").cypher


def test_prompt_splits_list_and_count_helpers():
    s = CYPHER_GENERATION_SYSTEM
    assert "literal_values_count" in s
    assert "how many X with status/phase/label" not in s.lower()
    fb = coverage_feedback(
        check_constraint_coverage(
            _COUNT_Q, _LIST_CYPHER, params=_PARAMS, template="literal_values"
        )
    )
    assert "literal_values_count" in fb


@pytest.mark.asyncio
async def test_memory_count_is_uncapped_scalar():
    """Count twin must not inherit the list helper's default LIMIT 25."""
    store = MemoryGraphStore()
    cat = store.session(GraphScope.for_catalog(layer="tenant", tenant_id="demo-tenant"))
    await upsert_type_pg(cat, name="Gadget", description="widget")
    session = store.session(GraphScope.for_instance("demo-tenant", "lab"))
    n_open = 30
    for i in range(n_open):
        eid = f"{IRI_BASE}/entities/Gadget/g{i}"
        await session.write_merge_entity(
            id=eid, primary_type="Gadget", name=f"g{i}", source="test"
        )
        await assert_fact(
            session,
            AssertionFact(
                subject_id=eid,
                kind="type",
                value="Gadget",
            ),
            dual_write_cache=True,
        )
        await assert_fact(
            session,
            AssertionFact(
                subject_id=eid,
                kind="literal",
                property_leaf="status_label",
                value="open",
            ),
            dual_write_cache=True,
        )
    listed = await session.execute_template(
        "literal_values",
        {
            "type_names": ["Gadget"],
            "prop_key": "status_label",
            "prop_value": "open",
            "limit": 25,
        },
    )
    assert len(listed) == 25
    counted = await session.execute_template(
        "literal_values_count",
        {
            "type_names": ["Gadget"],
            "prop_key": "status_label",
            "prop_value": "open",
        },
    )
    assert len(counted) == 1
    assert counted[0].get("n") == n_open

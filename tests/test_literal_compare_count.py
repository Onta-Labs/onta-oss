"""Hermetic list-vs-count: how-many + inequality must not dump rows.

Synthetic gadgets / years 2014 and 2019 only — no eval-set strings.
"""

from __future__ import annotations

import pytest

from infona_client.graph.iri import IRI_BASE
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_catalog import upsert_type_pg
from infona_client.graph.rdf_model import AssertionFact, assert_fact
from infona_client.graph.schema_bootstrap import TEMPLATES, get_template
from infona_client.graph.scope import GraphScope
from infona_client.nlp.cypher_filter_integrity import optional_match_filter_smell
from infona_client.nlp.prompts import CYPHER_GENERATION_SYSTEM
from infona_client.nlp.query_constraint_coverage_check import check_constraint_coverage
from infona_client.nlp.query_constraint_coverage_feedback import coverage_feedback
from infona_client.nlp.query_intent import sketch_query_intent

_COUNT_Q = "How many gadgets started in or after 2019?"
_LIST_Q = "List gadgets started in or after 2019"
_ARGMAX_Q = "Which gadget has the highest total unit_qty?"

_LIST_CYPHER = TEMPLATES["literal_compare"].cypher
_COUNT_CYPHER = TEMPLATES["literal_compare_count"].cypher
_PARAMS = {
    "type_names": ["Gadget"],
    "prop_key": "start_year",
    "op": "ge",
    "threshold": 2019,
}


def test_how_many_year_compare_is_count_intent():
    sk = sketch_query_intent(_COUNT_Q)
    assert "count" in sk.aggregate_ops
    sk_list = sketch_query_intent(_LIST_Q)
    assert "count" not in sk_list.aggregate_ops
    sk_top = sketch_query_intent(_ARGMAX_Q)
    assert "count" not in sk_top.aggregate_ops


def test_list_template_on_how_many_fail_closes():
    r = check_constraint_coverage(
        _COUNT_Q, _LIST_CYPHER, params=_PARAMS, template="literal_compare"
    )
    assert not r.ok
    assert r.fail_closed
    assert r.confidence == "low"
    reason = (r.reason or "").lower()
    assert "count" in reason
    assert "literal_compare_count" in reason
    assert r.extra.get("count_twin") == "literal_compare_count"


def test_count_template_on_how_many_passes():
    r = check_constraint_coverage(
        _COUNT_Q, _COUNT_CYPHER, params=_PARAMS, template="literal_compare_count"
    )
    assert r.ok
    assert not r.fail_closed
    assert r.confidence == "high"


def test_list_template_on_list_question_still_ok():
    r = check_constraint_coverage(
        _LIST_Q, _LIST_CYPHER, params=_PARAMS, template="literal_compare"
    )
    assert r.ok
    assert not r.fail_closed


def test_in_or_after_is_ge_threshold_not_year_list():
    """How-many + in-or-after YYYY binds ge/threshold, not a dumped year list."""
    assert _PARAMS["op"] == "ge"
    assert _PARAMS["threshold"] == 2019
    assert "prop_value" not in _PARAMS
    r = check_constraint_coverage(
        _COUNT_Q, _COUNT_CYPHER, params=_PARAMS, template="literal_compare_count"
    )
    assert r.ok
    blob = f"{_COUNT_CYPHER} {_PARAMS}".lower()
    assert "$op" in _COUNT_CYPHER
    assert "$threshold" in _COUNT_CYPHER
    assert "2014" not in blob


def test_argmax_list_template_not_caught_by_count_gate():
    from infona_client.nlp.query_constraint_coverage_count import (
        count_vs_list_fail_closed,
    )

    sk = sketch_query_intent(_ARGMAX_Q)
    assert "count" not in sk.aggregate_ops
    assert count_vs_list_fail_closed(sk, "literal_compare") is None


def test_count_twin_registered_and_returns_n():
    t = get_template("literal_compare_count")
    assert "count(DISTINCT e)" in t.cypher
    assert "$limit" not in t.cypher
    assert t.cypher != get_template("literal_compare").cypher
    list_t = get_template("literal_compare")
    assert "$op" in list_t.cypher and "$threshold" in list_t.cypher
    assert "$op" in t.cypher and "$threshold" in t.cypher
    assert optional_match_filter_smell(t.cypher) is None


def test_prompt_splits_list_and_count_helpers():
    s = CYPHER_GENERATION_SYSTEM
    assert "literal_compare_count" in s
    assert "$op=ge" in s
    fb = coverage_feedback(
        check_constraint_coverage(
            _COUNT_Q, _LIST_CYPHER, params=_PARAMS, template="literal_compare"
        )
    )
    assert "literal_compare_count" in fb


@pytest.mark.asyncio
async def test_memory_count_is_uncapped_scalar():
    """Count twin must not inherit the list helper's default LIMIT 25."""
    store = MemoryGraphStore()
    cat = store.session(GraphScope.for_catalog(layer="tenant", tenant_id="demo-tenant"))
    await upsert_type_pg(cat, name="Gadget", description="widget")
    session = store.session(GraphScope.for_instance("demo-tenant", "lab"))
    n_after = 30
    n_before = 8
    for i in range(n_before):
        eid = f"{IRI_BASE}/entities/Gadget/old{i}"
        await session.write_merge_entity(
            id=eid, primary_type="Gadget", name=f"old{i}", source="test"
        )
        await assert_fact(
            session, AssertionFact(subject_id=eid, kind="type", value="Gadget"),
            dual_write_cache=True,
        )
        await assert_fact(
            session,
            AssertionFact(
                subject_id=eid,
                kind="literal",
                property_leaf="start_year",
                value=2014,
            ),
            dual_write_cache=True,
        )
    for i in range(n_after):
        eid = f"{IRI_BASE}/entities/Gadget/new{i}"
        await session.write_merge_entity(
            id=eid, primary_type="Gadget", name=f"new{i}", source="test"
        )
        await assert_fact(
            session, AssertionFact(subject_id=eid, kind="type", value="Gadget"),
            dual_write_cache=True,
        )
        year = 2019 if i % 2 == 0 else 2020
        await assert_fact(
            session,
            AssertionFact(
                subject_id=eid,
                kind="literal",
                property_leaf="start_year",
                value=year,
            ),
            dual_write_cache=True,
        )
    listed = await session.execute_template(
        "literal_compare",
        {
            "type_names": ["Gadget"],
            "prop_key": "start_year",
            "op": "ge",
            "threshold": 2019,
            "limit": 25,
        },
    )
    assert len(listed) == 25
    counted = await session.execute_template(
        "literal_compare_count",
        {
            "type_names": ["Gadget"],
            "prop_key": "start_year",
            "op": "ge",
            "threshold": 2019,
        },
    )
    assert len(counted) == 1
    assert counted[0].get("n") == n_after
    before = await session.execute_template(
        "literal_compare_count",
        {
            "type_names": ["Gadget"],
            "prop_key": "start_year",
            "op": "ge",
            "threshold": 2014,
        },
    )
    assert before[0].get("n") == n_before + n_after

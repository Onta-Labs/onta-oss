"""Hermetic post-plan repair: how-many list→count + agg-wrapped leaves.

Synthetic Widget / Gadget names only. No eval-set questions, no NSCLC.
Tests the deterministic repair — they must pass without an LLM.
"""

from __future__ import annotations

from infona_client.graph.schema_bootstrap import TEMPLATES
from infona_client.nlp.ask_plan_repair import (
    apply_ask_plan_repair,
    repair_ask_plan,
    unwrap_agg_prefixed_prop,
)
from infona_client.nlp.query_constraint_coverage_check import check_constraint_coverage
from infona_client.nlp.schema_valid_cypher import OntologyLeafInventory

_COMPARE_PARAMS = {
    "type_names": ["Gadget"],
    "prop_key": "start_year",
    "op": "ge",
    "threshold": 2014,
}
_VALUES_PARAMS = {
    "type_names": ["Widget"],
    "prop_key": "status_label",
    "prop_value": "active",
}
_COUNT_Q = "How many gadgets started in or after 2014?"
_LIST_Q = "List gadgets started in or after 2014"
_EQ_COUNT_Q = "How many widgets have status_label active?"
_AVG_Q = "What is the average enrollment for widgets targeting north?"


def test_how_many_inequality_rewrites_to_count_twin():
    """How-many + literal_compare → literal_compare_count; filters stay."""
    got = repair_ask_plan(
        question=_COUNT_Q,
        template="literal_compare",
        params=_COMPARE_PARAMS,
        cypher=TEMPLATES["literal_compare"].cypher,
    )
    assert got.changed
    assert got.template == "literal_compare_count"
    assert got.params["prop_key"] == "start_year"
    assert got.params["op"] == "ge"
    assert got.params["threshold"] == 2014
    assert got.params["type_names"] == ["Gadget"]
    assert got.cypher == TEMPLATES["literal_compare_count"].cypher
    assert "count(DISTINCT e)" in got.cypher
    assert "$limit" not in got.cypher


def test_how_many_equality_rewrites_literal_values_to_count():
    got = repair_ask_plan(
        question=_EQ_COUNT_Q,
        template="literal_values",
        params=_VALUES_PARAMS,
        cypher=TEMPLATES["literal_values"].cypher,
    )
    assert got.changed
    assert got.template == "literal_values_count"
    assert got.params["prop_key"] == "status_label"
    assert got.params["prop_value"] == "active"
    assert got.cypher == TEMPLATES["literal_values_count"].cypher


def test_list_question_keeps_list_template():
    got = repair_ask_plan(
        question=_LIST_Q,
        template="literal_compare",
        params=_COMPARE_PARAMS,
        cypher=TEMPLATES["literal_compare"].cypher,
    )
    assert not got.changed
    assert got.template == "literal_compare"
    assert got.cypher == TEMPLATES["literal_compare"].cypher


def test_already_count_twin_is_noop():
    got = repair_ask_plan(
        question=_COUNT_Q,
        template="literal_compare_count",
        params=_COMPARE_PARAMS,
        cypher=TEMPLATES["literal_compare_count"].cypher,
    )
    assert not got.changed
    assert got.template == "literal_compare_count"


def test_avg_question_does_not_become_count():
    got = repair_ask_plan(
        question=_AVG_Q,
        template="literal_aggregate",
        params={"type_names": ["Widget"], "prop_key": "enrollment", "agg_op": "avg"},
        cypher=TEMPLATES["literal_aggregate"].cypher,
    )
    assert not got.changed
    assert got.template == "literal_aggregate"


def test_average_enrollment_unwraps_to_enrollment_leaf():
    """Invented average_enrollment → enrollment when that leaf is on the plan."""
    leaves = {"enrollment", "status_label", "start_year"}
    assert unwrap_agg_prefixed_prop("average_enrollment", leaves) == "enrollment"
    assert unwrap_agg_prefixed_prop("avg_enrollment", leaves) == "enrollment"
    assert unwrap_agg_prefixed_prop("mean_enrollment", leaves) == "enrollment"
    assert unwrap_agg_prefixed_prop("total_enrollment", leaves) == "enrollment"

    got = repair_ask_plan(
        question=_AVG_Q,
        template="literal_aggregate",
        params={
            "type_names": ["Widget"],
            "prop_key": "average_enrollment",
            "agg_op": "avg",
        },
        cypher="WHERE p.name = 'average_enrollment' RETURN avg(num) AS value",
        known_leaves=leaves,
    )
    assert got.changed
    assert got.params["prop_key"] == "enrollment"
    assert got.params["agg_op"] == "avg"
    assert got.template == "literal_aggregate"
    assert "average_enrollment" not in got.cypher
    assert "enrollment" in got.cypher


def test_does_not_rewrite_unrelated_keys():
    leaves = {"enrollment", "headcount"}
    assert unwrap_agg_prefixed_prop("unit_cost", leaves) is None
    assert unwrap_agg_prefixed_prop("vendor_code", leaves) is None
    assert unwrap_agg_prefixed_prop("average_sku", leaves) is None
    assert unwrap_agg_prefixed_prop("headcount", leaves) is None
    # Prefixed name is itself a declared leaf — leave it.
    assert (
        unwrap_agg_prefixed_prop("average_score", {"average_score", "score"}) is None
    )

    got = repair_ask_plan(
        question=_AVG_Q,
        template="literal_aggregate",
        params={"prop_key": "unit_cost", "agg_op": "avg"},
        known_leaves=leaves,
    )
    assert not got.changed
    assert got.params["prop_key"] == "unit_cost"


def test_repair_then_coverage_passes_for_how_many():
    """After rewrite, the count-vs-list fail-close must not fire."""
    got = repair_ask_plan(
        question=_COUNT_Q,
        template="literal_compare",
        params=_COMPARE_PARAMS,
        cypher=TEMPLATES["literal_compare"].cypher,
    )
    r = check_constraint_coverage(
        _COUNT_Q,
        got.cypher,
        params=got.params,
        template=got.template,
    )
    assert r.ok
    assert not r.fail_closed


def test_apply_ask_plan_repair_mutates_gen_from_inventory():
    inv = OntologyLeafInventory.from_leaves(
        attribute_leaves=("enrollment", "sku"),
        type_names=("Widget",),
    )
    gen = {
        "template": "literal_aggregate",
        "params": {"prop_key": "average_enrollment", "agg_op": "avg"},
        "cypher": "RETURN e.average_enrollment",
    }
    params, cypher, changed = apply_ask_plan_repair(
        gen,
        question=_AVG_Q,
        params=gen["params"],
        cypher=gen["cypher"],
        inventory=inv,
    )
    assert changed
    assert gen["params"]["prop_key"] == "enrollment"
    assert params["prop_key"] == "enrollment"
    assert "average_enrollment" not in cypher
    assert gen["template"] == "literal_aggregate"

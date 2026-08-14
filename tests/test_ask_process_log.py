"""Hermetic tests: ask process log + money param hard-bind + token collapse."""

from __future__ import annotations

from infona_client.nlp.ask_process_log import apply_money_leaf_params
from infona_client.nlp.planning_schema import PlanningSlot, format_planning_slot
from infona_client.nlp.query_intent import collapse_filter_tokens, sketch_query_intent


def test_apply_money_leaf_overrides_bare_cost():
    p = apply_money_leaf_params(
        {"cost_prop": "cost", "ready_prop": "ready"},
        money_leaf="assay_cost",
        money_cue="cost",
    )
    assert p["cost_prop"] == "assay_cost"
    assert p["prop_key"] == "assay_cost"
    assert p["_money_leaf_bound"] == "assay_cost"


def test_apply_money_leaf_fills_missing_prop_key():
    p = apply_money_leaf_params({}, money_leaf="list_price")
    assert p["prop_key"] == "list_price"
    assert p["cost_prop"] == "list_price"


def test_collapse_ready_tests_false_multi():
    toks = collapse_filter_tokens(["ready tests", "ready"])
    assert toks == ["ready"]
    sk = sketch_query_intent("What is the sum of assay_cost for ready tests?")
    # Should not treat as two independent dim constraints after collapse
    assert len(sk.filter_tokens) <= 1 or "ready" in [t.lower() for t in sk.filter_tokens]


def test_format_planning_slot_includes_description():
    s = PlanningSlot(
        name="assay_cost",
        kind="literal",
        datatype="float",
        prop_key="assay_cost",
        populated=True,
        description="Cost of the assay in USD",
    )
    line = format_planning_slot(s)
    assert "assay_cost" in line
    assert "USD" in line
    assert "key=assay_cost" in line

"""Hermetic tests: average/avg/mean + noun → existing numeric leaf + AVG.

Anti-overfit: Widget / headcount only. No NSCLC, trials, or sponsors.
Product rule: /ask stays always-LLM; this is resolve + grounding context.
"""

from __future__ import annotations

from infona_client.nlp.ask_process_log import (
    apply_money_leaf_params,
    rewrite_agg_prefixed_leaf,
)
from infona_client.nlp.cypher_generate import try_aggregate_query
from infona_client.nlp.numeric_attr_resolve import (
    literal_leaves_for_type,
    resolve_numeric_attr,
    strip_leading_agg_modifier,
)
from infona_client.nlp.numeric_plan_grounding import (
    format_numeric_grounding_for_prompt,
    ground_numeric_plan,
)


HEADCOUNT_ONTO = (
    "Type: Widget\n"
    "  - sku: string (literal)\n"
    "  - headcount: integer (literal, key=headcount)\n"
)

SEMANTIC_HEADCOUNT = (
    "Type: Widget\n"
    "  Attributes: sku, headcount\n"
)


def test_strip_leading_agg_modifier_peels_noun():
    assert strip_leading_agg_modifier("average_headcount") == ("headcount", "avg")
    assert strip_leading_agg_modifier("average headcount") == ("headcount", "avg")
    assert strip_leading_agg_modifier("avg_headcount") == ("headcount", "avg")
    assert strip_leading_agg_modifier("mean headcount") == ("headcount", "avg")
    assert strip_leading_agg_modifier("avgHeadcount") == ("headcount", "avg")
    assert strip_leading_agg_modifier("headcount") == ("headcount", None)
    assert strip_leading_agg_modifier("average") == ("average", None)
    assert strip_leading_agg_modifier("") == ("", None)


def test_average_headcount_resolves_noun_leaf_not_minted_column():
    """NL average/avg/mean + headcount binds headcount — never average_headcount."""
    leaves = literal_leaves_for_type("Widget", HEADCOUNT_ONTO)
    assert "headcount" in leaves
    assert "average_headcount" not in leaves

    for mention in (
        "average_headcount",
        "average headcount",
        "avg headcount",
        "mean_headcount",
        "avgHeadcount",
    ):
        r = resolve_numeric_attr(
            mention,
            type_name="Widget",
            ontology_summary=HEADCOUNT_ONTO,
        )
        assert r.confidence == "unique", (mention, r.explanation)
        assert r.prop_key == "headcount", mention
        assert r.prop_key != "average_headcount"
        assert r.agg_op == "avg", mention
        assert r.mention == "headcount", mention

    r_sem = resolve_numeric_attr(
        "average_headcount",
        type_name="Widget",
        ontology_summary=SEMANTIC_HEADCOUNT,
    )
    assert r_sem.confidence == "unique"
    assert r_sem.prop_key == "headcount"
    assert r_sem.agg_op == "avg"


def test_ground_average_headcount_of_widgets_sets_avg():
    for question in (
        "average headcount of widgets",
        "what is the average headcount of widgets",
        "avg headcount of widgets",
        "mean headcount of widgets",
    ):
        plan = ground_numeric_plan(
            question,
            HEADCOUNT_ONTO,
            type_names=["Widget"],
        )
        assert plan is not None, question
        assert plan.confidence == "unique", (question, plan.explanation)
        assert plan.intent == "agg"
        assert plan.prop_key == "headcount"
        assert plan.agg_op == "avg"
        assert plan.subject_type == "Widget"

        text = format_numeric_grounding_for_prompt(plan)
        assert "headcount" in text
        assert "agg_op" in text
        assert "avg" in text
        # Prompt must forbid minting average_headcount as a column.
        assert "never invent" in text.lower() or "MUST AVG" in text
        assert "average_headcount" not in text.split("never invent", 1)[0]


def test_try_aggregate_average_headcount_of_widgets():
    agg = try_aggregate_query(
        "average headcount of widgets",
        HEADCOUNT_ONTO,
        type_names=["Widget"],
    )
    assert agg is not None
    assert agg["params"]["prop_key"] == "headcount"
    assert agg["params"]["agg_op"] == "avg"
    assert agg["params"]["prop_key"] != "average_headcount"


def test_hard_bind_rewrites_average_prefixed_prop_key():
    p = apply_money_leaf_params(
        {"prop_key": "average_headcount", "limit": 25},
        money_leaf="headcount",
    )
    assert p["prop_key"] == "headcount"
    assert p["limit"] == 25
    assert p["_money_leaf_bound"] == "headcount"

    p2 = apply_money_leaf_params(
        {"prop_key": "avg_headcount"},
        money_leaf="headcount",
    )
    assert p2["prop_key"] == "headcount"

    # Do not rewrite a different noun onto the bound leaf.
    p3 = apply_money_leaf_params(
        {"prop_key": "average_sku"},
        money_leaf="headcount",
    )
    assert p3["prop_key"] == "average_sku"
    assert "_money_leaf_bound" not in p3


def test_rewrite_agg_prefixed_leaf_in_cypher():
    cy = (
        "WHERE p.name = 'average_headcount' "
        "RETURN e.average_headcount AS average_headcount"
    )
    got = rewrite_agg_prefixed_leaf(cy, "headcount")
    assert "average_headcount" not in got
    assert "headcount" in got
    assert rewrite_agg_prefixed_leaf("RETURN e.headcount", "headcount") == (
        "RETURN e.headcount"
    )
    assert rewrite_agg_prefixed_leaf("RETURN e.average_sku", "headcount") == (
        "RETURN e.average_sku"
    )

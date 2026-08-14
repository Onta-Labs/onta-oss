"""Hermetic tests: query probe (dim values + money leaves) + multi-filter gate.

Anti-overfit: SynthWidget / SynthAssay / status_label / region_code /
unit_cost / list_price / assay_cost only — no persona CSV hardcodes.
"""

from __future__ import annotations

import pytest

from infona_client.nlp.dim_registry import (
    DimEntry,
    DimInventorySlot,
    DimRegistry,
    DimValue,
    build_registry_from_inventory,
    bind_tokens_in_question,
)
from infona_client.nlp.numeric_attr_resolve import resolve_numeric_attr
from infona_client.nlp.query_build import (
    QueryBuildContext,
    TypePopulation,
    format_query_build_for_prompt,
)
from infona_client.nlp.query_constraint_coverage import check_constraint_coverage
from infona_client.nlp.query_intent import extract_filter_tokens, sketch_query_intent
from infona_client.nlp.query_probe import (
    build_probe_context,
    extract_money_cue,
    format_dim_values_for_prompt,
    format_money_candidates_for_prompt,
    probe_money_leaves,
    question_has_money_cue,
)


SYN_ONTO = (
    "Type: SynthAssay (12 entities)\n"
    "  Attributes: name, status_label, assay_cost\n"
    "Type: SynthWidget (8 entities)\n"
    "  Attributes: name, list_price, unit_cost, status_label, region_code\n"
    "Type: SynthCourse [no instances]\n"
    "  Attributes: name, tuition_usd\n"
)

UNFILTERED_SUM = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names
OPTIONAL MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg, subject_id: e.id})
  -[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg})
WHERE p.name = $prop_key
WITH e, coalesce(a.literal_value, e[$prop_key]) AS raw
WHERE raw IS NOT NULL
RETURN sum(toFloat(raw)) AS value
""".strip()

SINGLE_FILTER_SUM = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names AND e.region_code = $prop_value
OPTIONAL MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg, subject_id: e.id})
  -[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg})
WHERE p.name = $prop_key
WITH e, coalesce(a.literal_value, e[$prop_key]) AS raw
WHERE raw IS NOT NULL
RETURN sum(toFloat(raw)) AS value
""".strip()

DUAL_FILTER_SUM = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names
  AND e.region_code = $prop_value
  AND e.status_label = 'ready'
OPTIONAL MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg, subject_id: e.id})
  -[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg})
WHERE p.name = $prop_key
WITH e, coalesce(a.literal_value, e[$prop_key]) AS raw
WHERE raw IS NOT NULL
RETURN sum(toFloat(raw)) AS value
""".strip()


def test_extract_multi_filter_tokens_ready_and_docka():
    """DockA + ready both extract as filter tokens (multi-filter intent)."""
    q = "total cost of ready SynthWidget in DockA"
    toks = extract_filter_tokens(q)
    lows = {t.lower() for t in toks}
    assert "ready" in lows
    assert "docka" in lows
    sk = sketch_query_intent(q)
    assert sk.has_aggregate_intent
    assert sk.has_filter_intent
    assert len(sk.filter_tokens) >= 2


def test_multi_filter_unfiltered_sum_fail_closed():
    """≥2 filter tokens + unfiltered aggregate → fail_closed (not high)."""
    q = "total cost of ready widgets in DockA"
    r = check_constraint_coverage(
        q,
        UNFILTERED_SUM,
        params={"type_names": ["SynthWidget"], "prop_key": "unit_cost"},
    )
    assert not r.ok
    assert r.fail_closed
    assert r.confidence == "low"
    assert "multi" in (r.reason or "").lower() or "filter" in (r.reason or "").lower()


def test_multi_filter_single_dim_fail_closed():
    """≥2 constraints but plan applies only one dim filter → fail_closed."""
    q = "total cost of ready widgets in DockA"
    r = check_constraint_coverage(
        q,
        SINGLE_FILTER_SUM,
        params={
            "type_names": ["SynthWidget"],
            "prop_key": "unit_cost",
            "prop_value": "DockA",
        },
    )
    assert not r.ok
    assert r.fail_closed
    assert r.confidence == "low"
    assert "multi" in (r.reason or "").lower() or "only 1" in (r.reason or "").lower()


def test_multi_filter_both_dims_ok_high():
    """Both dim filters present → coverage ok / high (or not fail_closed)."""
    q = "total cost of ready widgets in DockA"
    r = check_constraint_coverage(
        q,
        DUAL_FILTER_SUM,
        params={
            "type_names": ["SynthWidget"],
            "prop_key": "unit_cost",
            "prop_value": "DockA",
        },
    )
    assert r.ok
    assert not r.fail_closed
    assert r.confidence in ("high", "medium")
    # Prefer high when both tokens bound.
    bound_l = {t.lower() for t in r.bound_tokens}
    assert "docka" in bound_l or "ready" in bound_l


def test_money_cost_cue_prefers_assay_cost_on_populated_assay():
    """'total cost of ready …' + assay_cost on populated SynthAssay → assay_cost."""
    cands = probe_money_leaves(
        SYN_ONTO,
        question="total cost of ready SynthAssay",
        populated_types=["SynthAssay", "SynthWidget"],
        type_hint="SynthAssay",
    )
    assert cands
    leaves = [c.leaf for c in cands]
    assert "assay_cost" in leaves
    # Preferred / top should not be empty-type tuition.
    assert cands[0].leaf != "tuition_usd"
    # Type-scoped resolve picks assay_cost for cost.
    r = resolve_numeric_attr(
        "cost",
        type_name="SynthAssay",
        ontology_summary=SYN_ONTO,
        money_family=True,
        populated_types=["SynthAssay", "SynthWidget"],
    )
    assert r.confidence == "unique"
    assert r.prop_key == "assay_cost"


def test_money_price_cue_prefers_list_price():
    """'price under N' + list_price populated → list_price."""
    cands = probe_money_leaves(
        SYN_ONTO,
        question="price under 20",
        populated_types=["SynthAssay", "SynthWidget"],
        type_hint="SynthWidget",
    )
    assert cands
    assert any(c.leaf == "list_price" for c in cands)
    r = resolve_numeric_attr(
        "price",
        type_name="SynthWidget",
        ontology_summary=SYN_ONTO,
        money_family=True,
        populated_types=["SynthAssay", "SynthWidget"],
    )
    assert r.confidence == "unique"
    assert r.prop_key == "list_price"


def test_money_candidates_text_lists_populated_hosts():
    cands = probe_money_leaves(
        SYN_ONTO,
        question="total cost",
        populated_types=["SynthAssay", "SynthWidget"],
    )
    text = format_money_candidates_for_prompt(cands, cue="cost")
    assert "Money" in text or "cost" in text.lower()
    assert "assay_cost" in text or "unit_cost" in text
    assert "tuition_usd" not in text.split("preferred")[0] or "assay_cost" in text
    # Empty-type-only leaf should rank below populated or be omitted from top.
    if "tuition_usd" in text:
        # tuition may appear lower; top preferred should be cost-stem on populated.
        top = cands[0].leaf
        assert top in {"assay_cost", "unit_cost", "list_price"}


def test_dim_values_appear_in_probe_text():
    reg = build_registry_from_inventory(
        [
            DimInventorySlot(
                subject_type="SynthWidget",
                leaf="status_label",
                kind="literal",
                values=("ready", "hold", "scrap"),
                distinct_count=3,
                type_entity_count=8,
                coverage=8,
            ),
            DimInventorySlot(
                subject_type="SynthWidget",
                leaf="region_code",
                kind="literal",
                values=("DockA", "DockB", "YardC"),
                distinct_count=3,
                type_entity_count=8,
                coverage=8,
            ),
        ],
        tenant_id="t",
        kg="k",
    )
    text = format_dim_values_for_prompt(reg)
    assert "status_label" in text
    assert "ready" in text
    assert "region_code" in text
    assert "DockA" in text
    assert "Low-cardinality" in text or "dim values" in text.lower() or "literal_enum" in text


def test_high_card_dim_values_capped():
    """High-card value lists are capped (never dump unbounded free text)."""
    many = tuple(f"v{i:03d}" for i in range(80))
    reg = build_registry_from_inventory(
        [
            DimInventorySlot(
                subject_type="SynthWidget",
                leaf="status_label",
                kind="literal",
                values=many,
                distinct_count=80,
                type_entity_count=200,
                coverage=200,
            ),
        ],
        tenant_id="t",
        kg="k",
    )
    # Registry itself may refuse high-card dims — if registered, format caps.
    text = format_dim_values_for_prompt(reg, max_values=20)
    if "status_label" in text:
        # At most ~20 quoted values
        assert text.count('"v') <= 25
        assert "capped" in text.lower() or "…" in text or text.count('"') <= 50
    # Explicit huge DimEntry still capped by formatter.
    big = DimRegistry(
        tenant_id="t",
        kg="k",
        dims=(
            DimEntry(
                subject_type="SynthWidget",
                leaf="free_text_bucket",
                kind="literal_enum",
                values=tuple(
                    DimValue(normalized=f"x{i}", display=f"X{i}") for i in range(100)
                ),
                distinct_count=100,
            ),
        ),
    )
    text2 = format_dim_values_for_prompt(big, max_values=20)
    assert text2.count('"X') <= 25


def test_format_query_build_mentions_multi_constraint_rule():
    ctx = QueryBuildContext(
        types=(TypePopulation("SynthWidget", 8),),
        question_type_hits=("SynthWidget",),
        total_entities=8,
    )
    text = format_query_build_for_prompt(ctx)
    assert "Multi-constraint" in text or "multi-constraint" in text.lower() or "all listed dims" in text.lower()


def test_money_cue_helpers():
    assert question_has_money_cue("total cost of ready assays")
    assert question_has_money_cue("price under 20")
    assert not question_has_money_cue("how many ready widgets in DockA")
    assert extract_money_cue("total cost of ready") == "cost"
    assert extract_money_cue("list price under 20") == "price"


@pytest.mark.asyncio
async def test_build_probe_context_no_store_money_only():
    """Probe works without GraphStore when ontology + money cue present."""
    ctx = await build_probe_context(
        None,
        tenant_id="t",
        kg="k",
        question="total cost of ready SynthAssay",
        ontology_summary=SYN_ONTO,
        populated_types=["SynthAssay", "SynthWidget"],
        type_hint="SynthAssay",
    )
    assert ctx.money_candidates
    assert ctx.money_text
    assert "assay_cost" in ctx.money_text or any(
        c.leaf == "assay_cost" for c in ctx.money_candidates
    )
    assert ctx.money_cue == "cost"


def test_dim_binds_surface_in_dim_values_format():
    reg = build_registry_from_inventory(
        [
            DimInventorySlot(
                subject_type="SynthWidget",
                leaf="status_label",
                kind="literal",
                values=("ready", "hold"),
                distinct_count=2,
                type_entity_count=8,
                coverage=8,
            ),
            DimInventorySlot(
                subject_type="SynthWidget",
                leaf="region_code",
                kind="literal",
                values=("DockA", "DockB"),
                distinct_count=2,
                type_entity_count=8,
                coverage=8,
            ),
        ],
        tenant_id="t",
        kg="k",
    )
    binds = bind_tokens_in_question("ready widgets in DockA", reg)
    assert len(binds) >= 1
    text = format_dim_values_for_prompt(reg, binds=binds)
    assert "Bound" in text or "ready" in text
    assert "DockA" in text or "region_code" in text or "status_label" in text

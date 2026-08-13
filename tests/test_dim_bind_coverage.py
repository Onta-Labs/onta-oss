"""Dim-registry binds as required predicates in constraint coverage.

Hermetic synthetic types/attrs only. Proves the residual persona class:

* registry uniquely binds a token → plan must constrain with that leaf+value
* wrong leaf (token string present, different property) fails closed on agg
* multi unique binds: any missing on sum/count → fail_closed
* ambiguous (non-unique) binds are not hard-required
* pipeline never returns high-conf wrong total when registry would bind

Anti-overfit: no persona CSV gold names as product branches.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from infona_client.graph.iri import IRI_BASE
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.nlp.dim_registry import (
    DimInventorySlot,
    bind_filter_token,
    bind_tokens_in_question,
    build_registry_from_inventory,
    put_cached_dim_registry,
    reset_dim_registry_for_tests,
)
from infona_client.nlp.pipeline import NLQueryPipeline
from infona_client.nlp.query_constraint_coverage import (
    check_constraint_coverage,
    coverage_feedback,
    plan_covers_dim_bind,
    plan_has_dimension_filter,
)

TENANT = "test-tenant"
KG = "synth-dim-bind-kg"

TYPE_WIDGET = "SynthWidget"
ATTR_REGION = "region_code"
ATTR_STATUS = "status_label"
ATTR_TIER = "tier_code"
ATTR_QTY = "unit_qty"
REL_LOCATED_IN = "synth_located_in"
TYPE_ZONE = "SynthZone"

SYN_ONTO = (
    f"Type: {TYPE_WIDGET}\n"
    f"  - {ATTR_REGION}: string (literal, key={ATTR_REGION})\n"
    f"  - {ATTR_STATUS}: string (literal, key={ATTR_STATUS})\n"
    f"  - {ATTR_TIER}: string (literal, key={ATTR_TIER})\n"
    f"  - {ATTR_QTY}: integer (literal, key={ATTR_QTY})\n"
    f"  - {REL_LOCATED_IN}: {TYPE_ZONE} (relationship)\n"
    f"Type: {TYPE_ZONE}\n"
    f"  - name: string (literal, key=name)\n"
)

# Aggregate without any dimension filter.
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

# Wrong leaf: filters tier_code but registry bound region_code.
WRONG_LEAF_SUM = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names AND e.tier_code = $prop_value
OPTIONAL MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg, subject_id: e.id})
  -[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg})
WHERE p.name = $prop_key
WITH e, coalesce(a.literal_value, e[$prop_key]) AS raw
WHERE raw IS NOT NULL
RETURN sum(toFloat(raw)) AS value
""".strip()

# Correct leaf + value.
CORRECT_LEAF_SUM = """
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

# Only status filter of a two-bind question.
PARTIAL_MULTI_SUM = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names AND e.status_label = $prop_value
OPTIONAL MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg, subject_id: e.id})
  -[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg})
WHERE p.name = $prop_key
WITH e, coalesce(a.literal_value, e[$prop_key]) AS raw
WHERE raw IS NOT NULL
RETURN sum(toFloat(raw)) AS value
""".strip()

# Both region + status applied.
BOTH_BINDS_SUM = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names
  AND e.region_code = $region_val
  AND e.status_label = $status_val
OPTIONAL MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg, subject_id: e.id})
  -[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg})
WHERE p.name = $prop_key
WITH e, coalesce(a.literal_value, e[$prop_key]) AS raw
WHERE raw IS NOT NULL
RETURN sum(toFloat(raw)) AS value
""".strip()

# Related-entity filter for entity_dim.
REL_FILTER_SUM = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names
MATCH (e)-[r:SYNTH_LOCATED_IN]->(t:Entity {tenant_id: $tenant_id, kg: $kg})
WHERE t.name = $target_name OR coalesce(t.label, t.name) = $target_name
OPTIONAL MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg, subject_id: e.id})
  -[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg})
WHERE p.name = $prop_key
WITH e, coalesce(a.literal_value, e[$prop_key]) AS raw
WHERE raw IS NOT NULL
RETURN sum(toFloat(raw)) AS value
""".strip()


@pytest.fixture(autouse=True)
def _clear_dim_cache():
    reset_dim_registry_for_tests()
    yield
    reset_dim_registry_for_tests()


def _literal_registry() -> tuple:
    """Registry with region_code + status_label closed sets."""
    slots = [
        DimInventorySlot(
            subject_type=TYPE_WIDGET,
            leaf=ATTR_REGION,
            kind="literal",
            datatype="string",
            values=("WestZone", "EastZone", "NorthZone"),
            distinct_count=3,
            coverage=30,
            type_entity_count=30,
        ),
        DimInventorySlot(
            subject_type=TYPE_WIDGET,
            leaf=ATTR_STATUS,
            kind="literal",
            datatype="string",
            values=("Ready", "Idle", "Blocked"),
            distinct_count=3,
            coverage=30,
            type_entity_count=30,
        ),
        DimInventorySlot(
            subject_type=TYPE_WIDGET,
            leaf=ATTR_TIER,
            kind="literal",
            datatype="string",
            values=("Alpha", "Beta", "Gamma"),
            distinct_count=3,
            coverage=30,
            type_entity_count=30,
        ),
    ]
    reg = build_registry_from_inventory(slots, tenant_id=TENANT, kg=KG)
    return reg, slots


def _entity_dim_registry():
    slots = [
        DimInventorySlot(
            subject_type=TYPE_WIDGET,
            leaf=REL_LOCATED_IN,
            kind="relationship",
            range_type=TYPE_ZONE,
            values=("YardX", "YardY", "YardZ"),
            distinct_count=3,
            coverage=12,
            type_entity_count=12,
        ),
    ]
    return build_registry_from_inventory(slots, tenant_id=TENANT, kg=KG)


# ---------------------------------------------------------------------------
# Unit: plan_covers_dim_bind / wrong leaf
# ---------------------------------------------------------------------------


def test_wrong_leaf_with_value_not_covered():
    """Token string + *different* leaf does not satisfy the registry bind."""
    reg, _ = _literal_registry()
    b = bind_filter_token("WestZone", registry=reg)
    assert b is not None
    assert b.dim.leaf == ATTR_REGION

    # Plan filters tier_code = WestZone — value present, wrong leaf.
    assert plan_has_dimension_filter(
        WRONG_LEAF_SUM,
        params={"prop_key": ATTR_QTY, "prop_value": "WestZone"},
    )
    assert not plan_covers_dim_bind(
        b,
        WRONG_LEAF_SUM,
        params={"prop_key": ATTR_QTY, "prop_value": "WestZone"},
    )


def test_correct_leaf_and_value_covered():
    reg, _ = _literal_registry()
    b = bind_filter_token("WestZone", registry=reg)
    assert b is not None
    assert plan_covers_dim_bind(
        b,
        CORRECT_LEAF_SUM,
        params={"prop_key": ATTR_QTY, "prop_value": "WestZone"},
    )


def test_registry_bind_wrong_leaf_aggregate_fail_closed():
    """1. Registry bind present, Cypher filters wrong leaf → fail_closed on agg."""
    reg, _ = _literal_registry()
    binds = bind_tokens_in_question(
        f"sum {ATTR_QTY} for WestZone", reg
    )
    assert any(b.dim.leaf == ATTR_REGION for b in binds)

    r = check_constraint_coverage(
        f"sum {ATTR_QTY} for WestZone",
        WRONG_LEAF_SUM,
        params={"type_names": [TYPE_WIDGET], "prop_key": ATTR_QTY, "prop_value": "WestZone"},
        dim_binds=binds,
    )
    assert not r.ok
    assert r.fail_closed
    assert r.confidence == "low"
    assert r.unbound_dim_binds
    assert any(ATTR_REGION in x for x in r.unbound_dim_binds)
    # Without dim_binds, old logic would pass (token bound + has_dim).
    legacy = check_constraint_coverage(
        f"sum {ATTR_QTY} for WestZone",
        WRONG_LEAF_SUM,
        params={"type_names": [TYPE_WIDGET], "prop_key": ATTR_QTY, "prop_value": "WestZone"},
    )
    assert legacy.ok and legacy.confidence == "high"


def test_registry_bind_correct_leaf_ok_high():
    """2. Correct leaf+value → coverage ok, high confidence."""
    reg, _ = _literal_registry()
    binds = bind_tokens_in_question(f"sum {ATTR_QTY} for WestZone", reg)
    r = check_constraint_coverage(
        f"sum {ATTR_QTY} for WestZone",
        CORRECT_LEAF_SUM,
        params={
            "type_names": [TYPE_WIDGET],
            "prop_key": ATTR_QTY,
            "prop_value": "WestZone",
        },
        dim_binds=binds,
    )
    assert r.ok
    assert not r.fail_closed
    assert r.confidence == "high"
    assert r.bound_dim_binds
    assert not r.unbound_dim_binds


def _two_unique_binds(reg):
    """Explicit unique binds (avoid extract_filter_tokens multi-word greed)."""
    b_region = bind_filter_token("WestZone", registry=reg)
    b_status = bind_filter_token("Ready", registry=reg)
    assert b_region is not None and b_region.dim.leaf == ATTR_REGION
    assert b_status is not None and b_status.dim.leaf == ATTR_STATUS
    return [b_region, b_status]


def test_multi_bind_only_one_applied_fail_closed():
    """3. Two unique binds, plan applies one → fail_closed multi-bind on sum."""
    reg, _ = _literal_registry()
    binds = _two_unique_binds(reg)
    # Question phrasing that sketch still marks as aggregate + filter intent.
    q = f'sum {ATTR_QTY} where region is WestZone and status is Ready'

    r = check_constraint_coverage(
        q,
        PARTIAL_MULTI_SUM,
        params={
            "type_names": [TYPE_WIDGET],
            "prop_key": ATTR_QTY,
            "prop_value": "Ready",
        },
        dim_binds=binds,
    )
    assert not r.ok
    assert r.fail_closed
    assert r.confidence == "low"
    assert "multi-bind" in (r.reason or "").lower() or "missing" in (r.reason or "").lower()
    assert any(ATTR_REGION in x for x in r.unbound_dim_binds)
    fb = coverage_feedback(r, previous_cypher=PARTIAL_MULTI_SUM)
    assert "dim-registry" in fb.lower() or "Unbound dim-registry" in fb
    assert ATTR_REGION in fb


def test_multi_bind_both_applied_ok_high():
    """4. Two unique binds, both applied → ok high."""
    reg, _ = _literal_registry()
    binds = _two_unique_binds(reg)
    q = f'sum {ATTR_QTY} where region is WestZone and status is Ready'

    r = check_constraint_coverage(
        q,
        BOTH_BINDS_SUM,
        params={
            "type_names": [TYPE_WIDGET],
            "prop_key": ATTR_QTY,
            "region_val": "WestZone",
            "status_val": "Ready",
        },
        dim_binds=binds,
    )
    assert r.ok
    assert not r.fail_closed
    assert r.confidence == "high"
    assert len(r.bound_dim_binds) >= 2
    assert not r.unbound_dim_binds


def test_ambiguous_bind_not_hard_required():
    """5. Ambiguous token (same value on two dims) → no unique bind hard-require."""
    slots = [
        DimInventorySlot(
            subject_type=TYPE_WIDGET,
            leaf=ATTR_REGION,
            kind="literal",
            datatype="string",
            values=("SharedTok", "WestZone"),
            distinct_count=2,
            coverage=10,
            type_entity_count=10,
        ),
        DimInventorySlot(
            subject_type=TYPE_WIDGET,
            leaf=ATTR_STATUS,
            kind="literal",
            datatype="string",
            values=("SharedTok", "Ready"),
            distinct_count=2,
            coverage=10,
            type_entity_count=10,
        ),
    ]
    reg = build_registry_from_inventory(slots, tenant_id=TENANT, kg=KG)
    # Unique path returns nothing for SharedTok.
    binds = bind_tokens_in_question("sum unit_qty for SharedTok", reg)
    assert binds == []

    # Without unique binds, wrong-leaf shape with value still passes if has_dim
    # (existing token logic). Ambiguous must not invent a hard require.
    r = check_constraint_coverage(
        "sum unit_qty for SharedTok",
        WRONG_LEAF_SUM,
        params={
            "type_names": [TYPE_WIDGET],
            "prop_key": ATTR_QTY,
            "prop_value": "SharedTok",
        },
        dim_binds=binds,
    )
    assert r.ok
    assert not r.fail_closed
    assert r.confidence == "high"


def test_entity_dim_rel_filter_covered():
    reg = _entity_dim_registry()
    b = bind_filter_token("YardX", registry=reg)
    assert b is not None
    assert b.dim.kind == "entity_dim"
    assert plan_covers_dim_bind(
        b,
        REL_FILTER_SUM,
        params={
            "prop_key": ATTR_QTY,
            "target_name": "YardX",
            "rel_attr": REL_LOCATED_IN,
        },
        template="related_entity_name_filter",
    )


def test_entity_dim_missing_on_aggregate_fail_closed():
    reg = _entity_dim_registry()
    binds = bind_tokens_in_question(f"sum {ATTR_QTY} for YardX", reg)
    assert binds
    r = check_constraint_coverage(
        f"sum {ATTR_QTY} for YardX",
        UNFILTERED_SUM,
        params={"type_names": [TYPE_WIDGET], "prop_key": ATTR_QTY},
        dim_binds=binds,
        template="literal_aggregate",
    )
    assert not r.ok
    assert r.fail_closed
    assert r.unbound_dim_binds


def test_list_plan_missing_bind_soft_medium():
    """Non-aggregate list: missing registry bind → soft medium, not fail_closed."""
    reg, _ = _literal_registry()
    binds = bind_tokens_in_question("list widgets for WestZone", reg)
    list_cypher = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names AND e.tier_code = $prop_value
RETURN e.id AS id, e.name AS name LIMIT 50
""".strip()
    r = check_constraint_coverage(
        "list widgets for WestZone",
        list_cypher,
        params={"type_names": [TYPE_WIDGET], "prop_value": "WestZone"},
        dim_binds=binds,
    )
    assert r.ok
    assert not r.fail_closed
    assert r.confidence == "medium"
    assert r.unbound_dim_binds


# ---------------------------------------------------------------------------
# Pipeline path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_wrong_leaf_fail_closes_with_registry():
    """6. Mock LLM wrong-leaf aggregate + seeded registry → reject/fail-closed."""
    reg, _ = _literal_registry()
    put_cached_dim_registry(reg)

    store = MemoryGraphStore()
    neptune = MagicMock()
    neptune.query = AsyncMock(side_effect=AssertionError("no sparql"))
    pipe = NLQueryPipeline(neptune, anthropic_key="test-key", graph_store=store)
    pipe._fetch_ontology = AsyncMock(return_value=SYN_ONTO)  # type: ignore[method-assign]

    calls: list[str] = []

    async def fake_llm(question, ontology, **kw):
        calls.append(kw.get("error_feedback") or "")
        return {
            "cypher": WRONG_LEAF_SUM,
            "params": {
                "type_names": [TYPE_WIDGET],
                "prop_key": ATTR_QTY,
                "prop_value": "WestZone",
                "tenant_id": TENANT,
                "kg": KG,
            },
            "explanation": "sum with wrong leaf",
            "functions_needed": [],
        }

    pipe._try_llm_cypher = fake_llm  # type: ignore[method-assign]
    pipe._rephrase_via_openrouter = AsyncMock(return_value=None)  # type: ignore[method-assign]
    exec_mock = AsyncMock(side_effect=AssertionError("must not execute wrong-leaf total"))
    pipe._execute_confined_cypher = exec_mock  # type: ignore[method-assign]

    result = await pipe.ask(
        f"sum {ATTR_QTY} for WestZone",
        graph_uri=f"{IRI_BASE}/graphs/{TENANT}",
        instance_graph=f"{IRI_BASE}/graphs/{TENANT}/kg/{KG}",
        use_cypher=True,
    )

    assert result.query_confidence == "low"
    assert "Could not answer" in (result.answer or "")
    assert result.timing.get("query_constraint_coverage_reject") == 1.0 or any(
        "dim-registry" in (fb or "").lower() or "CONSTRAINT COVERAGE" in (fb or "")
        for fb in calls[1:]
    )
    # Never high-conf wrong total.
    assert result.query_confidence != "high"
    exec_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_pipeline_correct_bind_executes_high():
    reg, _ = _literal_registry()
    put_cached_dim_registry(reg)

    store = MemoryGraphStore()
    neptune = MagicMock()
    neptune.query = AsyncMock(side_effect=AssertionError("no sparql"))
    pipe = NLQueryPipeline(neptune, anthropic_key="test-key", graph_store=store)
    pipe._fetch_ontology = AsyncMock(return_value=SYN_ONTO)  # type: ignore[method-assign]

    async def fake_llm(question, ontology, **kw):
        return {
            "cypher": CORRECT_LEAF_SUM,
            "params": {
                "type_names": [TYPE_WIDGET],
                "prop_key": ATTR_QTY,
                "prop_value": "WestZone",
                "tenant_id": TENANT,
                "kg": KG,
            },
            "explanation": "sum with correct leaf",
            "functions_needed": [],
        }

    pipe._try_llm_cypher = fake_llm  # type: ignore[method-assign]
    pipe._rephrase_via_openrouter = AsyncMock(return_value="")  # type: ignore[method-assign]
    pipe._execute_confined_cypher = AsyncMock(  # type: ignore[method-assign]
        return_value=([{"value": 7}], "freeform:mock")
    )

    result = await pipe.ask(
        f"sum {ATTR_QTY} for WestZone",
        graph_uri=f"{IRI_BASE}/graphs/{TENANT}",
        instance_graph=f"{IRI_BASE}/graphs/{TENANT}/kg/{KG}",
        use_cypher=True,
    )
    assert result.query_confidence == "high"
    assert result.timing.get("query_constraint_coverage_reject") in (None, 0, 0.0)
    pipe._execute_confined_cypher.assert_awaited()
    # Timing should surface bind count when registry hit.
    assert result.timing.get("dim_binds_count", 0) >= 1 or result.timing.get(
        "dim_registry"
    ) == "present"

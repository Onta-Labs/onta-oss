"""Hermetic tests: constraint coverage + query confidence (filter-miss class).

Synthetic types/attrs/values only — no persona CSV gold labels. Proves:

1. Agg + filter intent, unfiltered sum Cypher → coverage fail
2. Same with term filter in WHERE/params → pass
3. Integrity OPTIONAL MATCH smell still fails (compose/regression)
4. literal_aggregate with only prop_key + filter-intent → fail
5. Pipeline: unfiltered gen → reject → filtered or fail-closed (no wrong total)
6. Confidence high/low assignment
7. Fail-closed path exposes clarification / unbound tokens
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from infona_client.graph.iri import IRI_BASE
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.nlp.cypher_filter_integrity import (
    check_cypher_filter_integrity,
    optional_match_filter_smell,
)
from infona_client.nlp.pipeline import NLQueryPipeline
from infona_client.nlp.query_constraint_coverage import (
    assign_query_confidence,
    build_clarification_prompt,
    check_constraint_coverage,
    coverage_feedback,
    fail_closed_answer,
    plan_has_dimension_filter,
    tokens_bound_in_plan,
)
from infona_client.nlp.query_intent import (
    extract_filter_tokens,
    question_has_aggregate_intent,
    sketch_query_intent,
)


# ---------------------------------------------------------------------------
# Synthetic ontology / Cypher (anti-overfit: not Fall/seats/CourseOffering)
# ---------------------------------------------------------------------------

SYN_ONTO = (
    "Type: Widget\n"
    "  - status_label: string (literal, key=status_label)\n"
    "  - region_code: string (literal, key=region_code)\n"
    "  - unit_qty: integer (literal, key=unit_qty)\n"
    "  - tier_code: string (literal, key=tier_code)\n"
    "Type: Gadget\n"
    "  - name: string (literal, key=name)\n"
)

# Unfiltered measure aggregate (the silent-wrong-total shape).
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

# Same aggregate but with a dimension filter on the entity.
FILTERED_SUM = """
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

BAD_OPTIONAL_EQ = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names
OPTIONAL MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg, subject_id: e.id})
  -[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg})
WHERE p.name = $prop_key AND a.literal_value = $prop_value
RETURN count(e) AS n
""".strip()

GOOD_ENTITY_PROP = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names AND e.status_label = $prop_value
RETURN count(e) AS n
""".strip()

PURE_TYPE_COUNT = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names OR c.id IN $type_names
RETURN count(DISTINCT e) AS n
""".strip()


# ---------------------------------------------------------------------------
# Intent sketch
# ---------------------------------------------------------------------------


def test_aggregate_and_filter_intent_synthetic():
    sk = sketch_query_intent("sum unit_qty for North")
    assert sk.has_aggregate_intent
    assert sk.has_filter_intent
    assert "sum" in sk.aggregate_ops or question_has_aggregate_intent(sk.question)
    tokens = extract_filter_tokens("sum unit_qty for North")
    assert any(t.lower() == "north" for t in tokens)


def test_filter_tokens_quoted_and_multi():
    toks = extract_filter_tokens('count gadgets with status_label "open" in West')
    lows = {t.lower() for t in toks}
    assert "open" in lows
    assert "west" in lows


# ---------------------------------------------------------------------------
# Coverage unit cases
# ---------------------------------------------------------------------------


def test_unfiltered_sum_with_filter_intent_fails_coverage():
    """Agg + filter intent, measure-only Cypher → coverage fail (silent total)."""
    r = check_constraint_coverage(
        "sum unit_qty for North",
        UNFILTERED_SUM,
        params={"type_names": ["Widget"], "prop_key": "unit_qty"},
    )
    assert not r.ok
    assert r.fail_closed
    assert r.confidence == "low"
    assert any(
        x in (r.reason or "").lower()
        for x in ("filter", "unfiltered", "dimension", "aggregate")
    )
    # North should surface as unbound when extracted.
    assert r.unbound_tokens or r.clarification_prompt


def test_filtered_sum_with_param_passes_coverage():
    r = check_constraint_coverage(
        "sum unit_qty for North",
        FILTERED_SUM,
        params={
            "type_names": ["Widget"],
            "prop_key": "unit_qty",
            "prop_value": "North",
        },
    )
    assert r.ok
    assert not r.fail_closed
    assert r.confidence == "high"
    assert plan_has_dimension_filter(
        FILTERED_SUM,
        params={"prop_key": "unit_qty", "prop_value": "North"},
    )
    bound, unbound = tokens_bound_in_plan(
        ["North"], FILTERED_SUM, {"prop_value": "North"}
    )
    assert bound == ["North"]
    assert unbound == []


def test_optional_match_integrity_still_fails_compose():
    """Regression: OPTIONAL MATCH value filter smell still rejected by integrity."""
    assert optional_match_filter_smell(BAD_OPTIONAL_EQ) is not None
    integrity = check_cypher_filter_integrity(
        BAD_OPTIONAL_EQ,
        question="how many widgets where status_label is active",
        params={"prop_key": "status_label", "prop_value": "active"},
    )
    assert integrity is not None
    cov = check_constraint_coverage(
        "how many widgets where status_label is active",
        BAD_OPTIONAL_EQ,
        params={"prop_key": "status_label", "prop_value": "active"},
        integrity_reason=integrity,
    )
    assert not cov.ok
    assert cov.confidence == "low"
    assert cov.fail_closed
    assert "integrity" in (cov.reason or "").lower()


def test_literal_aggregate_template_no_dim_fails_under_filter_intent():
    """Do not allowlist literal_aggregate blindly when filter intent present."""
    r = check_constraint_coverage(
        "sum unit_qty for North",
        UNFILTERED_SUM,
        params={"type_names": ["Widget"], "prop_key": "unit_qty", "agg_op": "sum"},
        template="literal_aggregate",
    )
    assert not r.ok
    assert r.fail_closed
    assert r.confidence == "low"
    assert "literal_aggregate" in (r.reason or "") or "measure-only" in (
        r.reason or ""
    ).lower()

    # Integrity alone may still allowlist the template — coverage is the gate.
    integrity = check_cypher_filter_integrity(
        UNFILTERED_SUM,
        question="sum unit_qty for North",
        template="literal_aggregate",
        params={"prop_key": "unit_qty", "agg_op": "sum"},
    )
    # Document current integrity behavior: template is filtering-allowlisted.
    # Coverage must still fail (this test's product claim).
    assert integrity is None or r.fail_closed


def test_literal_aggregate_ok_without_filter_intent():
    r = check_constraint_coverage(
        "sum unit_qty of all widgets",
        UNFILTERED_SUM,
        params={"type_names": ["Widget"], "prop_key": "unit_qty", "agg_op": "sum"},
        template="literal_aggregate",
    )
    assert r.ok
    assert r.confidence == "high"


def test_confidence_high_low_assignment():
    low = check_constraint_coverage(
        "sum unit_qty for North",
        UNFILTERED_SUM,
        params={"prop_key": "unit_qty"},
    )
    high = check_constraint_coverage(
        "sum unit_qty for North",
        FILTERED_SUM,
        params={"prop_key": "unit_qty", "prop_value": "North"},
    )
    assert assign_query_confidence(coverage=low, integrity_ok=True) == "low"
    assert assign_query_confidence(coverage=high, integrity_ok=True) == "high"
    assert assign_query_confidence(coverage=high, integrity_ok=False) == "low"


def test_multi_token_partial_or_fail():
    """≥2 filter-like tokens, ≤1 in plan → low / fail-closed when no dim filter."""
    q = 'widgets with status_label "active" in North'
    r = check_constraint_coverage(
        q,
        UNFILTERED_SUM,
        params={"type_names": ["Widget"], "prop_key": "unit_qty"},
    )
    assert not r.ok
    assert r.confidence == "low"
    assert r.fail_closed


def test_clarification_on_unbound_tokens():
    r = check_constraint_coverage(
        "sum unit_qty for North",
        UNFILTERED_SUM,
        params={"prop_key": "unit_qty"},
    )
    assert r.clarification_prompt
    assert "North" in r.clarification_prompt or "field" in r.clarification_prompt.lower()
    ans = fail_closed_answer(r)
    assert "Could not answer" in ans
    assert "confidence" in ans.lower() or "filter" in ans.lower()
    fb = coverage_feedback(r, previous_cypher=UNFILTERED_SUM)
    assert "CONSTRAINT COVERAGE" in fb
    assert "Rejected query" in fb


def test_build_clarification_prompt_general():
    p = build_clarification_prompt(["Zeta"])
    assert "Zeta" in p
    assert "field" in p.lower()
    # No domain hardcodes.
    for banned in ("Fall", "seats", "CourseOffering", "bookstore"):
        assert banned not in p


# ---------------------------------------------------------------------------
# Zero-instance / pollution primary types (live inventory)
# ---------------------------------------------------------------------------


def test_zero_instance_pollution_type_fail_closed():
    """Cypher uses empty Product while question matches populated Widget → low."""
    r = check_constraint_coverage(
        "sum unit_qty of active widgets",
        FILTERED_SUM,
        params={
            "type_names": ["Product"],  # empty pollution type
            "prop_key": "unit_qty",
            "prop_value": "active",
        },
        populated_types=["Widget", "SynthGadget"],  # Widget=12, Product not present
        type_counts={"Widget": 12, "SynthGadget": 3, "Product": 0},
    )
    assert not r.ok
    assert r.fail_closed
    assert r.confidence == "low"
    assert "Product" in r.empty_plan_types
    assert "Widget" in r.matched_populated_types
    assert "0 entities" in (r.reason or "") or "zero" in (r.reason or "").lower()
    # Feedback lists populated alternatives for regenerate.
    fb = coverage_feedback(r, previous_cypher=FILTERED_SUM)
    assert "Widget" in fb
    assert "Product" in fb or "zero-instance" in fb.lower()
    ans = fail_closed_answer(r)
    assert "Could not answer" in ans
    assert "Widget" in ans


def test_populated_primary_type_with_filter_stays_high():
    """Cypher uses Widget (populated) with dimension filter → high (ok)."""
    r = check_constraint_coverage(
        "sum unit_qty of active widgets",
        FILTERED_SUM,
        params={
            "type_names": ["Widget"],
            "prop_key": "unit_qty",
            "prop_value": "active",
        },
        populated_types=["Widget", "SynthGadget"],
        type_counts={"Widget": 12, "Product": 0},
    )
    assert r.ok
    assert not r.fail_closed
    assert r.confidence == "high"
    assert r.empty_plan_types == ()


def test_zero_instance_without_inventory_skips_gate():
    """Without populated_types, pollution gate is inactive (legacy path)."""
    r = check_constraint_coverage(
        "sum unit_qty of active widgets",
        FILTERED_SUM,
        params={
            "type_names": ["Product"],
            "prop_key": "unit_qty",
            "prop_value": "active",
        },
    )
    # Dim filter present + filter intent → high under pre-inventory rules.
    assert r.ok
    assert r.confidence == "high"
    assert r.empty_plan_types == ()


def test_zero_instance_no_question_type_match_skips_hard_fail():
    """Empty plan type but question does not match any populated type → no gate."""
    r = check_constraint_coverage(
        "sum unit_qty for North",  # no Widget/Gadget type cue
        FILTERED_SUM,
        params={
            "type_names": ["Product"],
            "prop_key": "unit_qty",
            "prop_value": "North",
        },
        populated_types=["Widget", "SynthGadget"],
        type_counts={"Widget": 12, "SynthGadget": 3, "Product": 0},
    )
    # No alternative type match → do not fail closed on population alone.
    assert r.empty_plan_types == ()
    assert r.ok
    assert r.confidence == "high"


def test_mixed_populated_and_empty_plan_types_ok():
    """If any primary type is populated, inventory gate does not fail-close."""
    r = check_constraint_coverage(
        "sum unit_qty of widgets",
        FILTERED_SUM,
        params={
            "type_names": ["Widget", "Product"],
            "prop_key": "unit_qty",
            "prop_value": "North",
        },
        populated_types=["Widget"],
        type_counts={"Widget": 12, "Product": 0},
    )
    assert r.ok
    assert not r.fail_closed
    assert r.confidence == "high"


# ---------------------------------------------------------------------------
# Pipeline integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_retries_then_fail_closes_unfiltered_aggregate():
    """Unfiltered sum under filter intent never executes; fail-closed + clarify."""
    store = MemoryGraphStore()
    neptune = MagicMock()
    neptune.query = AsyncMock(side_effect=AssertionError("no sparql"))
    pipe = NLQueryPipeline(neptune, anthropic_key="test-key", graph_store=store)
    pipe._fetch_ontology = AsyncMock(return_value=SYN_ONTO)  # type: ignore[method-assign]

    calls: list[str] = []

    async def fake_llm(question, ontology, **kw):
        calls.append(kw.get("error_feedback") or "")
        return {
            "cypher": UNFILTERED_SUM,
            "params": {
                "type_names": ["Widget"],
                "prop_key": "unit_qty",
                "agg_op": "sum",
            },
            "template": "literal_aggregate",
            "explanation": "sum unit_qty (unfiltered)",
            "functions_needed": [],
        }

    pipe._try_llm_cypher = fake_llm  # type: ignore[method-assign]
    pipe._rephrase_via_openrouter = AsyncMock(return_value=None)  # type: ignore[method-assign]

    result = await pipe.ask(
        "sum unit_qty for North",
        graph_uri=f"{IRI_BASE}/graphs/demo-tenant",
        instance_graph=f"{IRI_BASE}/graphs/demo-tenant/kg/syn-widgets",
        use_cypher=True,
    )

    assert len(calls) == 3  # max_attempts
    assert any("CONSTRAINT COVERAGE" in (fb or "") for fb in calls[1:])
    assert result.timing.get("query_constraint_coverage_reject") == 1.0
    assert result.query_confidence == "low"
    assert result.query_confidence_reason
    assert "Could not answer" in result.answer
    # Must not look like a successful numeric total.
    assert result.timing.get("rows") in (None, 0) or "rows" not in result.timing
    # Clarification surfaced.
    assert result.clarification_prompt or "field" in result.answer.lower()


@pytest.mark.asyncio
async def test_pipeline_fail_closes_zero_instance_pollution_type(monkeypatch):
    """Inventory Widget=12 Product=0; plan uses Product only → fail-closed low conf."""
    from infona_client.nlp.query_build import QueryBuildContext, TypePopulation

    store = MemoryGraphStore()
    neptune = MagicMock()
    neptune.query = AsyncMock(side_effect=AssertionError("no sparql"))
    pipe = NLQueryPipeline(neptune, anthropic_key="test-key", graph_store=store)
    pipe._fetch_ontology = AsyncMock(return_value=SYN_ONTO)  # type: ignore[method-assign]

    async def fake_build(*_a, **_k):
        return QueryBuildContext(
            types=(
                TypePopulation("Widget", 12),
                TypePopulation("SynthGadget", 3),
            ),
            question_type_hits=("Widget",),
            total_entities=15,
        )

    monkeypatch.setattr(
        "infona_client.nlp.query_build.collect_query_build_context",
        fake_build,
    )

    calls: list[str] = []

    async def fake_llm(question, ontology, **kw):
        calls.append(kw.get("error_feedback") or "")
        # Looks "covered" (has dim filter) but wrong empty primary type.
        return {
            "cypher": FILTERED_SUM,
            "params": {
                "type_names": ["Product"],
                "prop_key": "unit_qty",
                "prop_value": "active",
            },
            "explanation": "sum on empty Product",
            "functions_needed": [],
        }

    pipe._try_llm_cypher = fake_llm  # type: ignore[method-assign]
    pipe._rephrase_via_openrouter = AsyncMock(return_value=None)  # type: ignore[method-assign]
    pipe._execute_confined_cypher = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("must not execute zero-instance plan")
    )

    result = await pipe.ask(
        "sum unit_qty of active widgets",
        graph_uri=f"{IRI_BASE}/graphs/demo-tenant",
        instance_graph=f"{IRI_BASE}/graphs/demo-tenant/kg/syn-widgets",
        use_cypher=True,
    )

    assert len(calls) == 3
    assert any(
        "zero-instance" in (fb or "").lower()
        or "POPULATED" in (fb or "")
        or "Widget" in (fb or "")
        for fb in calls[1:]
    )
    assert result.timing.get("query_constraint_coverage_reject") == 1.0
    assert result.timing.get("query_zero_instance_type") == 1.0
    assert result.query_confidence == "low"
    assert "Could not answer" in result.answer
    assert "Widget" in (result.answer or "") or "Product" in (
        result.query_confidence_reason or ""
    )
    pipe._execute_confined_cypher.assert_not_awaited()


@pytest.mark.asyncio
async def test_pipeline_accepts_filtered_plan_after_retry():
    """First gen unfiltered → reject; second gen filtered → execute (no reject)."""
    store = MemoryGraphStore()
    neptune = MagicMock()
    neptune.query = AsyncMock(side_effect=AssertionError("no sparql"))
    pipe = NLQueryPipeline(neptune, anthropic_key="test-key", graph_store=store)
    pipe._fetch_ontology = AsyncMock(return_value=SYN_ONTO)  # type: ignore[method-assign]

    n = {"i": 0}

    async def fake_llm(question, ontology, **kw):
        n["i"] += 1
        if n["i"] == 1:
            return {
                "cypher": UNFILTERED_SUM,
                "params": {
                    "type_names": ["Widget"],
                    "prop_key": "unit_qty",
                    "agg_op": "sum",
                },
                "template": "literal_aggregate",
                "explanation": "unfiltered",
                "functions_needed": [],
            }
        return {
            "cypher": FILTERED_SUM,
            "params": {
                "type_names": ["Widget"],
                "prop_key": "unit_qty",
                "prop_value": "North",
                "agg_op": "sum",
            },
            "explanation": "filtered sum",
            "functions_needed": [],
        }

    pipe._try_llm_cypher = fake_llm  # type: ignore[method-assign]
    pipe._rephrase_via_openrouter = AsyncMock(return_value="")  # type: ignore[method-assign]
    # MemoryGraphStore cannot run free-form Cypher; stub execute after the gate.
    pipe._execute_confined_cypher = AsyncMock(  # type: ignore[method-assign]
        return_value=([{"value": 42}], "freeform:mock")
    )

    result = await pipe.ask(
        "sum unit_qty for North",
        graph_uri=f"{IRI_BASE}/graphs/demo-tenant",
        instance_graph=f"{IRI_BASE}/graphs/demo-tenant/kg/syn-widgets",
        use_cypher=True,
    )

    assert n["i"] == 2
    assert result.timing.get("query_constraint_coverage_reject") in (None, 0, 0.0)
    assert result.timing.get("query_constraint_coverage_retry") == 1.0
    assert result.query_confidence == "high"
    assert result.timing.get("query_language") == "cypher"
    assert "Could not answer with confidence" not in (result.answer or "")
    pipe._execute_confined_cypher.assert_awaited()


@pytest.mark.asyncio
async def test_pipeline_integrity_regression_still_fail_closes():
    """Unrewritable type-scan still fail-closes (coverage composes, does not drop)."""
    store = MemoryGraphStore()
    neptune = MagicMock()
    neptune.query = AsyncMock(side_effect=AssertionError("no sparql"))
    pipe = NLQueryPipeline(neptune, anthropic_key="test-key", graph_store=store)
    pipe._fetch_ontology = AsyncMock(return_value=SYN_ONTO)  # type: ignore[method-assign]

    async def fake_llm(question, ontology, **kw):
        return {
            "cypher": PURE_TYPE_COUNT,
            "params": {"type_names": ["Widget"]},
            "explanation": "unfiltered type scan",
            "functions_needed": [],
        }

    pipe._try_llm_cypher = fake_llm  # type: ignore[method-assign]
    pipe._rephrase_via_openrouter = AsyncMock(return_value=None)  # type: ignore[method-assign]
    exec_mock = AsyncMock(side_effect=AssertionError("must not execute bad plan"))
    pipe._execute_confined_cypher = exec_mock  # type: ignore[method-assign]

    result = await pipe.ask(
        "how many widgets where status_label is active",
        graph_uri=f"{IRI_BASE}/graphs/demo-tenant",
        instance_graph=f"{IRI_BASE}/graphs/demo-tenant/kg/syn-widgets",
        use_cypher=True,
    )
    assert result.timing.get("cypher_filter_integrity_reject") == 1.0
    assert result.query_confidence == "low"
    assert "Could not answer" in result.answer
    exec_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_pipeline_good_entity_prop_high_confidence():
    store = MemoryGraphStore()
    neptune = MagicMock()
    neptune.query = AsyncMock(side_effect=AssertionError("no sparql"))
    pipe = NLQueryPipeline(neptune, anthropic_key="test-key", graph_store=store)
    pipe._fetch_ontology = AsyncMock(return_value=SYN_ONTO)  # type: ignore[method-assign]

    async def fake_llm(question, ontology, **kw):
        return {
            "cypher": GOOD_ENTITY_PROP,
            "params": {
                "type_names": ["Widget"],
                "prop_value": "active",
            },
            "explanation": "count with entity prop",
            "functions_needed": [],
        }

    pipe._try_llm_cypher = fake_llm  # type: ignore[method-assign]
    pipe._rephrase_via_openrouter = AsyncMock(return_value="")  # type: ignore[method-assign]
    pipe._execute_confined_cypher = AsyncMock(  # type: ignore[method-assign]
        return_value=([{"n": 2}], "freeform:mock")
    )

    result = await pipe.ask(
        "how many widgets where status_label is active",
        graph_uri=f"{IRI_BASE}/graphs/demo-tenant",
        instance_graph=f"{IRI_BASE}/graphs/demo-tenant/kg/syn-widgets",
        use_cypher=True,
    )
    assert result.timing.get("cypher_filter_integrity_reject") in (None, 0, 0.0)
    assert result.timing.get("query_constraint_coverage_reject") in (None, 0, 0.0)
    assert result.query_confidence == "high"
    # Value token extracted, not the attribute name blob.
    assert "status_label is active" not in (
        result.timing.get("unbound_filter_tokens") or ""
    )

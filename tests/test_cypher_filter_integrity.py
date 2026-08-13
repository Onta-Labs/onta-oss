"""Hermetic tests: free-form Cypher filter integrity (silent OPTIONAL MATCH filters).

Synthetic types/attrs only — no persona CSV gold labels. Proves:

1. Filter intent + OPTIONAL MATCH value filter without post-constraint → reject
2. Property-path / required MATCH / template-shaped plans → accept
3. Pure type count under filter intent → reject
4. Official ADR 0013 template bodies (literal_values / compare) → accept
5. Pipeline retries then fail-closes rather than execute a silent unfiltered plan
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from infona_client.graph.iri import IRI_BASE
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.rdfs_helpers import (
    LITERAL_COMPARE_CYPHER,
    LITERAL_VALUES_CYPHER,
)
from infona_client.nlp.cypher_filter_integrity import (
    check_cypher_filter_integrity,
    cypher_has_constraining_filter,
    filter_integrity_feedback,
    optional_match_filter_smell,
    pure_type_scan_without_filter,
    question_has_filter_intent,
)
from infona_client.nlp.pipeline import NLQueryPipeline


# ---------------------------------------------------------------------------
# Synthetic ontology (anti-overfit: not trials/phase/persona gold)
# ---------------------------------------------------------------------------

SYN_ONTO = (
    "Type: Widget\n"
    "  - status_label: string (literal, key=status_label)\n"
    "  - tier_code: string (literal, key=tier_code)\n"
    "  - unit_qty: integer (literal, key=unit_qty)\n"
    "Type: Gadget\n"
    "  - name: string (literal, key=name)\n"
)

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

BAD_OPTIONAL_LITERAL = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name = 'Widget'
OPTIONAL MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg, subject_id: e.id})
  -[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg})
WHERE p.name = 'status_label' AND a.literal_value = 'active'
RETURN count(*) AS n
""".strip()

GOOD_ENTITY_PROP = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names AND e.status_label = $prop_value
RETURN count(e) AS n
""".strip()

GOOD_REQUIRED_MATCH = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names
MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg, subject_id: e.id})
  -[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg})
WHERE p.name = $prop_key AND a.literal_value = $prop_value
RETURN count(DISTINCT e) AS n
""".strip()

GOOD_OPTIONAL_WITH_POST = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names
OPTIONAL MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg, subject_id: e.id})
  -[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg})
WHERE p.name = $prop_key AND a.literal_value = $prop_value
WITH DISTINCT e, a
WHERE a IS NOT NULL OR e[$prop_key] = $prop_value
RETURN count(e) AS n
""".strip()

PURE_TYPE_COUNT = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names OR c.id IN $type_names
RETURN count(DISTINCT e) AS n
""".strip()


def test_question_filter_intent_cues():
    assert question_has_filter_intent("how many widgets where status_label is active")
    assert question_has_filter_intent('widgets with tier_code = "T2"')
    assert question_has_filter_intent("sum unit_qty for active")
    assert question_has_filter_intent("how many Tier 2 widgets")
    assert not question_has_filter_intent("how many widgets")
    assert not question_has_filter_intent("list all gadgets")


def test_optional_match_filter_smell_rejects_unconstrained():
    reason = optional_match_filter_smell(BAD_OPTIONAL_EQ)
    assert reason is not None
    assert "OPTIONAL MATCH" in reason
    assert optional_match_filter_smell(BAD_OPTIONAL_LITERAL) is not None


def test_optional_match_filter_smell_accepts_post_constraint():
    assert optional_match_filter_smell(GOOD_OPTIONAL_WITH_POST) is None
    assert optional_match_filter_smell(LITERAL_VALUES_CYPHER) is None
    assert optional_match_filter_smell(LITERAL_COMPARE_CYPHER) is None


def test_optional_match_filter_smell_ignores_prop_key_only_aggregate_read():
    """OPTIONAL MATCH selecting p.name only (no value) is not a dropped filter."""
    agg_read = """
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
    assert optional_match_filter_smell(agg_read) is None


def test_property_path_and_required_match_accepted():
    assert check_cypher_filter_integrity(
        GOOD_ENTITY_PROP,
        question="how many widgets where status_label is active",
    ) is None
    assert check_cypher_filter_integrity(
        GOOD_REQUIRED_MATCH,
        question="how many widgets where status_label is active",
    ) is None
    assert cypher_has_constraining_filter(GOOD_ENTITY_PROP)
    assert cypher_has_constraining_filter(GOOD_REQUIRED_MATCH)


def test_check_rejects_bad_optional_with_filter_intent():
    reason = check_cypher_filter_integrity(
        BAD_OPTIONAL_EQ,
        question="how many widgets where status_label is active",
    )
    assert reason is not None
    assert "OPTIONAL MATCH" in reason


def test_check_rejects_pure_type_count_with_filter_intent():
    assert pure_type_scan_without_filter(PURE_TYPE_COUNT)
    reason = check_cypher_filter_integrity(
        PURE_TYPE_COUNT,
        question="how many widgets where status_label is active",
    )
    assert reason is not None
    assert "filter intent" in reason.lower() or "type-only" in reason.lower()


def test_check_accepts_pure_type_count_without_filter_intent():
    assert (
        check_cypher_filter_integrity(
            PURE_TYPE_COUNT,
            question="how many widgets",
        )
        is None
    )


def test_filtering_templates_always_ok():
    assert (
        check_cypher_filter_integrity(
            BAD_OPTIONAL_EQ,  # free-form body ignored when template is filtering
            question="how many widgets where status_label is active",
            template="literal_values",
            params={"prop_key": "status_label", "prop_value": "active"},
        )
        is None
    )
    assert (
        check_cypher_filter_integrity(
            "MATCH (e:Entity) RETURN count(*)",
            question="widgets under 10",
            template="literal_compare",
            params={"prop_key": "unit_qty", "op": "lt", "threshold": 10},
        )
        is None
    )


def test_pure_type_template_rejected_under_filter_intent():
    reason = check_cypher_filter_integrity(
        PURE_TYPE_COUNT,
        question="how many Tier 2 widgets",
        template="entities_of_type_count",
        params={"type_names": ["Widget"]},
    )
    assert reason is not None
    assert "entities_of_type_count" in reason


def test_feedback_mentions_rewrite_rules():
    fb = filter_integrity_feedback(
        "OPTIONAL MATCH value filter does not constrain primary rows",
        previous_cypher=BAD_OPTIONAL_EQ,
    )
    assert "literal_values" in fb
    assert "OPTIONAL MATCH" in fb
    assert "Rejected query" in fb


@pytest.mark.asyncio
async def test_pipeline_retries_then_fail_closes_on_filter_smell():
    """Bad free Cypher never executes; retry once then honest fail-closed answer."""
    store = MemoryGraphStore()
    neptune = MagicMock()
    neptune.query = AsyncMock(side_effect=AssertionError("no sparql"))
    pipe = NLQueryPipeline(neptune, anthropic_key="test-key", graph_store=store)
    pipe._fetch_ontology = AsyncMock(return_value=SYN_ONTO)  # type: ignore[method-assign]

    calls: list[str] = []

    async def fake_llm(question, ontology, **kw):
        calls.append(kw.get("error_feedback") or "")
        # Always emit the bad OPTIONAL MATCH plan (never self-heals) to prove
        # fail-closed after retries — not silent execute.
        return {
            "cypher": BAD_OPTIONAL_EQ,
            "params": {
                "type_names": ["Widget"],
                "prop_key": "status_label",
                "prop_value": "active",
            },
            "explanation": "count widgets with status via optional assertion",
            "functions_needed": [],
        }

    pipe._try_llm_cypher = fake_llm  # type: ignore[method-assign]
    # Avoid rephrase / embedding side effects.
    pipe._rephrase_via_openrouter = AsyncMock(return_value=None)  # type: ignore[method-assign]

    result = await pipe.ask(
        "how many widgets where status_label is active",
        graph_uri=f"{IRI_BASE}/graphs/demo-tenant",
        instance_graph=f"{IRI_BASE}/graphs/demo-tenant/kg/syn-widgets",
        use_cypher=True,
    )

    assert len(calls) == 3  # max_attempts
    # Attempts 2+ receive filter-integrity feedback.
    assert any("FILTER INTEGRITY" in (fb or "") for fb in calls[1:])
    assert result.timing.get("cypher_filter_integrity_reject") == 1.0
    assert "Could not answer" in result.answer
    assert "unfiltered" in result.answer.lower() or "incorrectly" in result.answer.lower()
    # Must not have successfully executed a silent wrong count.
    assert result.timing.get("rows") in (None, 0) or "rows" not in result.timing


@pytest.mark.asyncio
async def test_pipeline_accepts_entity_prop_filter_plan():
    """Good e.prop = $value free-form plan is not rejected by integrity gate."""
    store = MemoryGraphStore()
    neptune = MagicMock()
    neptune.query = AsyncMock(side_effect=AssertionError("no sparql"))
    pipe = NLQueryPipeline(neptune, anthropic_key="test-key", graph_store=store)
    pipe._fetch_ontology = AsyncMock(return_value=SYN_ONTO)  # type: ignore[method-assign]

    good = {
        "cypher": GOOD_ENTITY_PROP,
        "params": {
            "type_names": ["Widget"],
            "prop_value": "active",
        },
        "explanation": "count widgets with status_label via entity prop",
        "functions_needed": [],
    }

    async def fake_llm(question, ontology, **kw):
        return good

    pipe._try_llm_cypher = fake_llm  # type: ignore[method-assign]
    pipe._rephrase_via_openrouter = AsyncMock(return_value=None)  # type: ignore[method-assign]

    # Integrity gate must not reject before execute (empty graph may still
    # yield 0 rows — that is fine; proves we did not filter-integrity-reject).
    result = await pipe.ask(
        "how many widgets where status_label is active",
        graph_uri=f"{IRI_BASE}/graphs/demo-tenant",
        instance_graph=f"{IRI_BASE}/graphs/demo-tenant/kg/syn-widgets",
        use_cypher=True,
    )
    assert result.timing.get("cypher_filter_integrity_reject") in (None, 0, 0.0)
    assert result.timing.get("cypher_filter_integrity_retry") in (None, 0, 0.0)
    assert "OPTIONAL MATCH value filter" not in (result.answer or "")
    assert result.timing.get("query_language") == "cypher"

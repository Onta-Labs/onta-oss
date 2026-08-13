"""Schema-valid Cypher predicates + money synonym resolve (persona 9b3e3a).

Anti-overfit: Synth* types / leaves only — no Fall / seats / CourseOffering /
bookstore hardcodes. Proves:

1. Invented relationship (HAS_OFFERED_IN) not in ontology → reject
2. Schema leaf offered_in / OFFERED_IN → accept
3. Filter-intent agg with invalid hop → fail-closed after mock retries (no exec)
4. Bare price under N with only list_price → resolves list_price
5. Bare cost under N with only assay_cost → resolves assay_cost
6. Coverage: tokens "bound" but schema-invalid hop still low confidence
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from infona_client.graph.iri import IRI_BASE
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.nlp.cypher_generate import try_numeric_filter_query
from infona_client.nlp.numeric_attr_resolve import (
    is_money_family_leaf,
    resolve_cost_prop,
    resolve_numeric_attr,
)
from infona_client.nlp.pipeline import NLQueryPipeline
from infona_client.nlp.query_constraint_coverage import check_constraint_coverage
from infona_client.nlp.schema_valid_cypher import (
    OntologyLeafInventory,
    check_schema_valid_cypher,
    extract_cypher_rel_types,
    extract_relationship_leaves,
    fail_closed_schema_answer,
    schema_valid_feedback,
)


# ---------------------------------------------------------------------------
# Synthetic ontologies (anti-overfit)
# ---------------------------------------------------------------------------

SYNTH_OFFERING_ONTO = (
    "Type: SynthOffering\n"
    "  - seat_count: integer (literal, key=seat_count)\n"
    "  - term_code: string (literal, key=term_code)\n"
    "  - offered_in: relationship → SynthTerm\n"
    "Type: SynthTerm\n"
    "  - name: string (literal, key=name)\n"
    "  - code: string (literal, key=code)\n"
)

SYNTH_OFFERING_ATTRS_FORM = (
    "Type: SynthOffering\n"
    "  Attributes: seat_count, term_code\n"
    "  Relationships: offered_in → SynthTerm\n"
    "Type: SynthTerm\n"
    "  Attributes: name, code\n"
)

LIST_PRICE_ONLY = (
    "Type: SynthItem\n"
    "  - list_price: float (literal, key=list_price)\n"
    "  - name: string (literal, key=name)\n"
)

ASSAY_COST_ONLY = (
    "Type: SynthAssay\n"
    "  - assay_cost: float (literal, key=assay_cost)\n"
    "  - name: string (literal, key=name)\n"
)

TUITION_ONLY = (
    "Type: SynthCourse\n"
    "  - tuition_usd: float (literal, key=tuition_usd)\n"
    "  - title: string (literal, key=title)\n"
)

# Free-form invented hop (HAS_OFFERED_IN does not match offered_in → OFFERED_IN).
INVENTED_HAS_OFFERED_IN = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names
MATCH (e)-[:HAS_OFFERED_IN]->(t:Entity {tenant_id: $tenant_id, kg: $kg})
WHERE toLower(coalesce(t.name, t.display_name, '')) = toLower($target_name)
RETURN count(e) AS n
""".strip()

# Valid dual-write token for leaf offered_in.
VALID_OFFERED_IN = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names
MATCH (e)-[:OFFERED_IN]->(t:Entity {tenant_id: $tenant_id, kg: $kg})
WHERE toLower(coalesce(t.name, t.display_name, '')) = toLower($target_name)
RETURN count(e) AS n
""".strip()

# Assertion path with schema-grounded p.name.
VALID_ASSERTION_OFFERED_IN = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names
MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg})-[:SUBJECT]->(e)
MATCH (a)-[:OBJECT]->(t:Entity {tenant_id: $tenant_id, kg: $kg})
MATCH (a)-[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg})
WHERE p.name = $rel_attr
  AND toLower(coalesce(t.name, t.display_name, '')) = toLower($target_name)
RETURN count(e) AS n
""".strip()


# ---------------------------------------------------------------------------
# Unit: inventory + parse
# ---------------------------------------------------------------------------


def test_extract_relationship_leaves_dash_and_attributes_form():
    leaves = extract_relationship_leaves(SYNTH_OFFERING_ONTO)
    assert "offered_in" in [x.lower() for x in leaves]
    leaves2 = extract_relationship_leaves(SYNTH_OFFERING_ATTRS_FORM)
    assert "offered_in" in [x.lower() for x in leaves2]


def test_extract_cypher_rel_types_finds_typed_hops():
    rels = extract_cypher_rel_types(INVENTED_HAS_OFFERED_IN)
    assert any(r.upper() == "HAS_OFFERED_IN" for r in rels)
    assert any(r.upper() == "INSTANCE_OF" for r in rels)


def test_invented_relationship_rejected():
    """Cypher with HAS_OFFERED_IN when leaf is offered_in → reject."""
    r = check_schema_valid_cypher(
        INVENTED_HAS_OFFERED_IN,
        SYNTH_OFFERING_ONTO,
        params={"type_names": ["SynthOffering"], "target_name": "Zeta"},
    )
    assert not r.ok
    assert any(x.upper() == "HAS_OFFERED_IN" for x in r.invented_rel_types)
    assert "schema" in (r.reason or "").lower() or "ontology" in (r.reason or "").lower()
    fb = schema_valid_feedback(r, previous_cypher=INVENTED_HAS_OFFERED_IN)
    assert "SCHEMA PREDICATE" in fb
    ans = fail_closed_schema_answer(r)
    assert "Could not answer" in ans
    assert "confidence" in ans.lower() or "ontology" in ans.lower()
    # Anti-overfit: no persona hardcodes in feedback.
    for banned in ("Fall", "seats", "CourseOffering", "bookstore"):
        assert banned not in fb
        assert banned not in ans


def test_offered_in_leaf_accepted_as_typed_rel():
    """OFFERED_IN (sanitize of offered_in) is schema-valid."""
    r = check_schema_valid_cypher(
        VALID_OFFERED_IN,
        SYNTH_OFFERING_ONTO,
        params={"type_names": ["SynthOffering"], "target_name": "Zeta"},
    )
    assert r.ok, r.reason
    assert not r.invented_rel_types


def test_assertion_path_with_schema_rel_attr_accepted():
    r = check_schema_valid_cypher(
        VALID_ASSERTION_OFFERED_IN,
        SYNTH_OFFERING_ONTO,
        params={
            "type_names": ["SynthOffering"],
            "rel_attr": "offered_in",
            "target_name": "Zeta",
        },
        template="related_entity_name_filter",
    )
    assert r.ok, r.reason


def test_template_with_invented_rel_attr_rejected():
    r = check_schema_valid_cypher(
        VALID_ASSERTION_OFFERED_IN,
        SYNTH_OFFERING_ONTO,
        params={
            "type_names": ["SynthOffering"],
            "rel_attr": "has_offered_in",  # not declared
            "target_name": "Zeta",
        },
        template="related_entity_name_filter",
    )
    assert not r.ok
    assert any("has_offered_in" in p.lower() for p in r.invented_prop_keys)


def test_has_genre_leaf_allows_has_genre_token():
    """When leaf is literally has_genre, HAS_GENRE dual-write is valid."""
    onto = (
        "Type: SynthBook\n"
        "  - title: string (literal, key=title)\n"
        "  - has_genre: relationship → SynthGenre\n"
        "Type: SynthGenre\n"
        "  - name: string (literal, key=name)\n"
    )
    cypher = """
    MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
      tenant_id: $tenant_id, kg: $kg
    })
    WHERE c.name IN $type_names
    MATCH (e)-[:HAS_GENRE]->(g:Entity {tenant_id: $tenant_id, kg: $kg})
    RETURN e.id AS id LIMIT 10
    """.strip()
    r = check_schema_valid_cypher(
        cypher, onto, params={"type_names": ["SynthBook"]}
    )
    assert r.ok, r.reason


def test_inventory_empty_fails_open():
    r = check_schema_valid_cypher(
        INVENTED_HAS_OFFERED_IN,
        "No ontology defined yet.",
        params={},
    )
    assert r.ok


def test_coverage_schema_reason_fail_closed_even_if_tokens_bound():
    """Filter text present + invented hop → low conf / fail closed (not high)."""
    # target_name binds the NL value "Zeta" so token coverage looks OK.
    cov = check_constraint_coverage(
        "how many SynthOfferings in Zeta",
        INVENTED_HAS_OFFERED_IN,
        params={
            "type_names": ["SynthOffering"],
            "target_name": "Zeta",
        },
        schema_reason="relationship type(s) not in ontology: HAS_OFFERED_IN",
    )
    assert not cov.ok
    assert cov.fail_closed
    assert cov.confidence == "low"
    assert "schema" in (cov.reason or "").lower()


# ---------------------------------------------------------------------------
# Money synonym resolve (hermetic, Synth* only)
# ---------------------------------------------------------------------------


def test_bare_price_resolves_list_price_only_type():
    """Bare 'price under N' with only list_price on type → list_price."""
    assert is_money_family_leaf("list_price")
    r = resolve_numeric_attr(
        "price",
        type_name="SynthItem",
        ontology_summary=LIST_PRICE_ONLY,
        money_family=True,
    )
    assert r.confidence == "unique"
    assert r.prop_key == "list_price"
    assert resolve_cost_prop(LIST_PRICE_ONLY, type_name="SynthItem", mention="price") == (
        "list_price"
    )

    filt = try_numeric_filter_query(
        "Which SynthItems cost less than 15?",
        LIST_PRICE_ONLY,
        type_names=["SynthItem"],
    )
    assert filt is not None
    assert filt["params"]["prop_key"] == "list_price"
    assert filt["params"]["op"] == "lt"
    assert filt["params"]["threshold"] == 15.0


def test_bare_cost_resolves_assay_cost_only_type():
    """Bare 'cost under N' with only assay_cost → assay_cost."""
    assert is_money_family_leaf("assay_cost")
    r = resolve_numeric_attr(
        "cost",
        type_name="SynthAssay",
        ontology_summary=ASSAY_COST_ONLY,
        money_family=True,
    )
    assert r.confidence == "unique"
    assert r.prop_key == "assay_cost"

    filt = try_numeric_filter_query(
        "Which SynthAssays cost less than 50?",
        ASSAY_COST_ONLY,
        type_names=["SynthAssay"],
    )
    assert filt is not None
    assert filt["params"]["prop_key"] == "assay_cost"
    assert filt["params"]["threshold"] == 50.0


def test_tuition_nl_resolves_tuition_usd():
    r = resolve_numeric_attr(
        "tuition",
        type_name="SynthCourse",
        ontology_summary=TUITION_ONLY,
        money_family=True,
    )
    assert r.confidence == "unique"
    assert r.prop_key == "tuition_usd"

    # Bare price family also lands on tuition_usd when it is the sole money leaf.
    r2 = resolve_numeric_attr(
        "price",
        type_name="SynthCourse",
        ontology_summary=TUITION_ONLY,
        money_family=True,
    )
    assert r2.confidence == "unique"
    assert r2.prop_key == "tuition_usd"


def test_anti_overfit_no_persona_gold_in_module():
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "infona_client/nlp/schema_valid_cypher.py"
    text = src.read_text(encoding="utf-8")
    for banned in ("Fall", "CourseOffering", "bookstore", "seats_remaining"):
        assert banned not in text


# ---------------------------------------------------------------------------
# Pipeline: invented hop fail-closed (no execute)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_invalid_hop_fail_closes_after_retries():
    """Filter-intent count with invented HAS_* never executes; low conf."""
    store = MemoryGraphStore()
    neptune = MagicMock()
    neptune.query = AsyncMock(side_effect=AssertionError("no sparql"))
    pipe = NLQueryPipeline(neptune, anthropic_key="test-key", graph_store=store)
    pipe._fetch_ontology = AsyncMock(return_value=SYNTH_OFFERING_ONTO)  # type: ignore[method-assign]

    calls: list[str] = []

    async def fake_llm(question, ontology, **kw):
        calls.append(kw.get("error_feedback") or "")
        return {
            "cypher": INVENTED_HAS_OFFERED_IN,
            "params": {
                "type_names": ["SynthOffering"],
                "target_name": "Zeta",
            },
            "explanation": "count via invented hop",
            "functions_needed": [],
        }

    pipe._try_llm_cypher = fake_llm  # type: ignore[method-assign]
    pipe._rephrase_via_openrouter = AsyncMock(return_value=None)  # type: ignore[method-assign]
    exec_mock = AsyncMock(side_effect=AssertionError("must not execute bad plan"))
    pipe._execute_confined_cypher = exec_mock  # type: ignore[method-assign]

    result = await pipe.ask(
        "how many SynthOfferings in Zeta",
        graph_uri=f"{IRI_BASE}/graphs/demo-tenant",
        instance_graph=f"{IRI_BASE}/graphs/demo-tenant/kg/syn-offerings",
        use_cypher=True,
    )

    assert len(calls) == 3  # max_attempts
    assert any("SCHEMA PREDICATE" in (fb or "") for fb in calls[1:])
    assert result.timing.get("schema_valid_cypher_reject") == 1.0
    assert result.query_confidence == "low"
    assert "Could not answer" in (result.answer or "")
    # Must not look like a successful zero-total.
    assert result.timing.get("rows") in (None, 0) or "rows" not in result.timing
    exec_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_pipeline_accepts_valid_offered_in_after_retry():
    """First gen invented hop → reject; second gen OFFERED_IN → execute."""
    store = MemoryGraphStore()
    neptune = MagicMock()
    neptune.query = AsyncMock(side_effect=AssertionError("no sparql"))
    pipe = NLQueryPipeline(neptune, anthropic_key="test-key", graph_store=store)
    pipe._fetch_ontology = AsyncMock(return_value=SYNTH_OFFERING_ONTO)  # type: ignore[method-assign]

    n = {"i": 0}

    async def fake_llm(question, ontology, **kw):
        n["i"] += 1
        if n["i"] == 1:
            return {
                "cypher": INVENTED_HAS_OFFERED_IN,
                "params": {
                    "type_names": ["SynthOffering"],
                    "target_name": "Zeta",
                },
                "explanation": "invented",
                "functions_needed": [],
            }
        return {
            "cypher": VALID_OFFERED_IN,
            "params": {
                "type_names": ["SynthOffering"],
                "target_name": "Zeta",
            },
            "explanation": "valid offered_in",
            "functions_needed": [],
        }

    pipe._try_llm_cypher = fake_llm  # type: ignore[method-assign]
    pipe._rephrase_via_openrouter = AsyncMock(return_value="")  # type: ignore[method-assign]
    pipe._execute_confined_cypher = AsyncMock(  # type: ignore[method-assign]
        return_value=([{"n": 3}], "freeform:mock")
    )

    result = await pipe.ask(
        "how many SynthOfferings in Zeta",
        graph_uri=f"{IRI_BASE}/graphs/demo-tenant",
        instance_graph=f"{IRI_BASE}/graphs/demo-tenant/kg/syn-offerings",
        use_cypher=True,
    )

    assert n["i"] == 2
    assert result.timing.get("schema_valid_cypher_reject") in (None, 0, 0.0)
    assert result.timing.get("schema_valid_cypher_retry") == 1.0
    assert result.query_confidence == "high"
    assert "Could not answer with confidence" not in (result.answer or "")
    pipe._execute_confined_cypher.assert_awaited()


def test_ontology_leaf_inventory_allowed_tokens():
    inv = OntologyLeafInventory.from_ontology(SYNTH_OFFERING_ONTO)
    assert not inv.empty
    assert "OFFERED_IN" in inv.allowed_rel_tokens
    assert "HAS_OFFERED_IN" not in inv.allowed_rel_tokens
    assert "INSTANCE_OF" in inv.allowed_rel_tokens
    assert "offered_in" in inv.allowed_prop_keys
    assert "seat_count" in inv.allowed_prop_keys

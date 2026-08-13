"""Semantic cost-attr resolve + numeric storage + grounded numeric plans.

Anti-overfit: synthetic Widget / Gadget types only — no warehouse/persona CSV
hardcodes as the sole success path. Product rule: /ask always uses the LLM;
numeric grounding is structured prompt context only.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from infona_client.graph.assertion_model import normalize_store_literal
from infona_client.graph.iri import IRI_BASE
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.rdfs_helpers import (
    LITERAL_COMPARE_CYPHER,
    TEMPLATE_LITERAL_COMPARE,
)
from infona_client.graph.scope import GraphScope
from infona_client.nlp.cypher_generate import (
    try_aggregate_query,
    try_deterministic_cypher,
    try_numeric_filter_query,
)
from infona_client.nlp.numeric_attr_resolve import (
    is_money_family_leaf,
    resolve_cost_prop,
    resolve_numeric_attr,
)
from infona_client.nlp.numeric_plan_grounding import (
    format_numeric_grounding_for_prompt,
    ground_numeric_plan,
)
from infona_client.nlp.ontology_mention_index import OntologyMentionIndex
from infona_client.nlp.pipeline import NLQueryPipeline
from infona_client.nlp.prompts import build_cypher_generation_prompt
from infona_client.resolver.profiler import strip_money_wrappers
from infona_client.resolver.validator import coerce_value


# ---------------------------------------------------------------------------
# Synthetic ontologies (anti-overfit)
# ---------------------------------------------------------------------------

UNIT_COST_ONLY = (
    "Type: Widget\n"
    "  - sku: string (literal)\n"
    "  - unit_cost: float (literal, key=unit_cost)\n"
)
PRICE_AND_TITLE = (
    "Type: Widget\n"
    "  - title: string (literal)\n"
    "  - price: float (literal, key=price)\n"
)
AMBIGUOUS_MONEY = (
    "Type: Widget\n"
    "  - list_price: float (literal, key=list_price)\n"
    "  - sale_price: float (literal, key=sale_price)\n"
    "  - sku: string (literal)\n"
)
LIST_PRICE_ONLY = (
    "Type: Gadget\n"
    "  - list_price: float (literal, key=list_price)\n"
    "  - name: string (literal)\n"
)


# ---------------------------------------------------------------------------
# 1. Semantic / family resolve
# ---------------------------------------------------------------------------


def test_price_nl_resolves_unit_cost_when_only_money_leaf():
    """NL 'price' + ontology only unit_cost → unit_cost (family heuristic)."""
    r = resolve_numeric_attr(
        "price",
        type_name="Widget",
        ontology_summary=UNIT_COST_ONLY,
        money_family=True,
    )
    assert r.confidence == "unique"
    assert r.prop_key == "unit_cost"

    got = resolve_cost_prop(UNIT_COST_ONLY, type_name="Widget", mention="price")
    assert got == "unit_cost"

    filt = try_numeric_filter_query(
        "Which widgets cost less than 15?",
        UNIT_COST_ONLY,
        type_names=["Widget"],
    )
    assert filt is not None
    assert filt["template"] == "literal_compare"
    assert filt["params"]["prop_key"] == "unit_cost"
    assert filt["params"]["op"] == "lt"
    assert filt["params"]["threshold"] == 15.0


def test_exact_price_leaf_wins_when_present():
    r = resolve_numeric_attr(
        "price",
        type_name="Widget",
        ontology_summary=PRICE_AND_TITLE,
        money_family=True,
    )
    assert r.confidence == "unique"
    assert r.prop_key == "price"

    filt = try_numeric_filter_query(
        "widgets cost less than 15",
        PRICE_AND_TITLE,
        type_names=["Widget"],
    )
    assert filt is not None
    assert filt["params"]["prop_key"] == "price"


def test_ambiguous_two_equal_money_leaves_fail_closed():
    """list_price + sale_price with NL 'price' → ambiguous, no silent pick."""
    r = resolve_numeric_attr(
        "price",
        type_name="Widget",
        ontology_summary=AMBIGUOUS_MONEY,
        money_family=True,
    )
    assert r.confidence == "ambiguous"
    assert r.prop_key is None
    assert len(r.candidates) >= 2

    filt = try_numeric_filter_query(
        "widgets cost less than 15",
        AMBIGUOUS_MONEY,
        type_names=["Widget"],
    )
    # Fail closed: fixture returns None rather than inventing a prop.
    assert filt is None

    plan = ground_numeric_plan(
        "widgets cost less than 15",
        AMBIGUOUS_MONEY,
        type_names=["Widget"],
    )
    assert plan is not None
    assert plan.confidence == "ambiguous"
    assert plan.prop_key is None


def test_aggregate_average_price_maps_to_unit_cost():
    agg = try_aggregate_query(
        "average price of widgets",
        UNIT_COST_ONLY,
        type_names=["Widget"],
    )
    assert agg is not None
    assert agg["template"] == "literal_aggregate"
    assert agg["params"]["prop_key"] == "unit_cost"
    assert agg["params"]["agg_op"] == "avg"

    plan = ground_numeric_plan(
        "average price of widgets",
        UNIT_COST_ONLY,
        type_names=["Widget"],
    )
    assert plan is not None
    assert plan.intent == "agg"
    assert plan.confidence == "unique"
    assert plan.prop_key == "unit_cost"
    assert plan.agg_op == "avg"


def test_list_price_family_and_camel_variants():
    assert is_money_family_leaf("unitCost")
    assert is_money_family_leaf("list_price")
    assert is_money_family_leaf("MSRP")
    assert not is_money_family_leaf("sku")
    assert not is_money_family_leaf("title")

    r = resolve_numeric_attr(
        "price",
        type_name="Gadget",
        ontology_summary=LIST_PRICE_ONLY,
        money_family=True,
    )
    assert r.confidence == "unique"
    assert r.prop_key == "list_price"


# ---------------------------------------------------------------------------
# 2. FakeEmbedder / mention-index synonym boost
# ---------------------------------------------------------------------------


def _norm(v: list[float]) -> list[float]:
    a = np.asarray(v, dtype=np.float64)
    n = float(np.linalg.norm(a))
    return (a / n).tolist() if n else v


class FakeEmbedder:
    def __init__(self, table: dict[str, list[float]]) -> None:
        self.table = {k.lower(): _norm(v) for k, v in table.items()}

    def _vec_for(self, text: str) -> list[float]:
        tl = text.lower()
        for prefix in ("attribute:", "relationship:", "entity type:"):
            if prefix in tl:
                after = tl.split(prefix, 1)[1].strip().splitlines()[0].strip()
                name = after.split()[0] if after else ""
                if name and name in self.table:
                    return self.table[name]
                for k, v in self.table.items():
                    if k.replace(" ", "_") == name or k.replace("_", " ") == name:
                        return v
        best_k, best_v = "", _norm([0.33, 0.33, 0.34])
        for k, v in self.table.items():
            if k in tl and len(k) >= len(best_k):
                best_k, best_v = k, v
        return best_v

    async def __call__(self, texts):
        return [self._vec_for(t) for t in texts]


@pytest.mark.asyncio
async def test_mention_index_synonym_boost_when_string_weak():
    """Semantic attr resolve prefers unit_cost when embeddings cluster with price."""
    # Ontology with a weakly-named leaf that family alone might under-score
    # if competing with non-money leaves — use semantic to boost.
    onto = (
        "Type: Widget\n"
        "  - sku: string (literal)\n"
        "  - qty: integer (literal)\n"
        "  - unit_cost: float (literal, key=unit_cost)\n"
    )
    V_PRICE = [1.0, 0.0, 0.0]
    V_QTY = [0.0, 1.0, 0.0]
    V_SKU = [0.0, 0.0, 1.0]
    fe = FakeEmbedder(
        {
            "price": V_PRICE,
            "unit_cost": V_PRICE,
            "unit cost": V_PRICE,
            "qty": V_QTY,
            "quantity": V_QTY,
            "sku": V_SKU,
        }
    )
    idx = OntologyMentionIndex()
    idx.upsert_attr("unit_cost", domain="Widget", datatype="float")
    idx.upsert_attr("qty", domain="Widget", datatype="integer")
    idx.upsert_attr("sku", domain="Widget", datatype="string")
    await idx.embed_missing(fe)
    assert idx.attrs_fully_embedded(["unit_cost", "qty", "sku"])

    q_emb = (await fe(["price"]))[0]
    r = resolve_numeric_attr(
        "price",
        type_name="Widget",
        ontology_summary=onto,
        mention_index=idx,
        query_embedding=q_emb,
        money_family=True,
    )
    assert r.confidence == "unique"
    assert r.prop_key == "unit_cost"
    # Semantic reason should appear when embeddings participate.
    assert any("semantic" in c.reasons for c in r.candidates if c.leaf == "unit_cost")


def test_partial_attr_index_does_not_invent_leaf():
    """Partial embed: missing leaf → no semantic; family heuristic still ok."""
    idx = OntologyMentionIndex()
    idx.upsert_attr("unit_cost", domain="Widget", datatype="float")
    # No embedding stored → attrs_fully_embedded is False
    assert not idx.attrs_fully_embedded(["unit_cost", "list_price"])
    r = resolve_numeric_attr(
        "price",
        type_name="Widget",
        ontology_summary=UNIT_COST_ONLY,
        mention_index=idx,
        query_embedding=[1.0, 0.0, 0.0],
        money_family=True,
    )
    # Family heuristic still resolves unit_cost; must not invent a missing leaf.
    assert r.prop_key == "unit_cost"
    assert r.confidence == "unique"


# ---------------------------------------------------------------------------
# 3. Numeric storage / typing
# ---------------------------------------------------------------------------


def test_strip_money_wrappers_and_coerce_float():
    assert strip_money_wrappers("$12.50") == "12.50"
    assert strip_money_wrappers("1,234.56") == "1234.56"
    assert strip_money_wrappers("USD 9.99") == "9.99"
    assert strip_money_wrappers("12.5") == "12.5"
    assert strip_money_wrappers("about twelve") is None
    assert strip_money_wrappers("SKU-42") is None

    assert coerce_value("$12.50", "float") == "12.5" or float(
        coerce_value("$12.50", "float") or "0"
    ) == 12.5
    assert coerce_value("not-a-price", "float") is None


def test_normalize_store_literal_typed_money():
    assert normalize_store_literal(
        "12.50^^http://www.w3.org/2001/XMLSchema#float"
    ) == 12.5
    # Currency in lexical + float dtype after coerce path
    assert (
        normalize_store_literal("$12.50^^http://www.w3.org/2001/XMLSchema#float")
        == 12.5
    )
    # Untyped free text stays string (do not invent float)
    assert normalize_store_literal("$12.50") == "$12.50"


def test_profiler_currency_column_is_number():
    from infona_client.resolver.profiler import _is_number, _value_shape
    from collections import Counter

    assert _is_number("$12.50")
    assert _is_number("9.99")
    assert not _is_number("free shipping")
    shape = _value_shape(Counter({"$10.00": 3, "$12.50": 2, "9.99": 1}), 6)
    from infona_client.resolver.models import ValueShape

    assert shape == ValueShape.NUMBER


@pytest.mark.asyncio
async def test_numeric_storage_compare_e2e_memory():
    """assert_fact stores numbers; literal_compare returns expected rows."""
    from infona_client.graph.rdf_model import AssertionFact, assert_fact

    store = MemoryGraphStore()
    scope = GraphScope.for_instance("demo-tenant", "widgets")
    session = store.session(scope)

    cheap = f"{IRI_BASE}/entities/Widget/cheap"
    dear = f"{IRI_BASE}/entities/Widget/dear"
    for eid, sku, cost in (
        (cheap, "W-cheap", 9.99),
        (dear, "W-dear", 19.99),
    ):
        await assert_fact(
            session, AssertionFact(subject_id=eid, kind="type", value="Widget")
        )
        await assert_fact(
            session,
            AssertionFact(
                subject_id=eid, kind="literal", property_leaf="sku", value=sku
            ),
        )
        await assert_fact(
            session,
            AssertionFact(
                subject_id=eid,
                kind="literal",
                property_leaf="unit_cost",
                value=cost,
            ),
        )

    rows = await session.execute_template(
        "literal_compare",
        {
            "type_names": ["Widget"],
            "prop_key": "unit_cost",
            "op": "lt",
            "threshold": 15.0,
            "limit": 25,
        },
    )
    skus = {r.get("title") or r.get("sku") or r.get("name") for r in rows}
    # Memory store may surface different display keys; check values.
    values = [float(r.get("value")) for r in rows if r.get("value") is not None]
    assert any(v < 15 for v in values)
    assert all(v < 15 for v in values)
    assert 9.99 in values
    assert 19.99 not in values


# ---------------------------------------------------------------------------
# 4. Grounded numeric plan → prompt + always-LLM
# ---------------------------------------------------------------------------


def test_ground_numeric_plan_unique_and_prompt():
    plan = ground_numeric_plan(
        "widgets cost less than 15",
        UNIT_COST_ONLY,
        type_names=["Widget"],
    )
    assert plan is not None
    assert plan.confidence == "unique"
    assert plan.prop_key == "unit_cost"
    assert plan.op == "lt"
    assert plan.threshold == 15.0
    assert plan.template == TEMPLATE_LITERAL_COMPARE

    text = format_numeric_grounding_for_prompt(plan)
    assert "unit_cost" in text
    assert "prop_key" in text
    assert "Numeric grounding" in text

    prompt = build_cypher_generation_prompt(
        "widgets cost less than 15",
        UNIT_COST_ONLY,
        tenant_id="t",
        kg_name="kg",
        grounding_text=text,
    )
    assert "unit_cost" in prompt
    assert "Numeric grounding" in prompt


@pytest.mark.asyncio
async def test_always_llm_spy_numeric_grounding_in_prompt(monkeypatch):
    """Product rule: /ask still calls _try_llm_cypher; grounding carries prop_key."""
    store = MemoryGraphStore()
    from infona_client.graph.rdf_model import AssertionFact, assert_fact

    scope = GraphScope.for_instance("demo-tenant", "widgets")
    session = store.session(scope)
    wid = f"{IRI_BASE}/entities/Widget/a1"
    await assert_fact(
        session, AssertionFact(subject_id=wid, kind="type", value="Widget")
    )
    await assert_fact(
        session,
        AssertionFact(
            subject_id=wid, kind="literal", property_leaf="unit_cost", value=9.5
        ),
    )

    neptune = MagicMock()
    neptune.query = AsyncMock(side_effect=AssertionError("SPARQL path must not run"))
    pipe = NLQueryPipeline(neptune, anthropic_key="", graph_store=store)
    pipe._fetch_ontology = AsyncMock(return_value=UNIT_COST_ONLY)  # type: ignore[method-assign]
    pipe._openrouter_key = "test-key"  # type: ignore[attr-defined]
    pipe._query_provider = "openrouter"  # type: ignore[attr-defined]
    pipe._query_model = "test-model"  # type: ignore[attr-defined]

    captured: dict = {}

    async def fake_llm(question: str, ontology: str, *, grounding_text: str = "", **kw):
        captured["question"] = question
        captured["grounding_text"] = grounding_text
        return {
            "cypher": LITERAL_COMPARE_CYPHER,
            "template": TEMPLATE_LITERAL_COMPARE,
            "params": {
                "type_names": ["Widget"],
                "prop_key": "unit_cost",
                "op": "lt",
                "threshold": 15.0,
                "limit": 25,
            },
            "explanation": "llm numeric compare",
            "functions_needed": [],
        }

    pipe._try_llm_cypher = fake_llm  # type: ignore[method-assign]

    det_calls: list = []
    from infona_client.nlp.cypher_generate import try_deterministic_cypher as real_det

    def spy_det(*a, **k):
        det_calls.append((a, k))
        return real_det(*a, **k)

    monkeypatch.setattr(
        "infona_client.nlp.cypher_generate.try_deterministic_cypher", spy_det
    )
    monkeypatch.setattr(
        "infona_client.nlp.pipeline.try_deterministic_cypher",
        spy_det,
        raising=False,
    )

    result = await pipe.ask(
        "widgets cost less than 15",
        graph_uri=f"{IRI_BASE}/graphs/demo-tenant",
        instance_graph=f"{IRI_BASE}/graphs/demo-tenant/kg/widgets",
        use_cypher=True,
    )

    assert not det_calls, "deterministic fixtures must not run on /ask"
    assert captured.get("question") == "widgets cost less than 15"
    gtext = captured.get("grounding_text") or ""
    assert "unit_cost" in gtext
    assert "Numeric grounding" in gtext or "prop_key" in gtext
    assert result.timing.get("query_language") == "cypher"
    assert result.timing.get("cypher_stub") == 0.0
    conf = result.timing.get("numeric_grounding_confidence")
    assert conf in ("unique", "ambiguous", "none") or conf is None


def test_deterministic_price_under_still_works_for_unit_tests():
    """Fixtures remain for unit tests of template builders (not /ask)."""
    p = try_deterministic_cypher(
        "widgets cost less than 15",
        UNIT_COST_ONLY,
        type_names=["Widget"],
    )
    assert p is not None
    assert p["params"]["prop_key"] == "unit_cost"


# ---------------------------------------------------------------------------
# 5. Live /ask ontology formats (Attributes: line — why #363 missed live)
# ---------------------------------------------------------------------------

# Production semantic-retrieval / _fetch_ontology shape (NOT dash-literal).
SEMANTIC_UNIT_COST_ONLY = (
    "Type: Widget — URI: <https://graph.infona.ai/types/Widget>\n"
    "  Attributes: description, sku, unit_cost\n"
    "  Relationships: supplied_by\n"
    "Type: Supplier — URI: <https://graph.infona.ai/types/Supplier>\n"
    "  Attributes: name\n"
)
# Full ontology fetch with URI annotations + bracket notes.
FULL_FETCH_UNIT_COST = (
    "Type: Part — URI: <https://graph.infona.ai/types/Part>\n"
    "  Attributes: sku (string) — URI: <https://graph.infona.ai/types/Part/attrs/sku>, "
    "unit_cost (float) — URI: <https://graph.infona.ai/types/Part/attrs/unit_cost> "
    "[no instances]\n"
)


def test_literal_leaves_parse_attributes_line_format():
    """Live /ask ontology is Attributes: csv, not '- leaf: float (literal)'."""
    from infona_client.nlp.numeric_attr_resolve import literal_leaves_for_type

    leaves = literal_leaves_for_type("Widget", SEMANTIC_UNIT_COST_ONLY)
    assert "unit_cost" in leaves
    assert "sku" in leaves
    # Relationships line must not pollute leaves.
    assert "supplied_by" not in leaves

    leaves_p = literal_leaves_for_type("Part", FULL_FETCH_UNIT_COST)
    assert leaves_p == ["sku", "unit_cost"] or set(leaves_p) >= {"sku", "unit_cost"}


def test_price_nl_resolves_unit_cost_on_semantic_ontology():
    """NL 'price' + semantic Attributes: unit_cost only → unique unit_cost."""
    r = resolve_numeric_attr(
        "price",
        type_name="Widget",
        ontology_summary=SEMANTIC_UNIT_COST_ONLY,
        money_family=True,
    )
    assert r.confidence == "unique"
    assert r.prop_key == "unit_cost"

    got = resolve_cost_prop(
        SEMANTIC_UNIT_COST_ONLY, type_name="Widget", mention="price"
    )
    assert got == "unit_cost"


def test_ground_numeric_plan_live_format_and_prompt():
    """Grounding injects unit_cost into the Cypher prompt for live ontology text."""
    for question in (
        "widgets with price under 10",
        "widgets with unit cost under 10",
        "which widgets cost less than 10",
        "price under 10",  # type-less; sole money type
        "unit cost under 10",
    ):
        plan = ground_numeric_plan(
            question,
            SEMANTIC_UNIT_COST_ONLY,
            type_names=["Widget", "Supplier"],
        )
        assert plan is not None, question
        assert plan.confidence == "unique", (question, plan.explanation)
        assert plan.prop_key == "unit_cost", question
        assert plan.subject_type == "Widget", question
        assert plan.op == "lt"
        assert plan.threshold == 10.0

    plan = ground_numeric_plan(
        "parts with unit cost under 10",
        FULL_FETCH_UNIT_COST,
        type_names=["Part"],
    )
    assert plan is not None
    assert plan.prop_key == "unit_cost"
    assert plan.mention in ("unit_cost", "unit cost")

    text = format_numeric_grounding_for_prompt(plan)
    assert "unit_cost" in text
    assert "MUST use" in text or "prop_key" in text

    prompt = build_cypher_generation_prompt(
        "parts with price under 10",
        FULL_FETCH_UNIT_COST,
        tenant_id="t",
        kg_name="parts_alpha",
        grounding_text=text,
    )
    assert "unit_cost" in prompt
    # Must not leave the model without the resolved leaf.
    assert "prop_key" in prompt


@pytest.mark.asyncio
async def test_always_llm_semantic_ontology_grounding_prop_key(monkeypatch):
    """Pipeline with production Attributes: ontology still injects unit_cost."""
    store = MemoryGraphStore()
    from infona_client.graph.rdf_model import AssertionFact, assert_fact

    scope = GraphScope.for_instance("demo-tenant", "gadgets")
    session = store.session(scope)
    eid = f"{IRI_BASE}/entities/Widget/w1"
    await assert_fact(
        session, AssertionFact(subject_id=eid, kind="type", value="Widget")
    )
    await assert_fact(
        session,
        AssertionFact(
            subject_id=eid, kind="literal", property_leaf="unit_cost", value=7.5
        ),
    )

    neptune = MagicMock()
    neptune.query = AsyncMock(side_effect=AssertionError("SPARQL path must not run"))
    pipe = NLQueryPipeline(neptune, anthropic_key="", graph_store=store)
    pipe._fetch_ontology = AsyncMock(return_value=SEMANTIC_UNIT_COST_ONLY)  # type: ignore[method-assign]
    pipe._openrouter_key = "test-key"  # type: ignore[attr-defined]
    pipe._query_provider = "openrouter"  # type: ignore[attr-defined]
    pipe._query_model = "test-model"  # type: ignore[attr-defined]

    captured: dict = {}

    async def fake_llm(question: str, ontology: str, *, grounding_text: str = "", **kw):
        captured["grounding_text"] = grounding_text
        captured["ontology"] = ontology
        # Simulate a well-behaved model that obeys grounded prop_key.
        return {
            "cypher": LITERAL_COMPARE_CYPHER,
            "template": TEMPLATE_LITERAL_COMPARE,
            "params": {
                "type_names": ["Widget"],
                "prop_key": "unit_cost",
                "op": "lt",
                "threshold": 10.0,
                "limit": 25,
            },
            "explanation": "filter Widget.unit_cost < 10",
            "functions_needed": [],
        }

    pipe._try_llm_cypher = fake_llm  # type: ignore[method-assign]

    result = await pipe.ask(
        "widgets with price under 10",
        graph_uri=f"{IRI_BASE}/graphs/demo-tenant",
        instance_graph=f"{IRI_BASE}/graphs/demo-tenant/kg/gadgets",
        use_cypher=True,
    )

    gtext = captured.get("grounding_text") or ""
    assert "unit_cost" in gtext, gtext
    assert "prop_key" in gtext
    assert result.timing.get("numeric_grounding_prop") == "unit_cost" or (
        result.timing.get("numeric_grounding_confidence") == "unique"
    )
    # Always-LLM: no fixture short-circuit flag.
    assert result.timing.get("cypher_stub") == 0.0


def test_multi_word_unit_cost_fixture_and_mention():
    """'unit cost' (space) binds unit_cost, not invented price."""
    filt = try_numeric_filter_query(
        "widgets with unit cost under 10",
        UNIT_COST_ONLY,
        type_names=["Widget"],
    )
    assert filt is not None
    assert filt["params"]["prop_key"] == "unit_cost"

    filt_sem = try_numeric_filter_query(
        "widgets with unit cost under 10",
        SEMANTIC_UNIT_COST_ONLY,
        type_names=["Widget"],
    )
    assert filt_sem is not None
    assert filt_sem["params"]["prop_key"] == "unit_cost"

    plan = ground_numeric_plan(
        "parts where unit_cost is less than 10",
        FULL_FETCH_UNIT_COST,
        type_names=["Part"],
    )
    assert plan is not None
    assert plan.prop_key == "unit_cost"
    assert plan.mention == "unit_cost"  # not unit_cost_is

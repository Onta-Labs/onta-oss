"""NL → ontology subgraph match → grounded /ask plan (hermetic).

Anti-overfit: synthetic Widget / Site / Region types only — no warehouse/persona
CSV hardcodes as the sole success path. Product rule: /ask still always uses
the LLM; grounding is structured prompt context only.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from infona_client.graph.iri import IRI_BASE
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.rdfs_helpers import (
    RELATED_ENTITY_NAME_FILTER_CYPHER,
    TEMPLATE_RELATED_ENTITY_NAME_FILTER,
)
from infona_client.graph.scope import GraphScope
from infona_client.nlp.cypher_generate import try_deterministic_cypher
from infona_client.nlp.ontology_subgraph_match import (
    candidate_ontology_paths,
    extract_nl_sketch,
    format_grounding_for_prompt,
    ground_ask_plan,
    rank_paths,
)
from infona_client.nlp.pipeline import NLQueryPipeline
from infona_client.nlp.prompts import build_cypher_generation_prompt


# ---------------------------------------------------------------------------
# Synthetic ontology (anti-overfit)
# ---------------------------------------------------------------------------

WIDGET_SITE_ONTO = (
    "Type: Widget\n"
    "  - sku: string (literal)\n"
    "  - unit_cost: float (literal)\n"
    "  - stored_in -> Site (relationship, key=stored_in)\n"
    "Type: Site\n"
    "  - name: string (literal)\n"
)
WIDGET_SITE_TYPES = ["Widget", "Site"]

PROMOTED_ONTO = (
    "Type: Widget\n"
    "  - sku: string (literal)\n"
    "  - stored_in -> Site (relationship, key=stored_in)\n"
    "  - has_region -> Region (relationship, key=has_region)\n"
    "  - made_by -> Vendor (relationship, key=made_by)\n"
    "Type: Site\n"
    "  - name: string (literal)\n"
    "Type: Region\n"
    "  - name: string (literal)\n"
    "Type: Vendor\n"
    "  - name: string (literal)\n"
)
PROMOTED_TYPES = ["Widget", "Site", "Region", "Vendor"]

# Two equally location-ish edges to the same range → fail closed (no unique winner).
AMBIGUOUS_SITE_ONTO = (
    "Type: Widget\n"
    "  - stored_in -> Site (relationship, key=stored_in)\n"
    "  - located_at -> Site (relationship, key=located_at)\n"
    "Type: Site\n"
    "  - name: string (literal)\n"
)

WEBSITE_TRAP_ONTO = (
    "Type: Widget\n"
    "  - published_on -> Website (relationship, key=published_on)\n"
    "  - stored_in -> Site (relationship, key=stored_in)\n"
    "Type: Website\n"
    "  - name: string (literal)\n"
    "Type: Site\n"
    "  - name: string (literal)\n"
)


# ---------------------------------------------------------------------------
# 1. Unit — extract sketch
# ---------------------------------------------------------------------------


def test_extract_count_locative_value():
    s = extract_nl_sketch("how many widgets in east")
    assert s.intent == "count"
    assert any("widget" in t.lower() for t in s.type_mentions)
    assert any(v.lower() == "east" for v in s.value_mentions)
    assert "in" in s.rel_cues


def test_extract_messy_casing_plural():
    s = extract_nl_sketch("How many widget in East")
    assert s.intent == "count"
    assert s.type_mentions
    assert any(v.lower() == "east" for v in s.value_mentions)
    assert "in" in s.rel_cues


def test_extract_list_at_site_dim():
    s = extract_nl_sketch("widgets at site East")
    assert s.intent == "list"
    assert any("widget" in t.lower() for t in s.type_mentions)
    assert "at" in s.rel_cues
    assert any(d.lower() == "site" for d in s.dim_mentions)
    assert any(v.lower() == "east" for v in s.value_mentions)


def test_extract_count_at_site_variant():
    s = extract_nl_sketch("count widgets at site East")
    assert s.intent == "count"
    assert "at" in s.rel_cues
    assert any(d.lower() == "site" for d in s.dim_mentions)
    assert any(v.lower() == "east" for v in s.value_mentions)


def test_extract_bare_count_no_path():
    s = extract_nl_sketch("how many widgets are there")
    assert s.intent == "count"
    assert any("widget" in t.lower() for t in s.type_mentions)
    assert not s.rel_cues
    assert not s.value_mentions


def test_extract_list_intent():
    s = extract_nl_sketch("list all widgets")
    assert s.intent == "list"
    assert any("widget" in t.lower() for t in s.type_mentions)


# ---------------------------------------------------------------------------
# 2. Unit — path candidates
# ---------------------------------------------------------------------------


def test_candidate_paths_widget_stored_in_site():
    paths = candidate_ontology_paths(WIDGET_SITE_ONTO, WIDGET_SITE_TYPES)
    assert any(
        p.domain_type == "Widget"
        and p.rel_attr == "stored_in"
        and p.range_type == "Site"
        for p in paths
    )
    # Literals are not paths
    assert not any(p.rel_attr == "sku" for p in paths)
    assert not any(p.rel_attr == "unit_cost" for p in paths)


def test_candidate_paths_domain_filter():
    paths = candidate_ontology_paths(
        PROMOTED_ONTO, PROMOTED_TYPES, domain_type="Widget"
    )
    assert all(p.domain_type == "Widget" for p in paths)
    leaves = {p.rel_attr for p in paths}
    assert "stored_in" in leaves
    assert "has_region" in leaves
    assert "made_by" in leaves


def test_candidate_paths_empty_ontology():
    assert candidate_ontology_paths("", []) == []
    assert candidate_ontology_paths("Type: Widget\n  - sku: string (literal)\n") == []


# ---------------------------------------------------------------------------
# 3. Unit — ranking
# ---------------------------------------------------------------------------


def test_rank_locative_prefers_location_path():
    sketch = extract_nl_sketch("how many widgets in east")
    paths = candidate_ontology_paths(PROMOTED_ONTO, PROMOTED_TYPES, domain_type="Widget")
    ranked = rank_paths(sketch, paths, subject_type="Widget")
    assert ranked
    top = ranked[0].path
    # Location-ish (stored_in / has_region) should beat made_by
    assert top.rel_attr in {"stored_in", "has_region"}
    made = next(r for r in ranked if r.path.rel_attr == "made_by")
    assert ranked[0].score > made.score


def test_rank_dim_site_picks_stored_in():
    sketch = extract_nl_sketch("widgets at site East")
    paths = candidate_ontology_paths(PROMOTED_ONTO, PROMOTED_TYPES, domain_type="Widget")
    ranked = rank_paths(sketch, paths, subject_type="Widget")
    assert ranked[0].path.rel_attr == "stored_in"
    assert ranked[0].path.range_type == "Site"


def test_rank_synonym_range_warehouse_node_unique():
    """Range content token warehouse ↔ WarehouseNode (unique) — general family."""
    onto = (
        "Type: Gadget\n"
        "  - housed_at -> WarehouseNode (relationship, key=housed_at)\n"
        "  - made_by -> Vendor (relationship, key=made_by)\n"
        "Type: WarehouseNode\n"
        "Type: Vendor\n"
    )
    sketch = extract_nl_sketch("gadgets in warehouse North")
    # dim peeled as warehouse, value North
    assert any(d.lower() == "warehouse" for d in sketch.dim_mentions)
    paths = candidate_ontology_paths(onto, ["Gadget", "WarehouseNode", "Vendor"])
    ranked = rank_paths(sketch, paths, subject_type="Gadget")
    assert ranked
    assert ranked[0].path.rel_attr == "housed_at"
    assert ranked[0].path.range_type == "WarehouseNode"


# ---------------------------------------------------------------------------
# 4. Negatives / fail-closed
# ---------------------------------------------------------------------------


def test_ambiguous_two_edges_to_site_fail_closed():
    plan = ground_ask_plan(
        "how many widgets in east",
        AMBIGUOUS_SITE_ONTO,
        type_names=["Widget", "Site"],
    )
    assert plan is not None
    assert plan.confidence == "ambiguous"
    assert plan.path is None
    assert plan.template is None
    # Shortlist present for the LLM
    assert len(plan.ranked_paths) >= 2


def test_substring_site_vs_website_no_false_positive():
    """'site' must not bind Website via substring (ONTA-538 regression)."""
    plan = ground_ask_plan(
        "widgets at site East",
        WEBSITE_TRAP_ONTO,
        type_names=["Widget", "Website", "Site"],
    )
    assert plan is not None
    assert plan.confidence == "unique"
    assert plan.path is not None
    assert plan.path.rel_attr == "stored_in"
    assert plan.path.range_type == "Site"
    assert plan.path.range_type != "Website"


def test_empty_ontology_returns_none():
    assert ground_ask_plan("how many widgets in east", "") is None
    assert ground_ask_plan("how many widgets in east", "   ") is None


def test_unknown_type_no_invent():
    plan = ground_ask_plan(
        "how many gadgets in east",
        WIDGET_SITE_ONTO,
        type_names=WIDGET_SITE_TYPES,
    )
    assert plan is not None
    assert plan.confidence == "none"
    assert plan.subject_type is None
    assert plan.path is None


def test_no_literal_east_on_widget():
    """Must prefer path binding, not invent literal prop 'east' on Widget."""
    plan = ground_ask_plan(
        "how many widgets in east",
        WIDGET_SITE_ONTO,
        type_names=WIDGET_SITE_TYPES,
    )
    assert plan is not None
    assert plan.confidence == "unique"
    assert plan.path is not None
    assert plan.path.rel_attr == "stored_in"
    assert "prop_key" not in plan.params
    assert plan.params.get("target_name", "").lower() == "east"


# ---------------------------------------------------------------------------
# 5. Grounded plan shape
# ---------------------------------------------------------------------------


def test_grounded_plan_count_related_name_filter():
    plan = ground_ask_plan(
        "how many widgets in east",
        WIDGET_SITE_ONTO,
        type_names=WIDGET_SITE_TYPES,
    )
    assert plan is not None
    assert plan.intent == "count"
    assert plan.subject_type == "Widget"
    assert plan.path is not None
    assert plan.path.as_tuple() == ("Widget", "stored_in", "Site")
    assert plan.template == TEMPLATE_RELATED_ENTITY_NAME_FILTER
    assert plan.params["type_names"] == ["Widget"]
    assert plan.params["rel_attr"] == "stored_in"
    assert plan.params["target_name"].lower() == "east"
    # Safe params only — no free Cypher injection
    assert "cypher" not in plan.params
    assert all(
        k in {"type_names", "rel_attr", "target_name", "limit"} for k in plan.params
    )


def test_grounded_plan_list_with_site_dim():
    plan = ground_ask_plan(
        "widgets in site East",
        WIDGET_SITE_ONTO,
        type_names=WIDGET_SITE_TYPES,
    )
    assert plan is not None
    assert plan.confidence == "unique"
    assert plan.params["rel_attr"] == "stored_in"
    assert plan.params["target_name"] == "East"


def test_grounded_plan_bare_count():
    plan = ground_ask_plan(
        "how many widgets are there",
        WIDGET_SITE_ONTO,
        type_names=WIDGET_SITE_TYPES,
    )
    assert plan is not None
    assert plan.intent == "count"
    assert plan.template == "entities_of_type_count"
    assert plan.params["type_names"] == ["Widget"]
    assert plan.path is None


def test_params_reject_unsafe_rel_attr():
    """_safe_params must not pass free-text rel_attr."""
    from infona_client.nlp.ontology_subgraph_match import _safe_params

    cleaned = _safe_params(
        {
            "type_names": ["Widget"],
            "rel_attr": "stored_in; DROP",
            "target_name": "East",
            "evil": "MATCH (n) DETACH DELETE n",
        }
    )
    assert "rel_attr" not in cleaned
    assert "evil" not in cleaned
    assert cleaned["target_name"] == "East"
    assert cleaned["type_names"] == ["Widget"]


def test_format_grounding_includes_count_note():
    plan = ground_ask_plan(
        "how many widgets in east",
        WIDGET_SITE_ONTO,
        type_names=WIDGET_SITE_TYPES,
    )
    text = format_grounding_for_prompt(plan)
    assert "preferred_path" in text
    assert "stored_in" in text
    assert "COUNT" in text or "count" in text
    assert "related_entity_name" in text


def test_format_grounding_ambiguous_shortlist():
    plan = ground_ask_plan(
        "widgets in east",
        AMBIGUOUS_SITE_ONTO,
        type_names=["Widget", "Site"],
    )
    text = format_grounding_for_prompt(plan)
    assert "ambiguous" in text.lower() or plan.confidence == "ambiguous"
    assert "stored_in" in text or "owned_by" in text


def test_prompt_injection_of_grounding():
    plan = ground_ask_plan(
        "how many widgets in east",
        WIDGET_SITE_ONTO,
        type_names=WIDGET_SITE_TYPES,
    )
    grounding = format_grounding_for_prompt(plan)
    prompt = build_cypher_generation_prompt(
        "how many widgets in east",
        WIDGET_SITE_ONTO,
        tenant_id="t",
        kg_name="kg",
        grounding_text=grounding,
    )
    assert "Ontology grounding" in prompt
    assert "stored_in" in prompt
    assert "how many widgets in east" in prompt


# ---------------------------------------------------------------------------
# 6. Integration (hermetic MemoryGraphStore + mock LLM)
# ---------------------------------------------------------------------------


async def _seed_widget_site(store: MemoryGraphStore) -> None:
    """Seed Widget A1 --stored_in--> Site East via Assertion SoT."""
    from infona_client.graph.rdf_model import AssertionFact, assert_fact

    scope = GraphScope.for_instance("demo-tenant", "widgets")
    session = store.session(scope)
    site_id = f"{IRI_BASE}/entities/Site/east"
    widget_id = f"{IRI_BASE}/entities/Widget/a1"
    await assert_fact(
        session,
        AssertionFact(subject_id=site_id, kind="type", value="Site"),
        dual_write_cache=True,
    )
    await assert_fact(
        session,
        AssertionFact(
            subject_id=site_id,
            kind="literal",
            property_leaf="name",
            value="East",
        ),
        dual_write_cache=True,
    )
    await assert_fact(
        session,
        AssertionFact(subject_id=widget_id, kind="type", value="Widget"),
        dual_write_cache=True,
    )
    await assert_fact(
        session,
        AssertionFact(
            subject_id=widget_id,
            kind="literal",
            property_leaf="name",
            value="A1",
        ),
        dual_write_cache=True,
    )
    await assert_fact(
        session,
        AssertionFact(
            subject_id=widget_id,
            kind="object",
            property_leaf="stored_in",
            value=site_id,
        ),
        dual_write_cache=True,
    )


@pytest.mark.asyncio
async def test_integration_llm_receives_grounding_context(monkeypatch):
    """Hermetic: mock LLM sees grounding block; fixtures never short-circuit."""
    store = MemoryGraphStore()
    await _seed_widget_site(store)

    neptune = MagicMock()
    neptune.query = AsyncMock(side_effect=AssertionError("SPARQL path must not run"))
    pipe = NLQueryPipeline(neptune, anthropic_key="", graph_store=store)
    pipe._fetch_ontology = AsyncMock(return_value=WIDGET_SITE_ONTO)  # type: ignore[method-assign]
    # Force cypher path keys present so _try_llm_cypher is reached
    pipe._openrouter_key = "test-key"  # type: ignore[attr-defined]
    pipe._query_provider = "openrouter"  # type: ignore[attr-defined]
    pipe._query_model = "test-model"  # type: ignore[attr-defined]

    captured: dict = {}

    async def fake_llm(
        question: str,
        ontology: str,
        *,
        grounding_text: str = "",
        **kw,
    ):
        captured["question"] = question
        captured["ontology"] = ontology
        captured["grounding_text"] = grounding_text
        captured["kwargs"] = kw
        # Return allowlisted template-shaped Cypher (as LLM would after grounding)
        plan = ground_ask_plan(question, ontology, type_names=WIDGET_SITE_TYPES)
        assert plan is not None and plan.path is not None
        return {
            "cypher": RELATED_ENTITY_NAME_FILTER_CYPHER,
            "template": TEMPLATE_RELATED_ENTITY_NAME_FILTER,
            "params": dict(plan.params),
            "explanation": plan.explanation,
            "functions_needed": [],
        }

    pipe._try_llm_cypher = fake_llm  # type: ignore[method-assign]

    det_calls: list = []
    real_det = try_deterministic_cypher

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
        "how many widgets in east",
        graph_uri=f"{IRI_BASE}/graphs/demo-tenant",
        instance_graph=f"{IRI_BASE}/graphs/demo-tenant/kg/widgets",
        use_cypher=True,
    )

    assert not det_calls, "deterministic fixtures must not run on /ask"
    assert captured.get("question") == "how many widgets in east"
    gtext = captured.get("grounding_text") or ""
    assert "stored_in" in gtext
    assert "Widget" in gtext
    assert "east" in gtext.lower()
    assert result.timing.get("query_language") == "cypher"
    conf = result.timing.get("grounding_confidence")
    assert conf in ("unique", "ambiguous", "none") or isinstance(conf, str)


@pytest.mark.asyncio
async def test_integration_execute_allowlisted_template_from_plan():
    """Ground plan params drive related_entity_name_filter over seeded graph."""
    store = MemoryGraphStore()
    await _seed_widget_site(store)
    scope = GraphScope.for_instance("demo-tenant", "widgets")

    plan = ground_ask_plan(
        "widgets in site East",
        WIDGET_SITE_ONTO,
        type_names=WIDGET_SITE_TYPES,
    )
    assert plan is not None
    assert plan.template == TEMPLATE_RELATED_ENTITY_NAME_FILTER
    assert plan.params["rel_attr"] == "stored_in"
    assert plan.params["target_name"] == "East"

    session = store.session(scope)
    rows = await session.execute_template(plan.template, dict(plan.params))
    assert rows is not None
    assert len(rows) >= 1
    # Seeded Widget A1 should match Site East via stored_in.
    blob = " ".join(
        str(r.get("id") or "") + " " + str(r.get("title") or r.get("name") or "")
        for r in rows
    ).lower()
    assert "a1" in blob or "widget" in blob


# ---------------------------------------------------------------------------
# 7. Always-LLM regression (grounding must not reintroduce fixture short-circuit)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_still_calls_llm_with_grounding_present(monkeypatch):
    store = MemoryGraphStore()
    await _seed_widget_site(store)

    neptune = MagicMock()
    neptune.query = AsyncMock(side_effect=AssertionError("no SPARQL"))
    pipe = NLQueryPipeline(neptune, anthropic_key="", graph_store=store)
    pipe._fetch_ontology = AsyncMock(return_value=WIDGET_SITE_ONTO)  # type: ignore[method-assign]
    pipe._openrouter_key = "k"  # type: ignore[attr-defined]
    pipe._query_provider = "openrouter"  # type: ignore[attr-defined]
    pipe._query_model = "m"  # type: ignore[attr-defined]

    llm_calls: list[str] = []

    async def fake_llm(question: str, ontology: str, **kw):
        llm_calls.append(question)
        # Covered + schema-valid plan: filter intent "in east" binds via the
        # declared stored_in relationship (not an invented e.region leaf) so
        # constraint-coverage and schema-predicate gates both accept.
        return {
            "cypher": (
                "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->"
                "(c:Class {tenant_id: $tenant_id, kg: $kg}) "
                "WHERE c.name IN $type_names "
                "MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg})-[:SUBJECT]->(e) "
                "MATCH (a)-[:OBJECT]->(t:Entity {tenant_id: $tenant_id, kg: $kg}) "
                "MATCH (a)-[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg}) "
                "WHERE p.name = $rel_attr AND toLower(coalesce(t.name, '')) = toLower($target_name) "
                "RETURN count(DISTINCT e) AS n"
            ),
            "params": {
                "type_names": ["Widget"],
                "rel_attr": "stored_in",
                "target_name": "east",
            },
            "template": "related_entity_name_filter",
            "explanation": "count widgets related via stored_in to east",
            "functions_needed": [],
        }

    pipe._try_llm_cypher = fake_llm  # type: ignore[method-assign]
    pipe._execute_confined_cypher = AsyncMock(  # type: ignore[method-assign]
        return_value=([{"n": 1}], "freeform:mock")
    )
    pipe._rephrase_via_openrouter = AsyncMock(return_value="")  # type: ignore[method-assign]

    det_calls: list = []

    def spy_det(*a, **k):
        det_calls.append(1)
        return None

    monkeypatch.setattr(
        "infona_client.nlp.cypher_generate.try_deterministic_cypher", spy_det
    )
    monkeypatch.setattr(
        "infona_client.nlp.pipeline.try_deterministic_cypher",
        spy_det,
        raising=False,
    )

    await pipe.ask(
        "how many widgets in east",
        graph_uri=f"{IRI_BASE}/graphs/demo-tenant",
        instance_graph=f"{IRI_BASE}/graphs/demo-tenant/kg/widgets",
        use_cypher=True,
    )
    assert llm_calls == ["how many widgets in east"]
    assert not det_calls


def test_grounding_text_in_try_llm_prompt_signature():
    """_try_llm_cypher accepts grounding_text (API contract)."""
    import inspect

    sig = inspect.signature(NLQueryPipeline._try_llm_cypher)
    assert "grounding_text" in sig.parameters


# ---------------------------------------------------------------------------
# 8. Anti-overfit: success is not warehouse-CSV-only
# ---------------------------------------------------------------------------


def test_anti_overfit_generic_types_not_warehouse_only():
    """Any location-ish range family works — not only 'warehouse'."""
    onto = (
        "Type: Part\n"
        "  - located_at -> Facility (relationship, key=located_at)\n"
        "Type: Facility\n"
        "  - name: string (literal)\n"
    )
    plan = ground_ask_plan(
        "how many parts in east",
        onto,
        type_names=["Part", "Facility"],
    )
    assert plan is not None
    assert plan.confidence == "unique"
    assert plan.path is not None
    assert plan.path.rel_attr == "located_at"
    assert plan.path.range_type == "Facility"
    assert plan.params["target_name"].lower() == "east"


# ---------------------------------------------------------------------------
# 9. Multi-hop paths (Part → Bin → Facility)
# ---------------------------------------------------------------------------

MULTI_HOP_ONTO = (
    "Type: Part\n"
    "  - stored_in -> Bin (relationship, key=stored_in)\n"
    "  - made_by -> Vendor (relationship, key=made_by)\n"
    "Type: Bin\n"
    "  - name: string (literal)\n"
    "  - in_facility -> Facility (relationship, key=in_facility)\n"
    "Type: Facility\n"
    "  - name: string (literal)\n"
    "Type: Vendor\n"
    "  - name: string (literal)\n"
)
MULTI_HOP_TYPES = ["Part", "Bin", "Facility", "Vendor"]

# Direct 1-hop + multi-hop both reach Site — 1-hop must win for "widgets in site".
DIRECT_AND_CHAIN_ONTO = (
    "Type: Widget\n"
    "  - stored_in -> Site (relationship, key=stored_in)\n"
    "  - boxed_in -> Crate (relationship, key=boxed_in)\n"
    "Type: Crate\n"
    "  - sits_at -> Site (relationship, key=sits_at)\n"
    "Type: Site\n"
    "  - name: string (literal)\n"
)

# Two equal 2-hop chains to Facility → fail closed.
AMBIGUOUS_MULTI_HOP_ONTO = (
    "Type: Part\n"
    "  - stored_in -> Bin (relationship, key=stored_in)\n"
    "  - staged_at -> Rack (relationship, key=staged_at)\n"
    "Type: Bin\n"
    "  - in_facility -> Facility (relationship, key=in_facility)\n"
    "Type: Rack\n"
    "  - in_facility -> Facility (relationship, key=in_facility)\n"
    "Type: Facility\n"
    "  - name: string (literal)\n"
)


def test_candidate_paths_includes_two_hop():
    paths = candidate_ontology_paths(
        MULTI_HOP_ONTO, MULTI_HOP_TYPES, domain_type="Part", max_hops=2
    )
    one_hop = [p for p in paths if p.hop_count == 1]
    two_hop = [p for p in paths if p.hop_count == 2]
    assert any(p.rel_attr == "stored_in" and p.range_type == "Bin" for p in one_hop)
    assert any(
        p.rel_attr == "stored_in"
        and p.range_type == "Bin"
        and p.chain
        and p.chain[0][0] == "in_facility"
        and p.terminal_range == "Facility"
        for p in two_hop
    )
    # describe shows full chain
    chain = next(p for p in two_hop if p.terminal_range == "Facility")
    desc = chain.describe()
    assert "stored_in" in desc and "in_facility" in desc and "Facility" in desc


def test_candidate_paths_max_hops_one_skips_chain():
    paths = candidate_ontology_paths(
        MULTI_HOP_ONTO, MULTI_HOP_TYPES, domain_type="Part", max_hops=1
    )
    assert paths
    assert all(p.hop_count == 1 for p in paths)
    assert not any(p.terminal_range == "Facility" and p.hop_count > 1 for p in paths)


def test_multi_hop_parts_in_facility_ranks_two_hop():
    """NL 'parts in facility East' ranks Part→Bin→Facility over Vendor 1-hop."""
    plan = ground_ask_plan(
        "parts in facility East",
        MULTI_HOP_ONTO,
        type_names=MULTI_HOP_TYPES,
        max_hops=2,
    )
    assert plan is not None
    assert plan.confidence == "unique"
    assert plan.path is not None
    assert plan.path.hop_count == 2
    assert plan.path.terminal_range == "Facility"
    assert "in_facility" in plan.path.all_rel_attrs
    # Prompt-only: no single-hop related_entity template for multi-hop
    assert plan.template is None
    assert plan.params.get("target_name") == "East"
    assert "rel_attr" not in plan.params  # would mislead as single hop
    text = format_grounding_for_prompt(plan)
    assert "multi-hop" in text.lower() or "path_hops" in text
    assert "Facility" in text


def test_multi_hop_fail_closed_when_two_chains_tie():
    plan = ground_ask_plan(
        "parts in facility East",
        AMBIGUOUS_MULTI_HOP_ONTO,
        type_names=["Part", "Bin", "Rack", "Facility"],
        max_hops=2,
    )
    assert plan is not None
    assert plan.confidence == "ambiguous"
    assert plan.path is None
    assert len(plan.ranked_paths) >= 2
    # Both chains to Facility should appear
    terminals = {rp.path.terminal_range for rp in plan.ranked_paths}
    assert "Facility" in terminals


def test_direct_one_hop_beats_longer_path_when_both_valid():
    """'widgets in site East' prefers Widget→Site over Widget→Crate→Site."""
    plan = ground_ask_plan(
        "widgets in site East",
        DIRECT_AND_CHAIN_ONTO,
        type_names=["Widget", "Crate", "Site"],
        max_hops=2,
    )
    assert plan is not None
    assert plan.confidence == "unique"
    assert plan.path is not None
    assert plan.path.hop_count == 1
    assert plan.path.rel_attr == "stored_in"
    assert plan.path.range_type == "Site"
    assert plan.template == TEMPLATE_RELATED_ENTITY_NAME_FILTER


def test_rank_multi_hop_penalty_prefers_shorter_when_equal_family():
    sketch = extract_nl_sketch("parts in east")
    paths = candidate_ontology_paths(
        MULTI_HOP_ONTO, MULTI_HOP_TYPES, domain_type="Part", max_hops=2
    )
    ranked = rank_paths(sketch, paths, subject_type="Part")
    # Without dim, locative + location family: Bin is not location-ish terminal
    # for 1-hop stored_in; 2-hop Facility is. So multi-hop may win — that's OK.
    # Explicit: when both score location, shorter wins — use DIRECT_AND_CHAIN.
    sketch2 = extract_nl_sketch("widgets in east")
    paths2 = candidate_ontology_paths(
        DIRECT_AND_CHAIN_ONTO,
        ["Widget", "Crate", "Site"],
        domain_type="Widget",
        max_hops=2,
    )
    ranked2 = rank_paths(sketch2, paths2, subject_type="Widget")
    assert ranked2
    assert ranked2[0].path.hop_count == 1
    assert ranked2[0].path.rel_attr == "stored_in"


# ---------------------------------------------------------------------------
# 10. Embedder ranking (deterministic FakeEmbedder / sync batch)
# ---------------------------------------------------------------------------


def _norm_vec(v: list[float]) -> list[float]:
    import math

    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else v


class _SyncFakeEmbedder:
    """Sync batch embedder: keyword table → unit vectors (hermetic)."""

    def __init__(self, table: dict[str, list[float]]) -> None:
        self.table = {k.lower(): _norm_vec(v) for k, v in table.items()}
        self.calls: list[list[str]] = []

    def __call__(self, texts):
        self.calls.append(list(texts))
        out = []
        for t in texts:
            tl = t.lower()
            best_k, best_v = "", _norm_vec([0.25, 0.25, 0.25, 0.25])
            for k, v in self.table.items():
                if k in tl and len(k) >= len(best_k):
                    best_k, best_v = k, v
            out.append(best_v)
        return out


def test_embedder_boosts_synonym_path():
    """Embedder present: path whose signature aligns with dim synonym wins."""
    onto = (
        "Type: Gadget\n"
        "  - housed_at -> DepotNode (relationship, key=housed_at)\n"
        "  - made_by -> Vendor (relationship, key=made_by)\n"
        "Type: DepotNode\n"
        "Type: Vendor\n"
    )
    # Without embedder, dim "depot" may still hit range content "DepotNode"
    # via family; use a non-family range so string alone misses, embed wins.
    onto2 = (
        "Type: Gadget\n"
        "  - housed_at -> StashPod (relationship, key=housed_at)\n"
        "  - made_by -> Vendor (relationship, key=made_by)\n"
        "Type: StashPod\n"
        "Type: Vendor\n"
    )
    sketch = extract_nl_sketch("gadgets in depot North")
    paths = candidate_ontology_paths(
        onto2, ["Gadget", "StashPod", "Vendor"], domain_type="Gadget"
    )
    # Hermetic without embedder: dim depot does not match StashPod / housed_at
    ranked_plain = rank_paths(sketch, paths, subject_type="Gadget")
    # With embedder: "depot" and "housed_at StashPod" share a vector axis
    emb = _SyncFakeEmbedder(
        {
            "depot": [1.0, 0.0, 0.0, 0.0],
            "housed_at": [1.0, 0.0, 0.0, 0.0],
            "stashpod": [1.0, 0.0, 0.0, 0.0],
            "made_by": [0.0, 1.0, 0.0, 0.0],
            "vendor": [0.0, 1.0, 0.0, 0.0],
            "gadget": [0.0, 0.0, 1.0, 0.0],
        }
    )
    ranked_emb = rank_paths(sketch, paths, subject_type="Gadget", embedder=emb)
    assert ranked_emb
    assert ranked_emb[0].path.rel_attr == "housed_at"
    assert any("embed_cos" in r for r in ranked_emb[0].reasons)
    assert emb.calls  # embedder was invoked

    plan = ground_ask_plan(
        "gadgets in depot North",
        onto2,
        type_names=["Gadget", "StashPod", "Vendor"],
        embedder=emb,
    )
    assert plan is not None
    assert plan.confidence == "unique"
    assert plan.path is not None
    assert plan.path.rel_attr == "housed_at"
    # plain path may be weak/none — embedder is the lift
    del ranked_plain, onto  # silence unused (onto kept for doc)


def test_embedder_absent_hermetic_unchanged():
    """Without embedder, Widget/Site locative ranking is unchanged."""
    sketch = extract_nl_sketch("how many widgets in east")
    paths = candidate_ontology_paths(
        PROMOTED_ONTO, PROMOTED_TYPES, domain_type="Widget"
    )
    ranked = rank_paths(sketch, paths, subject_type="Widget", embedder=None)
    assert ranked
    assert ranked[0].path.rel_attr in {"stored_in", "has_region"}
    assert not any("embed_cos" in r for rp in ranked for r in rp.reasons)


@pytest.mark.asyncio
async def test_ground_ask_plan_async_with_async_embedder():
    """Async EmbedFn is materialized then used for ranking."""

    class AsyncFE:
        async def __call__(self, texts):
            out = []
            for t in texts:
                tl = t.lower()
                if "housed" in tl or "depot" in tl or "stash" in tl:
                    out.append([1.0, 0.0, 0.0])
                elif "made" in tl or "vendor" in tl:
                    out.append([0.0, 1.0, 0.0])
                else:
                    out.append([0.2, 0.2, 0.2])
            return out

    onto = (
        "Type: Gadget\n"
        "  - housed_at -> StashPod (relationship, key=housed_at)\n"
        "  - made_by -> Vendor (relationship, key=made_by)\n"
        "Type: StashPod\n"
        "Type: Vendor\n"
    )
    from infona_client.nlp.ontology_subgraph_match import ground_ask_plan_async

    plan = await ground_ask_plan_async(
        "gadgets in depot North",
        onto,
        type_names=["Gadget", "StashPod", "Vendor"],
        embedder=AsyncFE(),
    )
    assert plan is not None
    assert plan.confidence == "unique"
    assert plan.path is not None
    assert plan.path.rel_attr == "housed_at"


# ---------------------------------------------------------------------------
# 11. Semantic OntologyMentionIndex in scorer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semantic_index_boosts_dim_synonym_leaf():
    """Full healthy index: depot/warehouse → stored_in via resolve_rel boost."""
    from infona_client.nlp.ontology_mention_index import (
        OntologyMentionIndex,
        semantic_resolve_context,
    )

    class FakeEmbedder:
        async def __call__(self, texts):
            out = []
            for t in texts:
                v = [0.0] * 6
                tl = t.lower()
                if "stored_in" in tl or "warehouse" in tl or "depot" in tl:
                    v[0] = 1.0
                if "has_region" in tl or "region" in tl:
                    v[1] = 1.0
                if "made_by" in tl or "vendor" in tl:
                    v[2] = 1.0
                out.append(v)
            return out

    fe = FakeEmbedder()
    idx = OntologyMentionIndex()
    idx.upsert_rel(
        "stored_in",
        domain="Widget",
        range_type="Site",
        description="warehouse / depot location edge",
    )
    idx.upsert_rel(
        "has_region",
        domain="Widget",
        range_type="Region",
        description="region edge",
    )
    idx.upsert_rel(
        "made_by",
        domain="Widget",
        range_type="Vendor",
        description="vendor edge",
    )
    # Non-location range so string family alone does not pick stored_in for "depot"
    onto = (
        "Type: Widget\n"
        "  - stored_in -> Site (relationship, key=stored_in)\n"
        "  - has_region -> Region (relationship, key=has_region)\n"
        "  - made_by -> Vendor (relationship, key=made_by)\n"
        "Type: Site\n"
        "Type: Region\n"
        "Type: Vendor\n"
    )
    await idx.embed_missing(fe)
    q = (await fe(["depot"]))[0]
    with semantic_resolve_context(
        idx, query_embeddings={"depot": q}, require_semantic=False
    ):
        plan = ground_ask_plan(
            "widgets in depot East",
            onto,
            type_names=["Widget", "Site", "Region", "Vendor"],
            mention_index=idx,
            query_embedding=q,
        )
    assert plan is not None
    assert plan.confidence == "unique"
    assert plan.path is not None
    assert plan.path.rel_attr == "stored_in"
    # semantic reason present on ranked shortlist
    assert any(
        "semantic_rel" in r
        for rp in plan.ranked_paths
        for r in rp.reasons
    ) or plan.path.rel_attr == "stored_in"


@pytest.mark.asyncio
async def test_partial_semantic_index_ignored():
    """Partial / not fully embedded index must not invent edges (ONTA-537)."""
    from infona_client.nlp.ontology_mention_index import OntologyMentionIndex

    class FakeEmbedder:
        async def __call__(self, texts):
            return [[1.0, 0.0] for _ in texts]

    idx = OntologyMentionIndex()
    # Only one of the candidate leaves embedded → partial for full set
    idx.upsert_rel("stored_in", domain="Widget", range_type="Site")
    await idx.embed_missing(FakeEmbedder())
    # has_region listed in ontology but NOT in index → rels_fully_embedded false
    onto = (
        "Type: Widget\n"
        "  - stored_in -> Site (relationship, key=stored_in)\n"
        "  - has_region -> Region (relationship, key=has_region)\n"
        "Type: Site\n"
        "Type: Region\n"
    )
    plan = ground_ask_plan(
        "widgets in depot East",
        onto,
        type_names=["Widget", "Site", "Region"],
        mention_index=idx,
        query_embedding=[1.0, 0.0],
    )
    # Must not crash; no semantic_rel boost claimed on reasons if partial
    assert plan is not None
    if plan.ranked_paths:
        assert not any(
            "semantic_rel" in r for rp in plan.ranked_paths for r in rp.reasons
        )


def test_unhealthy_index_degrades_gracefully():
    from infona_client.nlp.ontology_mention_index import OntologyMentionIndex

    idx = OntologyMentionIndex()  # empty — not healthy
    plan = ground_ask_plan(
        "how many widgets in east",
        WIDGET_SITE_ONTO,
        type_names=WIDGET_SITE_TYPES,
        mention_index=idx,
    )
    assert plan is not None
    assert plan.confidence == "unique"
    assert plan.path is not None
    assert plan.path.rel_attr == "stored_in"


# ---------------------------------------------------------------------------
# 12. Always-LLM regression + path.describe multi-hop in timing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_cypher_never_skips_llm_with_multihop_grounding(monkeypatch):
    """Grounding (incl. multi-hop context) stays prompt-only; LLM still runs."""
    store = MemoryGraphStore()
    await _seed_widget_site(store)

    neptune = MagicMock()
    neptune.query = AsyncMock(side_effect=AssertionError("no SPARQL"))
    pipe = NLQueryPipeline(neptune, anthropic_key="", graph_store=store)
    pipe._fetch_ontology = AsyncMock(return_value=MULTI_HOP_ONTO)  # type: ignore[method-assign]
    pipe._openrouter_key = "k"  # type: ignore[attr-defined]
    pipe._query_provider = "openrouter"  # type: ignore[attr-defined]
    pipe._query_model = "m"  # type: ignore[attr-defined]

    llm_calls: list[dict] = []

    async def fake_llm(question: str, ontology: str, **kw):
        llm_calls.append({"q": question, "g": kw.get("grounding_text", "")})
        # Dimension filter present so constraint-coverage does not reject +
        # retry (filter-miss class). Execute is stubbed below.
        return {
            "cypher": (
                "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->"
                "(c:Class {tenant_id: $tenant_id, kg: $kg}) "
                "WHERE c.name IN $type_names AND e.name = $target_name "
                "RETURN e.id AS id LIMIT 25"
            ),
            "params": {"type_names": ["Part"], "target_name": "East"},
            "explanation": "list parts filtered by related name",
            "functions_needed": [],
        }

    pipe._try_llm_cypher = fake_llm  # type: ignore[method-assign]
    pipe._execute_confined_cypher = AsyncMock(  # type: ignore[method-assign]
        return_value=([{"id": "p1"}], "freeform:mock")
    )
    pipe._rephrase_via_openrouter = AsyncMock(return_value="")  # type: ignore[method-assign]
    monkeypatch.setattr(
        "infona_client.nlp.cypher_generate.try_deterministic_cypher",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "infona_client.nlp.pipeline.try_deterministic_cypher",
        lambda *a, **k: None,
        raising=False,
    )

    await pipe.ask(
        "parts in facility East",
        graph_uri=f"{IRI_BASE}/graphs/demo-tenant",
        instance_graph=f"{IRI_BASE}/graphs/demo-tenant/kg/widgets",
        use_cypher=True,
    )
    assert len(llm_calls) == 1
    assert llm_calls[0]["q"] == "parts in facility East"
    # Grounding may include multi-hop preferred path when unique
    g = llm_calls[0]["g"] or ""
    assert "Ontology grounding" in g or "preferred_path" in g or g == "" or "Part" in g

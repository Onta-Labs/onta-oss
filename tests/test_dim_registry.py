"""Low-cardinality dimension registry — hermetic synthetic tests.

Anti-overfit: Synth* types only. No persona CSV gold names (Term/Fall as
*product* hardcodes are forbidden; tests use synthetic labels that happen to
look like seasons only as *data*, never as code branches).
"""

from __future__ import annotations

import pytest

from infona_client.graph.iri import IRI_BASE
from infona_client.graph.kg_writer import insert_facts, refresh_after_write
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_catalog import upsert_attribute, upsert_type
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.schema_bootstrap import TEMPLATES
from infona_client.nlp.dim_registry import (
    DimInventorySlot,
    bind_filter_token,
    bind_filter_token_result,
    build_registry_from_inventory,
    dim_cardinality_threshold,
    ensure_dim_registry,
    format_dims_for_prompt,
    get_cached_dim_registry,
    invalidate_dim_registry,
    normalize_dim_token,
    planning_dim_binds,
    planning_dim_context,
    planning_dim_grounding,
    refresh_dim_registry,
    reset_dim_registry_for_tests,
)
from infona_client.nlp.numeric_plan_grounding import merge_grounding_texts

TENANT = "test-tenant"
KG = "synth-dim-kg"
GRAPH = f"{IRI_BASE}/graphs/{TENANT}/kg/{KG}"

# Synthetic type / leaf names only.
TYPE_OFFERING = "SynthCourseOffering"
TYPE_TERM = "SynthTerm"
TYPE_ASSET = "SynthAsset"
TYPE_OPERATOR = "SynthOperator"
REL_OFFERED_IN = "synth_offered_in"
REL_OPERATED_BY = "synth_operated_by"
ATTR_OPERATOR_NAME = "synth_operator_name"
ATTR_STATUS = "synth_status_label"
ATTR_FREE_TEXT = "synth_note_description"
ATTR_HIGH_CARD = "synth_serial_code"


@pytest.fixture(autouse=True)
def _clear_dim_cache():
    reset_dim_registry_for_tests()
    yield
    reset_dim_registry_for_tests()


def test_dim_templates_registered():
    for name in ("entity_type_prop_distinct", "entity_type_rel_target_distinct"):
        assert name in TEMPLATES
        assert TEMPLATES[name].writing is False
        assert "$tenant_id" in TEMPLATES[name].cypher
        assert "$kg" in TEMPLATES[name].cypher


def test_normalize_token_case_and_underscores():
    assert normalize_dim_token("NorthFleet") == "north fleet"
    assert normalize_dim_token("north_fleet") == "north fleet"
    assert normalize_dim_token("  NORTH-FLEET ") == "north fleet"
    assert normalize_dim_token("Fall") == "fall"


def test_cardinality_threshold_bounds():
    # Small types still use floor; large types cap at 50.
    assert dim_cardinality_threshold(10) == 20
    assert dim_cardinality_threshold(1000) == 50
    assert dim_cardinality_threshold(0) == 20


def test_build_registry_literal_and_entity_dims():
    """Pure inventory → both entity_dim and literal_enum register."""
    slots = [
        DimInventorySlot(
            subject_type=TYPE_OFFERING,
            leaf=REL_OFFERED_IN,
            kind="relationship",
            range_type=TYPE_TERM,
            values=("Fall", "Spring", "Summer"),
            distinct_count=3,
            coverage=12,
            type_entity_count=12,
        ),
        DimInventorySlot(
            subject_type=TYPE_ASSET,
            leaf=ATTR_OPERATOR_NAME,
            kind="literal",
            datatype="string",
            values=("NorthFleet", "SouthFleet", "EastFleet"),
            distinct_count=3,
            coverage=9,
            type_entity_count=9,
        ),
    ]
    reg = build_registry_from_inventory(slots, tenant_id=TENANT, kg=KG)
    kinds = {(d.subject_type, d.leaf, d.kind) for d in reg.dims}
    assert (TYPE_OFFERING, REL_OFFERED_IN, "entity_dim") in kinds
    assert (TYPE_ASSET, ATTR_OPERATOR_NAME, "literal_enum") in kinds


def test_high_cardinality_not_registered():
    """Distinct above threshold → not a dim."""
    # 60 distinct on a large type exceeds absolute cap 50.
    values = tuple(f"V{i:03d}" for i in range(60))
    slots = [
        DimInventorySlot(
            subject_type=TYPE_ASSET,
            leaf=ATTR_HIGH_CARD,
            kind="literal",
            datatype="string",
            values=values,
            distinct_count=60,
            coverage=200,
            type_entity_count=200,
        ),
        # Control: closed status still registers.
        DimInventorySlot(
            subject_type=TYPE_ASSET,
            leaf=ATTR_STATUS,
            kind="literal",
            datatype="string",
            values=("Active", "Idle", "Retired"),
            distinct_count=3,
            coverage=200,
            type_entity_count=200,
        ),
    ]
    reg = build_registry_from_inventory(slots, tenant_id=TENANT, kg=KG)
    leaves = {d.leaf for d in reg.dims}
    assert ATTR_HIGH_CARD not in leaves
    assert ATTR_STATUS in leaves


def test_bind_filter_token_fall_unique():
    reg = build_registry_from_inventory(
        [
            DimInventorySlot(
                subject_type=TYPE_OFFERING,
                leaf=REL_OFFERED_IN,
                kind="relationship",
                range_type=TYPE_TERM,
                values=("Fall", "Spring", "Winter"),
                distinct_count=3,
                coverage=10,
                type_entity_count=10,
            ),
        ],
        tenant_id=TENANT,
        kg=KG,
    )
    b = bind_filter_token("Fall", registry=reg)
    assert b is not None
    assert b.dim.leaf == REL_OFFERED_IN
    assert b.dim.range_type == TYPE_TERM
    assert b.matched_value.display == "Fall"
    assert b.dim.kind == "entity_dim"


def test_bind_filter_token_northfleet_unique():
    reg = build_registry_from_inventory(
        [
            DimInventorySlot(
                subject_type=TYPE_ASSET,
                leaf=ATTR_OPERATOR_NAME,
                kind="literal",
                datatype="string",
                values=("NorthFleet", "SouthFleet"),
                distinct_count=2,
                coverage=5,
                type_entity_count=5,
            ),
        ],
        tenant_id=TENANT,
        kg=KG,
    )
    b = bind_filter_token("NorthFleet", registry=reg)
    assert b is not None
    assert b.dim.leaf == ATTR_OPERATOR_NAME
    assert b.matched_value.display == "NorthFleet"


def test_bind_ambiguous_token_fail_closed():
    """Same value in two dims → no unique bind (fail closed)."""
    reg = build_registry_from_inventory(
        [
            DimInventorySlot(
                subject_type=TYPE_OFFERING,
                leaf=REL_OFFERED_IN,
                kind="relationship",
                range_type=TYPE_TERM,
                values=("SharedLabel", "Fall"),
                distinct_count=2,
                coverage=8,
                type_entity_count=8,
            ),
            DimInventorySlot(
                subject_type=TYPE_ASSET,
                leaf=ATTR_OPERATOR_NAME,
                kind="literal",
                datatype="string",
                values=("SharedLabel", "NorthFleet"),
                distinct_count=2,
                coverage=8,
                type_entity_count=8,
            ),
        ],
        tenant_id=TENANT,
        kg=KG,
    )
    result = bind_filter_token_result("SharedLabel", registry=reg)
    assert result.ambiguous is True
    assert result.unique is None
    assert len(result.candidates) >= 2
    assert bind_filter_token("SharedLabel", registry=reg) is None


def test_bind_normalization_north_fleet_variants():
    reg = build_registry_from_inventory(
        [
            DimInventorySlot(
                subject_type=TYPE_ASSET,
                leaf=ATTR_OPERATOR_NAME,
                kind="literal",
                datatype="string",
                values=("NorthFleet",),
                distinct_count=1,
                coverage=3,
                type_entity_count=3,
            ),
        ],
        tenant_id=TENANT,
        kg=KG,
    )
    for tok in ("north_fleet", "NorthFleet", "NORTH FLEET", "north-fleet"):
        b = bind_filter_token(tok, registry=reg)
        assert b is not None, tok
        assert b.matched_value.display == "NorthFleet"


def test_format_dims_block_for_prompt():
    reg = build_registry_from_inventory(
        [
            DimInventorySlot(
                subject_type=TYPE_ASSET,
                leaf=ATTR_OPERATOR_NAME,
                kind="literal",
                datatype="string",
                values=("NorthFleet", "SouthFleet"),
                distinct_count=2,
                coverage=4,
                type_entity_count=4,
            ),
        ],
        tenant_id=TENANT,
        kg=KG,
    )
    b = bind_filter_token("NorthFleet", registry=reg)
    text = format_dims_for_prompt(reg, binds=[b] if b else ())
    assert "Known low-cardinality dimensions" in text
    assert ATTR_OPERATOR_NAME in text
    assert "NorthFleet" in text
    assert "Bound filter tokens" in text


def test_merge_grounding_includes_dim_block():
    """Planning format: dim block appears when registry has data (pipeline path)."""
    reg = build_registry_from_inventory(
        [
            DimInventorySlot(
                subject_type=TYPE_OFFERING,
                leaf=REL_OFFERED_IN,
                kind="relationship",
                range_type=TYPE_TERM,
                values=("Fall", "Spring"),
                distinct_count=2,
                coverage=6,
                type_entity_count=6,
            ),
        ],
        tenant_id=TENANT,
        kg=KG,
    )
    dim_text = format_dims_for_prompt(reg)
    merged = merge_grounding_texts(
        "Numeric grounding (stub):\n  intent: none\n",
        dim_text,
    )
    assert "Known low-cardinality dimensions" in merged
    assert REL_OFFERED_IN in merged
    assert "Fall" in merged


# ---------------------------------------------------------------------------
# GraphStore-backed refresh (MemoryGraphStore)
# ---------------------------------------------------------------------------


async def _seed_offering_and_asset(store: MemoryGraphStore) -> None:
    """SynthCourseOffering→SynthTerm entity dim + SynthAsset literal enum."""
    t_fall = entity_uri(TYPE_TERM, "fall")
    t_spring = entity_uri(TYPE_TERM, "spring")
    t_summer = entity_uri(TYPE_TERM, "summer")
    o1 = entity_uri(TYPE_OFFERING, "o1")
    o2 = entity_uri(TYPE_OFFERING, "o2")
    o3 = entity_uri(TYPE_OFFERING, "o3")
    a1 = entity_uri(TYPE_ASSET, "a1")
    a2 = entity_uri(TYPE_ASSET, "a2")
    a3 = entity_uri(TYPE_ASSET, "a3")

    triples: list[tuple[str, str, str]] = [
        (t_fall, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/{TYPE_TERM}"),
        (t_fall, "http://www.w3.org/2000/01/rdf-schema#label", "Fall"),
        (t_spring, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/{TYPE_TERM}"),
        (t_spring, "http://www.w3.org/2000/01/rdf-schema#label", "Spring"),
        (t_summer, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/{TYPE_TERM}"),
        (t_summer, "http://www.w3.org/2000/01/rdf-schema#label", "Summer"),
        (o1, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/{TYPE_OFFERING}"),
        (o1, "http://www.w3.org/2000/01/rdf-schema#label", "Offering 1"),
        (o1, f"{IRI_BASE}/onto/{REL_OFFERED_IN}", t_fall),
        (o2, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/{TYPE_OFFERING}"),
        (o2, "http://www.w3.org/2000/01/rdf-schema#label", "Offering 2"),
        (o2, f"{IRI_BASE}/onto/{REL_OFFERED_IN}", t_spring),
        (o3, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/{TYPE_OFFERING}"),
        (o3, "http://www.w3.org/2000/01/rdf-schema#label", "Offering 3"),
        (o3, f"{IRI_BASE}/onto/{REL_OFFERED_IN}", t_fall),
        (a1, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/{TYPE_ASSET}"),
        (a1, "http://www.w3.org/2000/01/rdf-schema#label", "Asset 1"),
        (a1, f"{IRI_BASE}/types/{TYPE_ASSET}/attrs/{ATTR_OPERATOR_NAME}", "NorthFleet"),
        (a2, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/{TYPE_ASSET}"),
        (a2, "http://www.w3.org/2000/01/rdf-schema#label", "Asset 2"),
        (a2, f"{IRI_BASE}/types/{TYPE_ASSET}/attrs/{ATTR_OPERATOR_NAME}", "SouthFleet"),
        (a3, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/{TYPE_ASSET}"),
        (a3, "http://www.w3.org/2000/01/rdf-schema#label", "Asset 3"),
        (a3, f"{IRI_BASE}/types/{TYPE_ASSET}/attrs/{ATTR_OPERATOR_NAME}", "NorthFleet"),
    ]
    await insert_facts(None, GRAPH, triples, store=store)

    for t, desc in (
        (TYPE_OFFERING, "synthetic course offering"),
        (TYPE_TERM, "synthetic academic term"),
        (TYPE_ASSET, "synthetic asset"),
        (TYPE_OPERATOR, "synthetic operator type"),
    ):
        await upsert_type(
            store=store, name=t, description=desc, layer="tenant", tenant_id=TENANT
        )
    await upsert_attribute(
        store=store,
        type_name=TYPE_OFFERING,
        attr_name=REL_OFFERED_IN,
        datatype=TYPE_TERM,
        layer="tenant",
        tenant_id=TENANT,
    )
    await upsert_attribute(
        store=store,
        type_name=TYPE_ASSET,
        attr_name=ATTR_OPERATOR_NAME,
        datatype="string",
        layer="tenant",
        tenant_id=TENANT,
    )


@pytest.mark.asyncio
async def test_refresh_registry_from_store_entity_and_literal():
    store = MemoryGraphStore()
    try:
        await _seed_offering_and_asset(store)
        reg = await refresh_dim_registry(store, tenant_id=TENANT, kg=KG)
        assert reg.dims, "expected dims from synthetic inventory"
        by_leaf = {d.leaf: d for d in reg.dims}
        assert REL_OFFERED_IN in by_leaf
        assert by_leaf[REL_OFFERED_IN].kind == "entity_dim"
        displays = {v.display for v in by_leaf[REL_OFFERED_IN].values}
        assert "Fall" in displays
        assert "Spring" in displays

        assert ATTR_OPERATOR_NAME in by_leaf
        assert by_leaf[ATTR_OPERATOR_NAME].kind == "literal_enum"
        op_vals = {v.display for v in by_leaf[ATTR_OPERATOR_NAME].values}
        assert "NorthFleet" in op_vals
        assert "SouthFleet" in op_vals

        # Bind against the refreshed process cache.
        b_fall = bind_filter_token("Fall", tenant_id=TENANT, kg=KG)
        assert b_fall is not None
        assert b_fall.dim.leaf == REL_OFFERED_IN

        b_nf = bind_filter_token("north_fleet", tenant_id=TENANT, kg=KG)
        assert b_nf is not None
        assert b_nf.dim.leaf == ATTR_OPERATOR_NAME
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_high_card_from_store_not_registered():
    store = MemoryGraphStore()
    try:
        # One type, many unique serial codes (above absolute cap).
        triples: list[tuple[str, str, str]] = []
        for i in range(55):
            eid = entity_uri(TYPE_ASSET, f"h{i}")
            triples.append(
                (
                    eid,
                    "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
                    f"{IRI_BASE}/types/{TYPE_ASSET}",
                )
            )
            triples.append(
                (
                    eid,
                    f"{IRI_BASE}/types/{TYPE_ASSET}/attrs/{ATTR_HIGH_CARD}",
                    f"SERIAL-{i:04d}",
                )
            )
            # Closed status for control.
            triples.append(
                (
                    eid,
                    f"{IRI_BASE}/types/{TYPE_ASSET}/attrs/{ATTR_STATUS}",
                    "Active" if i % 2 == 0 else "Idle",
                )
            )
        await insert_facts(None, GRAPH, triples, store=store)
        await upsert_type(
            store=store,
            name=TYPE_ASSET,
            description="high card synth",
            layer="tenant",
            tenant_id=TENANT,
        )
        await upsert_attribute(
            store=store,
            type_name=TYPE_ASSET,
            attr_name=ATTR_HIGH_CARD,
            datatype="string",
            layer="tenant",
            tenant_id=TENANT,
        )
        await upsert_attribute(
            store=store,
            type_name=TYPE_ASSET,
            attr_name=ATTR_STATUS,
            datatype="string",
            layer="tenant",
            tenant_id=TENANT,
        )
        reg = await refresh_dim_registry(store, tenant_id=TENANT, kg=KG)
        leaves = {d.leaf for d in reg.dims}
        assert ATTR_HIGH_CARD not in leaves
        assert ATTR_STATUS in leaves
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_planning_dim_grounding_prompt_block():
    store = MemoryGraphStore()
    try:
        await _seed_offering_and_asset(store)
        text = await planning_dim_grounding(
            store,
            tenant_id=TENANT,
            kg=KG,
            question="how many offerings for Fall with NorthFleet",
        )
        assert text
        assert "Known low-cardinality dimensions" in text
        # Token binds should surface Fall / NorthFleet when present.
        assert "Fall" in text or REL_OFFERED_IN in text
        assert ATTR_OPERATOR_NAME in text or "NorthFleet" in text
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_planning_dim_context_returns_structured_binds():
    """planning_dim_context / planning_dim_binds expose unique DimBind list."""
    store = MemoryGraphStore()
    try:
        await _seed_offering_and_asset(store)
        # Quoted tokens avoid multi-word greed in extract_filter_tokens.
        q = 'how many offerings for "Fall" operated by "NorthFleet"'
        text, binds = await planning_dim_context(
            store, tenant_id=TENANT, kg=KG, question=q
        )
        assert text
        assert "Known low-cardinality dimensions" in text
        assert isinstance(binds, list)
        # Structured binds must also be available standalone.
        binds2 = await planning_dim_binds(
            store, tenant_id=TENANT, kg=KG, question=q
        )
        leaves = {b.dim.leaf for b in binds}
        leaves2 = {b.dim.leaf for b in binds2}
        assert leaves == leaves2
        # At least one of the synthetic dims should uniquely bind.
        assert leaves & {REL_OFFERED_IN, ATTR_OPERATOR_NAME}
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_refresh_after_write_invalidates_dim_cache():
    store = MemoryGraphStore()
    try:
        await _seed_offering_and_asset(store)
        await refresh_dim_registry(store, tenant_id=TENANT, kg=KG)
        assert get_cached_dim_registry(TENANT, KG) is not None
        await refresh_after_write(
            None,
            tenant_id=TENANT,
            kg_name=KG,
            affected_types=[TYPE_ASSET],
            recompute_stats=False,
            store=store,
        )
        assert get_cached_dim_registry(TENANT, KG) is None
        # Lazy rebuild still works.
        reg = await ensure_dim_registry(store, tenant_id=TENANT, kg=KG)
        assert reg is not None
        assert reg.dims
    finally:
        await store.close()


def test_free_text_leaf_excluded():
    slots = [
        DimInventorySlot(
            subject_type=TYPE_ASSET,
            leaf=ATTR_FREE_TEXT,
            kind="literal",
            datatype="string",
            values=("a long free text note", "another note"),
            distinct_count=2,
            coverage=2,
            type_entity_count=2,
        ),
    ]
    reg = build_registry_from_inventory(slots, tenant_id=TENANT, kg=KG)
    assert not reg.dims


def test_invalidate_scoped_to_tenant_kg():
    reg = build_registry_from_inventory(
        [
            DimInventorySlot(
                subject_type=TYPE_ASSET,
                leaf=ATTR_STATUS,
                kind="literal",
                datatype="string",
                values=("A", "B"),
                distinct_count=2,
                coverage=2,
                type_entity_count=2,
            ),
        ],
        tenant_id=TENANT,
        kg=KG,
    )
    from infona_client.nlp.dim_registry import put_cached_dim_registry

    put_cached_dim_registry(reg)
    put_cached_dim_registry(
        build_registry_from_inventory(
            [
                DimInventorySlot(
                    subject_type=TYPE_ASSET,
                    leaf=ATTR_STATUS,
                    kind="literal",
                    datatype="string",
                    values=("X",),
                    distinct_count=1,
                    coverage=1,
                    type_entity_count=1,
                ),
            ],
            tenant_id="other-tenant",
            kg=KG,
        )
    )
    invalidate_dim_registry(TENANT, KG)
    assert get_cached_dim_registry(TENANT, KG) is None
    assert get_cached_dim_registry("other-tenant", KG) is not None

"""ONTA-538: promoted low-cardinality dims route NL filters via related entities.

Anti-overfit: synthetic Widget / Site / Region types only — no warehouse/persona
CSV hardcodes in product code (range-type + leaf resolve are general).
"""

from __future__ import annotations

import pytest

from infona_client.nlp.cypher_generate import (
    _resolve_relationship_attr,
    try_deterministic_cypher,
    try_filter_query,
    try_related_name_filter_query,
)


# Widget --stored_in--> Site, --has_region--> Region (promoted dim shape)
PROMOTED_ONTO = (
    "Type: Widget\n"
    "  - sku: string (literal)\n"
    "  - unit_cost: float (literal)\n"
    "  - stored_in -> Site (relationship, key=stored_in)\n"
    "  - has_region -> Region (relationship, key=has_region)\n"
    "Type: Site\n"
    "  - name: string (literal)\n"
    "Type: Region\n"
    "  - name: string (literal)\n"
)
TYPES = ["Widget", "Site", "Region"]


def test_resolve_range_type_site_to_stored_in():
    """Hermetic: dim word matches range type Site → stored_in leaf."""
    assert (
        _resolve_relationship_attr(
            "site", type_name="Widget", ontology_summary=PROMOTED_ONTO
        )
        == "stored_in"
    )
    assert (
        _resolve_relationship_attr(
            "sites", type_name="Widget", ontology_summary=PROMOTED_ONTO
        )
        == "stored_in"
    )


def test_resolve_range_type_region_to_has_region():
    assert (
        _resolve_relationship_attr(
            "region", type_name="Widget", ontology_summary=PROMOTED_ONTO
        )
        == "has_region"
    )


def test_resolve_leaf_name_still_works():
    assert (
        _resolve_relationship_attr(
            "stored_in", type_name="Widget", ontology_summary=PROMOTED_ONTO
        )
        == "stored_in"
    )


def test_with_site_routes_related_filter():
    p = try_related_name_filter_query(
        "widgets with site East", PROMOTED_ONTO, type_names=TYPES
    )
    assert p is not None
    assert p["template"] == "related_entity_name_filter"
    assert p["params"]["rel_attr"] == "stored_in"
    assert p["params"]["target_name"] == "East"


def test_in_site_and_at_site_routes_related_filter():
    for q in (
        "widgets in site East",
        "widgets at site East",
        "widgets from site East",
    ):
        p = try_deterministic_cypher(q, PROMOTED_ONTO, type_names=TYPES)
        assert p is not None, q
        assert p["template"] == "related_entity_name_filter", q
        assert p["params"]["rel_attr"] == "stored_in", q
        assert p["params"]["target_name"] == "East", q


def test_equality_on_range_type_not_literal():
    """'where site is East' must NOT emit literal_values on prop site."""
    p = try_filter_query(
        "widgets where site is East", PROMOTED_ONTO, type_names=TYPES
    )
    assert p is not None
    assert p["template"] == "related_entity_name_filter"
    assert p["params"]["rel_attr"] == "stored_in"
    assert p["params"]["target_name"] == "East"
    assert "prop_key" not in p["params"]


def test_equality_on_leaf_name_routes_related():
    p = try_deterministic_cypher(
        "widgets where stored_in is East", PROMOTED_ONTO, type_names=TYPES
    )
    assert p is not None
    assert p["template"] == "related_entity_name_filter"
    assert p["params"]["rel_attr"] == "stored_in"


def test_literal_attrs_still_literal_equality():
    p = try_deterministic_cypher(
        "widgets where sku is A1", PROMOTED_ONTO, type_names=TYPES
    )
    assert p is not None
    assert p["template"] == "literal_values"
    assert p["params"]["prop_key"] == "sku"
    assert p["params"]["prop_value"] == "A1"


def test_ambiguous_range_types_fall_through():
    """Two rels ranging the same type → None (prefer clarify over empty)."""
    onto = (
        "Type: Widget\n"
        "  - stored_in -> Site (relationship, key=stored_in)\n"
        "  - owned_by -> Site (relationship, key=owned_by)\n"
        "Type: Site\n"
    )
    assert (
        _resolve_relationship_attr(
            "site", type_name="Widget", ontology_summary=onto
        )
        is None
    )
    assert (
        try_related_name_filter_query(
            "widgets with site East", onto, type_names=["Widget", "Site"]
        )
        is None
    )


def test_literal_same_name_not_stolen_by_range():
    """If type still has a literal leaf equal to the dim word, leave it alone."""
    onto = (
        "Type: Widget\n"
        "  - site: string (literal)\n"
        "  - stored_in -> Site (relationship, key=stored_in)\n"
        "Type: Site\n"
    )
    # Range path skips when literal declared; exact leaf path won't match "site"
    # as a relationship leaf → None (fall through). Equality keeps literal.
    assert (
        _resolve_relationship_attr(
            "site", type_name="Widget", ontology_summary=onto
        )
        is None
    )
    p = try_filter_query(
        "widgets where site is East", onto, type_names=["Widget", "Site"]
    )
    assert p is not None
    assert p["template"] == "literal_values"
    assert p["params"]["prop_key"] == "site"


def test_colon_form_range_resolve():
    onto = (
        "Type: Widget\n"
        "  - unit_cost: float (literal)\n"
        "  - stored_in: relationship → Site\n"
        "Type: Site\n"
    )
    assert (
        _resolve_relationship_attr(
            "site", type_name="Widget", ontology_summary=onto
        )
        == "stored_in"
    )


def test_region_with_phrase():
    p = try_deterministic_cypher(
        "widgets with region West", PROMOTED_ONTO, type_names=TYPES
    )
    assert p is not None
    assert p["template"] == "related_entity_name_filter"
    assert p["params"]["rel_attr"] == "has_region"
    assert p["params"]["target_name"] == "West"


@pytest.mark.asyncio
async def test_memory_e2e_promoted_dim_filter():
    """Widget A1 stored_in→Site(East); NL 'widgets with site East' returns A1."""
    from infona_client.graph.iri import IRI_BASE
    from infona_client.graph.memory_store import MemoryGraphStore
    from infona_client.graph.rdf_model import AssertionFact, assert_fact
    from infona_client.graph.scope import GraphScope

    store = MemoryGraphStore()
    scope = GraphScope.for_instance("demo-tenant", "promoted-dim")
    session = store.session(scope)

    a1 = f"{IRI_BASE}/entities/Widget/A1"
    a2 = f"{IRI_BASE}/entities/Widget/A2"
    east = f"{IRI_BASE}/entities/Site/East"
    west = f"{IRI_BASE}/entities/Site/West"

    for eid, ptype, name in (
        (a1, "Widget", "A1"),
        (a2, "Widget", "A2"),
        (east, "Site", "East"),
        (west, "Site", "West"),
    ):
        await session.write_merge_entity(
            id=eid, primary_type=ptype, name=name, source="test"
        )
        await assert_fact(
            session, AssertionFact(subject_id=eid, kind="type", value=ptype)
        )

    await assert_fact(
        session,
        AssertionFact(
            subject_id=a1, kind="object", property_leaf="stored_in", value=east
        ),
    )
    await assert_fact(
        session,
        AssertionFact(
            subject_id=a2, kind="object", property_leaf="stored_in", value=west
        ),
    )

    payload = try_deterministic_cypher(
        "widgets with site East", PROMOTED_ONTO, type_names=TYPES
    )
    assert payload is not None
    assert payload["template"] == "related_entity_name_filter"
    rows = await session.execute_template(payload["template"], payload["params"])
    ids = {str(r.get("id") or "") for r in rows}
    assert any("A1" in i for i in ids), rows
    assert not any("A2" in i for i in ids), rows


@pytest.mark.asyncio
async def test_semantic_warehouse_to_stored_in_when_index_present():
    """ONTA-537 path: synonym warehouse → stored_in under full embed guards."""
    from infona_client.nlp.ontology_mention_index import (
        OntologyMentionIndex,
        format_rel_embed_text,
        semantic_resolve_context,
    )

    class FakeEmbedder:
        async def __call__(self, texts):
            # Deterministic orthogonal-ish vectors keyed by content tokens.
            out = []
            for t in texts:
                v = [0.0] * 8
                tl = t.lower()
                if "stored_in" in tl or "warehouse" in tl or "location" in tl:
                    v[0] = 1.0
                if "has_region" in tl or "region" in tl:
                    v[1] = 1.0
                if "warehouse" in tl and "stored" not in tl:
                    # query phrase "warehouse" shares axis 0 with stored_in text
                    v[0] = 1.0
                out.append(v)
            return out

    fe = FakeEmbedder()
    idx = OntologyMentionIndex()
    idx.upsert_rel(
        "stored_in",
        domain="Widget",
        range_type="Site",
        description="warehouse location edge",
    )
    idx.upsert_rel(
        "has_region",
        domain="Widget",
        range_type="Region",
        description="region edge",
    )
    await idx.embed_missing(fe)
    q = (await fe(["warehouse"]))[0]
    with semantic_resolve_context(
        idx, query_embeddings={"warehouse": q}, require_semantic=False
    ):
        hit = _resolve_relationship_attr(
            "warehouse", type_name="Widget", ontology_summary=PROMOTED_ONTO
        )
        p = try_deterministic_cypher(
            "widgets in warehouse East",
            PROMOTED_ONTO,
            type_names=TYPES,
        )
    assert hit == "stored_in"
    assert p is not None
    assert p["template"] == "related_entity_name_filter"
    assert p["params"]["rel_attr"] == "stored_in"
    assert p["params"]["target_name"] == "East"

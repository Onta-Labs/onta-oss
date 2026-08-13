"""Prefer instance-populated schema over stale declared edges for NL planning.

Persona 56a8c2 P1: tenant catalog still declares ``has_sponsor→Sponsor`` /
``has_phase→Phase`` while THIS KG only has ``sponsored_by→Organization`` +
literal ``phase_label``. The LLM then plans dead edges → empty/wrong answers.

Mechanism under test (anti-overfit):
  * pure merge ranks populated leaves first and demotes declared-empty ones
  * GraphStore ontology_from_graph_store surfaces instance-only leaves as primary
  * dead declared edges stay visible but marked ``[no instances]`` (ONTA-248)

Synthetic leaves only — no persona CSV / live API.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from infona_client.graph.iri import IRI_BASE
from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_catalog import (
    OntoAttrRecord,
    SchemaTypeSummary,
    upsert_attribute,
    upsert_type,
)
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.store import reset_graph_store_for_tests
from infona_client.nlp.cypher_generate import (
    format_schema_types_for_cypher,
    ontology_from_graph_store,
)
from infona_client.nlp.planning_schema import (
    NO_INSTANCES_MARK,
    build_planning_type,
    format_planning_ontology,
    merge_declared_and_populated,
    order_planning_types,
    planning_types_from_schema_and_summaries,
)

TENANT = "plan-schema-tenant"
KG = "plan-schema-kg"
GRAPH = f"{IRI_BASE}/graphs/{TENANT}/kg/{KG}"

# Synthetic domain: study + org. Declared leaves ≠ instance leaves (the bug).
TYPE_STUDY = "SynthStudy"
TYPE_ORG = "SynthOrg"
TYPE_SPONSOR = "SynthSponsor"  # declared range only; no instances
TYPE_PHASE = "SynthPhase"  # declared range only; no instances

DECL_HAS_SPONSOR = "has_sponsor"  # declared, empty in KG
DECL_HAS_PHASE = "has_phase"  # declared, empty in KG
INST_SPONSORED_BY = "sponsored_by"  # instance-only relationship
INST_PHASE_LABEL = "phase_label"  # instance-only literal
INST_TITLE = "study_title"  # declared + populated


@dataclass
class _FakeAttr:
    name: str
    kind: str = "literal"
    datatype: str | None = "string"
    range_type: str | None = None
    prop_key: str | None = None
    count: int = 0
    target_type: str | None = None


def test_merge_prefers_populated_over_declared_empty():
    """Core pure-function contract: sponsored_by primary, has_sponsor secondary."""
    declared = [
        _FakeAttr(
            name=DECL_HAS_SPONSOR,
            kind="relationship",
            range_type=TYPE_SPONSOR,
            datatype=None,
        ),
        _FakeAttr(
            name=DECL_HAS_PHASE,
            kind="relationship",
            range_type=TYPE_PHASE,
            datatype=None,
        ),
        _FakeAttr(name=INST_TITLE, kind="literal", datatype="string"),
    ]
    pop_lits = [
        _FakeAttr(name=INST_TITLE, count=3, datatype="string"),
        _FakeAttr(name=INST_PHASE_LABEL, count=2, datatype="string"),
    ]
    pop_rels = [
        _FakeAttr(
            name=INST_SPONSORED_BY,
            count=3,
            target_type=TYPE_ORG,
            kind="relationship",
        ),
    ]
    slots = merge_declared_and_populated(
        declared=declared,
        populated_literals=pop_lits,
        populated_relationships=pop_rels,
    )
    names = [s.name for s in slots]
    # Populated first (by count then name): sponsored_by (3), study_title (3),
    # phase_label (2), then empty declared has_*.
    assert names[0] in (INST_SPONSORED_BY, INST_TITLE)
    assert INST_SPONSORED_BY in names
    assert INST_PHASE_LABEL in names
    assert DECL_HAS_SPONSOR in names
    assert DECL_HAS_PHASE in names

    by = {s.name: s for s in slots}
    assert by[INST_SPONSORED_BY].populated is True
    assert by[INST_SPONSORED_BY].kind == "relationship"
    assert by[INST_SPONSORED_BY].range_type == TYPE_ORG
    assert by[INST_PHASE_LABEL].populated is True
    assert by[INST_PHASE_LABEL].kind == "literal"
    assert by[DECL_HAS_SPONSOR].populated is False
    assert by[DECL_HAS_PHASE].populated is False

    # Ordering: every populated leaf before every empty declared leaf.
    first_empty = next(i for i, s in enumerate(slots) if not s.populated)
    assert all(s.populated for s in slots[:first_empty])
    assert all(not s.populated for s in slots[first_empty:])


def test_format_puts_dead_declared_edge_after_populated():
    ptype = build_planning_type(
        name=TYPE_STUDY,
        entity_count=5,
        declared=[
            _FakeAttr(
                name=DECL_HAS_SPONSOR,
                kind="relationship",
                range_type=TYPE_SPONSOR,
                datatype=None,
            ),
            _FakeAttr(name=INST_TITLE, kind="literal", datatype="string"),
        ],
        populated_literals=[
            _FakeAttr(name=INST_TITLE, count=5, datatype="string"),
            _FakeAttr(name=INST_PHASE_LABEL, count=4, datatype="string"),
        ],
        populated_relationships=[
            _FakeAttr(
                name=INST_SPONSORED_BY,
                count=5,
                target_type=TYPE_ORG,
                kind="relationship",
            ),
        ],
    )
    text = format_planning_ontology([ptype], preface=True)
    assert "Planning note:" in text
    assert f"Type: {TYPE_STUDY} (5 entities)" in text
    assert INST_SPONSORED_BY in text
    assert INST_PHASE_LABEL in text
    assert DECL_HAS_SPONSOR in text
    assert NO_INSTANCES_MARK in text

    # Primary (no mark) before secondary (marked) for the sponsor leaves.
    pos_pop = text.index(INST_SPONSORED_BY)
    pos_dead = text.index(DECL_HAS_SPONSOR)
    assert pos_pop < pos_dead
    # Dead edge carries the mark on its line.
    dead_line = next(
        ln for ln in text.splitlines() if DECL_HAS_SPONSOR in ln
    )
    assert NO_INSTANCES_MARK in dead_line
    pop_line = next(
        ln for ln in text.splitlines() if INST_SPONSORED_BY in ln
    )
    assert NO_INSTANCES_MARK not in pop_line


def test_order_planning_types_caps_empty_shells():
    types = [
        build_planning_type(name=f"EmptyShell{i}", entity_count=0)
        for i in range(20)
    ] + [build_planning_type(name=TYPE_STUDY, entity_count=3)]
    ordered = order_planning_types(types, max_empty_types=5)
    assert ordered[0].name == TYPE_STUDY
    empties = [t for t in ordered if t.entity_count == 0]
    assert len(empties) == 5
    # Force-include bypasses the cap.
    ordered2 = order_planning_types(
        types, max_empty_types=2, force_include=["EmptyShell19"]
    )
    names = {t.name for t in ordered2}
    assert "EmptyShell19" in names
    assert TYPE_STUDY in names


def test_format_schema_types_for_cypher_catalog_only_still_usable():
    """Catalog-only formatter: type-level empty mark; slots on live types usable."""
    rows = [
        SchemaTypeSummary(
            name="Book",
            layer="tenant",
            entity_count=3,
            attributes=(
                OntoAttrRecord(
                    name="title",
                    domain="Book",
                    layer="tenant",
                    tenant_id="t",
                    kg="__ontology__",
                    kind="literal",
                    datatype="string",
                    prop_key="title",
                ),
                OntoAttrRecord(
                    name="author",
                    domain="Book",
                    layer="tenant",
                    tenant_id="t",
                    kg="__ontology__",
                    kind="relationship",
                    range_type="Author",
                    prop_key="author",
                ),
            ),
        ),
        SchemaTypeSummary(
            name="Author", layer="tenant", entity_count=0, attributes=()
        ),
    ]
    text = format_schema_types_for_cypher(rows)
    assert "Type: Book (3 entities)" in text
    assert "Type: Author [no instances]" in text
    assert "title: string" in text
    assert "author -> Author" in text
    # Catalog-only must NOT mark live-type declared slots as empty.
    book_block = text.split("Type: Author")[0]
    assert NO_INSTANCES_MARK not in book_block


async def _seed_declared_vs_instance(store: MemoryGraphStore) -> None:
    """Catalog has has_sponsor/has_phase; instances use sponsored_by/phase_label."""
    s1 = entity_uri(TYPE_STUDY, "s1")
    s2 = entity_uri(TYPE_STUDY, "s2")
    o1 = entity_uri(TYPE_ORG, "o1")
    triples = [
        (s1, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/{TYPE_STUDY}"),
        (s1, "http://www.w3.org/2000/01/rdf-schema#label", "Study One"),
        (s1, f"{IRI_BASE}/types/{TYPE_STUDY}/attrs/{INST_TITLE}", "Alpha"),
        (s1, f"{IRI_BASE}/types/{TYPE_STUDY}/attrs/{INST_PHASE_LABEL}", "Phase 2"),
        (s1, f"{IRI_BASE}/onto/{INST_SPONSORED_BY}", o1),
        (s2, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/{TYPE_STUDY}"),
        (s2, "http://www.w3.org/2000/01/rdf-schema#label", "Study Two"),
        (s2, f"{IRI_BASE}/types/{TYPE_STUDY}/attrs/{INST_TITLE}", "Beta"),
        (s2, f"{IRI_BASE}/types/{TYPE_STUDY}/attrs/{INST_PHASE_LABEL}", "Phase 3"),
        (s2, f"{IRI_BASE}/onto/{INST_SPONSORED_BY}", o1),
        (o1, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{IRI_BASE}/types/{TYPE_ORG}"),
        (o1, "http://www.w3.org/2000/01/rdf-schema#label", "Org One"),
    ]
    await insert_facts(None, GRAPH, triples, store=store)

    await upsert_type(
        store=store, name=TYPE_STUDY, description="synthetic study",
        layer="tenant", tenant_id=TENANT,
    )
    await upsert_type(
        store=store, name=TYPE_ORG, description="synthetic org",
        layer="tenant", tenant_id=TENANT,
    )
    await upsert_type(
        store=store, name=TYPE_SPONSOR, description="declared empty sponsor type",
        layer="tenant", tenant_id=TENANT,
    )
    await upsert_type(
        store=store, name=TYPE_PHASE, description="declared empty phase type",
        layer="tenant", tenant_id=TENANT,
    )
    # Declared (stale) edges — not present on instances.
    await upsert_attribute(
        store=store, type_name=TYPE_STUDY, attr_name=DECL_HAS_SPONSOR,
        datatype=TYPE_SPONSOR, layer="tenant", tenant_id=TENANT,
    )
    await upsert_attribute(
        store=store, type_name=TYPE_STUDY, attr_name=DECL_HAS_PHASE,
        datatype=TYPE_PHASE, layer="tenant", tenant_id=TENANT,
    )
    # Declared + populated literal.
    await upsert_attribute(
        store=store, type_name=TYPE_STUDY, attr_name=INST_TITLE,
        datatype="string", layer="tenant", tenant_id=TENANT,
    )
    # Note: INST_SPONSORED_BY and INST_PHASE_LABEL intentionally NOT declared.


@pytest.mark.asyncio
async def test_ontology_from_graph_store_prefers_instance_edges():
    store = MemoryGraphStore()
    try:
        await _seed_declared_vs_instance(store)
        text, names = await ontology_from_graph_store(
            store, tenant_id=TENANT, kg=KG, prefer_populated=True
        )
        assert text, "expected planning ontology text"
        assert TYPE_STUDY in names
        assert TYPE_ORG in names

        # Instance leaves are primary plan context.
        assert INST_SPONSORED_BY in text
        assert INST_PHASE_LABEL in text
        assert INST_TITLE in text

        # Dead declared edges still listed (ONTA-248) but not primary.
        assert DECL_HAS_SPONSOR in text
        assert DECL_HAS_PHASE in text

        study_block = text
        if f"Type: {TYPE_STUDY}" in text:
            # Take from study header to next Type: or end.
            start = text.index(f"Type: {TYPE_STUDY}")
            rest = text[start:]
            nxt = rest.find("\nType: ", 1)
            study_block = rest if nxt < 0 else rest[:nxt]

        pos_pop = study_block.index(INST_SPONSORED_BY)
        pos_dead = study_block.index(DECL_HAS_SPONSOR)
        assert pos_pop < pos_dead, (
            "populated sponsored_by must appear before declared-empty has_sponsor"
        )
        dead_line = next(
            ln for ln in study_block.splitlines() if DECL_HAS_SPONSOR in ln
        )
        assert NO_INSTANCES_MARK in dead_line
        pop_line = next(
            ln for ln in study_block.splitlines() if INST_SPONSORED_BY in ln
        )
        assert NO_INSTANCES_MARK not in pop_line
        assert "-> " in pop_line and TYPE_ORG in pop_line

        # Empty declared types demoted / marked.
        assert TYPE_SPONSOR in text
        assert f"Type: {TYPE_SPONSOR} {NO_INSTANCES_MARK}" in text or (
            f"Type: {TYPE_SPONSOR}" in text and NO_INSTANCES_MARK in text
        )
    finally:
        await store.close()
        reset_graph_store_for_tests()


@pytest.mark.asyncio
async def test_ontology_from_graph_store_catalog_only_flag():
    """prefer_populated=False keeps catalog-only leaves (no instance overlay)."""
    store = MemoryGraphStore()
    try:
        await _seed_declared_vs_instance(store)
        text, _ = await ontology_from_graph_store(
            store, tenant_id=TENANT, kg=KG, prefer_populated=False
        )
        assert DECL_HAS_SPONSOR in text
        # Instance-only leaves are invisible without inventory overlay.
        assert INST_SPONSORED_BY not in text
        assert INST_PHASE_LABEL not in text
    finally:
        await store.close()
        reset_graph_store_for_tests()


def test_planning_types_from_summaries_overlay():
    """Duck-typed schema + summary rows produce the same preference order."""
    schema = [
        SchemaTypeSummary(
            name=TYPE_STUDY,
            layer="tenant",
            entity_count=2,
            attributes=(
                OntoAttrRecord(
                    name=DECL_HAS_SPONSOR,
                    domain=TYPE_STUDY,
                    layer="tenant",
                    tenant_id=TENANT,
                    kg="__ontology__",
                    kind="relationship",
                    range_type=TYPE_SPONSOR,
                ),
                OntoAttrRecord(
                    name=INST_TITLE,
                    domain=TYPE_STUDY,
                    layer="tenant",
                    tenant_id=TENANT,
                    kg="__ontology__",
                    kind="literal",
                    datatype="string",
                ),
            ),
        ),
    ]

    @dataclass
    class _Sum:
        name: str
        entity_count: int
        attributes: tuple
        relationships: tuple
        description: str = ""
        parent_type: str | None = None

    @dataclass
    class _A:
        name: str
        count: int
        datatype: str = "string"

    @dataclass
    class _R:
        name: str
        count: int
        target_type: str | None

    summary = _Sum(
        name=TYPE_STUDY,
        entity_count=2,
        attributes=(
            _A(name=INST_TITLE, count=2),
            _A(name=INST_PHASE_LABEL, count=2),
        ),
        relationships=(
            _R(name=INST_SPONSORED_BY, count=2, target_type=TYPE_ORG),
        ),
    )
    types = planning_types_from_schema_and_summaries(
        schema, {TYPE_STUDY: summary}, inventory_probed=True
    )
    assert len(types) == 1
    names = [s.name for s in types[0].slots]
    assert names.index(INST_SPONSORED_BY) < names.index(DECL_HAS_SPONSOR)
    by = {s.name: s for s in types[0].slots}
    assert by[DECL_HAS_SPONSOR].populated is False
    assert by[INST_SPONSORED_BY].populated is True

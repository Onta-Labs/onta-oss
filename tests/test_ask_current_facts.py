"""Closed HQ (SF) must not appear next to current Austin on /ask or explore.

Hermetic MemoryGraphStore. Invented Widget schema — no real customer data.
After ``write_with_conflict_resolution`` Austin wins; SF stays stored.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from infona_client.api_registry.spec import AuthorityLevel
from infona_client.graph.current_facts import (
    CURRENT_INTERVAL_KEEP_CYPHER,
    CURRENT_INTERVAL_OPTIONAL_CYPHER,
)
from infona_client.graph.explore_store import get_entity_detail
from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_queries import attr_uri
from infona_client.graph.queries import kg_graph_uri
from infona_client.graph.rdfs_helpers_templates import (
    LITERAL_AGGREGATE_CYPHER,
    LITERAL_ARGMAX_BY_DIM_CYPHER,
    LITERAL_COMPARE_CYPHER,
    LITERAL_DISTINCT_COUNT_CYPHER,
    LITERAL_VALUES_COUNT_CYPHER,
    LITERAL_VALUES_CYPHER,
)
from infona_client.graph.scope import GraphScope
from infona_client.graph.store import get_graph_store
from infona_client.pipeline.conflict import FactClaim
from infona_client.pipeline.mutations import write_with_conflict_resolution

TENANT, KG = "test-tenant", "kg"
GRAPH = kg_graph_uri(TENANT, KG)
TYPE = "Widget"
ENTITY = "https://graph.infona.ai/entities/Widget/e1"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
TYPE_URI = "https://graph.infona.ai/types/Widget"
HQ = attr_uri(TYPE, "headquarters")
SKU = attr_uri(TYPE, "sku")
AUSTIN, SF = "Austin", "San Francisco"
AT = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _lits(subject: str, pred: str) -> set:
    leaf = pred.rstrip("/").rsplit("/", 1)[-1]
    return {
        a.get("literal_value")
        for a in get_graph_store().snapshot_assertions()
        if a.get("subject_id") == subject
        and str(a.get("property_id") or "").endswith(leaf)
    }


def _hq_values(props: dict) -> set[str]:
    raw = props.get("headquarters")
    if raw is None:
        return set()
    if isinstance(raw, (list, tuple, set)):
        return {str(v) for v in raw}
    return {str(raw)}


async def _seed_austin_beats_sf() -> None:
    await insert_facts(None, GRAPH, [(ENTITY, RDF_TYPE, TYPE_URI), (ENTITY, HQ, AUSTIN)])
    receipt = await write_with_conflict_resolution(
        None,
        GRAPH,
        subject=ENTITY,
        predicate=HQ,
        type_name=TYPE,
        value=SF,
        authority=AuthorityLevel.supplementary,
        source="directory",
        observed_at=AT,
        existing_claims=[
            FactClaim(
                value=AUSTIN,
                authority=AuthorityLevel.source_of_truth,
                source="erp",
                observed_at=AT,
            )
        ],
        run_id="ask-cur-hq",
        refresh=False,
    )
    assert receipt.conflict is True
    assert receipt.winner == (ENTITY, HQ, AUSTIN)
    assert receipt.loser == (ENTITY, HQ, SF)


async def _literal_eq(prop_value: str) -> list:
    store = get_graph_store()
    session = store.session(GraphScope.for_instance(TENANT, KG))
    return await session.execute_template(
        "literal_values",
        {
            "type_names": [TYPE],
            "prop_key": "headquarters",
            "prop_value": prop_value,
            "limit": 25,
        },
    )


def test_literal_templates_optional_match_validity_interval():
    for body in (
        LITERAL_VALUES_CYPHER,
        LITERAL_VALUES_COUNT_CYPHER,
        LITERAL_COMPARE_CYPHER,
        LITERAL_AGGREGATE_CYPHER,
        LITERAL_ARGMAX_BY_DIM_CYPHER,
        LITERAL_DISTINCT_COUNT_CYPHER,
    ):
        assert "ValidityInterval" in body
        assert CURRENT_INTERVAL_OPTIONAL_CYPHER in body
        assert CURRENT_INTERVAL_KEEP_CYPHER in body
        assert "valid_to IS NULL" in body


def test_conflict_winner_literal_template_hides_closed_sf():
    async def run():
        await _seed_austin_beats_sf()
        austin_rows = await _literal_eq(AUSTIN)
        sf_rows = await _literal_eq(SF)
        assert {r.get("id") for r in austin_rows} == {ENTITY}
        assert {r.get("literal_value") for r in austin_rows} == {AUSTIN}
        assert sf_rows == []
        store = get_graph_store()
        session = store.session(GraphScope.for_instance(TENANT, KG))
        count_rows = await session.execute_template(
            "literal_values_count",
            {
                "type_names": [TYPE],
                "prop_key": "headquarters",
                "prop_value": SF,
            },
        )
        assert int(count_rows[0].get("n") or 0) == 0
        assert _lits(ENTITY, HQ) >= {AUSTIN, SF}
        closed = [
            v
            for v in store.snapshot_validity()
            if v.get("object_repr") == SF and v.get("valid_to")
        ]
        assert closed

    _run(run())


def test_explore_properties_drop_closed_hq():
    async def run():
        await _seed_austin_beats_sf()
        detail = await get_entity_detail(
            store=get_graph_store(),
            tenant_id=TENANT,
            kg=KG,
            entity_id=ENTITY,
        )
        assert detail is not None
        assert _hq_values(detail.properties) == {AUSTIN}
        assert SF not in _hq_values(detail.properties)
        assert _lits(ENTITY, HQ) >= {AUSTIN, SF}

    _run(run())


def test_legacy_unannotated_literal_stays_current():
    async def run():
        await insert_facts(
            None, GRAPH, [(ENTITY, RDF_TYPE, TYPE_URI), (ENTITY, SKU, "W-1")]
        )
        store = get_graph_store()
        session = store.session(GraphScope.for_instance(TENANT, KG))
        rows = await session.execute_template(
            "literal_values",
            {
                "type_names": [TYPE],
                "prop_key": "sku",
                "prop_value": "W-1",
                "limit": 25,
            },
        )
        assert {r.get("id") for r in rows} == {ENTITY}
        detail = await get_entity_detail(
            store=store, tenant_id=TENANT, kg=KG, entity_id=ENTITY
        )
        assert detail is not None
        assert detail.properties.get("sku") == "W-1"

    _run(run())


def test_memory_store_is_the_hermetic_backend():
    assert isinstance(get_graph_store(), MemoryGraphStore)

"""Hermetic unique-literal count. Gadgets / vendor_code only."""

from __future__ import annotations

import pytest

from infona_client.graph.iri import IRI_BASE
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_catalog import upsert_type_pg
from infona_client.graph.rdf_model import AssertionFact, assert_fact
from infona_client.graph.schema_bootstrap import TEMPLATES, get_template
from infona_client.graph.scope import GraphScope
from infona_client.nlp.prompts import CYPHER_GENERATION_SYSTEM
from infona_client.nlp.query_constraint_coverage_check import check_constraint_coverage
from infona_client.nlp.query_intent import sketch_query_intent

_UNIQUE_LEAF_Q = "How many unique vendor_code values among gadgets?"
_UNIQUE_TYPE_Q = "How many unique gadgets are there?"
_PARAMS = {"type_names": ["Gadget"], "prop_key": "vendor_code"}


def test_unique_intent_and_noun():
    sk = sketch_query_intent(_UNIQUE_LEAF_Q)
    assert sk.has_unique_count_intent
    sk_t = sketch_query_intent(_UNIQUE_TYPE_Q)
    assert sk_t.has_unique_count_intent


def test_unique_leaf_plus_type_scan_fail_closes():
    r = check_constraint_coverage(
        _UNIQUE_LEAF_Q,
        TEMPLATES["entities_of_type_count"].cypher,
        params={"type_names": ["Gadget"]},
        template="entities_of_type_count",
    )
    assert not r.ok
    assert r.fail_closed
    reason = (r.reason or "").lower()
    assert "literal_distinct_count" in reason
    assert "sponsor" not in reason
    assert "trial" not in reason


def test_unique_type_plus_type_scan_still_ok():
    r = check_constraint_coverage(
        _UNIQUE_TYPE_Q,
        TEMPLATES["entities_of_type_count"].cypher,
        params={"type_names": ["Gadget"]},
        template="entities_of_type_count",
    )
    assert r.ok
    assert not r.fail_closed


def test_distinct_helper_passes_coverage():
    r = check_constraint_coverage(
        _UNIQUE_LEAF_Q,
        TEMPLATES["literal_distinct_count"].cypher,
        params=_PARAMS,
        template="literal_distinct_count",
    )
    assert r.ok
    assert not r.fail_closed


def test_template_registered_and_prompt():
    t = get_template("literal_distinct_count")
    assert "$prop_key" in t.cypher
    assert "count(DISTINCT val)" in t.cypher
    assert "$limit" not in t.cypher
    s = CYPHER_GENERATION_SYSTEM.lower()
    assert "literal_distinct_count" in s
    # Scope to this helper's teaching line. The rest of the prompt already
    # mentions lead_sponsor as a generic rel-type leaf (not an eval-set list).
    i = s.find("literal_distinct_count")
    chunk = s[i : i + 400]
    assert "enrollment" not in chunk
    assert "nsclc" not in chunk
    assert "sponsor" not in chunk


@pytest.mark.asyncio
async def test_memory_distinct_not_entity_count():
    store = MemoryGraphStore()
    cat = store.session(GraphScope.for_catalog(layer="tenant", tenant_id="demo-tenant"))
    await upsert_type_pg(cat, name="Gadget", description="widget")
    session = store.session(GraphScope.for_instance("demo-tenant", "lab"))

    async def _g(eid: str, vendor: str) -> None:
        uri = f"{IRI_BASE}/entities/Gadget/{eid}"
        await session.write_merge_entity(
            id=uri, primary_type="Gadget", name=eid, source="test"
        )
        await assert_fact(
            session, AssertionFact(subject_id=uri, kind="type", value="Gadget"),
            dual_write_cache=True,
        )
        await assert_fact(
            session,
            AssertionFact(
                subject_id=uri, kind="literal", property_leaf="vendor_code", value=vendor
            ),
            dual_write_cache=True,
        )

    await _g("a1", "VendA")
    await _g("a2", "VendA")
    await _g("b1", "VendB")
    rows = await session.execute_template("literal_distinct_count", _PARAMS)
    assert len(rows) == 1
    assert rows[0].get("n") == 2
    n_ent = await session.execute_template(
        "entities_of_type_count", {"type_names": ["Gadget"]}
    )
    assert n_ent[0].get("n") == 3

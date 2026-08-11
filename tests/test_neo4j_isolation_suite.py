"""Neo4j GraphStore isolation suite (hermetic).

MemoryGraphStore + ADR 0013 Assertion writes — no live Neo4j, no network.

Pins the multi-tenant / multi-kg contract for the optional Neo4j backend
(Neo4j is the production default; this suite is hermetic MemoryGraphStore):

1. **Two tenants, two kgs, no cross-leak** — same entity id string in
   tenant A/kg-a and tenant B/kg-b never appears in the peer session.
2. **Wrong kg empty** — sibling kg under the same tenant sees zero rows.
3. **Session overwrites scope** — caller-supplied ``tenant_id`` / ``kg``
   parameters are replaced by the session scope on every write/read.
4. **Explore list/count via INSTANCE_OF + Class** — type membership is
   Assertion-derived (ADR 0013); denorm ``primary_type`` alone is not SoT.

Companion live smoke: ``tests/test_graph_store_neo4j_integration.py``
(``@pytest.mark.neo4j``).
"""

from __future__ import annotations

import asyncio

import pytest

from infona_client.graph.explore_store import (
    count_entities,
    list_entities_by_type,
    type_counts,
)
from infona_client.graph.assertion_model import property_uri
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_queries import entity_uri, type_uri
from infona_client.graph.rdf_model import AssertionFact, assert_fact
from infona_client.graph.rdfs_helpers import (
    session_assertions_for_subject,
    session_entities_of_type,
    session_literal_values,
)
from infona_client.graph.schema_bootstrap import (
    ENTITY_COUNT_BY_PRIMARY_TYPE_CYPHER,
    ENTITY_COUNT_BY_TYPE_CYPHER,
    ENTITY_GET_CYPHER,
    ENTITY_LIST_BY_TYPE_CYPHER,
    ENTITY_LIST_BY_TYPE_PAGE_CYPHER,
    ENTITY_MERGE_CYPHER,
    TEMPLATES,
)
from infona_client.graph.scope import GraphScope
from infona_client.graph.store import merge_scope_params, reset_graph_store_for_tests


TENANT_A = "iso-tenant-a"
TENANT_B = "iso-tenant-b"
KG_A = "kg-a"
KG_B = "kg-b"

# Unique markers so a coincidence substring cannot pass a leak assertion.
A_NAME = "ISO_NEO4J_A_alice_x7k9qp"
B_NAME = "ISO_NEO4J_B_bob_y8l0wr"
A_EMAIL = "ISO_NEO4J_A_email_a@example.test"
B_EMAIL = "ISO_NEO4J_B_email_b@example.test"

# Same id string in both tenants — isolation is (tenant_id, kg, id).
SHARED_ENTITY_ID = entity_uri("Person", "shared_person")


@pytest.fixture
def store():
    s = MemoryGraphStore()
    yield s
    asyncio.run(s.close())
    reset_graph_store_for_tests()


def _session(store: MemoryGraphStore, tenant: str, kg: str):
    return store.session(GraphScope.for_instance(tenant, kg))


async def _seed_person(
    session,
    *,
    entity_id: str,
    name: str,
    email: str,
) -> None:
    """Write type + literal Assertions (SoT) with dual-write cache."""
    await assert_fact(
        session,
        AssertionFact(
            subject_id=entity_id,
            kind="type",
            value="Person",
            source_url="https://iso.test/type",
            run_id="iso-seed",
        ),
        dual_write_cache=True,
    )
    await assert_fact(
        session,
        AssertionFact(
            subject_id=entity_id,
            kind="literal",
            property_leaf="name",
            value=name,
            source_url="https://iso.test/name",
            run_id="iso-seed",
        ),
        dual_write_cache=True,
    )
    await assert_fact(
        session,
        AssertionFact(
            subject_id=entity_id,
            kind="literal",
            property_leaf="email",
            value=email,
            source_url="https://iso.test/email",
            run_id="iso-seed",
        ),
        dual_write_cache=True,
    )


async def _seed_two_tenants(store: MemoryGraphStore) -> None:
    await store.bootstrap_schema()
    sess_a = _session(store, TENANT_A, KG_A)
    sess_b = _session(store, TENANT_B, KG_B)
    await _seed_person(
        sess_a, entity_id=SHARED_ENTITY_ID, name=A_NAME, email=A_EMAIL
    )
    await _seed_person(
        sess_b, entity_id=SHARED_ENTITY_ID, name=B_NAME, email=B_EMAIL
    )


# ---------------------------------------------------------------------------
# Template contract: list/count must use INSTANCE_OF → Class (not primary_type alone)
# ---------------------------------------------------------------------------


def test_explore_list_count_templates_use_instance_of_class():
    """Entity list/count SoT is INSTANCE_OF + Class (ADR 0013; known-bug pin)."""
    for name, cypher in (
        ("entity_list_by_type", ENTITY_LIST_BY_TYPE_CYPHER),
        ("entity_list_by_type_page", ENTITY_LIST_BY_TYPE_PAGE_CYPHER),
        ("entity_count_by_type", ENTITY_COUNT_BY_TYPE_CYPHER),
        ("entity_count_by_primary_type", ENTITY_COUNT_BY_PRIMARY_TYPE_CYPHER),
    ):
        assert name in TEMPLATES
        body = TEMPLATES[name].cypher
        assert body == cypher or body  # registry wired
        assert "INSTANCE_OF" in body, f"{name} must traverse INSTANCE_OF"
        assert ":Class" in body, f"{name} must match Class"
        # Must not filter solely on denorm primary_type property.
        assert "e.primary_type = $primary_type" not in body
        assert "WHERE e.primary_type" not in body
        assert "$tenant_id" in body and "$kg" in body


# ---------------------------------------------------------------------------
# Two tenants, two kgs — no cross-leak
# ---------------------------------------------------------------------------


def test_two_tenants_two_kgs_no_cross_leak(store):
    async def run():
        await _seed_two_tenants(store)
        sess_a = _session(store, TENANT_A, KG_A)
        sess_b = _session(store, TENANT_B, KG_B)

        # Same entity id string is distinct per (tenant, kg).
        a_get = await sess_a.execute_template(
            "entity_get", {"id": SHARED_ENTITY_ID}
        )
        b_get = await sess_b.execute_template(
            "entity_get", {"id": SHARED_ENTITY_ID}
        )
        assert len(a_get) == 1 and len(b_get) == 1
        assert a_get[0]["name"] == A_NAME
        assert b_get[0]["name"] == B_NAME
        assert a_get[0]["tenant_id"] == TENANT_A
        assert a_get[0]["kg"] == KG_A
        assert b_get[0]["tenant_id"] == TENANT_B
        assert b_get[0]["kg"] == KG_B

        # Assertion SoT: each session only sees its own literals.
        a_asserts = await session_assertions_for_subject(
            sess_a, SHARED_ENTITY_ID
        )
        b_asserts = await session_assertions_for_subject(
            sess_b, SHARED_ENTITY_ID
        )
        a_blob = str(a_asserts)
        b_blob = str(b_asserts)
        assert A_NAME in a_blob and A_EMAIL in a_blob
        assert B_NAME in b_blob and B_EMAIL in b_blob
        assert B_NAME not in a_blob and B_EMAIL not in a_blob
        assert A_NAME not in b_blob and A_EMAIL not in b_blob

        email_prop = property_uri("email")
        a_emails = await session_literal_values(
            sess_a, SHARED_ENTITY_ID, email_prop
        )
        b_emails = await session_literal_values(
            sess_b, SHARED_ENTITY_ID, email_prop
        )
        assert a_emails == [A_EMAIL]
        assert b_emails == [B_EMAIL]

        # Explore path: list / type_counts / count are tenant+kg scoped.
        page_a = await list_entities_by_type(
            store=store,
            tenant_id=TENANT_A,
            kg=KG_A,
            type_name="Person",
        )
        page_b = await list_entities_by_type(
            store=store,
            tenant_id=TENANT_B,
            kg=KG_B,
            type_name="Person",
        )
        assert page_a is not None and page_b is not None
        assert page_a.total == 1 and page_b.total == 1
        assert page_a.entities[0].name == A_NAME
        assert page_b.entities[0].name == B_NAME

        counts_a = await type_counts(
            store=store, tenant_id=TENANT_A, kg=KG_A
        )
        counts_b = await type_counts(
            store=store, tenant_id=TENANT_B, kg=KG_B
        )
        assert counts_a is not None and counts_b is not None
        assert {c.name: c.entity_count for c in counts_a} == {"Person": 1}
        assert {c.name: c.entity_count for c in counts_b} == {"Person": 1}

        n_a = await count_entities(store=store, tenant_id=TENANT_A, kg=KG_A)
        n_b = await count_entities(store=store, tenant_id=TENANT_B, kg=KG_B)
        assert n_a == 1 and n_b == 1

        # Peer tenant + peer kg must not see the other's row (cross product).
        leak_ab = await list_entities_by_type(
            store=store,
            tenant_id=TENANT_A,
            kg=KG_B,
            type_name="Person",
        )
        leak_ba = await list_entities_by_type(
            store=store,
            tenant_id=TENANT_B,
            kg=KG_A,
            type_name="Person",
        )
        assert leak_ab is not None and leak_ab.total == 0
        assert leak_ba is not None and leak_ba.total == 0

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Wrong kg empty
# ---------------------------------------------------------------------------


def test_wrong_kg_empty(store):
    async def run():
        await store.bootstrap_schema()
        sess = _session(store, TENANT_A, KG_A)
        await _seed_person(
            sess,
            entity_id=entity_uri("Person", "only_in_a"),
            name=A_NAME,
            email=A_EMAIL,
        )

        # Sibling kg under the same tenant: empty.
        wrong = _session(store, TENANT_A, "other-kg")
        rows = await wrong.execute_template(
            "entity_get", {"id": entity_uri("Person", "only_in_a")}
        )
        assert rows == []

        assert (
            await session_assertions_for_subject(
                wrong, entity_uri("Person", "only_in_a")
            )
            == []
        )
        assert (
            await session_entities_of_type(wrong, type_uri("Person"))
            == []
        )

        empty_page = await list_entities_by_type(
            store=store,
            tenant_id=TENANT_A,
            kg="other-kg",
            type_name="Person",
        )
        assert empty_page is not None
        assert empty_page.total == 0
        assert empty_page.entities == ()

        assert (
            await count_entities(
                store=store, tenant_id=TENANT_A, kg="other-kg"
            )
            == 0
        )
        assert (
            await type_counts(
                store=store, tenant_id=TENANT_A, kg="other-kg"
            )
            == []
        )

        # Right kg still has the entity.
        right_page = await list_entities_by_type(
            store=store,
            tenant_id=TENANT_A,
            kg=KG_A,
            type_name="Person",
        )
        assert right_page is not None and right_page.total == 1
        assert right_page.entities[0].name == A_NAME

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Session overwrites scope
# ---------------------------------------------------------------------------


def test_session_overwrites_scope_on_merge_params():
    """merge_scope_params always binds session tenant/kg (never caller values)."""
    scope = GraphScope.for_instance("real-tenant", "real-kg")
    bound = merge_scope_params(
        scope,
        {
            "tenant_id": "evil-tenant",
            "kg": "evil-kg",
            "id": SHARED_ENTITY_ID,
            "primary_type": "Person",
            "name": "X",
            "source": "unit",
            "ts": "t",
        },
        for_write=True,
    )
    assert bound["tenant_id"] == "real-tenant"
    assert bound["kg"] == "real-kg"
    assert bound["id"] == SHARED_ENTITY_ID


def test_session_overwrites_scope_on_entity_write(store):
    """execute_write / execute_template land under session scope, not params."""
    async def run():
        await store.bootstrap_schema()
        session = _session(store, TENANT_A, KG_A)
        eid = entity_uri("Person", "overwrite_probe")

        written = await session.execute_write(
            ENTITY_MERGE_CYPHER,
            {
                "id": eid,
                "primary_type": "Person",
                "name": "ShouldLandInA",
                "source": "iso",
                "ts": "2026-08-09T00:00:00Z",
                "tenant_id": "evil-tenant",
                "kg": "evil-kg",
            },
        )
        assert len(written) == 1
        assert written[0]["tenant_id"] == TENANT_A
        assert written[0]["kg"] == KG_A
        assert written[0]["name"] == "ShouldLandInA"

        # Template path also overwrites.
        written2 = await session.execute_template(
            "entity_merge",
            {
                "id": eid,
                "primary_type": "Person",
                "name": "StillInA",
                "source": "iso",
                "ts": "2026-08-09T00:00:01Z",
                "tenant_id": "other-evil",
                "kg": "other-evil-kg",
            },
        )
        assert written2[0]["tenant_id"] == TENANT_A
        assert written2[0]["kg"] == KG_A

        # Evil scope empty; real scope has the row.
        evil = _session(store, "evil-tenant", "evil-kg")
        assert (
            await evil.execute_read(ENTITY_GET_CYPHER, {"id": eid})
            == []
        )
        rows = await session.execute_read(ENTITY_GET_CYPHER, {"id": eid})
        assert len(rows) == 1
        assert rows[0]["tenant_id"] == TENANT_A
        assert rows[0]["kg"] == KG_A

    asyncio.run(run())


def test_session_overwrites_scope_on_assertion_write(store):
    """Assertion writes use session scope only (ADR 0013 SoT path)."""
    async def run():
        await store.bootstrap_schema()
        session = _session(store, TENANT_A, KG_A)
        eid = entity_uri("Person", "assert_scope")

        await assert_fact(
            session,
            AssertionFact(
                subject_id=eid,
                kind="type",
                value="Person",
                source_url="https://evil.example/should-not-set-scope",
            ),
            dual_write_cache=True,
        )
        await assert_fact(
            session,
            AssertionFact(
                subject_id=eid,
                kind="literal",
                property_leaf="email",
                value=A_EMAIL,
            ),
            dual_write_cache=True,
        )

        asserts = await session_assertions_for_subject(session, eid)
        assert asserts
        for row in asserts:
            assert row["tenant_id"] == TENANT_A
            assert row["kg"] == KG_A

        # Peer tenant must not see the Assertion rows.
        peer = _session(store, TENANT_B, KG_A)
        assert await session_assertions_for_subject(peer, eid) == []
        assert await session_entities_of_type(peer, type_uri("Person")) == []

        # Explore list via INSTANCE_OF finds the entity only in the real scope.
        page = await list_entities_by_type(
            store=store,
            tenant_id=TENANT_A,
            kg=KG_A,
            type_name="Person",
        )
        assert page is not None and page.total == 1
        assert page.entities[0].id == eid

        wrong = await list_entities_by_type(
            store=store,
            tenant_id=TENANT_B,
            kg=KG_A,
            type_name="Person",
        )
        assert wrong is not None and wrong.total == 0

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Assertion-derived list/count (INSTANCE_OF dual-write from type Assertion)
# ---------------------------------------------------------------------------


def test_list_count_after_type_assertion_uses_instance_of(store):
    """Type Assertion dual-writes INSTANCE_OF so explore list/count work."""
    async def run():
        await store.bootstrap_schema()
        session = _session(store, TENANT_A, KG_A)
        alice = entity_uri("Person", "alice")
        bob = entity_uri("Person", "bob")
        acme = entity_uri("Organization", "acme")

        for eid, t, name in (
            (alice, "Person", "Alice"),
            (bob, "Person", "Bob"),
            (acme, "Organization", "Acme"),
        ):
            await assert_fact(
                session,
                AssertionFact(subject_id=eid, kind="type", value=t),
                dual_write_cache=True,
            )
            await assert_fact(
                session,
                AssertionFact(
                    subject_id=eid,
                    kind="literal",
                    property_leaf="name",
                    value=name,
                ),
                dual_write_cache=True,
            )

        people = await list_entities_by_type(
            store=store,
            tenant_id=TENANT_A,
            kg=KG_A,
            type_name="Person",
        )
        assert people is not None
        assert people.total == 2
        assert {e.name for e in people.entities} == {"Alice", "Bob"}

        orgs = await list_entities_by_type(
            store=store,
            tenant_id=TENANT_A,
            kg=KG_A,
            type_name="Organization",
        )
        assert orgs is not None and orgs.total == 1
        assert orgs.entities[0].name == "Acme"

        counts = await type_counts(
            store=store, tenant_id=TENANT_A, kg=KG_A
        )
        assert counts is not None
        assert {c.name: c.entity_count for c in counts} == {
            "Person": 2,
            "Organization": 1,
        }
        assert await count_entities(
            store=store, tenant_id=TENANT_A, kg=KG_A
        ) == 3

    asyncio.run(run())

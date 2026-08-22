"""Live Neo4j smoke for the ONTA-279 suppression marker (E7 port).

The hermetic suite drives ``MemoryGraphStore``, which never executes Cypher — so
a green ``tests/test_suppression_store_port.py`` says nothing about whether the
``:Suppression`` MERGE/MATCH statements are valid Neo4j 5. This file runs them
against a real server (the ``live-neo4j`` job in ``.github/workflows/neo4j.yml``).

Skipped unless ``NEO4J_URI`` and ``NEO4J_PASSWORD`` are set **and** the ``neo4j``
package is importable. Run::

    docker compose up -d neo4j
    export NEO4J_URI=bolt://localhost:7687
    export NEO4J_USER=neo4j
    export NEO4J_PASSWORD=infona-dev-password
    pytest -m neo4j -q
"""

from __future__ import annotations

import os
import uuid

import pytest

from infona_client.graph.scope import GraphScope
from infona_client.graph.store import env_neo4j_configured

pytestmark = pytest.mark.neo4j

XSD_INT = "http://www.w3.org/2001/XMLSchema#integer"


def _neo4j_available() -> bool:
    if not env_neo4j_configured():
        return False
    try:
        import neo4j  # noqa: F401
    except ImportError:
        return False
    return True


requires_neo4j = pytest.mark.skipif(
    not _neo4j_available(),
    reason="NEO4J_URI + NEO4J_PASSWORD and neo4j package required",
)


@pytest.fixture
async def neo4j_store():
    from infona_client.graph.neo4j_store import Neo4jGraphStore

    uri = os.environ["NEO4J_URI"].strip()
    user = (os.environ.get("NEO4J_USER") or "neo4j").strip() or "neo4j"
    password = os.environ["NEO4J_PASSWORD"]
    database = (os.environ.get("NEO4J_DATABASE") or "").strip() or None
    store = Neo4jGraphStore(uri=uri, user=user, password=password, database=database)
    if not await store.health():
        await store.close()
        pytest.skip("Neo4j not reachable at NEO4J_URI")
    try:
        yield store
    finally:
        await store.close()


@requires_neo4j
@pytest.mark.asyncio
async def test_suppression_roundtrip_is_scoped_idempotent_and_term_faithful(
    neo4j_store,
):
    """The real Cypher: bootstrap → MERGE → MATCH, on a live server.

    Covers everything the memory store cannot prove: that the statements parse,
    that the composite constraint applies, that ``MERGE … SET … coalesce()`` is
    idempotent and non-clobbering, and that scope + term matching behave against
    the actual query planner.
    """
    applied = await neo4j_store.bootstrap_schema()
    assert "suppression_tenant_kg_mark_unique" in applied
    assert "suppression_subject_lookup" in applied

    # Unique per run so re-runs on a dirty volume still pass.
    tenant = f"itest-{uuid.uuid4().hex[:10]}"
    kg = "suppression"
    subject = f"https://graph.infona.ai/entities/Widget/{uuid.uuid4().hex[:8]}"
    predicate = "https://graph.infona.ai/types/Widget/attrs/sku"
    session = neo4j_store.session(GraphScope.for_instance(tenant, kg))

    await session.write_suppression(
        mark_id=f"https://graph.infona.ai/suppression/mark/{uuid.uuid4().hex}",
        kind="fact",
        statement_id="abc123",
        subject=subject,
        predicate=predicate,
        object_repr="WX-RECALLED",
        reason="retract",
        suppressed_at="2026-01-02T00:00:00+00:00",
        graph_uri=f"https://graph.infona.ai/graphs/{tenant}/kg/{kg}",
    )

    rows = await session.read_suppressions(
        kind="fact", subject=subject, predicate=predicate
    )
    assert len(rows) == 1
    assert rows[0]["object_repr"] == "WX-RECALLED"
    assert rows[0]["reason"] == "retract"

    # A FACT mark must not answer the ENTITY question.
    assert await session.read_suppressions(kind="entity") == []

    # Sibling scopes must not see the mark (isolation, both axes).
    other_kg = neo4j_store.session(GraphScope.for_instance(tenant, "other-kg"))
    assert await other_kg.read_suppressions(kind="fact", subject=subject) == []
    other_tenant = neo4j_store.session(
        GraphScope.for_instance(f"{tenant}-other", kg)
    )
    assert await other_tenant.read_suppressions(kind="fact", subject=subject) == []


@requires_neo4j
@pytest.mark.asyncio
async def test_re_merging_a_mark_is_idempotent_and_does_not_blank_annotations(
    neo4j_store,
):
    """``MERGE`` on the sha1-keyed mark id collapses onto one node, and a later
    write that omits ``reason`` must not erase the stored one (``coalesce``)."""
    await neo4j_store.bootstrap_schema()

    tenant = f"itest-{uuid.uuid4().hex[:10]}"
    kg = "suppression"
    subject = f"https://graph.infona.ai/entities/Widget/{uuid.uuid4().hex[:8]}"
    predicate = "https://graph.infona.ai/types/Widget/attrs/qty"
    mark_id = f"https://graph.infona.ai/suppression/mark/{uuid.uuid4().hex}"
    session = neo4j_store.session(GraphScope.for_instance(tenant, kg))

    typed = f"42^^{XSD_INT}"
    for reason in ("retract", ""):
        await session.write_suppression(
            mark_id=mark_id,
            kind="fact",
            subject=subject,
            predicate=predicate,
            object_repr=typed,
            reason=reason,
        )

    rows = await session.read_suppressions(kind="fact", subject=subject)
    assert len(rows) == 1, "re-suppressing the same value must MERGE, not duplicate"
    assert rows[0]["reason"] == "retract", "coalesce must not blank an annotation"
    # Term faithfulness against the real store: the typed literal keeps its tail,
    # so it can never be matched by the plain string "42".
    assert rows[0]["object_repr"] == typed


@requires_neo4j
@pytest.mark.asyncio
async def test_entity_tombstone_is_readable_after_the_entity_is_gone(neo4j_store):
    """An ENTITY mark is a tombstone: it carries no edge to the subject, so it
    stays readable when no such ``:Entity`` node exists at all."""
    await neo4j_store.bootstrap_schema()

    tenant = f"itest-{uuid.uuid4().hex[:10]}"
    kg = "suppression"
    subject = f"https://graph.infona.ai/entities/Widget/{uuid.uuid4().hex[:8]}"
    session = neo4j_store.session(GraphScope.for_instance(tenant, kg))

    await session.write_suppression(
        mark_id=f"https://graph.infona.ai/suppression/entity/{uuid.uuid4().hex}",
        kind="entity",
        subject=subject,
        reason="gdpr erasure",
    )

    rows = await session.read_suppressions(kind="entity")
    assert [r["subject"] for r in rows] == [subject]
    # …and it does not answer a (s, p, o) fact question.
    assert await session.read_suppressions(kind="fact", subject=subject) == []

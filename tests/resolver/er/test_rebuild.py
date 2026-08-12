"""Tests for the second-pass `er rebuild` (MOE-22).

The cluster computation is pure (no graph), so most of this exercises
compute_clusters / choose_canonical directly. A couple of async tests drive
rebuild_type against a seeded store to cover the orchestration + idempotency
end-to-end. Since ADR 0007 the merge is owned by `kg_writer.rewrite_subject`, so
the two-direction move is asserted through that primitive.

ONTA-527: `rewrite_subject` now re-keys through `pg_ops.rewrite_entity_id` — the
`queries.rewrite_subject_update` SPARQL builder it used to compose is no longer
called by any writer. The two-direction assertion moved onto the primitive
itself (see `test_er_merge_moves_facts_in_both_directions`), which is the
behavior that ever mattered; asserting the dead builder's string would prove
nothing about a merge.
"""

from __future__ import annotations

import pytest

from infona_client.resolver.er.rebuild import (
    choose_canonical,
    compute_clusters,
    rebuild_type,
)
from infona_client.resolver.er.types import DEFAULT_GUEST_CONFIG, NormalizedSignals


def _ns(**kwargs) -> NormalizedSignals:
    return NormalizedSignals(**kwargs)


# Three fragments of ONE human: "John Smith", "Jon Smith", "J. Smith". They
# share an email (decisive) and/or a name-block. Distinct PMS rows that ingest
# minted as separate URIs because they couldn't see each other mid-batch.
JOHN_A = _ns(name="john smith", name_tokens=("john", "smith"),
             email="john.smith0@gmail.com", email_local="johnsmith0",
             phone_e164="+442258595506")
JOHN_B = _ns(name="jon smith", name_tokens=("jon", "smith"),
             email="john.smith0@gmail.com", email_local="johnsmith0",
             phone_e164="+442258595506")
JOHN_C = _ns(name="john smith", name_tokens=("john", "smith"),
             phone_e164="+442258595506")  # no email, but shares lastname+phone block

# A DIFFERENT human who also happens to be named John Smith — different email,
# different phone. Must NEVER merge with the cluster above.
OTHER_JOHN = _ns(name="john smith", name_tokens=("john", "smith"),
                 email="jsmith99@yahoo.com", email_local="jsmith99",
                 phone_e164="+15551234000")


def test_collapses_fragments_of_one_human() -> None:
    entities = {
        "uri:johnA": JOHN_A,
        "uri:johnB": JOHN_B,
        "uri:johnC": JOHN_C,
    }
    clusters = compute_clusters(entities, DEFAULT_GUEST_CONFIG)
    assert len(clusters) == 1
    assert set(clusters[0]) == {"uri:johnA", "uri:johnB", "uri:johnC"}


def test_zero_false_merge_across_distinct_humans() -> None:
    # Two unrelated John Smiths in the population alongside the real cluster.
    entities = {
        "uri:johnA": JOHN_A,
        "uri:johnB": JOHN_B,
        "uri:other": OTHER_JOHN,
    }
    clusters = compute_clusters(entities, DEFAULT_GUEST_CONFIG)
    # Exactly one cluster (the real John), and it must not include the other.
    assert len(clusters) == 1
    assert "uri:other" not in clusters[0]
    assert set(clusters[0]) == {"uri:johnA", "uri:johnB"}


def test_no_clusters_when_all_distinct() -> None:
    entities = {
        "uri:a": _ns(name="alice brown", name_tokens=("alice", "brown"),
                     email="alice@x.com", email_local="alice"),
        "uri:b": _ns(name="bob green", name_tokens=("bob", "green"),
                     email="bob@y.com", email_local="bob"),
        "uri:other": OTHER_JOHN,
    }
    assert compute_clusters(entities, DEFAULT_GUEST_CONFIG) == []


def test_singletons_are_omitted() -> None:
    entities = {"uri:only": JOHN_A}
    assert compute_clusters(entities, DEFAULT_GUEST_CONFIG) == []


def test_choose_canonical_prefers_richest_then_stable() -> None:
    entities = {
        "uri:rich": JOHN_A,   # name + email + phone
        "uri:poor": JOHN_C,   # name + phone only
    }
    # The signal-richer entity wins regardless of URI ordering.
    assert choose_canonical(["uri:poor", "uri:rich"], entities) == "uri:rich"
    # Deterministic tie-break: equal richness → smallest URI.
    tie = {"uri:zzz": JOHN_A, "uri:aaa": JOHN_A}
    assert choose_canonical(["uri:zzz", "uri:aaa"], tie) == "uri:aaa"


# ---------------------------------------------------------------------------
# Async orchestration against a seeded store
#
# ONTA-527: these used to hand ``rebuild_type`` a fake Neptune that served
# SPARQL bindings and collected the merge SPARQL. Neo4j is the only backend, so
# the population is SEEDED into the process store (the ER index facts a real
# ingest writes, through the same ``insert_facts`` write path) and the merge is
# asserted on the graph: the loser's node is gone, its facts are on the
# canonical node, and the unrelated John is untouched. ``neptune`` is passed as
# None throughout — nothing on this path may call it.
# ---------------------------------------------------------------------------


TENANT = "test-tenant"
KG = "hotel"
INSTANCE_GRAPH = f"https://graph.infona.ai/graphs/{TENANT}/kg/{KG}"
PERSON_TYPE_URI = "https://graph.infona.ai/types/Person"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
ER_NS = "https://graph.infona.ai/er/"


async def _seed_person(uri: str, *, extra=None, **signals) -> None:
    """Write one Person node plus the ER index facts ingest would have written.

    Block keys come from the same ``generate_block_keys`` the rebuild re-blocks
    with, so the seeded population is exactly what a prior ingest leaves behind.
    """
    from infona_client.graph.kg_writer import insert_facts
    from infona_client.resolver.er.blocking import SparqlBlocker, generate_block_keys
    from infona_client.resolver.er.normalize import DefaultNormalizer
    from infona_client.resolver.er.types import EntitySignals

    normalized = DefaultNormalizer().normalize(
        EntitySignals(
            name=signals.get("name"),
            email=signals.get("email"),
            phone=signals.get("phone"),
        )
    )
    triples = [
        (uri, RDF_TYPE, PERSON_TYPE_URI),
        (uri, RDFS_LABEL, signals.get("name") or uri),
    ]
    triples.extend(extra or [])
    triples.extend(
        SparqlBlocker.index_triples(uri, normalized, generate_block_keys(normalized))
    )
    await insert_facts(None, INSTANCE_GRAPH, triples)


async def _person_ids() -> list[str]:
    from infona_client.graph.explore_store import list_entities_by_type

    page = await list_entities_by_type(
        tenant_id=TENANT, kg=KG, type_name="Person"
    )
    return sorted(e.id for e in page.entities)


async def _detail(uri: str):
    from infona_client.graph.explore_store import get_entity_detail

    return await get_entity_detail(tenant_id=TENANT, kg=KG, entity_id=uri)


JOHN_A_URI = "https://graph.infona.ai/entities/Person/johnA"
JOHN_B_URI = "https://graph.infona.ai/entities/Person/johnB"
OTHER_URI = "https://graph.infona.ai/entities/Person/other"


@pytest.mark.asyncio
async def test_rebuild_type_merges_and_reports() -> None:
    await _seed_person(
        JOHN_A_URI, name="John Smith", email="john.smith0@gmail.com",
        phone="+442258595506",
    )
    await _seed_person(
        JOHN_B_URI, name="Jon Smith", email="john.smith0@gmail.com",
        phone="+442258595506",
        # A fact only the fragment carries — it must survive the merge on the
        # canonical node (rewrite_subject re-keys, it does not delete+insert).
        extra=[(JOHN_B_URI, "https://graph.infona.ai/types/Person/attrs/room", "512")],
    )
    await _seed_person(
        OTHER_URI, name="John Smith", email="jsmith99@yahoo.com",
        phone="+15551234000",
    )

    report = await rebuild_type(
        None, INSTANCE_GRAPH, "Person", PERSON_TYPE_URI, DEFAULT_GUEST_CONFIG,
    )
    assert report["entities_before"] == 3
    assert report["entities_after"] == 2          # johnA + johnB collapse; other stays
    assert report["clusters_merged"] == 1
    assert report["fragments_absorbed"] == 1

    # The merge landed in the graph: the fragment's node is gone, and the fact
    # it alone carried now hangs off the canonical node.
    assert await _person_ids() == sorted([JOHN_A_URI, OTHER_URI])
    assert await _detail(JOHN_B_URI) is None
    canonical = await _detail(JOHN_A_URI)
    assert canonical is not None
    assert canonical.properties.get("room") == "512"
    # The "other" John was never a merge loser — untouched, still its own node.
    other = await _detail(OTHER_URI)
    assert other is not None
    assert other.properties.get("erSignal_email") == "jsmith99@yahoo.com"


@pytest.mark.asyncio
async def test_rebuild_type_idempotent_on_distinct_population() -> None:
    await _seed_person(
        "https://graph.infona.ai/entities/Person/a",
        name="Alice Brown", email="alice@x.com",
    )
    await _seed_person(
        "https://graph.infona.ai/entities/Person/b",
        name="Bob Green", email="bob@y.com",
    )
    before = await _person_ids()

    report = await rebuild_type(
        None, INSTANCE_GRAPH, "Person", PERSON_TYPE_URI, DEFAULT_GUEST_CONFIG,
    )
    assert report["fragments_absorbed"] == 0
    assert report["entities_after"] == 2
    # Nothing to merge → the population is byte-for-byte the same afterwards.
    assert await _person_ids() == before


@pytest.mark.asyncio
async def test_er_merge_moves_facts_in_both_directions() -> None:
    """A merge moves the loser's OUTGOING edges and the INCOMING references to
    it onto the canonical — the property the deleted SPARQL builder encoded as
    two DELETE/INSERT pairs, asserted here on the graph the merge produces."""
    from infona_client.graph.kg_writer import insert_facts, rewrite_subject

    loser = "https://graph.infona.ai/entities/Person/loser"
    canon = "https://graph.infona.ai/entities/Person/canon"
    room = "https://graph.infona.ai/entities/Room/512"
    booker = "https://graph.infona.ai/entities/Person/booker"
    stayed_in = "https://graph.infona.ai/onto/stayed_in"
    booked_for = "https://graph.infona.ai/onto/booked_for"

    await insert_facts(
        None,
        INSTANCE_GRAPH,
        [
            (loser, RDF_TYPE, PERSON_TYPE_URI),
            (loser, RDFS_LABEL, "John Smith"),
            (canon, RDF_TYPE, PERSON_TYPE_URI),
            (canon, RDFS_LABEL, "John Smith"),
            (room, RDF_TYPE, "https://graph.infona.ai/types/Room"),
            (booker, RDF_TYPE, PERSON_TYPE_URI),
            # outgoing: loser -> room
            (loser, stayed_in, room),
            # incoming: booker -> loser
            (booker, booked_for, loser),
        ],
    )

    await rewrite_subject(
        None, INSTANCE_GRAPH, loser, canon,
        touched_types=["Person"], reason="er-merge test",
    )

    assert await _detail(loser) is None
    merged = await _detail(canon)
    assert merged is not None
    # ...outgoing edge of the loser now hangs off the canonical...
    assert [(r.attr, r.other_id) for r in merged.outgoing] == [("stayed_in", room)]
    # ...and the incoming reference repointed at the canonical.
    assert [(r.attr, r.other_id) for r in merged.incoming] == [("booked_for", booker)]

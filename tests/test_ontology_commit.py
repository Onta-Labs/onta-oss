"""ONTA-403 — commit_ontology body, extended fingerprint, concurrency, revision.

Pure-seam tests (no store) cover fingerprint discrimination, order-
independence, and timestamp-independence.

**Ported by ONTA-527.** The async half of this file used to drive
:func:`commit_ontology` through a ~240-line in-file SPARQL emulator
(``MemNeptune``) that parsed the ``INSERT DATA`` / ``DELETE WHERE`` strings the
:mod:`infona_client.graph.ontology_queries` builders emit and answered the
SELECT shapes they use. Production is Neo4j-only and ``commit_ontology`` now
takes its GraphStore branch (``_commit_ontology_graph_store``) whenever a store
is configured — which it always is in production and, via the autouse
``_hermetic_graph_store`` fixture, in this suite. Every one of those tests
therefore asserted a transport that no longer runs, so the emulator is deleted
and each case is re-expressed against the shipped path: commit through the real
function, then read the result back out of the ontology catalog
(``:OntoType`` / ``:OntoAttr``) that production actually stores it in.

Re-expressing them made the size of the gap explicit. The GraphStore branch
implements four of the thirteen :class:`OntologyOpKind` ops
(``UPSERT_TYPE`` / ``UPSERT_ATTRIBUTE`` / ``UPSERT_RELATIONSHIP`` /
``SET_SUBCLASS``); the other nine hit its ``else:`` arm, which logs
``ontology_store_op_skipped`` and moves on — no exception, no change record, and
``applied`` silently missing the op. It also returns the literal string
``"neo4j"`` as both ``version_before`` and ``version_after``, so the ONTA-403
concurrency token and the changelog/revision writes are gone. The cases that
covered those are kept as ``strict=True`` xfails naming the mechanism, so the
day someone ports them the xpass fails CI and the markers come off.
"""

from __future__ import annotations

import asyncio

import pytest

from infona_client.graph import ontology_catalog as oc
from infona_client.graph.ontology_commit import (
    OntologyGraphImmutable,
    OntologyVersionConflict,
    changelog_graph_uri_for,
    commit_ontology,
    ontology_write_lock,
    versions_graph_uri,
)
from infona_client.graph.ontology_queries import ontology_version
from infona_client.models.ontology import (
    ChangeKind,
    OntologyMutation,
    OntologyOpKind,
)

# Workspace ontology graph URIs. The GraphStore branch recovers the tenant from
# the `/graphs/<tenant>` segment and writes the tenant catalog scope.
TENANT_ID = "t"
GRAPH = f"https://graph.infona.ai/graphs/{TENANT_ID}"
PEER_TENANT_ID = "t-peer"
PEER_GRAPH = f"https://graph.infona.ai/graphs/{PEER_TENANT_ID}"

# `name` is a RESERVED_ENTITY_PROPERTY_KEY on the property graph (graph/facts.py)
# and is rejected at schema time, so the fixtures use `full_name`.
FULL_NAME = "full_name"


def _upsert_type(name: str, **kw) -> OntologyMutation:
    return OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name=name, **kw)


def _upsert_attr(type_name: str, slot: str, datatype: str = "string", **kw):
    return OntologyMutation(
        op=OntologyOpKind.UPSERT_ATTRIBUTE,
        type_name=type_name,
        slot_name=slot,
        datatype=datatype,
        **kw,
    )


async def _types(tenant_id: str = TENANT_ID) -> dict[str, oc.OntoTypeRecord]:
    return {t.name: t for t in await oc.list_types(tenant_id=tenant_id)}


async def _attrs(tenant_id: str = TENANT_ID) -> dict[tuple[str, str], oc.OntoAttrRecord]:
    return {
        (a.domain, a.name): a for a in await oc.list_attributes(tenant_id=tenant_id)
    }


# ---------------------------------------------------------------------------
# Fingerprint discrimination (pure)
# ---------------------------------------------------------------------------


def test_empty_fingerprint_unchanged():
    assert ontology_version({}, {}) == "e3b0c44298fc1c14"


def test_fingerprint_discriminates_type_comment():
    base = ontology_version({"Person": ""}, {})
    with_comment = ontology_version({"Person": "a human being"}, {})
    assert base != with_comment
    # same comment via explicit comments map
    via_map = ontology_version({"Person": ""}, {}, comments={"Person": "a human being"})
    assert with_comment == via_map or via_map != base  # either channel shifts


def test_fingerprint_discriminates_attr_comment():
    class S:
        def __init__(self, dt, desc=""):
            self.datatype = dt
            self.description = desc

    bare = ontology_version({"P": ""}, {"P": {"name": S("string")}})
    noted = ontology_version({"P": ""}, {"P": {"name": S("string", "display name")}})
    assert bare != noted


def test_fingerprint_discriminates_core_slot():
    base = ontology_version({"P": ""}, {"P": {"name": "string"}})
    core = ontology_version(
        {"P": ""}, {"P": {"name": "string"}}, core_slots=[("P", "name")]
    )
    assert base != core


def test_fingerprint_discriminates_text_kind():
    base = ontology_version({"P": ""}, {"P": {"bio": "string"}})
    tk = ontology_version(
        {"P": ""},
        {"P": {"bio": "string"}},
        text_kinds={("P", "bio"): "free_text"},
    )
    assert base != tk


def test_fingerprint_discriminates_range_change():
    """Relationship ranges already covered by datatype channel."""
    lit = ontology_version({"P": ""}, {"P": {"employer": "string"}})
    rel = ontology_version({"P": ""}, {"P": {"employer": "Company"}})
    assert lit != rel


def test_fingerprint_order_independent_with_extensions():
    a = ontology_version(
        {"B": "bb", "A": "aa"},
        {"A": {"y": "string", "x": "integer"}},
        {"A": "Base"},
        core_slots=[("A", "x"), ("A", "y")],
        text_kinds={("A", "y"): "free_text", ("A", "x"): "not_text"},
    )
    b = ontology_version(
        {"A": "aa", "B": "bb"},
        {"A": {"x": "integer", "y": "string"}},
        {"A": "Base"},
        core_slots=[("A", "y"), ("A", "x")],
        text_kinds={("A", "x"): "not_text", ("A", "y"): "free_text"},
    )
    assert a == b


def test_fingerprint_timestamp_independent():
    """No wall-clock in the digest — two calls agree."""
    kwargs = dict(
        types={"P": "person"},
        attrs={"P": {"n": "string"}},
        parent_of={},
        core_slots=[("P", "n")],
        text_kinds={("P", "n"): "free_text"},
    )
    assert ontology_version(**kwargs) == ontology_version(**kwargs)


def test_companion_graph_uri_helpers():
    assert versions_graph_uri(GRAPH) == f"{GRAPH}/versions"
    assert changelog_graph_uri_for(GRAPH) == f"{GRAPH}/changelog"
    assert versions_graph_uri(GRAPH + "/") == f"{GRAPH}/versions"


# ---------------------------------------------------------------------------
# commit_ontology on the shipped (GraphStore) path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_commit_is_a_no_op():
    r = await commit_ontology(None, GRAPH, [])
    assert r.applied == []
    assert r.change_records == []
    assert r.version_before == r.version_after
    assert await oc.list_types(tenant_id=TENANT_ID) == []


@pytest.mark.asyncio
async def test_commit_upsert_type_lands_in_the_tenant_catalog():
    r = await commit_ontology(
        None,
        GRAPH,
        [_upsert_type("Person", description="a human being")],
        actor="tester",
        message="add Person",
    )
    assert len(r.applied) == 1
    assert any(c.kind is ChangeKind.ADD_TYPE for c in r.change_records)

    types = await _types()
    assert set(types) == {"Person"}
    assert types["Person"].description == "a human being"
    assert types["Person"].layer == "tenant"
    assert types["Person"].tenant_id == TENANT_ID


@pytest.mark.asyncio
async def test_commit_batch_applies_every_op():
    """A multi-type ingest-style batch is ONE commit; every op lands."""
    muts = [
        _upsert_type("Person"),
        _upsert_type("Company"),
        _upsert_type("Employee"),
        _upsert_attr("Person", FULL_NAME),
        OntologyMutation(
            op=OntologyOpKind.SET_SUBCLASS,
            type_name="Employee",
            parent_type="Person",
        ),
    ]
    r = await commit_ontology(None, GRAPH, muts, message="multi-type batch")
    assert len(r.applied) == 5

    types = await _types()
    assert set(types) == {"Person", "Company", "Employee"}
    assert types["Employee"].parent_type == "Person"
    assert ("Person", FULL_NAME) in await _attrs()
    kinds = {c.kind for c in r.change_records}
    assert {ChangeKind.ADD_TYPE, ChangeKind.ADD_ATTRIBUTE, ChangeKind.ADD_SUBCLASS} <= kinds


@pytest.mark.asyncio
async def test_commit_distinguishes_literal_attributes_from_relationships():
    await commit_ontology(
        None,
        GRAPH,
        [
            _upsert_type("Person"),
            _upsert_type("Company"),
            _upsert_attr("Person", FULL_NAME, "string"),
            OntologyMutation(
                op=OntologyOpKind.UPSERT_RELATIONSHIP,
                type_name="Person",
                slot_name="employer",
                target_type="Company",
                description="works at",
            ),
        ],
    )
    attrs = await _attrs()
    literal = attrs[("Person", FULL_NAME)]
    assert literal.kind == "literal"
    assert literal.datatype == "string"

    rel = attrs[("Person", "employer")]
    assert rel.kind == "relationship"
    assert rel.range_type == "Company"


@pytest.mark.asyncio
async def test_commit_is_confined_to_the_target_tenants_catalog():
    """TENANT ISOLATION: a schema write names one workspace's graph and lands
    in that workspace's catalog only."""
    await commit_ontology(None, GRAPH, [_upsert_type("Person")])
    await commit_ontology(None, PEER_GRAPH, [_upsert_type("Invoice")])

    assert set(await _types()) == {"Person"}
    assert set(await _types(PEER_TENANT_ID)) == {"Invoice"}


@pytest.mark.asyncio
async def test_commit_refuses_an_immutable_version_graph():
    """Published release / revision snapshot graphs stay immutable (ONTA-406).

    The refusal precedes the backend branch, so it survived the port — it is
    checked here because it is the one write-refusal a client can still hit.
    """
    for immutable in (f"{GRAPH}/v1", f"{GRAPH}/revisions/r3"):
        with pytest.raises(OntologyGraphImmutable):
            await commit_ontology(None, immutable, [_upsert_type("X")])
    assert await oc.list_types(tenant_id=TENANT_ID) == []


@pytest.mark.asyncio
async def test_concurrent_commits_serialize_on_the_shared_lock():
    """Two concurrent commits serialize on the ONE process lock; both land."""
    assert ontology_write_lock() is ontology_write_lock()

    async def add(name: str):
        return await commit_ontology(None, GRAPH, [_upsert_type(name)], actor=name)

    r1, r2 = await asyncio.gather(add("Alpha"), add("Beta"))
    assert r1.applied and r2.applied
    assert set(await _types()) == {"Alpha", "Beta"}


# ---------------------------------------------------------------------------
# Behaviour the GraphStore port dropped — strict xfails, not deletions.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_bumps_the_ontology_version():
    before = await commit_ontology(None, GRAPH, [])
    after = await commit_ontology(None, GRAPH, [_upsert_type("Person")])
    assert after.version_before == before.version_after
    assert after.version_after != after.version_before


@pytest.mark.asyncio
async def test_commit_to_a_global_layer_graph_writes_the_global_catalog():
    await commit_ontology(
        None, "https://graph.infona.ai/graphs/global/public", [_upsert_type("Hotel")]
    )
    assert {t.name for t in await oc.list_types(layer="public")} == {"Hotel"}
    assert await oc.list_types(tenant_id="global") == []


@pytest.mark.asyncio
async def test_stale_expected_version_is_rejected():
    first = await commit_ontology(None, GRAPH, [_upsert_type("A")])
    with pytest.raises(OntologyVersionConflict):
        await commit_ontology(
            None,
            GRAPH,
            [_upsert_type("B")],
            expected_version=first.version_before,  # stale
        )


@pytest.mark.asyncio
async def test_delete_attribute_removes_the_declaration():
    await commit_ontology(
        None, GRAPH, [_upsert_type("Person"), _upsert_attr("Person", FULL_NAME)]
    )
    r = await commit_ontology(
        None,
        GRAPH,
        [
            OntologyMutation(
                op=OntologyOpKind.DELETE_ATTRIBUTE,
                type_name="Person",
                slot_name=FULL_NAME,
            )
        ],
    )
    assert len(r.applied) == 1
    assert ("Person", FULL_NAME) not in await _attrs()


@pytest.mark.asyncio
async def test_core_slot_and_text_kind_ops_are_applied():
    r = await commit_ontology(
        None,
        GRAPH,
        [
            _upsert_type("Doc"),
            _upsert_attr("Doc", "body"),
            OntologyMutation(
                op=OntologyOpKind.SET_TEXT_KIND,
                type_name="Doc",
                slot_name="body",
                text_kind="free_text",
            ),
            OntologyMutation(
                op=OntologyOpKind.SET_CORE_SLOT,
                type_name="Doc",
                slot_name="body",
                core_slot=True,
            ),
        ],
    )
    kinds = {c.kind for c in r.change_records}
    assert ChangeKind.CHANGE_TEXT_KIND in kinds
    assert ChangeKind.CHANGE_CORE_SLOT in kinds


@pytest.mark.asyncio
async def test_register_alias_records_the_alias():
    await commit_ontology(
        None,
        GRAPH,
        [
            _upsert_type("Guest"),
            _upsert_attr("Guest", "phone"),
            _upsert_attr("Guest", "phone_num"),
        ],
    )
    r = await commit_ontology(
        None,
        GRAPH,
        [
            OntologyMutation(
                op=OntologyOpKind.REGISTER_ALIAS,
                type_name="Guest",
                alias_from="phone_num",
                alias_to="phone",
            )
        ],
        actor="test",
        message="rename phone_num → phone",
    )
    assert any(
        c.kind is ChangeKind.RENAME_WITH_ALIAS
        and c.from_name == "phone_num"
        and c.to_name == "phone"
        for c in r.change_records
    )


@pytest.mark.asyncio
async def test_rename_attribute_moves_the_declaration():
    await commit_ontology(
        None, GRAPH, [_upsert_type("Guest"), _upsert_attr("Guest", "phone_num")]
    )
    r = await commit_ontology(
        None,
        GRAPH,
        [
            OntologyMutation(
                op=OntologyOpKind.RENAME_ATTRIBUTE,
                type_name="Guest",
                alias_from="phone_num",
                alias_to="phone",
                datatype="string",
            )
        ],
    )
    kinds = {c.kind for c in r.change_records}
    assert ChangeKind.RENAME_WITH_ALIAS in kinds
    attrs = await _attrs()
    assert ("Guest", "phone") in attrs
    assert ("Guest", "phone_num") not in attrs


@pytest.mark.asyncio
async def test_alias_ops_still_validate_their_arguments():
    with pytest.raises(ValueError, match="alias_from and alias_to"):
        await commit_ontology(
            None,
            GRAPH,
            [OntologyMutation(op=OntologyOpKind.REGISTER_ALIAS, type_name="Guest")],
        )
    with pytest.raises(ValueError, match="different attribute"):
        await commit_ontology(
            None,
            GRAPH,
            [
                OntologyMutation(
                    op=OntologyOpKind.REGISTER_ALIAS,
                    type_name="Guest",
                    alias_from="phone",
                    alias_to="phone",
                )
            ],
        )
    with pytest.raises(ValueError, match="data_graph_uri"):
        await commit_ontology(
            None,
            GRAPH,
            [
                OntologyMutation(
                    op=OntologyOpKind.RETIRE_ALIAS,
                    type_name="Guest",
                    alias_from="phone_num",
                )
            ],
        )
    with pytest.raises(ValueError, match="type renames are not supported"):
        await commit_ontology(
            None,
            GRAPH,
            [
                OntologyMutation(
                    op=OntologyOpKind.RENAME_ATTRIBUTE,
                    type_name="Guest",
                    alias_from="https://graph.infona.ai/types/Guest",
                    alias_to="https://graph.infona.ai/types/Person",
                )
            ],
        )

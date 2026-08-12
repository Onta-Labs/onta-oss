"""ONTA-406 — ontology snapshots, structural diff, restore, immutability.

Acceptance:
- Snapshot → mutate heavily → restore → fingerprint identity
- Write into published version graph refused
- Diff correctness + symmetry (diff(a,a)=[], invert(diff(a,b))==diff(b,a))
- Cleanup drops version artifacts
- plan_*/execute dry-run writes nothing

**Ported by ONTA-527.** The async half used to run against a ~370-line in-file
SPARQL emulator (``MemNeptune``) that implemented ``INSERT DATA``, ``INSERT …
WHERE`` graph copies, ``CLEAR``/``DROP SILENT GRAPH`` and the SELECT shapes this
module issues — i.e. it re-implemented a triple store so the module could talk
to something. Production is Neo4j-only: Neptune was decommissioned, the SPARQL
execution path is deleted, and schema now lives in the ontology catalog
(``:OntoType`` / ``:OntoAttr``), which none of this module can read. Keeping the
emulator would have kept a green suite that proves nothing about production, so
it is deleted. Each acceptance case is re-expressed on the shipped path — seed
via ``commit_ontology`` (which takes its GraphStore branch), then snapshot —
and marked ``strict=True`` xfail with the mechanism named, EXCEPT the version
graph immutability refusal, which is backend-independent and still passes.

The pure diff/fingerprint tests below are untouched: ``diff_shapes`` /
``invert_diff`` / ``diffs_symmetric`` operate on :class:`OntologyShape` values
and carry the real complexity of this module, whichever backend fills the shape.
"""

from __future__ import annotations

from collections import defaultdict

import pytest

from infona_client.graph.ontology_commit import (
    OntologyGraphImmutable,
    OntologyShape,
    commit_ontology,
    fingerprint_ontology,
    is_immutable_version_graph,
    load_ontology_shape,
    release_graph_uri,
    revision_graph_uri,
    versions_graph_uri,
)
from infona_client.graph.ontology_snapshots import (
    ReleaseRecord,
    cleanup_version_artifacts,
    diff_shapes,
    diffs_symmetric,
    execute_restore,
    execute_snapshot,
    invert_diff,
    layer_for_graph,
    list_snapshots,
    plan_cleanup_version_artifacts,
    plan_restore,
    plan_snapshot,
    restore_ontology,
    snapshot_ontology,
)
from infona_client.graph.ontology_queries import ontology_version
from infona_client.models.ontology import (
    ChangeKind,
    ChangeRecord,
    OntologyMutation,
    OntologyOpKind,
)


PUBLIC = "https://graph.infona.ai/graphs/global/public"
ENHANCED = "https://graph.infona.ai/graphs/global/enhanced"
TENANT = "https://graph.infona.ai/graphs/acme"

# `name` is a reserved Entity property key on the property graph
# (graph/facts.py::RESERVED_ENTITY_PROPERTY_KEYS) and is rejected at schema
# time, so the seeded slot is `full_name`.
SLOT = "full_name"


# ---------------------------------------------------------------------------
# URI / immutability helpers
# ---------------------------------------------------------------------------


def test_version_graph_uri_helpers():
    assert release_graph_uri(PUBLIC, 3) == f"{PUBLIC}/v3"
    assert revision_graph_uri(TENANT, 7) == f"{TENANT}/revisions/r7"
    assert versions_graph_uri(PUBLIC).endswith("/versions")
    assert layer_for_graph(PUBLIC) == "public"
    assert layer_for_graph(ENHANCED) == "enhanced"
    assert layer_for_graph(TENANT) == "tenant"


def test_is_immutable_version_graph():
    assert is_immutable_version_graph(f"{PUBLIC}/v1")
    assert is_immutable_version_graph(f"{ENHANCED}/v12")
    assert is_immutable_version_graph(f"{TENANT}/revisions/r3")
    assert not is_immutable_version_graph(PUBLIC)
    assert not is_immutable_version_graph(TENANT)
    assert not is_immutable_version_graph(versions_graph_uri(TENANT))
    assert not is_immutable_version_graph(f"{TENANT}/kg/foo")


# ---------------------------------------------------------------------------
# Pure diff
# ---------------------------------------------------------------------------


def _shape(**kwargs) -> OntologyShape:
    return OntologyShape(**kwargs)


def test_diff_identity_is_empty():
    a = _shape(
        types={"Person": "a human"},
        attrs={"Person": {"name": "string", "employer": "Company"}},
        parent_of={"Employee": "Person"},
        core_slots=[("Person", "name")],
        text_kinds={("Person", "bio"): "free_text"},
    )
    assert diff_shapes(a, a) == []


def test_diff_add_remove_type_attr_rel_subclass():
    a = _shape(types={"Person": ""}, attrs={"Person": {"name": "string"}})
    b = _shape(
        types={"Person": "", "Company": ""},
        attrs={
            "Person": {"name": "string", "employer": "Company"},
            "Company": {"legal_name": "string"},
        },
        parent_of={"Employee": "Person"},
    )
    # Also add Employee type for the subclass edge target to be meaningful.
    b.types["Employee"] = ""
    records = diff_shapes(a, b)
    kinds = {r.kind for r in records}
    assert ChangeKind.ADD_TYPE in kinds
    assert ChangeKind.ADD_RELATIONSHIP in kinds or ChangeKind.ADD_ATTRIBUTE in kinds
    assert ChangeKind.ADD_SUBCLASS in kinds
    # employer is a relationship (non-literal range)
    assert any(
        r.kind is ChangeKind.ADD_RELATIONSHIP and r.slot_name == "employer"
        for r in records
    )
    assert any(
        r.kind is ChangeKind.ADD_ATTRIBUTE and r.slot_name == "legal_name"
        for r in records
    )


def test_diff_comment_range_core_text_kind():
    a = _shape(
        types={"Person": "old"},
        attrs={"Person": {"name": "string", "employer": "Org"}},
        attr_comments={"Person": {"name": "display"}},
        core_slots=[("Person", "name")],
        text_kinds={("Person", "bio"): "free_text"},
    )
    b = _shape(
        types={"Person": "new"},
        attrs={"Person": {"name": "string", "employer": "Company"}},
        attr_comments={"Person": {"name": "full name"}},
        core_slots=[],
        text_kinds={("Person", "bio"): "identifier"},
    )
    records = diff_shapes(a, b)
    by_kind = defaultdict(list)
    for r in records:
        by_kind[r.kind].append(r)
    assert any(
        r.old_value == "old" and r.new_value == "new"
        for r in by_kind[ChangeKind.CHANGE_COMMENT]
        if r.slot_name is None
    )
    assert any(
        r.slot_name == "name" and r.old_value == "display"
        for r in by_kind[ChangeKind.CHANGE_COMMENT]
    )
    assert any(
        r.slot_name == "employer"
        and r.old_value == "Org"
        and r.new_value == "Company"
        for r in by_kind[ChangeKind.CHANGE_RANGE]
    )
    assert any(
        r.slot_name == "name" and r.new_value == "false"
        for r in by_kind[ChangeKind.CHANGE_CORE_SLOT]
    )
    assert any(
        r.slot_name == "bio" and r.new_value == "identifier"
        for r in by_kind[ChangeKind.CHANGE_TEXT_KIND]
    )


def test_diff_symmetry():
    a = _shape(
        types={"Person": "p", "Company": ""},
        attrs={
            "Person": {"name": "string", "age": "integer"},
            "Company": {"legal_name": "string"},
        },
        parent_of={"Employee": "Person"},
        core_slots=[("Person", "name")],
        text_kinds={("Person", "bio"): "free_text"},
        alias_map={
            "https://graph.infona.ai/types/Person/attrs/phone_num":
            "https://graph.infona.ai/types/Person/attrs/phone",
        },
    )
    b = _shape(
        types={"Person": "person", "Org": ""},
        attrs={
            "Person": {"name": "string", "employer": "Org"},
            "Org": {"legal_name": "string"},
        },
        parent_of={"Staff": "Person"},
        core_slots=[("Person", "employer")],
        text_kinds={("Person", "bio"): "identifier"},
    )
    assert diffs_symmetric(a, b)
    assert diffs_symmetric(b, a)
    # Multiset equality of inverted lists
    ab = diff_shapes(a, b)
    ba = diff_shapes(b, a)
    inv = invert_diff(ab)

    from infona_client.graph.ontology_snapshots import _record_key

    assert sorted(_record_key(r) for r in inv) == sorted(_record_key(r) for r in ba)


def test_diff_empty_fingerprint_constant_still_holds():
    assert ontology_version({}, {}) == "e3b0c44298fc1c14"
    assert _shape().fingerprint() == "e3b0c44298fc1c14"



# ---------------------------------------------------------------------------
# Snapshot / restore / immutability against the shipped path
# (ported by ONTA-527 — see module docstring)
# ---------------------------------------------------------------------------


class _DecommissionedSparql:
    """The SPARQL endpoint production no longer has.

    Amazon Neptune was decommissioned 2026-08-11 and the execution path is
    deleted, so every SPARQL call this module still makes fails in production.
    Standing it in here is deliberate: a snapshot test must not pass because a
    hand-rolled in-test triple store answered a query nothing can run. When the
    snapshot stack reads and writes through the GraphStore, this double stops
    being touched and the assertions stand on their own.
    """

    async def query(self, sparql: str) -> dict:
        raise RuntimeError("Neptune is decommissioned (ONTA-527)")

    async def update(self, sparql: str) -> None:
        raise RuntimeError("Neptune is decommissioned (ONTA-527)")


SNAPSHOT_GAP = (
    "BUG (ONTA-527 port gap): ontology snapshots/releases do not exist on Neo4j. "
    "graph/ontology_snapshots.py is SPARQL-only end to end — it reads shapes via "
    "neptune.query, copies content with INSERT { GRAPH <v{N}> } WHERE { GRAPH "
    "<live> }, and drops artifacts with DROP SILENT GRAPH — and there is no "
    "GraphStore equivalent for any of it. Its input is gone too: "
    "graph/ontology_commit.py::load_ontology_shape early-returns an EMPTY "
    "OntologyShape whenever a GraphStore is configured, so the live schema in "
    "the :OntoType/:OntoAttr catalog is invisible to snapshot, diff, restore and "
    "cleanup alike. Every version artifact a workspace could publish is "
    "unreachable in production, and graph/ontology_base_pin.py (which pins "
    "workspaces to published base versions via list_snapshots) is dead behind it."
)


async def _seed_basic(graph_uri: str) -> None:
    """Seed Person(full_name) + Employee ⊂ Person through the shipped path."""
    await commit_ontology(
        None,
        graph_uri,
        [
            OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="Person"),
            OntologyMutation(
                op=OntologyOpKind.UPSERT_ATTRIBUTE,
                type_name="Person",
                slot_name=SLOT,
                datatype="string",
            ),
            OntologyMutation(
                op=OntologyOpKind.UPSERT_TYPE,
                type_name="Employee",
                parent_type="Person",
            ),
        ],
        actor="seed",
        message="seed",
    )


@pytest.mark.asyncio
async def test_write_into_published_version_graph_refused():
    """Published release / revision graphs are immutable (ONTA-406).

    This one survived the port intact: the refusal is a URI-shape check that
    runs before either backend is touched, which is why it is asserted here
    against the decommissioned SPARQL double — nothing may reach it.
    """
    n = _DecommissionedSparql()
    await _seed_basic(PUBLIC)
    snap = f"{PUBLIC}/v1"

    with pytest.raises(OntologyGraphImmutable):
        await commit_ontology(
            None,
            snap,
            [OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="X")],
        )
    with pytest.raises(OntologyGraphImmutable):
        await plan_snapshot(n, snap, kind="release")
    with pytest.raises(OntologyGraphImmutable):
        await plan_restore(n, snap, 1)
    with pytest.raises(OntologyGraphImmutable):
        await plan_snapshot(n, f"{TENANT}/revisions/r3", kind="revision")


@pytest.mark.xfail(reason=SNAPSHOT_GAP, strict=True)
@pytest.mark.asyncio
async def test_snapshot_mutate_restore_fingerprint_identity():
    """The ONTA-406 acceptance: snapshot → mutate heavily → restore → identity."""
    n = _DecommissionedSparql()
    await _seed_basic(PUBLIC)
    fp0 = await fingerprint_ontology(n, PUBLIC)

    rec = await snapshot_ontology(
        n,
        PUBLIC,
        kind="release",
        publisher="ops@infona.ai",
        change_summary="initial public release",
    )
    assert isinstance(rec, ReleaseRecord)
    assert rec.version == 1
    assert rec.fingerprint == fp0
    assert rec.snapshot_graph_uri == f"{PUBLIC}/v1"
    assert rec.publisher == "ops@infona.ai"

    await commit_ontology(
        None,
        PUBLIC,
        [
            OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="Company"),
            OntologyMutation(
                op=OntologyOpKind.UPSERT_ATTRIBUTE,
                type_name="Person",
                slot_name="email",
                datatype="string",
            ),
            OntologyMutation(
                op=OntologyOpKind.UPSERT_RELATIONSHIP,
                type_name="Person",
                slot_name="employer",
                target_type="Company",
                description="works at",
            ),
        ],
    )
    assert await fingerprint_ontology(n, PUBLIC) != fp0

    after = await restore_ontology(n, PUBLIC, 1, kind="release")
    assert after == fp0
    assert await fingerprint_ontology(n, PUBLIC) == fp0


@pytest.mark.xfail(reason=SNAPSHOT_GAP, strict=True)
@pytest.mark.asyncio
async def test_snapshot_overwrite_refused():
    """Re-publishing an existing version number must refuse, not rewrite it."""
    n = _DecommissionedSparql()
    await _seed_basic(PUBLIC)
    await snapshot_ontology(n, PUBLIC, kind="release", version=1)
    await commit_ontology(
        None,
        PUBLIC,
        [OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="Extra")],
    )
    plan = await plan_snapshot(n, PUBLIC, kind="release", version=1)
    with pytest.raises(OntologyGraphImmutable):
        await execute_snapshot(n, plan)


@pytest.mark.xfail(reason=SNAPSHOT_GAP, strict=True)
@pytest.mark.asyncio
async def test_list_snapshots_orders_versions_and_carries_the_parent_delta():
    n = _DecommissionedSparql()
    await _seed_basic(PUBLIC)
    r1 = await snapshot_ontology(n, PUBLIC, kind="release", publisher="a")
    await commit_ontology(
        None,
        PUBLIC,
        [OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="Org")],
    )
    r2 = await snapshot_ontology(
        n, PUBLIC, kind="release", publisher="b", change_summary="add Org"
    )

    listed = await list_snapshots(n, PUBLIC, kind="release")
    assert [r.version for r in listed] == [1, 2]
    assert listed[0].fingerprint == r1.fingerprint
    assert listed[1].fingerprint == r2.fingerprint
    assert listed[1].parent_version == 1
    assert listed[1].change_summary == "add Org"
    assert any(
        c.kind is ChangeKind.ADD_TYPE and c.type_name == "Org"
        for c in listed[1].change_records
    )


@pytest.mark.xfail(reason=SNAPSHOT_GAP, strict=True)
@pytest.mark.asyncio
async def test_revision_snapshot_for_a_workspace():
    n = _DecommissionedSparql()
    await _seed_basic(TENANT)
    rec = await snapshot_ontology(
        n, TENANT, kind="revision", change_summary="job boundary"
    )
    assert rec.kind == "revision"
    assert rec.snapshot_graph_uri == f"{TENANT}/revisions/r{rec.version}"
    assert rec.layer == "tenant"

    listed = await list_snapshots(n, TENANT, kind="revision")
    assert [r.version for r in listed] == [rec.version]


@pytest.mark.xfail(reason=SNAPSHOT_GAP, strict=True)
@pytest.mark.asyncio
async def test_enhanced_layer_release_uri():
    n = _DecommissionedSparql()
    await _seed_basic(ENHANCED)
    rec = await snapshot_ontology(n, ENHANCED, kind="release")
    assert rec.layer == "enhanced"
    assert rec.snapshot_graph_uri == f"{ENHANCED}/v1"


@pytest.mark.xfail(reason=SNAPSHOT_GAP, strict=True)
@pytest.mark.asyncio
async def test_plan_and_dry_run_write_nothing():
    """Planning and dry-running are read-only; only execute publishes."""
    n = _DecommissionedSparql()
    await _seed_basic(PUBLIC)

    plan = await plan_snapshot(n, PUBLIC, kind="release")
    rec = await execute_snapshot(n, plan, dry_run=True, publisher="dry")
    assert rec.version == plan.version
    assert rec.fingerprint == plan.fingerprint
    # Nothing was published: the version is still free to take.
    assert await list_snapshots(n, PUBLIC, kind="release") == []

    await execute_snapshot(n, plan, publisher="real")
    assert [r.version for r in await list_snapshots(n, PUBLIC, kind="release")] == [
        plan.version
    ]

    rplan = await plan_restore(n, PUBLIC, plan.version)
    assert rplan.fingerprint_after == plan.fingerprint
    assert await execute_restore(n, rplan, dry_run=True) == plan.fingerprint


@pytest.mark.xfail(reason=SNAPSHOT_GAP, strict=True)
@pytest.mark.asyncio
async def test_cleanup_drops_version_artifacts_but_not_the_live_graph():
    n = _DecommissionedSparql()
    await _seed_basic(TENANT)
    await snapshot_ontology(n, TENANT, kind="revision")
    await snapshot_ontology(n, TENANT, kind="release", version=1)

    planned = await plan_cleanup_version_artifacts(n, TENANT)
    assert versions_graph_uri(TENANT) in planned
    assert any("/revisions/r" in u for u in planned)
    assert any(u.endswith("/v1") for u in planned)

    live_before = await fingerprint_ontology(n, TENANT)
    dropped = await cleanup_version_artifacts(n, TENANT)
    assert versions_graph_uri(TENANT) in dropped
    assert await list_snapshots(n, TENANT) == []
    # Version cleanup never touches the live ontology.
    assert await fingerprint_ontology(n, TENANT) == live_before


@pytest.mark.xfail(reason=SNAPSHOT_GAP, strict=True)
@pytest.mark.asyncio
async def test_cleanup_dry_run_keeps_every_artifact():
    n = _DecommissionedSparql()
    await _seed_basic(TENANT)
    await snapshot_ontology(n, TENANT, kind="release", version=1)

    planned = await cleanup_version_artifacts(n, TENANT, dry_run=True)
    assert planned
    assert [r.version for r in await list_snapshots(n, TENANT, kind="release")] == [1]


@pytest.mark.xfail(reason=SNAPSHOT_GAP, strict=True)
@pytest.mark.asyncio
async def test_diff_between_consecutive_releases_matches_the_stored_delta():
    n = _DecommissionedSparql()
    await _seed_basic(PUBLIC)
    await snapshot_ontology(n, PUBLIC, kind="release")
    await commit_ontology(
        None,
        PUBLIC,
        [
            OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="Place"),
            OntologyMutation(
                op=OntologyOpKind.UPSERT_ATTRIBUTE,
                type_name="Place",
                slot_name="city",
                datatype="string",
            ),
        ],
    )
    rec2 = await snapshot_ontology(n, PUBLIC, kind="release")
    assert any(
        c.kind is ChangeKind.ADD_TYPE and c.type_name == "Place"
        for c in rec2.change_records
    )

    s1 = await load_ontology_shape(n, f"{PUBLIC}/v1")
    s2 = await load_ontology_shape(n, f"{PUBLIC}/v2")
    assert s1.fingerprint() != s2.fingerprint()
    assert diffs_symmetric(s1, s2)

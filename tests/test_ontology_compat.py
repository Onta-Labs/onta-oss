"""ONTA-404 — backward-compatibility classifier + publish gate.

Table-driven pure classifier tests + the gated release path.

**Ported by ONTA-527.** The integration half ran the publish gate over a
~300-line in-file SPARQL emulator (``MemNeptune``): it seeded schema by letting
``commit_ontology`` emit ``INSERT DATA``, then let ``snapshot_ontology`` copy
named graphs around with ``INSERT { GRAPH … } WHERE { GRAPH … }``. Production is
Neo4j-only: schema now lands in the ontology catalog (``:OntoType`` /
``:OntoAttr``) and there is no SPARQL endpoint to copy graphs in, so the
emulator was the only thing keeping those tests alive. It is deleted; the cases
are re-expressed on the shipped path — seed through ``commit_ontology`` (which
takes its GraphStore branch), then publish — and marked ``strict=True`` xfail.

The gap they now document is worse than "unported". ``load_ontology_shape``
early-returns an EMPTY :class:`OntologyShape` whenever a GraphStore is
configured, so if the SPARQL endpoint were still up, every release would read an
empty live schema AND an empty parent: the diff would always be empty, every
release would classify ADDITIVE, and the ONTA-404 gate could never block a
breaking change nor fail closed. The pure classifier below is unaffected — it is
the wiring into live schema that is gone.
"""

from __future__ import annotations

import pytest

from infona_client.graph.ontology_commit import (
    OntologyShape,
    commit_ontology,
    load_ontology_shape,
)
from infona_client.graph.ontology_compat import (
    CompatClass,
    OntologyCompatError,
    assert_publishable,
    classify_change,
    classify_diff,
    describe_range_change,
    is_ancestor,
)
from infona_client.graph.ontology_snapshots import (
    diff_shapes,
    execute_snapshot,
    plan_snapshot,
    snapshot_ontology,
)
from infona_client.models.ontology import (
    ChangeKind,
    ChangeRecord,
    OntologyMutation,
    OntologyOpKind,
)


# ---------------------------------------------------------------------------
# Table-driven per-kind classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "record,expected",
    [
        (ChangeRecord(kind=ChangeKind.ADD_TYPE, type_name="Person"), CompatClass.ADDITIVE),
        (
            ChangeRecord(kind=ChangeKind.ADD_ATTRIBUTE, type_name="P", slot_name="name"),
            CompatClass.ADDITIVE,
        ),
        (
            ChangeRecord(
                kind=ChangeKind.ADD_RELATIONSHIP, type_name="P", slot_name="employer",
                new_value="Company",
            ),
            CompatClass.ADDITIVE,
        ),
        (
            ChangeRecord(kind=ChangeKind.ADD_SUBCLASS, type_name="E", parent_type="P"),
            CompatClass.ADDITIVE,
        ),
        (
            ChangeRecord(
                kind=ChangeKind.RENAME_WITH_ALIAS, from_name="phone_num", to_name="phone",
            ),
            CompatClass.ADDITIVE,
        ),
        (
            ChangeRecord(kind=ChangeKind.CHANGE_COMMENT, type_name="P", new_value="hi"),
            CompatClass.ANNOTATIVE,
        ),
        (
            ChangeRecord(
                kind=ChangeKind.CHANGE_TEXT_KIND, type_name="P", slot_name="bio",
                new_value="prose",
            ),
            CompatClass.ANNOTATIVE,
        ),
        (
            ChangeRecord(
                kind=ChangeKind.CHANGE_CORE_SLOT, type_name="P", slot_name="name",
                new_value="true",
            ),
            CompatClass.ANNOTATIVE,
        ),
        (
            ChangeRecord(
                kind=ChangeKind.DEPRECATE, type_name="Legacy", superseded_by="Thing",
            ),
            CompatClass.DEPRECATING,
        ),
        (
            ChangeRecord(kind=ChangeKind.DEPRECATE, type_name="Legacy"),
            CompatClass.DEPRECATING,
        ),
        (ChangeRecord(kind=ChangeKind.REMOVE_TYPE, type_name="X"), CompatClass.BREAKING),
        (
            ChangeRecord(kind=ChangeKind.REMOVE_ATTRIBUTE, type_name="P", slot_name="x"),
            CompatClass.BREAKING,
        ),
        (
            ChangeRecord(
                kind=ChangeKind.REMOVE_RELATIONSHIP, type_name="P", slot_name="rel",
            ),
            CompatClass.BREAKING,
        ),
        (
            ChangeRecord(kind=ChangeKind.REMOVE_SUBCLASS, type_name="E", parent_type="P"),
            CompatClass.BREAKING,
        ),
        # Widening integer → float: BREAKING (ONTA-404 ruling)
        (
            ChangeRecord(
                kind=ChangeKind.CHANGE_RANGE, type_name="P", slot_name="n",
                old_value="integer", new_value="float",
            ),
            CompatClass.BREAKING,
        ),
        # Narrowing float → integer: BREAKING
        (
            ChangeRecord(
                kind=ChangeKind.CHANGE_RANGE, type_name="P", slot_name="n",
                old_value="float", new_value="integer",
            ),
            CompatClass.BREAKING,
        ),
        # Relationship range change: BREAKING
        (
            ChangeRecord(
                kind=ChangeKind.CHANGE_RANGE, type_name="P", slot_name="employer",
                old_value="Org", new_value="Company",
            ),
            CompatClass.BREAKING,
        ),
        # Equal range: annotative no-op
        (
            ChangeRecord(
                kind=ChangeKind.CHANGE_RANGE, type_name="P", slot_name="n",
                old_value="string", new_value="string",
            ),
            CompatClass.ANNOTATIVE,
        ),
    ],
    ids=lambda x: (
        x.value if isinstance(x, CompatClass)
        else f"{x.kind.value}:{x.old_value or ''}->{x.new_value or x.superseded_by or x.type_name or ''}"
    ),
)
def test_classify_change_table(record, expected):
    result = classify_change(record)
    assert result.compat_class is expected


def test_widening_and_narrowing_messages_both_breaking():
    w = describe_range_change("integer", "float")
    n = describe_range_change("float", "integer")
    assert "widened" in w and "breaking" in w
    assert "narrowed" in n and "breaking" in n


# ---------------------------------------------------------------------------
# Re-parent ancestry
# ---------------------------------------------------------------------------


def _chain_shape() -> OntologyShape:
    # Thing <- Entity <- Person <- Employee
    return OntologyShape(
        types={"Thing": "", "Entity": "", "Person": "", "Employee": "", "Org": ""},
        parent_of={
            "Entity": "Thing",
            "Person": "Entity",
            "Employee": "Person",
        },
    )


def test_is_ancestor_walks_parent_chain():
    s = _chain_shape()
    assert is_ancestor(s, of="Employee", ancestor="Person")
    assert is_ancestor(s, of="Employee", ancestor="Entity")
    assert is_ancestor(s, of="Employee", ancestor="Thing")
    assert not is_ancestor(s, of="Employee", ancestor="Org")
    assert not is_ancestor(s, of="Person", ancestor="Employee")


def test_reparent_to_ancestor_is_non_breaking():
    # Employee was Person; re-parent to Entity (ancestor of Person).
    records = [
        ChangeRecord(
            kind=ChangeKind.REMOVE_SUBCLASS, type_name="Employee", parent_type="Person",
        ),
        ChangeRecord(
            kind=ChangeKind.ADD_SUBCLASS, type_name="Employee", parent_type="Entity",
        ),
    ]
    v = classify_diff(records, parent_shape=_chain_shape())
    assert v.overall is CompatClass.ANNOTATIVE
    assert not v.requires_major


def test_reparent_to_sibling_is_breaking():
    records = [
        ChangeRecord(
            kind=ChangeKind.REMOVE_SUBCLASS, type_name="Employee", parent_type="Person",
        ),
        ChangeRecord(
            kind=ChangeKind.ADD_SUBCLASS, type_name="Employee", parent_type="Org",
        ),
    ]
    v = classify_diff(records, parent_shape=_chain_shape())
    assert v.overall is CompatClass.BREAKING
    assert v.requires_major


# ---------------------------------------------------------------------------
# Adversarial rename / delete-then-re-add
# ---------------------------------------------------------------------------


def test_rename_with_alias_is_non_breaking():
    v = classify_diff([
        ChangeRecord(
            kind=ChangeKind.RENAME_WITH_ALIAS, from_name="a", to_name="b",
        ),
    ])
    assert v.overall is CompatClass.ADDITIVE
    assert not v.requires_major


def test_rename_bundle_remove_add_alias_is_additive():
    """Structural rename: REMOVE+ADD+RENAME_WITH_ALIAS → additive (B2)."""
    v = classify_diff([
        ChangeRecord(
            kind=ChangeKind.REMOVE_ATTRIBUTE,
            type_name="Guest",
            slot_name="phone_num",
            old_value="string",
        ),
        ChangeRecord(
            kind=ChangeKind.ADD_ATTRIBUTE,
            type_name="Guest",
            slot_name="phone",
            new_value="string",
        ),
        ChangeRecord(
            kind=ChangeKind.RENAME_WITH_ALIAS,
            from_name="phone_num",
            to_name="phone",
            type_name="Guest",
        ),
    ])
    assert v.overall is CompatClass.ADDITIVE
    assert not v.requires_major
    assert v.semver_bump == "minor"


def test_rename_bundle_relationship_slots_is_additive():
    v = classify_diff([
        ChangeRecord(
            kind=ChangeKind.REMOVE_RELATIONSHIP,
            type_name="P",
            slot_name="works_at",
            old_value="Org",
        ),
        ChangeRecord(
            kind=ChangeKind.ADD_RELATIONSHIP,
            type_name="P",
            slot_name="employer",
            new_value="Org",
        ),
        ChangeRecord(
            kind=ChangeKind.RENAME_WITH_ALIAS,
            from_name="works_at",
            to_name="employer",
        ),
    ])
    assert v.overall is CompatClass.ADDITIVE
    assert not v.requires_major


def test_remove_plus_add_type_is_breaking_not_silent_rename():
    """Adversarial: delete-then-re-add under a new name in one release."""
    v = classify_diff([
        ChangeRecord(kind=ChangeKind.REMOVE_TYPE, type_name="OldName"),
        ChangeRecord(kind=ChangeKind.ADD_TYPE, type_name="NewName"),
    ])
    assert v.overall is CompatClass.BREAKING
    assert v.requires_major


def test_remove_attr_plus_add_unrelated_attr_is_breaking():
    """No RENAME_WITH_ALIAS → still breaking (adversarial silent rename)."""
    v = classify_diff([
        ChangeRecord(
            kind=ChangeKind.REMOVE_ATTRIBUTE, type_name="P", slot_name="old",
        ),
        ChangeRecord(
            kind=ChangeKind.ADD_ATTRIBUTE, type_name="P", slot_name="new",
            new_value="string",
        ),
    ])
    assert v.overall is CompatClass.BREAKING
    assert v.requires_major


def test_remove_add_with_mismatched_alias_still_breaking():
    """Alias names that don't match the remove/add leaves stay unpaired."""
    v = classify_diff([
        ChangeRecord(
            kind=ChangeKind.REMOVE_ATTRIBUTE, type_name="P", slot_name="old",
        ),
        ChangeRecord(
            kind=ChangeKind.ADD_ATTRIBUTE, type_name="P", slot_name="new",
            new_value="string",
        ),
        ChangeRecord(
            kind=ChangeKind.RENAME_WITH_ALIAS,
            from_name="other",
            to_name="else",
        ),
    ])
    assert v.overall is CompatClass.BREAKING
    assert v.requires_major


def test_empty_diff_is_additive_ok():
    v = classify_diff([])
    assert v.overall is CompatClass.ADDITIVE
    assert not v.requires_major
    assert v.semver_bump == "minor"
    assert "empty" in v.summary[0]


def test_diff_a_a_empty_still():
    s = OntologyShape(types={"P": "person"}, attrs={"P": {"name": "string"}})
    assert diff_shapes(s, s) == []
    assert classify_diff(diff_shapes(s, s)).overall is CompatClass.ADDITIVE


def test_overall_worst_of_set():
    v = classify_diff([
        ChangeRecord(kind=ChangeKind.ADD_TYPE, type_name="X"),
        ChangeRecord(kind=ChangeKind.CHANGE_COMMENT, type_name="Y", new_value="z"),
        ChangeRecord(kind=ChangeKind.REMOVE_ATTRIBUTE, type_name="P", slot_name="a"),
    ])
    assert v.overall is CompatClass.BREAKING


def test_deprecating_outranks_additive_for_overall():
    v = classify_diff([
        ChangeRecord(kind=ChangeKind.ADD_TYPE, type_name="X"),
        ChangeRecord(kind=ChangeKind.DEPRECATE, type_name="Y", superseded_by="X"),
    ])
    assert v.overall is CompatClass.DEPRECATING
    assert v.semver_bump == "minor"
    assert not v.requires_major


def test_assert_publishable_blocks_and_allows():
    breaking = [ChangeRecord(kind=ChangeKind.REMOVE_TYPE, type_name="X")]
    with pytest.raises(OntologyCompatError) as ei:
        assert_publishable(breaking)
    assert ei.value.verdict.requires_major
    assert "declare_major" in str(ei.value)

    v = assert_publishable(breaking, declare_major=True)
    assert v.overall is CompatClass.BREAKING
    assert v.stored_compat_class == "breaking"




# ---------------------------------------------------------------------------
# Deprecation diff (pure)
# ---------------------------------------------------------------------------


def test_diff_shapes_emits_deprecate():
    a = OntologyShape(types={"Person": ""})
    b = OntologyShape(
        types={"Person": ""},
        deprecated_types={"Person": "Entity"},
    )
    recs = diff_shapes(a, b)
    assert any(r.kind is ChangeKind.DEPRECATE and r.type_name == "Person" for r in recs)
    v = classify_diff(recs)
    assert v.overall is CompatClass.DEPRECATING


# ---------------------------------------------------------------------------
# Publish gate over live schema (ported — see module docstring)
# ---------------------------------------------------------------------------


class _DecommissionedSparql:
    """The SPARQL endpoint production no longer has.

    Amazon Neptune was decommissioned 2026-08-11 and the execution path is
    deleted, so every call the snapshot/release stack still makes through a
    ``NeptuneClient`` fails in production. Standing it in here keeps these tests
    from passing on a hand-rolled triple store that ships to nobody: once the
    release path reads and writes through the GraphStore, this double is never
    touched and the assertions below stand on their own.
    """

    async def query(self, sparql: str) -> dict:
        raise RuntimeError("Neptune is decommissioned (ONTA-527)")

    async def update(self, sparql: str) -> None:
        raise RuntimeError("Neptune is decommissioned (ONTA-527)")


PUBLIC = "https://graph.infona.ai/graphs/global/public"
TENANT = "https://graph.infona.ai/graphs/acme"

# `name` is a reserved Entity property key on the property graph and is rejected
# at schema time (graph/facts.py::RESERVED_ENTITY_PROPERTY_KEYS), so the seeded
# slot is `full_name`.
SLOT = "full_name"

RELEASE_GAP = (
    "BUG (ONTA-527 port gap): the ONTA-404 publish gate has nothing to classify "
    "on Neo4j. graph/ontology_snapshots.py is SPARQL-only — plan_snapshot / "
    "execute_snapshot / snapshot_ontology read shapes with neptune.query and "
    "copy named graphs with INSERT { GRAPH … } WHERE { GRAPH … } — and "
    "graph/ontology_commit.py::load_ontology_shape early-returns an EMPTY "
    "OntologyShape whenever a GraphStore is configured. So the live schema sits "
    "in the :OntoType/:OntoAttr catalog where neither the snapshot nor the diff "
    "can see it. Against the decommissioned endpoint no release can be published "
    "at all; were one still up, every release would read an empty shape, diff to "
    "nothing, and publish as ADDITIVE."
)


async def _seed_person(graph_uri: str) -> None:
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
        ],
    )


@pytest.mark.xfail(reason=RELEASE_GAP, strict=True)
@pytest.mark.asyncio
async def test_gate_blocks_a_breaking_release_by_default():
    n = _DecommissionedSparql()
    await _seed_person(PUBLIC)
    first = await snapshot_ontology(n, PUBLIC, kind="release")
    assert first.compat_class == "additive"

    # Breaking mutation: remove an attribute that v1 published.
    await commit_ontology(
        None,
        PUBLIC,
        [
            OntologyMutation(
                op=OntologyOpKind.DELETE_ATTRIBUTE,
                type_name="Person",
                slot_name=SLOT,
            )
        ],
    )
    with pytest.raises(OntologyCompatError) as ei:
        await snapshot_ontology(n, PUBLIC, kind="release")
    assert ei.value.verdict.overall is CompatClass.BREAKING
    assert "declare_major" in str(ei.value)


@pytest.mark.xfail(reason=RELEASE_GAP, strict=True)
@pytest.mark.asyncio
async def test_declare_major_publishes_and_stores_breaking():
    n = _DecommissionedSparql()
    await _seed_person(PUBLIC)
    await snapshot_ontology(n, PUBLIC, kind="release")
    await commit_ontology(
        None,
        PUBLIC,
        [
            OntologyMutation(
                op=OntologyOpKind.DELETE_ATTRIBUTE,
                type_name="Person",
                slot_name=SLOT,
            )
        ],
    )
    rec = await snapshot_ontology(n, PUBLIC, kind="release", declare_major=True)
    assert rec.compat_class == "breaking"
    assert rec.version == 2
    assert any(r.kind is ChangeKind.REMOVE_ATTRIBUTE for r in rec.change_records)


@pytest.mark.xfail(reason=RELEASE_GAP, strict=True)
@pytest.mark.asyncio
async def test_freeform_compat_class_never_overrides_the_classifier():
    """A caller-supplied compat_class is advisory; the classifier decides."""
    n = _DecommissionedSparql()
    await _seed_person(PUBLIC)
    rec = await snapshot_ontology(n, PUBLIC, kind="release", compat_class="major")
    # First release, empty delta → classifier says additive, not "major".
    assert rec.compat_class == "additive"


@pytest.mark.xfail(reason=RELEASE_GAP, strict=True)
@pytest.mark.asyncio
async def test_revision_snapshot_is_not_gated_on_breaking():
    """Workspace revisions checkpoint whatever is live — they never refuse."""
    n = _DecommissionedSparql()
    await _seed_person(TENANT)
    await commit_ontology(
        None,
        TENANT,
        [
            OntologyMutation(
                op=OntologyOpKind.DELETE_ATTRIBUTE,
                type_name="Person",
                slot_name=SLOT,
            )
        ],
    )
    rec = await snapshot_ontology(n, TENANT, kind="revision")
    assert rec.kind == "revision"


@pytest.mark.xfail(reason=RELEASE_GAP, strict=True)
@pytest.mark.asyncio
async def test_dry_run_release_enforces_the_same_gate():
    """A dry run must refuse what the real publish would refuse."""
    n = _DecommissionedSparql()
    await _seed_person(PUBLIC)
    await snapshot_ontology(n, PUBLIC, kind="release")
    await commit_ontology(
        None,
        PUBLIC,
        [OntologyMutation(op=OntologyOpKind.DELETE_TYPE, type_name="Person")],
    )
    plan = await plan_snapshot(n, PUBLIC, kind="release")
    with pytest.raises(OntologyCompatError):
        await execute_snapshot(n, plan, dry_run=True)


@pytest.mark.xfail(
    reason=(
        RELEASE_GAP
        + " This case is the sharpest consequence: the B1 fail-closed guard "
        "compares the parent's RECORDED fingerprint against the fingerprint of "
        "the shape it just loaded, so with load_ontology_shape stubbed to empty "
        "both sides are the empty digest e3b0c44298fc1c14, the guard sees a "
        "clean parent load, and an unreadable/absent parent can no longer raise "
        "'cannot classify release vs parent'."
    ),
    strict=True,
)
@pytest.mark.asyncio
async def test_release_fails_closed_when_the_parent_shape_is_unreadable():
    n = _DecommissionedSparql()
    await _seed_person(PUBLIC)
    await snapshot_ontology(n, PUBLIC, kind="release")
    await commit_ontology(
        None,
        PUBLIC,
        [
            OntologyMutation(
                op=OntologyOpKind.UPSERT_ATTRIBUTE,
                type_name="Person",
                slot_name="email",
                datatype="string",
            )
        ],
    )
    # The parent release's content is gone / unreadable. Publishing must refuse
    # rather than silently classify the release additive.
    with pytest.raises(RuntimeError, match="cannot classify release vs parent"):
        await snapshot_ontology(n, PUBLIC, kind="release")


@pytest.mark.xfail(reason=RELEASE_GAP, strict=True)
@pytest.mark.asyncio
async def test_rename_release_does_not_require_declare_major():
    """B2: a rename that keeps the alias is additive, not breaking."""
    n = _DecommissionedSparql()
    await _seed_person(PUBLIC)
    r1 = await snapshot_ontology(n, PUBLIC, kind="release")
    assert r1.version == 1

    await commit_ontology(
        None,
        PUBLIC,
        [
            OntologyMutation(
                op=OntologyOpKind.RENAME_ATTRIBUTE,
                type_name="Person",
                alias_from=SLOT,
                alias_to="display_name",
            )
        ],
    )
    rec = await snapshot_ontology(n, PUBLIC, kind="release")
    assert rec.version == 2
    assert rec.compat_class == "additive"
    assert {r.kind for r in rec.change_records} & {
        ChangeKind.REMOVE_ATTRIBUTE,
        ChangeKind.ADD_ATTRIBUTE,
        ChangeKind.RENAME_WITH_ALIAS,
    }


DEPRECATE_GAP = (
    "BUG (ONTA-527 port gap): the DEPRECATE op is dropped on Neo4j. "
    "graph/ontology_commit.py::_commit_ontology_graph_store implements four op "
    "kinds and sends DEPRECATE to its `else:` arm (logged "
    "ontology_store_op_skipped, no change record, still reported as a "
    "successful commit); the catalog has no deprecatedAt / supersededBy column "
    "and load_ontology_shape returns an empty shape, so nothing can read a "
    "marker back either. Deprecation — ONTA-404's whole non-breaking retirement "
    "path — is unavailable in production."
)


@pytest.mark.xfail(reason=DEPRECATE_GAP, strict=True)
@pytest.mark.asyncio
async def test_deprecate_marks_the_type_and_keeps_it():
    await _seed_person(TENANT)
    result = await commit_ontology(
        None,
        TENANT,
        [
            OntologyMutation(
                op=OntologyOpKind.DEPRECATE,
                type_name="Person",
                superseded_by="Entity",
            )
        ],
    )
    assert any(r.kind is ChangeKind.DEPRECATE for r in result.change_records)

    shape = await load_ontology_shape(_DecommissionedSparql(), TENANT)
    assert "Person" in shape.types  # deprecation keeps the type readable
    assert shape.deprecated_types.get("Person") == "Entity"


@pytest.mark.xfail(reason=DEPRECATE_GAP, strict=True)
@pytest.mark.asyncio
async def test_deprecate_marks_a_slot_and_keeps_the_attribute():
    await _seed_person(TENANT)
    await commit_ontology(
        None,
        TENANT,
        [
            OntologyMutation(
                op=OntologyOpKind.DEPRECATE,
                type_name="Person",
                slot_name=SLOT,
                superseded_by="display_name",
            )
        ],
    )
    shape = await load_ontology_shape(_DecommissionedSparql(), TENANT)
    assert ("Person", SLOT) in shape.deprecated_slots
    assert shape.attrs.get("Person", {}).get(SLOT) == "string"  # still declared


@pytest.mark.xfail(reason=DEPRECATE_GAP, strict=True)
@pytest.mark.asyncio
async def test_a_deprecating_release_is_minor_not_breaking():
    n = _DecommissionedSparql()
    await _seed_person(PUBLIC)
    await snapshot_ontology(n, PUBLIC, kind="release")
    await commit_ontology(
        None,
        PUBLIC,
        [
            OntologyMutation(
                op=OntologyOpKind.DEPRECATE,
                type_name="Person",
                superseded_by="Entity",
            )
        ],
    )
    # No declare_major needed — deprecating is minor.
    rec = await snapshot_ontology(n, PUBLIC, kind="release")
    assert rec.compat_class == "deprecating"
    assert any(r.kind is ChangeKind.DEPRECATE for r in rec.change_records)

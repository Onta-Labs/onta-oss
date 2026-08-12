"""ONTA-405 — workspace base pinning, upgrade preview, rollback, entitlement degrade.

Acceptance:
- Pin stability: pinned stack keeps v1 graph URI after live publishes v2
- auto_upgrade path sees latest on ensure
- Upgrade then rollback restores exact fingerprint
- Backfill: no pin → ensure at latest; second ensure is no-op
- Collision preview for overlapping tenant/base names
- Entitlement degrade: enhanced pin + entitled=False → no throw, public only
- Deprecation preview for attrs present on tenant shape

**Ported by ONTA-527.** Every acceptance case above ran against a ~340-line
in-file SPARQL emulator (``MemNeptune``) which stored the pin triples, the
published release records and the ontology content the previews diff. Production
is Neo4j-only — Neptune is decommissioned and the SPARQL execution path is
deleted — and ``graph/ontology_base_pin.py`` has no GraphStore path at all: the
pin itself is read/written with ``neptune.query`` / ``neptune.update`` against a
``…/base-pin`` named graph, the versions it pins to come from
``ontology_snapshots.list_snapshots`` (SPARQL), and the preview diffs
``load_ontology_shape`` results (which early-return EMPTY whenever a GraphStore
is configured). The emulator was therefore the only reason any of this was
green, including the two cases that were still passing before this port — they
were false green, not survivors. It is deleted, the cases are re-expressed on
the shipped path, and each is a ``strict=True`` xfail naming the mechanism.

The pure LayerStack / URI tests below are backend-independent and untouched.
"""

from __future__ import annotations

import pytest

from infona_client.graph.layers import Layer, LayerStack, public_graph_uri
from infona_client.graph.ontology_base_pin import (
    BasePin,
    BasePinReadError,
    base_graph_uri_for_stack,
    base_pin_graph_uri,
    ensure_workspace_base_pin,
    fingerprint_base_layer,
    get_base_pin,
    layer_stack_for_workspace,
    layer_stack_from_pin,
    preview_base_upgrade,
    rollback_base_pin,
    set_base_pin,
    upgrade_base_pin,
)
from infona_client.graph.ontology_commit import (
    commit_ontology,
    release_graph_uri,
)
from infona_client.graph.ontology_snapshots import snapshot_ontology
from infona_client.models.ontology import (
    ChangeKind,
    OntologyMutation,
    OntologyOpKind,
)

PUBLIC = "https://graph.infona.ai/graphs/global/public"
ENHANCED = "https://graph.infona.ai/graphs/global/enhanced"
TENANT_ID = "acme"
TENANT = f"https://graph.infona.ai/graphs/{TENANT_ID}"

# `name` is a reserved Entity property key on the property graph
# (graph/facts.py::RESERVED_ENTITY_PROPERTY_KEYS) and is rejected at schema
# time, so the seeded slot is `full_name`.
SLOT = "full_name"


# ---------------------------------------------------------------------------
# LayerStack version dimension (additive; existing callers unbroken)
# ---------------------------------------------------------------------------


def test_layer_stack_defaults_are_live():
    stack = LayerStack(TENANT, entitled=False)
    assert stack.public_version is None
    assert stack.enhanced_version is None
    assert stack.graph_uri_for(Layer.PUBLIC) == PUBLIC


def test_layer_stack_public_version_pins_release_uri():
    stack = LayerStack(TENANT, entitled=False, public_version=3)
    assert stack.graph_uri_for(Layer.PUBLIC) == release_graph_uri(PUBLIC, 3)
    assert stack.graph_uri_for(Layer.TENANT) == TENANT


def test_layer_stack_enhanced_version_pins_when_entitled():
    stack = LayerStack(TENANT, entitled=True, enhanced_version=5)
    assert stack.graph_uri_for(Layer.ENHANCED) == release_graph_uri(ENHANCED, 5)
    # Non-entitled still excludes enhanced from layers even if version set.
    free = LayerStack(TENANT, entitled=False, enhanced_version=5)
    assert Layer.ENHANCED not in free.layers


# ---------------------------------------------------------------------------
# Helpers / URI
# ---------------------------------------------------------------------------


def test_base_pin_graph_uri():
    assert base_pin_graph_uri("acme") == "https://graph.infona.ai/graphs/acme/base-pin"
    with pytest.raises(ValueError):
        base_pin_graph_uri("")


def test_fingerprint_base_uri_entitled_uses_enhanced():
    """N2: entitled live stack keys base fingerprint off Enhanced."""
    stack = LayerStack(TENANT, entitled=True)
    assert base_graph_uri_for_stack(stack) == ENHANCED
    stack_pinned = LayerStack(TENANT, entitled=True, enhanced_version=3)
    assert base_graph_uri_for_stack(stack_pinned) == release_graph_uri(ENHANCED, 3)
    free = LayerStack(TENANT, entitled=False, public_version=2)
    assert base_graph_uri_for_stack(free) == release_graph_uri(PUBLIC, 2)


# ---------------------------------------------------------------------------
# Base pinning against the shipped path (ported by ONTA-527 — see docstring)
# ---------------------------------------------------------------------------


class _DecommissionedSparql:
    """The SPARQL endpoint production no longer has.

    Amazon Neptune was decommissioned 2026-08-11 and the execution path is
    deleted, so every call ``ontology_base_pin`` makes — the pin SELECT/INSERT,
    the release listing, the shape loads behind a preview — fails in production.
    Standing it in here keeps these tests from going green on a hand-rolled
    triple store that ships to nobody.
    """

    async def query(self, sparql: str) -> dict:
        raise RuntimeError("Neptune is decommissioned (ONTA-527)")

    async def update(self, sparql: str) -> None:
        raise RuntimeError("Neptune is decommissioned (ONTA-527)")


BASE_PIN_GAP = (
    "BUG (ONTA-527 port gap): workspace base pinning does not exist on Neo4j. "
    "graph/ontology_base_pin.py is SPARQL-only in all three of its dependencies: "
    "the pin record itself is read/written with neptune.query / neptune.update "
    "against the <…/base-pin> named graph, the versions it may pin to come from "
    "ontology_snapshots.list_snapshots (also SPARQL-only), and previews diff "
    "ontology_commit.load_ontology_shape output, which early-returns an EMPTY "
    "OntologyShape whenever a GraphStore is configured. None of it has a "
    "GraphStore path, so GET/POST /ontology/base (routes/ontology.py calls "
    "ensure_workspace_base_pin / preview_base_upgrade / upgrade_base_pin / "
    "get_base_pin with a NeptuneClient) cannot work in production."
)


async def _seed_public_v1(n) -> str:
    """Seed public live with Person.full_name and publish it as v1."""
    await commit_ontology(
        None,
        PUBLIC,
        [
            OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="Person"),
            OntologyMutation(
                op=OntologyOpKind.UPSERT_ATTRIBUTE,
                type_name="Person",
                slot_name=SLOT,
                datatype="string",
            ),
        ],
        actor="seed",
        message="public v1",
    )
    rec = await snapshot_ontology(n, PUBLIC, kind="release", version=1, publisher="ops")
    return rec.fingerprint


async def _publish_public_v2(n) -> str:
    """Add Person.email on public live and publish it as v2."""
    await commit_ontology(
        None,
        PUBLIC,
        [
            OntologyMutation(
                op=OntologyOpKind.UPSERT_ATTRIBUTE,
                type_name="Person",
                slot_name="email",
                datatype="string",
            ),
        ],
        actor="seed",
        message="public v2",
    )
    rec = await snapshot_ontology(n, PUBLIC, kind="release", version=2, publisher="ops")
    return rec.fingerprint


@pytest.mark.xfail(reason=BASE_PIN_GAP, strict=True)
@pytest.mark.asyncio
async def test_pin_stability_core():
    """Pin at v1; publish v2; the pinned stack still resolves v1 + its fingerprint.

    auto_upgrade=True is the opt-in that follows the latest release instead.
    """
    n = _DecommissionedSparql()
    fp_v1 = await _seed_public_v1(n)

    pin = await set_base_pin(
        n,
        TENANT_ID,
        BasePin(
            base_layer="public",
            base_version=1,
            auto_upgrade=False,
            tenant_id=TENANT_ID,
        ),
    )
    assert pin.base_version == 1
    assert pin.auto_upgrade is False

    stack_before = await layer_stack_for_workspace(
        n, TENANT_ID, entitled=False, auto_ensure=False
    )
    assert stack_before.public_version == 1
    assert stack_before.graph_uri_for(Layer.PUBLIC) == release_graph_uri(PUBLIC, 1)
    assert await fingerprint_base_layer(n, stack_before) == fp_v1

    fp_v2 = await _publish_public_v2(n)
    assert fp_v2 != fp_v1

    stack_after = await layer_stack_for_workspace(
        n, TENANT_ID, entitled=False, auto_ensure=True
    )
    assert stack_after.public_version == 1
    assert stack_after.graph_uri_for(Layer.PUBLIC) == release_graph_uri(PUBLIC, 1)
    assert await fingerprint_base_layer(n, stack_after) == fp_v1

    await set_base_pin(
        n,
        TENANT_ID,
        BasePin(
            base_layer="public",
            base_version=1,
            auto_upgrade=True,
            tenant_id=TENANT_ID,
        ),
    )
    stack_auto = await layer_stack_for_workspace(
        n, TENANT_ID, entitled=False, auto_ensure=True
    )
    assert stack_auto.public_version == 2
    assert await fingerprint_base_layer(n, stack_auto) == fp_v2


@pytest.mark.xfail(reason=BASE_PIN_GAP, strict=True)
@pytest.mark.asyncio
async def test_upgrade_then_rollback_restores_fingerprint():
    n = _DecommissionedSparql()
    fp_v1 = await _seed_public_v1(n)
    fp_v2 = await _publish_public_v2(n)

    await set_base_pin(
        n,
        TENANT_ID,
        BasePin(base_layer="public", base_version=1, tenant_id=TENANT_ID),
    )
    stack_v1 = layer_stack_from_pin(
        TENANT_ID, await get_base_pin(n, TENANT_ID), entitled=False
    )
    assert await fingerprint_base_layer(n, stack_v1) == fp_v1

    upgraded = await upgrade_base_pin(n, TENANT_ID, entitled=False, to_version=2)
    assert upgraded.base_version == 2
    assert upgraded.previous_version == 1
    assert upgraded.has_previous is True
    stack_v2 = layer_stack_from_pin(TENANT_ID, upgraded, entitled=False)
    assert await fingerprint_base_layer(n, stack_v2) == fp_v2

    rolled = await rollback_base_pin(n, TENANT_ID)
    assert rolled.base_version == 1
    assert rolled.previous_version == 2
    stack_rolled = layer_stack_from_pin(TENANT_ID, rolled, entitled=False)
    assert await fingerprint_base_layer(n, stack_rolled) == fp_v1
    assert stack_rolled.graph_uri_for(Layer.PUBLIC) == release_graph_uri(PUBLIC, 1)


@pytest.mark.xfail(reason=BASE_PIN_GAP, strict=True)
@pytest.mark.asyncio
async def test_backfill_ensure_at_latest_then_noop():
    n = _DecommissionedSparql()
    await _seed_public_v1(n)
    await _publish_public_v2(n)
    assert await get_base_pin(n, TENANT_ID) is None

    pin1 = await ensure_workspace_base_pin(n, TENANT_ID, entitled=False)
    assert pin1.base_layer == "public"
    assert pin1.base_version == 2  # latest
    assert pin1.auto_upgrade is False
    assert pin1.has_previous is False

    pin2 = await ensure_workspace_base_pin(n, TENANT_ID, entitled=False)
    assert pin2.base_version == 2
    assert pin2.updated_at == pin1.updated_at  # second ensure must not rewrite


@pytest.mark.xfail(reason=BASE_PIN_GAP, strict=True)
@pytest.mark.asyncio
async def test_ensure_with_no_releases_pins_live():
    n = _DecommissionedSparql()
    pin_live = await ensure_workspace_base_pin(n, "empty", entitled=False)
    assert pin_live.base_version is None
    assert pin_live.is_live


@pytest.mark.xfail(reason=BASE_PIN_GAP, strict=True)
@pytest.mark.asyncio
async def test_collision_preview_tenant_overlaps_base_addition():
    """An upgrade preview must name attributes the workspace already defines."""
    n = _DecommissionedSparql()
    await _seed_public_v1(n)

    await commit_ontology(
        None,
        TENANT,
        [
            OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="Person"),
            OntologyMutation(
                op=OntologyOpKind.UPSERT_ATTRIBUTE,
                type_name="Person",
                slot_name="risk_score",
                datatype="float",
            ),
        ],
        actor="tenant",
        message="tenant risk_score",
    )
    await set_base_pin(
        n,
        TENANT_ID,
        BasePin(base_layer="public", base_version=1, tenant_id=TENANT_ID),
    )
    # v2 of public adds the SAME attribute the workspace already has.
    await commit_ontology(
        None,
        PUBLIC,
        [
            OntologyMutation(
                op=OntologyOpKind.UPSERT_ATTRIBUTE,
                type_name="Person",
                slot_name="risk_score",
                datatype="float",
            ),
        ],
        actor="ops",
        message="public risk_score",
    )
    await snapshot_ontology(n, PUBLIC, kind="release", version=2, publisher="ops")

    preview = await preview_base_upgrade(n, TENANT_ID, entitled=False, to_version=2)
    assert preview.from_version == 1
    assert preview.to_version == 2
    assert any(
        c.kind is ChangeKind.ADD_ATTRIBUTE
        and c.type_name == "Person"
        and c.slot_name == "risk_score"
        for c in preview.changes
    )
    assert any(
        c.type_name == "Person" and c.slot_name == "risk_score"
        for c in preview.collisions
    )
    assert any("risk_score" in s for s in preview.summary)


@pytest.mark.xfail(reason=BASE_PIN_GAP, strict=True)
@pytest.mark.asyncio
async def test_preview_flags_deprecations_the_workspace_uses():
    n = _DecommissionedSparql()
    await _seed_public_v1(n)

    await commit_ontology(
        None,
        TENANT,
        [
            OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="Person"),
            OntologyMutation(
                op=OntologyOpKind.UPSERT_ATTRIBUTE,
                type_name="Person",
                slot_name=SLOT,
                datatype="string",
            ),
        ],
        actor="tenant",
        message="tenant person",
    )
    await set_base_pin(
        n,
        TENANT_ID,
        BasePin(base_layer="public", base_version=1, tenant_id=TENANT_ID),
    )
    await commit_ontology(
        None,
        PUBLIC,
        [
            OntologyMutation(
                op=OntologyOpKind.DEPRECATE,
                type_name="Person",
                slot_name=SLOT,
                superseded_by="display_name",
            ),
        ],
        actor="ops",
        message="deprecate full_name",
    )
    await snapshot_ontology(n, PUBLIC, kind="release", version=2, publisher="ops")

    preview = await preview_base_upgrade(n, TENANT_ID, entitled=False, to_version=2)
    assert any(c.kind is ChangeKind.DEPRECATE for c in preview.changes)
    assert any(
        d.kind is ChangeKind.DEPRECATE
        and d.type_name == "Person"
        and d.slot_name == SLOT
        for d in preview.deprecated_used
    )


@pytest.mark.xfail(reason=BASE_PIN_GAP, strict=True)
@pytest.mark.asyncio
async def test_entitlement_degrade_enhanced_pin_without_entitlement():
    """ENTITLEMENT: an enhanced pin held by a non-entitled workspace degrades to
    public — never a throw, and never enhanced content."""
    n = _DecommissionedSparql()
    await commit_ontology(
        None,
        ENHANCED,
        [
            OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="Org"),
            OntologyMutation(
                op=OntologyOpKind.UPSERT_ATTRIBUTE,
                type_name="Org",
                slot_name="lei",
                datatype="string",
            ),
        ],
        actor="ops",
        message="enhanced v1",
    )
    await snapshot_ontology(n, ENHANCED, kind="release", version=7, publisher="ops")
    await set_base_pin(
        n,
        TENANT_ID,
        BasePin(base_layer="enhanced", base_version=7, tenant_id=TENANT_ID),
    )

    stack_paid = layer_stack_from_pin(
        TENANT_ID, await get_base_pin(n, TENANT_ID), entitled=True
    )
    assert Layer.ENHANCED in stack_paid.layers
    assert stack_paid.enhanced_version == 7
    assert stack_paid.graph_uri_for(Layer.ENHANCED) == release_graph_uri(ENHANCED, 7)

    stack_free = layer_stack_from_pin(
        TENANT_ID, await get_base_pin(n, TENANT_ID), entitled=False
    )
    assert Layer.ENHANCED not in stack_free.layers
    assert Layer.PUBLIC in stack_free.layers
    assert stack_free.graph_uri_for(Layer.PUBLIC) == public_graph_uri()

    stack_ws = await layer_stack_for_workspace(
        n, TENANT_ID, entitled=False, auto_ensure=False
    )
    assert Layer.ENHANCED not in stack_ws.layers


@pytest.mark.xfail(reason=BASE_PIN_GAP, strict=True)
@pytest.mark.asyncio
async def test_pin_read_failure_does_not_repin_to_latest():
    """B1 FAIL-CLOSED: a pin read that errors must not silently move the pin.

    v2 exists, so an implementation that fell back to "latest" on a read error
    would jump this workspace from v1 to v2 behind its back. The read must raise
    and the stored pin must be untouched.
    """
    n = _DecommissionedSparql()
    await _seed_public_v1(n)
    await _publish_public_v2(n)
    await set_base_pin(
        n,
        TENANT_ID,
        BasePin(
            base_layer="public",
            base_version=1,
            auto_upgrade=False,
            tenant_id=TENANT_ID,
        ),
    )
    assert (await get_base_pin(n, TENANT_ID)).base_version == 1

    class _PinReadFails(_DecommissionedSparql):
        async def query(self, sparql: str) -> dict:
            if "WorkspaceBasePin" in sparql:
                raise RuntimeError("pin read unavailable")
            return await _DecommissionedSparql.query(self, sparql)

    broken = _PinReadFails()
    with pytest.raises(BasePinReadError):
        await get_base_pin(broken, TENANT_ID)
    with pytest.raises(BasePinReadError):
        await ensure_workspace_base_pin(broken, TENANT_ID, entitled=False)

    # Soft degrade on the workspace stack: live layers, no rewrite of the pin.
    stack = await layer_stack_for_workspace(
        broken, TENANT_ID, entitled=False, auto_ensure=True
    )
    assert stack.public_version is None
    assert stack.graph_uri_for(Layer.PUBLIC) == PUBLIC

    # Once reads work again the pin is still exactly where it was.
    after = await get_base_pin(n, TENANT_ID)
    assert after is not None
    assert after.base_version == 1
    assert after.auto_upgrade is False


@pytest.mark.xfail(reason=BASE_PIN_GAP, strict=True)
@pytest.mark.asyncio
async def test_upgrade_refuses_missing_target_version():
    """VALIDATION: pinning to a version nobody published must refuse."""
    n = _DecommissionedSparql()
    await _seed_public_v1(n)
    await set_base_pin(
        n,
        TENANT_ID,
        BasePin(base_layer="public", base_version=1, tenant_id=TENANT_ID),
    )
    with pytest.raises(ValueError, match="no public release v99"):
        await upgrade_base_pin(n, TENANT_ID, entitled=False, to_version=99)
    pin = await get_base_pin(n, TENANT_ID)
    assert pin is not None and pin.base_version == 1


@pytest.mark.xfail(reason=BASE_PIN_GAP, strict=True)
@pytest.mark.asyncio
async def test_rollback_without_previous_raises():
    n = _DecommissionedSparql()
    await set_base_pin(
        n,
        TENANT_ID,
        BasePin(base_layer="public", base_version=1, tenant_id=TENANT_ID),
    )
    with pytest.raises(ValueError, match="previous_version"):
        await rollback_base_pin(n, TENANT_ID)

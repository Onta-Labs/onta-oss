"""Mapping-shape governance seam tests (ADR 0003 §5, COG-56).

ADR 0002 §2 gates type names entering shared layers; COG-56 extends the same
pipeline to MAPPING SHAPE. Covers the proposal extraction from a posted
CSVSchemaMapping (promotions, core slots on pre-existing types, dataset
constants, low-confidence reason-pass decisions), the OSS default panel (a
no-op holder that just records pending proposals — tenant-layer-only
behavior), the register_governance_panel plugin protocol, the fire-and-forget
enqueue + drain seam, and the /ingest/csv/rows wiring: the tenant-layer
pre-registration writes happen synchronously exactly as before, proposals are
enqueued AFTER they succeed, and the route returns WITHOUT the panel running
(ingestion never blocks on governance — the acceptance criterion).

All mocked — no live Neptune, no LLM, no network. Module-level seam state
(registered panel, pending holder, background tasks) is reset around every
test by an autouse fixture.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

from cograph_client.resolver.governance import (
    LOW_CONFIDENCE_THRESHOLD,
    MappingShapeProposal,
    PendingShapeProposals,
    _shape_tasks,
    drain_shape_governance,
    enqueue_shape_proposals,
    governance_panel,
    mapping_shape_proposals,
    pending_shape_proposals,
    register_governance_panel,
)
from cograph_client.resolver.models import (
    ColumnMapping,
    ColumnRole,
    CoreSlot,
    CSVSchemaMapping,
    DatasetConstant,
    EntitySpec,
    IngestResult,
    OntologyExtensions,
    TypeExtension,
)


@pytest.fixture(autouse=True)
def _reset_shape_governance():
    """Seam state is module-level (panel registry, pending holder, task list);
    leave every test the way we found it. Stale tasks from a closed TestClient
    loop must be dropped or a later drain would gather across loops."""
    register_governance_panel(None)
    pending_shape_proposals.clear()
    _shape_tasks.clear()
    yield
    register_governance_panel(None)
    pending_shape_proposals.clear()
    _shape_tasks.clear()


def _promotion_extension(**overrides) -> TypeExtension:
    """The canonical dependent-identifier shape, promoted at low confidence —
    `(issued_by → Issuer, identifies → Target, id_string)` — with a dataset
    constant on the issuer slot (the whole file is one party's catalog)."""
    fields = dict(
        type_name="DistributorProductIdentifier",
        promoted_from_attribute="sku",
        confidence=0.65,  # < 0.7: held for review AND judge-panel material
        held_for_review=True,
        core_slots=[
            CoreSlot(
                name="issued_by", kind="relationship", target_type="Supplier",
                why="an identifier exists only relative to its issuer",
                dataset_constant=DatasetConstant(value="Acme Corp", confidence=0.9),
            ),
            CoreSlot(name="identifies", kind="relationship", target_type="Item"),
            CoreSlot(name="id_string", kind="attribute"),
        ],
    )
    fields.update(overrides)
    return TypeExtension(**fields)


def _mapping() -> CSVSchemaMapping:
    """Multi-entity v2 mapping exercising every proposal kind at once."""
    return CSVSchemaMapping(
        entity_type="Item",
        columns=[
            ColumnMapping(
                column_name="name", role=ColumnRole.ATTRIBUTE, attribute_name="name",
                entity="item", confidence=0.95, why="complete unique label column",
            ),
            ColumnMapping(
                column_name="sku", role=ColumnRole.ATTRIBUTE, attribute_name="sku",
                entity="item", confidence=0.6, why="ambiguous column role",
            ),
            ColumnMapping(
                column_name="wh", role=ColumnRole.ATTRIBUTE, attribute_name="wh",
                entity="warehouse",
            ),
        ],
        entities=[
            EntitySpec(name="item", type_name="Item", id_column="name", confidence=0.95),
            EntitySpec(
                name="warehouse", type_name="Warehouse", id_column="wh",
                confidence=0.55, why="weak evidence this column is an entity",
            ),
        ],
        ontology_extensions=OntologyExtensions(types=[
            _promotion_extension(),
            TypeExtension(
                type_name="Item",
                core_slots=[CoreSlot(
                    name="supplied_by", kind="relationship", target_type="Supplier",
                    confidence=0.85, why="an item entails a supplying party",
                )],
            ),
        ]),
    )


def _by_kind(proposals: list[MappingShapeProposal], kind: str) -> list[MappingShapeProposal]:
    return [p for p in proposals if p.kind == kind]


class RecordingPanel:
    """Minimal ShapeGovernancePanel impl — also exercises the Protocol seam."""

    def __init__(self):
        self.received: list[MappingShapeProposal] = []

    async def submit(self, proposal: MappingShapeProposal) -> None:
        self.received.append(proposal)


# ---------------------------------------------------------------------------
# mapping_shape_proposals — what is judge-panel material (ADR 0003 §5)
# ---------------------------------------------------------------------------


def test_promotion_yields_one_proposal_carrying_the_full_extension():
    proposals = mapping_shape_proposals(
        _mapping(), "acme", dataset_hint="catalog.csv", proposer_model="test-model",
    )
    promos = _by_kind(proposals, "promotion")
    assert len(promos) == 1
    p = promos[0]
    assert p.subject == "DistributorProductIdentifier"
    # The whole Pass D payload travels with the proposal — the panel judges
    # the shape as a unit, not slot by slot.
    assert p.extension == _promotion_extension()
    assert p.confidence == 0.65
    # Source context: tenant, dataset hint, proposer, and the host type the
    # attribute was promoted from (the `identifies` alignment anchor).
    assert p.tenant_id == "acme"
    assert p.dataset_hint == "catalog.csv"
    assert p.proposer_model == "test-model"
    assert p.host_type == "Item"  # the "sku" column belongs to entity "item"
    assert "issuer" in p.reasoning


def test_core_slot_on_existing_type_yields_core_slot_proposal():
    proposals = mapping_shape_proposals(_mapping(), "acme")
    slots = _by_kind(proposals, "core_slot")
    # Only the NON-promoted extension's slots: the promotion's slots are
    # judged as part of the promotion proposal.
    assert [p.subject for p in slots] == ["Item.supplied_by"]
    assert slots[0].slot_name == "supplied_by"
    assert slots[0].confidence == 0.85
    assert slots[0].extension is not None
    assert slots[0].extension.type_name == "Item"


def test_dataset_constant_yields_its_own_proposal():
    proposals = mapping_shape_proposals(_mapping(), "acme")
    constants = _by_kind(proposals, "dataset_constant")
    assert [p.subject for p in constants] == ["DistributorProductIdentifier.issued_by"]
    p = constants[0]
    assert p.slot_name == "issued_by"
    # The constant carries ITS confidence (0.9), not the promotion's (0.65).
    assert p.confidence == 0.9
    assert "Acme Corp" in p.reasoning


def test_low_confidence_reason_decisions_are_proposed_high_confidence_not():
    proposals = mapping_shape_proposals(_mapping(), "acme")
    low = _by_kind(proposals, "low_confidence_decision")
    assert {p.subject for p in low} == {"entity:warehouse", "column:sku"}
    by_subject = {p.subject: p for p in low}
    assert by_subject["entity:warehouse"].confidence == 0.55
    assert by_subject["entity:warehouse"].reasoning == "weak evidence this column is an entity"
    assert by_subject["column:sku"].confidence == 0.6
    # 0.95 decisions and legacy no-confidence columns are NOT panel material.
    assert all(p.confidence < LOW_CONFIDENCE_THRESHOLD for p in low)


def test_legacy_mapping_yields_no_proposals():
    """Pre-v2 payload (no confidences, no extensions): nothing is judge
    material — auto-commit to the tenant layer stays exactly as today."""
    legacy = CSVSchemaMapping(
        entity_type="Item",
        columns=[ColumnMapping(column_name="name", role=ColumnRole.TYPE_ID)],
    )
    assert mapping_shape_proposals(legacy, "acme") == []


# ---------------------------------------------------------------------------
# Default panel + registration protocol
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_panel_records_pending_proposals_tenant_layer_only():
    """No premium service registered: proposals land in the pending holder and
    NOTHING else happens — tenant-layer-only behavior."""
    assert governance_panel() is pending_shape_proposals
    proposals = mapping_shape_proposals(_mapping(), "acme")

    assert enqueue_shape_proposals(proposals) == len(proposals)
    await drain_shape_governance()

    pending = pending_shape_proposals.pending()
    assert pending == proposals
    assert {p.kind for p in pending} == {
        "promotion", "core_slot", "dataset_constant", "low_confidence_decision",
    }


@pytest.mark.asyncio
async def test_registered_panel_receives_proposals_instead_of_default():
    panel = RecordingPanel()
    register_governance_panel(panel)
    assert governance_panel() is panel
    proposals = mapping_shape_proposals(_mapping(), "acme")

    enqueue_shape_proposals(proposals)
    await drain_shape_governance()

    assert panel.received == proposals
    assert pending_shape_proposals.pending() == []  # default holder bypassed


@pytest.mark.asyncio
async def test_register_none_restores_default_holder():
    register_governance_panel(RecordingPanel())
    register_governance_panel(None)
    assert governance_panel() is pending_shape_proposals


@pytest.mark.asyncio
async def test_pending_holder_is_bounded():
    holder = PendingShapeProposals(max_pending=3)
    for i in range(5):
        await holder.submit(
            MappingShapeProposal(kind="promotion", subject=f"T{i}", tenant_id="acme"),
        )
    assert [p.subject for p in holder.pending()] == ["T2", "T3", "T4"]


# ---------------------------------------------------------------------------
# Enqueue seam — fire-and-forget, never raises, never blocks
# ---------------------------------------------------------------------------


class GatedPanel:
    """Panel that blocks until released — proves callers never wait on it."""

    def __init__(self):
        self.release = asyncio.Event()
        self.started = 0
        self.completed = 0

    async def submit(self, proposal: MappingShapeProposal) -> None:
        self.started += 1
        await self.release.wait()
        self.completed += 1


@pytest.mark.asyncio
async def test_enqueue_returns_before_panel_runs():
    """enqueue_shape_proposals returns immediately — the panel has not even
    STARTED when it returns (the task runs on the next loop turn)."""
    panel = GatedPanel()
    register_governance_panel(panel)
    proposals = mapping_shape_proposals(_mapping(), "acme")

    n = enqueue_shape_proposals(proposals)

    assert n == len(proposals)
    assert panel.started == 0 and panel.completed == 0
    panel.release.set()
    await drain_shape_governance()
    assert panel.completed == len(proposals)


@pytest.mark.asyncio
async def test_panel_exception_swallowed_and_later_proposals_still_submitted():
    class FlakyPanel(RecordingPanel):
        async def submit(self, proposal: MappingShapeProposal) -> None:
            if proposal.kind == "promotion":
                raise RuntimeError("judge service down")
            await super().submit(proposal)

    panel = FlakyPanel()
    register_governance_panel(panel)
    proposals = mapping_shape_proposals(_mapping(), "acme")
    assert proposals[0].kind == "promotion"  # the failing one comes first

    enqueue_shape_proposals(proposals)
    await drain_shape_governance()  # never raises

    # Everything after the failing proposal was still submitted.
    assert panel.received == [p for p in proposals if p.kind != "promotion"]


@pytest.mark.asyncio
async def test_enqueue_nothing_is_a_noop():
    assert enqueue_shape_proposals([]) == 0
    await drain_shape_governance()  # safe with nothing pending
    assert pending_shape_proposals.pending() == []


# ---------------------------------------------------------------------------
# /ingest/csv/rows wiring — tenant layer first, proposals async, never blocks
# ---------------------------------------------------------------------------
#
# Route tests need a CONTEXT-MANAGED TestClient: its event loop stays alive
# between requests, so the fire-and-forget submission task actually runs after
# the response is sent (the conftest `client` tears the loop down per request).


@pytest.fixture
def live_client(app, mock_neptune):
    with TestClient(app) as c:
        # Lifespan startup replaced the mocked Neptune client; restore it.
        app.state.neptune_client = mock_neptune
        yield c


@pytest.fixture
def mock_schema_resolver():
    from unittest.mock import patch

    with patch("cograph_client.api.routes.ingest.SchemaResolver") as cls:
        instance = AsyncMock()
        instance._fetch_ontology.return_value = ({}, {})
        instance._resolve_and_insert.return_value = IngestResult()
        cls.return_value = instance
        yield cls


def _route_mapping() -> dict:
    """JSON body: a low-confidence promotion in the canonical
    dependent-identifier shape (the acceptance-criteria case)."""
    return {
        "entity_type": "Item",
        "columns": [
            {"column_name": "name", "role": "type_id", "datatype": "string",
             "attribute_name": "name"},
            {"column_name": "sku", "role": "attribute", "datatype": "string",
             "attribute_name": "sku"},
        ],
        "ontology_extensions": {
            "types": [
                {"type_name": "DistributorProductIdentifier",
                 "promoted_from_attribute": "sku",
                 "confidence": 0.65, "held_for_review": True,
                 "core_slots": [
                     {"name": "issued_by", "kind": "relationship", "target_type": "Supplier",
                      "why": "an identifier exists only relative to its issuer"},
                     {"name": "identifies", "kind": "relationship", "target_type": "Item"},
                     {"name": "id_string", "kind": "attribute"},
                 ]},
            ],
        },
    }


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_csv_rows_low_confidence_promotion_lands_tenant_layer_and_pends_proposal(
    live_client, auth_headers, mock_neptune, mock_schema_resolver,
):
    """ACCEPTANCE: a promotion with confidence < 0.7 (1) lands in the tenant
    layer synchronously — the pre-registration writes are unchanged — and
    (2) creates a pending governance proposal (OSS default: recorded, judged
    by nothing until a premium panel registers)."""
    response = live_client.post(
        "/graphs/test-tenant/ingest/csv/rows",
        json={"mapping": _route_mapping(), "rows": [{"name": "One", "sku": "SKU-1"}]},
        headers=auth_headers,
    )
    assert response.status_code == 200

    # (1) Tenant-layer writes happened on the request path, exactly as before.
    updates = [c.args[0] for c in mock_neptune.update.call_args_list]
    assert any(
        "types/DistributorProductIdentifier" in u and "Class" in u
        and "promoted from attribute 'sku'" in u
        for u in updates
    )
    assert any(
        "types/DistributorProductIdentifier/attrs/issued_by" in u and "coreSlot" in u
        for u in updates
    )
    # No Global/Public write from the OSS seam — promotion is PENDING, not applied.
    assert not any("global/public" in u for u in updates)

    # (2) The pending proposal exists (background task — poll the holder).
    assert _wait_until(lambda: len(pending_shape_proposals.pending()) >= 1)
    pending = pending_shape_proposals.pending()
    promos = _by_kind(pending, "promotion")
    assert len(promos) == 1
    assert promos[0].subject == "DistributorProductIdentifier"
    assert promos[0].confidence == 0.65
    assert promos[0].tenant_id == "test-tenant"
    assert promos[0].host_type == "Item"
    assert promos[0].extension is not None
    assert [s.name for s in promos[0].extension.core_slots] == [
        "issued_by", "identifies", "id_string",
    ]


def test_csv_rows_returns_without_the_panel_running(
    live_client, auth_headers, mock_neptune, mock_schema_resolver,
):
    """ACCEPTANCE (non-blocking): gating is async — the route returns while a
    deliberately-hung panel is still mid-submit, so a slow (or dead) judge
    service can never add latency to /ingest/csv/rows."""

    class HungPanel:
        def __init__(self):
            self.started = False
            self.finished = False

        async def submit(self, proposal: MappingShapeProposal) -> None:
            self.started = True
            await asyncio.sleep(30)  # far longer than any request budget
            self.finished = True

    panel = HungPanel()
    register_governance_panel(panel)

    t0 = time.monotonic()
    response = live_client.post(
        "/graphs/test-tenant/ingest/csv/rows",
        json={"mapping": _route_mapping(), "rows": [{"name": "One", "sku": "SKU-1"}]},
        headers=auth_headers,
    )
    elapsed = time.monotonic() - t0

    # The route returned successfully, fast, with the panel still hanging.
    assert response.status_code == 200
    assert elapsed < 10  # nowhere near the panel's 30s sleep
    assert _wait_until(lambda: panel.started)  # the proposal WAS dispatched...
    assert panel.finished is False             # ...but the route never awaited it


def test_csv_rows_legacy_mapping_enqueues_nothing(
    live_client, auth_headers, mock_neptune, mock_schema_resolver,
):
    mapping = _route_mapping()
    del mapping["ontology_extensions"]
    response = live_client.post(
        "/graphs/test-tenant/ingest/csv/rows",
        json={"mapping": mapping, "rows": [{"name": "One", "sku": "SKU-1"}]},
        headers=auth_headers,
    )
    assert response.status_code == 200
    # Give any (wrongly) scheduled task a chance to land, then assert silence.
    assert not _wait_until(lambda: pending_shape_proposals.pending(), timeout=0.3)
    assert pending_shape_proposals.pending() == []

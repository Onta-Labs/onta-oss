"""GraphStore home for the workspace base pin (ONTA-405 / ONTA-534).

Split out of :mod:`ontology_base_pin` so that file stays inside its size
budget. The pin used to live in a per-tenant SPARQL named graph
(``…/graphs/{tenant}/base-pin``); that graph went out with the Neptune
backend, so on the shipped Neo4j GraphStore the pin lives on the ontology
companion instead — the same move ``_current_revision_counter`` and
``list_snapshots`` already made for the revision counter and the snapshot list
(ONTA-531), with the same non-durable, process-local contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:  # pragma: no cover — import cycle at runtime
    from infona_client.graph.ontology_base_pin import BasePin

logger = structlog.stdlib.get_logger("infona.graph.ontology_base_pin")


def _graph_store_configured() -> bool:
    """True when the Neo4j GraphStore backend is live (ONTA-534).

    The SPARQL named graph this module was written against went out with the
    Neptune backend, so under GraphStore the pin lives on the ontology
    companion instead — the same move ``_current_revision_counter`` /
    ``list_snapshots`` already made for the revision counter and the snapshot
    list (ONTA-531).
    """
    from infona_client.graph.store import GraphConfigError, get_graph_store

    try:
        get_graph_store()
    except GraphConfigError:
        return False
    except Exception:  # noqa: BLE001 — treat an unusable store as "not configured"
        return False
    return True


def _pin_from_companion(tenant_id: str) -> BasePin | None:
    """The GraphStore-companion pin for ``tenant_id``, or ``None`` if unset."""
    from infona_client.graph.ontology_base_pin import BasePin
    from infona_client.graph.ontology_companion import get_ontology_companion

    row = get_ontology_companion().base_pins.get(tenant_id)
    if not row:
        return None
    return BasePin(
        tenant_id=tenant_id,
        base_layer=row.get("base_layer", "public"),
        base_version=row.get("base_version"),
        auto_upgrade=bool(row.get("auto_upgrade", False)),
        previous_version=row.get("previous_version"),
        updated_at=row.get("updated_at"),
        has_previous=bool(row.get("has_previous", False)),
    )


def _pin_to_companion(tenant_id: str, pin: BasePin) -> None:
    """Store ``pin`` on the GraphStore companion."""
    from infona_client.graph.ontology_companion import get_ontology_companion

    get_ontology_companion().base_pins[tenant_id] = {
        "base_layer": pin.base_layer,
        "base_version": pin.base_version,
        "auto_upgrade": bool(pin.auto_upgrade),
        "previous_version": pin.previous_version,
        "updated_at": pin.updated_at,
        "has_previous": bool(pin.has_previous),
    }


def read_companion_pin(tenant_id: str) -> BasePin | None:
    """The companion pin for ``tenant_id``, or ``None`` when none is stored.

    A read that ERRORS is not "no pin": collapsing it to ``None`` would let
    :func:`~infona_client.graph.ontology_base_pin.ensure_workspace_base_pin`
    silently re-pin the workspace to latest. Same fail-closed rule the SPARQL
    arm has always had (review B1 / ONTA-405).
    """
    from infona_client.graph.ontology_base_pin import BasePinReadError

    try:
        return _pin_from_companion(tenant_id)
    except Exception as exc:  # noqa: BLE001 — fail closed, never "no pin"
        logger.warning("base_pin_read_failed", tenant_id=tenant_id, exc_info=True)
        raise BasePinReadError(tenant_id) from exc

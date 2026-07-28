"""Workspace base-layer version pin + upgrade / rollback (ONTA-405).

Once Global-Public / Global-Enhanced are versioned (ONTA-406), each workspace
must **pin** which base release it extends. Without a pin, every global release
silently changes every customer's effective ontology.

Storage (RDF only — no Postgres)
--------------------------------
Companion named graph per tenant::

    https://cograph.tech/graphs/{tenant}/base-pin

Single subject::

    https://cograph.tech/meta/WorkspaceBasePin

Predicates (under ``https://cograph.tech/onto/``)::

    baseLayer        "public" | "enhanced"   (from entitlement, not user-chosen)
    baseVersion      xsd:integer             (omitted when None = live)
    autoUpgrade      "true" | "false"
    previousVersion  xsd:integer             (optional; for rollback)
    updatedAt        xsd:dateTime

Schema-meta only — not instance data. Writes CLEAR + INSERT the small pin
graph; allowlisted on the write-path guard.

Defaults
--------
* ``auto_upgrade=False`` for new pins — pin stability is the default guarantee.
* No releases yet → pin ``base_version=None`` (live). Once releases exist,
  backfill / ensure pins the **latest** release number of the entitled base.
* Missing snapshot at a pin: loaders treat the release URI as an empty layer
  (never silent fall-through to live) so fingerprints cannot jump to latest.

Resolution
----------
``layer_stack_for_workspace`` builds a versioned
:class:`~cograph_client.graph.layers.LayerStack`. Workspace reads (overlay
shadows base on name collision) must use that stack. Operator global browsers
keep using live graphs (no pin).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Sequence

import structlog

from cograph_client.graph.layers import (
    Layer,
    LayerStack,
    enhanced_graph_uri,
    public_graph_uri,
)
from cograph_client.graph.ontology_commit import (
    OntologyShape,
    load_ontology_shape,
    release_graph_uri,
)
from cograph_client.graph.ontology_compat import classify_diff
from cograph_client.graph.ontology_queries import OMNIX_ONTO, XSD
from cograph_client.graph.ontology_snapshots import (
    diff_shapes,
    list_snapshots,
)
from cograph_client.graph.parser import parse_sparql_results
from cograph_client.graph.queries import insert_triples, tenant_graph_uri
from cograph_client.models.ontology import ChangeKind, ChangeRecord

logger = structlog.stdlib.get_logger("cograph.graph.ontology_base_pin")

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

PIN_SUBJECT = "https://cograph.tech/meta/WorkspaceBasePin"

_PIN_BASE_LAYER = f"{OMNIX_ONTO}/baseLayer"
_PIN_BASE_VERSION = f"{OMNIX_ONTO}/baseVersion"
_PIN_AUTO_UPGRADE = f"{OMNIX_ONTO}/autoUpgrade"
_PIN_PREVIOUS_VERSION = f"{OMNIX_ONTO}/previousVersion"
_PIN_UPDATED_AT = f"{OMNIX_ONTO}/updatedAt"

BaseLayerName = Literal["public", "enhanced"]


def base_pin_graph_uri(tenant_id: str) -> str:
    """Companion named graph holding the workspace base pin (schema-meta)."""
    tid = (tenant_id or "").strip().strip("/")
    if not tid:
        raise ValueError("tenant_id is required for base pin graph")
    return f"https://cograph.tech/graphs/{tid}/base-pin"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


# previousVersion = 0 in RDF means "was live" (so we can distinguish
# "never upgraded / no rollback" from "roll back to live").
_LIVE_SENTINEL = 0


class BasePinReadError(Exception):
    """Infrastructure failure reading the workspace base pin (not "missing").

    Must **not** be treated as an absent pin: mapping read failures to
    ``None`` would let :func:`ensure_workspace_base_pin` backfill to latest
    and destroy a stable pin (review B1 / ONTA-405).
    """

    def __init__(self, tenant_id: str, message: str | None = None):
        self.tenant_id = tenant_id
        super().__init__(
            message
            or f"failed to read workspace base pin for tenant {tenant_id!r}"
        )


@dataclass(frozen=True)
class BasePin:
    """Explicit base-layer pin for one workspace (inspectable)."""

    base_layer: BaseLayerName
    base_version: int | None  # None => live (no releases yet, or explicit live)
    auto_upgrade: bool = False
    previous_version: int | None = None  # None => live when has_previous
    updated_at: str | None = None
    tenant_id: str = ""
    #: True when an upgrade recorded a previous pin (enables rollback).
    has_previous: bool = False

    @property
    def is_live(self) -> bool:
        """True when the pin tracks the live global graph (no version number)."""
        return self.base_version is None


@dataclass(frozen=True)
class CollisionRecord:
    """A name the upgrade would add that the tenant overlay already defines."""

    type_name: str
    slot_name: str | None = None
    kind: str = "type"  # "type" | "slot"
    detail: str = ""


@dataclass(frozen=True)
class UpgradePreview:
    """Typed preview of upgrading a workspace base pin to a target version."""

    from_version: int | None
    to_version: int | None
    base_layer: BaseLayerName
    changes: tuple[ChangeRecord, ...] = ()
    collisions: tuple[CollisionRecord, ...] = ()
    deprecated_used: tuple[ChangeRecord, ...] = ()
    summary: tuple[str, ...] = ()
    from_fingerprint: str | None = None
    to_fingerprint: str | None = None


# ---------------------------------------------------------------------------
# Read / write pin
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_int_lit(val: str | None) -> int | None:
    if val is None or val == "":
        return None
    try:
        return int(str(val).split("^")[0].strip('"'))
    except (TypeError, ValueError):
        return None


def _parse_bool_lit(val: str | None, default: bool = False) -> bool:
    if val is None:
        return default
    s = str(val).split("^")[0].strip('"').lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    return default


async def get_base_pin(neptune, tenant_id: str) -> BasePin | None:
    """Load the workspace base pin, or ``None`` if never ensured / missing.

    A successful empty query result means no pin (``None``). Query / parse
    infrastructure failures raise :class:`BasePinReadError` — they must not
    be collapsed to ``None`` (that would trigger a silent re-pin to latest).
    """
    g = base_pin_graph_uri(tenant_id)
    q = (
        f"SELECT ?p ?o FROM <{g}> WHERE {{\n"
        f"  <{PIN_SUBJECT}> ?p ?o .\n"
        f"}}"
    )
    try:
        raw = await neptune.query(q)
        _, rows = parse_sparql_results(raw)
    except BasePinReadError:
        raise
    except Exception as exc:
        logger.warning("base_pin_read_failed", tenant_id=tenant_id, exc_info=True)
        raise BasePinReadError(tenant_id) from exc

    if not rows:
        return None

    props: dict[str, str] = {}
    for row in rows:
        p = (row.get("p") or "").strip()
        o = (row.get("o") or "").strip()
        if p:
            props[p] = o

    layer_raw = (props.get(_PIN_BASE_LAYER) or "public").strip('"').lower()
    if layer_raw not in ("public", "enhanced"):
        layer_raw = "public"

    has_previous = _PIN_PREVIOUS_VERSION in props
    prev_raw = _parse_int_lit(props.get(_PIN_PREVIOUS_VERSION))
    if has_previous and prev_raw == _LIVE_SENTINEL:
        previous_version: int | None = None  # was live
    else:
        previous_version = prev_raw

    return BasePin(
        tenant_id=tenant_id,
        base_layer=layer_raw,  # type: ignore[arg-type]
        base_version=_parse_int_lit(props.get(_PIN_BASE_VERSION)),
        auto_upgrade=_parse_bool_lit(props.get(_PIN_AUTO_UPGRADE), default=False),
        previous_version=previous_version,
        updated_at=(props.get(_PIN_UPDATED_AT) or None),
        has_previous=has_previous,
    )


async def set_base_pin(
    neptune,
    tenant_id: str,
    pin: BasePin,
    *,
    updated_at: str | None = None,
) -> BasePin:
    """Persist ``pin`` (replaces the entire pin graph). Returns the stored pin."""
    g = base_pin_graph_uri(tenant_id)
    ts = updated_at or _now_iso()
    layer: BaseLayerName = pin.base_layer if pin.base_layer in (
        "public",
        "enhanced",
    ) else "public"

    stored = BasePin(
        tenant_id=tenant_id,
        base_layer=layer,
        base_version=pin.base_version,
        auto_upgrade=bool(pin.auto_upgrade),
        previous_version=pin.previous_version,
        updated_at=ts,
        has_previous=bool(pin.has_previous),
    )

    triples: list[tuple[str, str, str]] = [
        (PIN_SUBJECT, _PIN_BASE_LAYER, stored.base_layer),
        (
            PIN_SUBJECT,
            _PIN_AUTO_UPGRADE,
            "true" if stored.auto_upgrade else "false",
        ),
        (PIN_SUBJECT, _PIN_UPDATED_AT, f"{ts}^^{XSD}#dateTime"),
    ]
    if stored.base_version is not None:
        if stored.base_version < 1:
            raise ValueError(
                f"base_version must be >= 1 or None (live), got {stored.base_version}"
            )
        triples.append(
            (
                PIN_SUBJECT,
                _PIN_BASE_VERSION,
                f"{int(stored.base_version)}^^{XSD}#integer",
            )
        )
    if stored.has_previous:
        # 0 = was live; positive = prior release number.
        prev_store = (
            _LIVE_SENTINEL
            if stored.previous_version is None
            else int(stored.previous_version)
        )
        triples.append(
            (
                PIN_SUBJECT,
                _PIN_PREVIOUS_VERSION,
                f"{prev_store}^^{XSD}#integer",
            )
        )

    # Full replace of the tiny schema-meta graph (not instance data).
    await neptune.update(f"CLEAR SILENT GRAPH <{g}>")
    await neptune.update(insert_triples(g, triples))
    logger.info(
        "base_pin_set",
        tenant_id=tenant_id,
        base_layer=stored.base_layer,
        base_version=stored.base_version,
        auto_upgrade=stored.auto_upgrade,
        previous_version=stored.previous_version,
    )
    return stored


# ---------------------------------------------------------------------------
# Ensure / backfill
# ---------------------------------------------------------------------------


async def latest_base_release_version(
    neptune,
    base_layer: BaseLayerName,
) -> int | None:
    """Latest published release version for the live base layer, or None."""
    live = public_graph_uri() if base_layer == "public" else enhanced_graph_uri()
    records = await list_snapshots(neptune, live, kind="release")
    if not records:
        return None
    return max(r.version for r in records)


def _entitled_base_layer(entitled: bool) -> BaseLayerName:
    return "enhanced" if entitled else "public"


async def ensure_workspace_base_pin(
    neptune,
    tenant_id: str,
    *,
    entitled: bool,
) -> BasePin:
    """Return the workspace pin, creating / refreshing as needed.

    * Missing pin → backfill to current latest release of the entitled base
      layer (enhanced if entitled else public). No releases yet →
      ``base_version=None`` (live). ``auto_upgrade=False`` by default.
    * Existing pin with ``auto_upgrade=True`` → refresh ``base_version`` to
      latest when a newer release exists (stores prior in ``previous_version``).
    * Existing pin with ``auto_upgrade=False`` → returned as-is (no-op).
    * Entitlement only gates which *layer* is chosen on **create**; an
      existing enhanced pin is not rewritten when entitlement is lost (the
      stack builder excludes Enhanced instead — predictable degrade).
    * Pin **read** failures (:class:`BasePinReadError`) **re-raise** — never
      treat as missing / never overwrite an unreadable pin with latest.
    * Pin **write** failures (read-only store, network blip) degrade to an
      ephemeral in-memory pin so ensure can still return a value when the
      pin was confirmed missing; the next successful ensure persists.
    """
    # Read errors propagate (BasePinReadError) — do not catch and backfill.
    existing = await get_base_pin(neptune, tenant_id)
    if existing is not None:
        if not existing.auto_upgrade:
            return existing
        # auto_upgrade: refresh to latest of the pin's base layer.
        latest = await latest_base_release_version(neptune, existing.base_layer)
        if latest is None or latest == existing.base_version:
            return existing
        if existing.base_version is not None and latest < existing.base_version:
            return existing
        updated = BasePin(
            tenant_id=tenant_id,
            base_layer=existing.base_layer,
            base_version=latest,
            auto_upgrade=True,
            previous_version=existing.base_version,
            has_previous=True,
            updated_at=None,
        )
        return await _set_base_pin_soft(neptune, tenant_id, updated)

    # Backfill new pin.
    layer = _entitled_base_layer(entitled)
    latest = await latest_base_release_version(neptune, layer)
    pin = BasePin(
        tenant_id=tenant_id,
        base_layer=layer,
        base_version=latest,  # None when no releases yet → live
        auto_upgrade=False,
        previous_version=None,
    )
    return await _set_base_pin_soft(neptune, tenant_id, pin)


async def _set_base_pin_soft(
    neptune, tenant_id: str, pin: BasePin
) -> BasePin:
    """Persist pin; on write failure return ephemeral pin (reads must not 500)."""
    try:
        return await set_base_pin(neptune, tenant_id, pin)
    except Exception:
        logger.warning(
            "base_pin_ensure_write_failed",
            tenant_id=tenant_id,
            base_layer=pin.base_layer,
            base_version=pin.base_version,
            exc_info=True,
        )
        return BasePin(
            tenant_id=tenant_id,
            base_layer=pin.base_layer,
            base_version=pin.base_version,
            auto_upgrade=pin.auto_upgrade,
            previous_version=pin.previous_version,
            has_previous=pin.has_previous,
            updated_at=pin.updated_at or _now_iso(),
        )


# ---------------------------------------------------------------------------
# LayerStack from pin
# ---------------------------------------------------------------------------


def layer_stack_from_pin(
    tenant_id: str,
    pin: BasePin | None,
    *,
    entitled: bool,
) -> LayerStack:
    """Build a :class:`LayerStack` from an optional pin + current entitlement.

    Entitlement loss: an enhanced pin with ``entitled=False`` still builds a
    stack (Enhanced excluded); never raises. The public layer is included with
    a version only when the pin's base layer is public.
    """
    tenant_g = tenant_graph_uri(tenant_id)
    public_version: int | None = None
    enhanced_version: int | None = None

    if pin is not None:
        if pin.base_layer == "public":
            public_version = pin.base_version
        elif pin.base_layer == "enhanced":
            # Pin version applies to Enhanced when the stack is entitled.
            # Public stays live (None) — dual-layer independent pinning is
            # out of scope for ONTA-405 (single base pin per workspace).
            if entitled:
                enhanced_version = pin.base_version
            # When not entitled, enhanced is excluded from layers entirely;
            # public_version stays None (live Public). Predictable degrade.

    return LayerStack(
        tenant_graph_uri=tenant_g,
        entitled=entitled,
        public_version=public_version,
        enhanced_version=enhanced_version,
    )


async def layer_stack_for_workspace(
    neptune,
    tenant_id: str,
    *,
    entitled: bool,
    auto_ensure: bool = True,
) -> LayerStack:
    """Versioned :class:`LayerStack` for a workspace (ONTA-405).

    With ``auto_ensure=True`` (default), missing pins are backfilled so every
    workspace has an explicit inspectable pin. Pass ``auto_ensure=False`` for
    pure reads that must not write (still returns an unversioned stack when no
    pin exists).

    On :class:`BasePinReadError` (pin graph unreadable): degrade to an
    **ephemeral unversioned/live** stack **without writing**. Availability over
    a 500 on every workspace GET; never silent re-pin to latest.
    """
    try:
        if auto_ensure:
            pin = await ensure_workspace_base_pin(
                neptune, tenant_id, entitled=entitled
            )
        else:
            pin = await get_base_pin(neptune, tenant_id)
    except BasePinReadError:
        logger.warning(
            "base_pin_read_failed_degrade_live",
            tenant_id=tenant_id,
            auto_ensure=auto_ensure,
            exc_info=True,
        )
        return LayerStack(
            tenant_graph_uri=tenant_graph_uri(tenant_id),
            entitled=entitled,
            # Explicit live — no pin versions, no set_base_pin.
        )
    return layer_stack_from_pin(tenant_id, pin, entitled=entitled)


# ---------------------------------------------------------------------------
# Upgrade preview / apply / rollback
# ---------------------------------------------------------------------------


def _live_uri_for(layer: BaseLayerName) -> str:
    return public_graph_uri() if layer == "public" else enhanced_graph_uri()


def _graph_uri_for_version(layer: BaseLayerName, version: int | None) -> str:
    live = _live_uri_for(layer)
    if version is None:
        return live
    return release_graph_uri(live, version)


async def _load_shape_soft(neptune, graph_uri: str) -> OntologyShape:
    """Load shape; empty on failure (pin-stability soft degrade)."""
    try:
        return await load_ontology_shape(neptune, graph_uri)
    except Exception:
        logger.warning(
            "base_pin_shape_load_failed", graph_uri=graph_uri, exc_info=True
        )
        return OntologyShape()


def _slot_keys(shape: OntologyShape) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for t, attrs in shape.attrs.items():
        for a in attrs:
            keys.add((t, a))
    return keys


def _find_collisions(
    tenant_shape: OntologyShape,
    changes: Sequence[ChangeRecord],
) -> list[CollisionRecord]:
    """Names present on tenant overlay that the upgrade would add on base."""
    tenant_types = set(tenant_shape.types.keys())
    tenant_slots = _slot_keys(tenant_shape)
    out: list[CollisionRecord] = []
    for rec in changes:
        if rec.kind is ChangeKind.ADD_TYPE and rec.type_name:
            if rec.type_name in tenant_types:
                out.append(
                    CollisionRecord(
                        type_name=rec.type_name,
                        kind="type",
                        detail=(
                            f"workspace already defines type {rec.type_name!r}; "
                            f"base upgrade would also add it (workspace shadows)"
                        ),
                    )
                )
        elif rec.kind in (
            ChangeKind.ADD_ATTRIBUTE,
            ChangeKind.ADD_RELATIONSHIP,
        ) and rec.type_name and rec.slot_name:
            if (rec.type_name, rec.slot_name) in tenant_slots:
                out.append(
                    CollisionRecord(
                        type_name=rec.type_name,
                        slot_name=rec.slot_name,
                        kind="slot",
                        detail=(
                            f"workspace already defines "
                            f"{rec.type_name}.{rec.slot_name}; "
                            f"base upgrade would also add it (workspace shadows)"
                        ),
                    )
                )
    return out


def _find_deprecated_used(
    tenant_shape: OntologyShape,
    changes: Sequence[ChangeRecord],
) -> list[ChangeRecord]:
    """Deprecations in the upgrade that touch types/slots present on tenant."""
    tenant_types = set(tenant_shape.types.keys())
    tenant_slots = _slot_keys(tenant_shape)
    out: list[ChangeRecord] = []
    for rec in changes:
        if rec.kind is not ChangeKind.DEPRECATE:
            continue
        if rec.slot_name and rec.type_name:
            if (rec.type_name, rec.slot_name) in tenant_slots:
                out.append(rec)
            elif rec.type_name in tenant_types:
                # Type exists on tenant; slot may be inherited from base —
                # still surface as used when the type name overlaps.
                out.append(rec)
        elif rec.type_name and rec.type_name in tenant_types:
            out.append(rec)
    return out


def _preview_summary(
    from_version: int | None,
    to_version: int | None,
    changes: Sequence[ChangeRecord],
    collisions: Sequence[CollisionRecord],
    deprecated_used: Sequence[ChangeRecord],
) -> list[str]:
    fv = "live" if from_version is None else f"v{from_version}"
    tv = "live" if to_version is None else f"v{to_version}"
    lines = [f"Base upgrade {fv} → {tv}: {len(changes)} change(s)."]
    if changes:
        verdict = classify_diff(list(changes))
        lines.append(
            f"Compat class: {verdict.overall.value} "
            f"(semver {verdict.semver_bump})."
        )
        for s in verdict.summary[:6]:
            lines.append(s)
    if collisions:
        lines.append(
            f"{len(collisions)} name collision(s) with workspace overlay "
            f"(workspace will continue to shadow base)."
        )
        for c in collisions[:5]:
            lines.append(f"  collision: {c.detail or c.type_name}")
    if deprecated_used:
        lines.append(
            f"{len(deprecated_used)} deprecation(s) touch names present on "
            f"the workspace shape."
        )
        for d in deprecated_used[:5]:
            if d.slot_name:
                lines.append(
                    f"  deprecated used: {d.type_name}.{d.slot_name}"
                )
            else:
                lines.append(f"  deprecated used: {d.type_name}")
    return lines


async def preview_base_upgrade(
    neptune,
    tenant_id: str,
    *,
    entitled: bool,
    to_version: int | None = None,
) -> UpgradePreview:
    """Preview upgrading the workspace base pin to ``to_version`` (or latest).

    Does not write. Uses structural ``diff_shapes`` between the currently
    pinned release (or live) and the target. Surfaces:

    * typed ``ChangeRecord`` list
    * collisions (tenant overlay already defines a name base would add)
    * deprecations that match types/attrs present on the tenant shape
    * customer-facing summary strings
    """
    pin = await get_base_pin(neptune, tenant_id)
    if pin is None:
        # Ensure so preview has a defined from_version without requiring a
        # prior ensure call from the UI — soft backfill for inspectability.
        pin = await ensure_workspace_base_pin(
            neptune, tenant_id, entitled=entitled
        )

    layer = pin.base_layer
    # If pin is enhanced but no longer entitled, preview the public path so
    # the degrade stays predictable (never error).
    if layer == "enhanced" and not entitled:
        layer = "public"

    from_version = pin.base_version if pin.base_layer == layer else None
    if to_version is None:
        to_version = await latest_base_release_version(neptune, layer)

    from_uri = _graph_uri_for_version(layer, from_version)
    to_uri = _graph_uri_for_version(layer, to_version)

    from_shape = await _load_shape_soft(neptune, from_uri)
    to_shape = await _load_shape_soft(neptune, to_uri)
    changes = diff_shapes(from_shape, to_shape)

    tenant_shape = await _load_shape_soft(neptune, tenant_graph_uri(tenant_id))
    collisions = _find_collisions(tenant_shape, changes)
    deprecated_used = _find_deprecated_used(tenant_shape, changes)
    summary = _preview_summary(
        from_version, to_version, changes, collisions, deprecated_used
    )

    return UpgradePreview(
        from_version=from_version,
        to_version=to_version,
        base_layer=layer,
        changes=tuple(changes),
        collisions=tuple(collisions),
        deprecated_used=tuple(deprecated_used),
        summary=tuple(summary),
        from_fingerprint=from_shape.fingerprint(),
        to_fingerprint=to_shape.fingerprint(),
    )


async def upgrade_base_pin(
    neptune,
    tenant_id: str,
    *,
    entitled: bool,
    to_version: int | None = None,
) -> BasePin:
    """Point the workspace pin at ``to_version`` (or latest). Stores previous.

    Creates a pin via ensure when missing. ``previous_version`` is set to the
    prior pin version so :func:`rollback_base_pin` can restore it.

    Raises ``ValueError`` when an explicit ``to_version`` has no published
    release record for the target base layer.
    """
    pin = await ensure_workspace_base_pin(
        neptune, tenant_id, entitled=entitled
    )
    layer = pin.base_layer
    if layer == "enhanced" and not entitled:
        # Entitlement lost — retarget pin to public on explicit upgrade so
        # the workspace has a coherent public base going forward.
        layer = "public"

    if to_version is None:
        to_version = await latest_base_release_version(neptune, layer)
        if to_version is None:
            # Still no releases — stay on live.
            return pin
    else:
        if to_version < 1:
            raise ValueError(
                f"to_version must be >= 1 or None (latest), got {to_version}"
            )
        live = _live_uri_for(layer)
        records = await list_snapshots(neptune, live, kind="release")
        if not any(r.version == to_version for r in records):
            raise ValueError(
                f"no {layer} release v{to_version} for {live!r}"
            )

    if to_version == pin.base_version and layer == pin.base_layer:
        return pin

    updated = BasePin(
        tenant_id=tenant_id,
        base_layer=layer,
        base_version=to_version,
        auto_upgrade=pin.auto_upgrade,
        previous_version=pin.base_version,
        has_previous=True,
    )
    return await set_base_pin(neptune, tenant_id, updated)


async def rollback_base_pin(neptune, tenant_id: str) -> BasePin:
    """Restore the pin to ``previous_version`` (swap for re-rollback).

    Raises ``ValueError`` when no pin exists or no prior upgrade recorded.
    ``previous_version=None`` with ``has_previous=True`` means roll back to live.
    """
    pin = await get_base_pin(neptune, tenant_id)
    if pin is None:
        raise ValueError(f"no base pin for tenant {tenant_id!r}")
    if not pin.has_previous:
        raise ValueError(
            f"no previous_version to roll back to for tenant {tenant_id!r}"
        )

    # Swap so a second rollback re-applies the upgrade.
    rolled = BasePin(
        tenant_id=tenant_id,
        base_layer=pin.base_layer,
        base_version=pin.previous_version,
        auto_upgrade=pin.auto_upgrade,
        previous_version=pin.base_version,
        has_previous=True,
    )
    return await set_base_pin(neptune, tenant_id, rolled)


# ---------------------------------------------------------------------------
# Fingerprint helpers (tests + diagnostics)
# ---------------------------------------------------------------------------


def base_graph_uri_for_stack(stack: LayerStack) -> str:
    """The base-layer graph URI the stack resolves for its highest global layer.

    For pin-stability tests: pin at v1 → this URI ends with ``/v1``; a later
    live publish does not change it until upgrade / auto_upgrade.
    """
    if stack.entitled:
        return stack.graph_uri_for(Layer.ENHANCED)
    return stack.graph_uri_for(Layer.PUBLIC)


async def fingerprint_base_layer(
    neptune,
    stack: LayerStack,
) -> str:
    """Fingerprint of the stack's resolved base-layer graph content.

    Uses :func:`base_graph_uri_for_stack` consistently (entitled → enhanced,
    else public — including live URIs when unpinned). Empty/missing snapshot
    → empty-shape fingerprint (stable; does **not** fall back to live when
    a version pin points at a missing graph).
    """
    uri = base_graph_uri_for_stack(stack)
    shape = await _load_shape_soft(neptune, uri)
    return shape.fingerprint()


__all__ = [
    "BasePin",
    "BaseLayerName",
    "BasePinReadError",
    "CollisionRecord",
    "PIN_SUBJECT",
    "UpgradePreview",
    "base_graph_uri_for_stack",
    "base_pin_graph_uri",
    "ensure_workspace_base_pin",
    "fingerprint_base_layer",
    "get_base_pin",
    "latest_base_release_version",
    "layer_stack_for_workspace",
    "layer_stack_from_pin",
    "preview_base_upgrade",
    "rollback_base_pin",
    "set_base_pin",
    "upgrade_base_pin",
]

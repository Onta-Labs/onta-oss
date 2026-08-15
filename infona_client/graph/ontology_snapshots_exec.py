"""Plan + execute ontology snapshots (ONTA-406)."""

from __future__ import annotations

from datetime import datetime, timezone

import structlog

from infona_client.graph.ontology_commit import (
    OntologyGraphImmutable,
    OntologyShape,
    is_immutable_version_graph,
    load_ontology_shape,
    ontology_write_lock,
    release_graph_uri,
    revision_graph_uri,
    versions_graph_uri,
)
from infona_client.graph.ontology_compat import assert_publishable, classify_diff
from infona_client.graph.ontology_queries_uris import INFONA_ONTO
from infona_client.graph.ontology_snapshots_diff import diff_shapes
from infona_client.graph.ontology_snapshots_list import list_snapshots
from infona_client.graph.ontology_snapshots_models import (
    ReleaseRecord,
    SnapshotKind,
    SnapshotPlan,
    layer_for_graph,
)
from infona_client.graph.ontology_snapshots_sparql import (
    _copy_graph_sparql,
    _release_metadata_triples,
)
from infona_client.graph.parser import parse_sparql_results
from infona_client.graph.queries import insert_triples
from infona_client.models.ontology import ChangeRecord

logger = structlog.stdlib.get_logger("infona.graph.ontology_snapshots")


async def _next_release_version(neptune, live_graph_uri: str, kind: SnapshotKind) -> int:
    """Max recorded version for ``kind`` on this live graph + 1 (or 1)."""
    records = await list_snapshots(neptune, live_graph_uri, kind=kind)
    if not records:
        return 1
    return max(r.version for r in records) + 1


async def _current_revision_counter(neptune, live_graph_uri: str) -> int:
    """Read the ONTA-403 workspaceRevision counter (0 if absent)."""
    from infona_client.graph.store import GraphConfigError, get_graph_store

    try:
        get_graph_store()
        from infona_client.graph.ontology_companion import (
            get_ontology_companion,
            live_graph_uri as _live,
        )

        live = _live(live_graph_uri)
        return int(get_ontology_companion().revisions.get(live, 0))
    except GraphConfigError:
        pass
    except Exception:
        logger.warning(
            "revision_counter_read_failed", graph_uri=live_graph_uri, exc_info=True
        )

    rev_graph = versions_graph_uri(live_graph_uri)
    pred = f"{INFONA_ONTO}/workspaceRevision"
    try:
        raw = await neptune.query(
            f"SELECT ?r FROM <{rev_graph}> WHERE {{ "
            f"<{live_graph_uri}> <{pred}> ?r }}"
        )
        _, rows = parse_sparql_results(raw)
        if rows and rows[0].get("r") is not None:
            return int(str(rows[0]["r"]).split("^")[0])
    except Exception:
        logger.warning(
            "revision_counter_read_failed", graph_uri=live_graph_uri, exc_info=True
        )
    return 0


async def plan_snapshot(
    neptune,
    live_graph_uri: str,
    *,
    kind: SnapshotKind = "release",
    version: int | None = None,
) -> SnapshotPlan:
    """Build a snapshot plan without writing (dry-runable).

    For ``kind="release"`` (A/B): next monotonic release number (or ``version``).
    For ``kind="revision"`` (C): uses the current workspaceRevision counter as
    the revision number when ``version`` is omitted — call after a commit so
    the counter already reflects the boundary, or pass an explicit number for
    a named checkpoint.
    """
    if is_immutable_version_graph(live_graph_uri):
        raise OntologyGraphImmutable(live_graph_uri)

    shape = await load_ontology_shape(neptune, live_graph_uri)
    fp = shape.fingerprint()
    layer = layer_for_graph(live_graph_uri)

    if kind == "revision":
        if version is None:
            version = await _current_revision_counter(neptune, live_graph_uri)
            if version < 1:
                # No commits yet — still allow a checkpoint at r1.
                version = 1
        snap_uri = revision_graph_uri(live_graph_uri, version)
    else:
        if version is None:
            version = await _next_release_version(neptune, live_graph_uri, kind)
        snap_uri = release_graph_uri(live_graph_uri, version)

    parent_version: int | None = None
    parent_fp: str | None = None
    change_vs_parent: list[ChangeRecord] = []
    existing = await list_snapshots(neptune, live_graph_uri, kind=kind)
    parents = [r for r in existing if r.version < version]
    if parents:
        parent = max(parents, key=lambda r: r.version)
        parent_version = parent.version
        parent_fp = parent.fingerprint
        try:
            parent_shape = await load_ontology_shape(
                neptune, parent.snapshot_graph_uri
            )
            loaded_fp = parent_shape.fingerprint()
            # load_ontology_shape swallows per-query errors and can return an
            # empty shape. For releases that is fatal: an empty synthetic delta
            # would pass the ONTA-404 gate as additive. Match the recorded
            # parent fingerprint so silent degradation fails closed.
            if (
                kind == "release"
                and parent_fp
                and loaded_fp != parent_fp
            ):
                raise RuntimeError(
                    f"cannot classify release vs parent snapshot "
                    f"{parent.snapshot_graph_uri!r}: fingerprint mismatch "
                    f"(recorded {parent_fp!r}, loaded {loaded_fp!r}) — "
                    f"parent content unreadable or corrupted"
                )
            change_vs_parent = diff_shapes(parent_shape, shape)
        except Exception as exc:
            # Fail closed for releases (ONTA-404). Revisions stay best-effort.
            if kind == "release":
                if isinstance(exc, RuntimeError) and "cannot classify release" in str(
                    exc
                ):
                    raise
                logger.error(
                    "snapshot_parent_diff_failed",
                    parent=parent.snapshot_graph_uri,
                    live=live_graph_uri,
                    exc_info=True,
                )
                raise RuntimeError(
                    f"cannot classify release vs parent snapshot "
                    f"{parent.snapshot_graph_uri!r}: parent load/diff failed "
                    f"({type(exc).__name__}: {exc})"
                ) from exc
            logger.warning(
                "snapshot_parent_diff_failed",
                parent=parent.snapshot_graph_uri,
                exc_info=True,
            )

    return SnapshotPlan(
        live_graph_uri=live_graph_uri,
        snapshot_graph_uri=snap_uri,
        version=version,
        kind=kind,
        layer=layer,
        fingerprint=fp,
        parent_version=parent_version,
        change_records_vs_parent=change_vs_parent,
        parent_fingerprint=parent_fp,
    )


async def execute_snapshot(
    neptune,
    plan: SnapshotPlan,
    *,
    dry_run: bool = False,
    publisher: str | None = None,
    change_summary: str | None = None,
    compat_class: str | None = None,
    declare_major: bool = False,
    timestamp: str | None = None,
    parent_shape: OntologyShape | None = None,
) -> ReleaseRecord:
    """Materialize ``plan``: copy live → snapshot graph + write release record.

    Refuses to overwrite an existing snapshot graph (immutability). With
    ``dry_run=True`` returns the would-be :class:`ReleaseRecord` without writes.

    **Publish gate (ONTA-404):** for ``kind="release"`` the typed diff vs parent
    is classified; a breaking release raises :class:`OntologyCompatError` unless
    ``declare_major=True``. The stored ``compat_class`` always comes from the
    classifier — free-form ``compat_class=`` from callers is ignored for
    releases (kept on the signature only so older call sites do not break).
    ``kind="revision"`` is not gated (workspace C is automatic) but is still
    classified for metadata when cheap.
    """
    if is_immutable_version_graph(plan.live_graph_uri):
        raise OntologyGraphImmutable(plan.live_graph_uri)

    # Classify vs parent. First release (no parent / empty delta) → additive.
    if plan.kind == "release":
        verdict = assert_publishable(
            plan.change_records_vs_parent,
            declare_major=declare_major,
            parent_shape=parent_shape,
        )
        stored_compat = verdict.stored_compat_class
    else:
        # Revisions: classify for metadata; never refuse.
        verdict = classify_diff(
            plan.change_records_vs_parent, parent_shape=parent_shape,
        )
        stored_compat = verdict.stored_compat_class
        # Free-form override only meaningful for non-release (legacy). Prefer
        # classifier output; ignore arbitrary caller strings on releases above.
        if compat_class and not plan.change_records_vs_parent:
            stored_compat = compat_class

    ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    record = ReleaseRecord(
        live_graph_uri=plan.live_graph_uri,
        snapshot_graph_uri=plan.snapshot_graph_uri,
        version=plan.version,
        kind=plan.kind,
        layer=plan.layer,
        fingerprint=plan.fingerprint,
        parent_version=plan.parent_version,
        publisher=publisher,
        timestamp=ts,
        change_summary=change_summary,
        compat_class=stored_compat,
        change_records=tuple(plan.change_records_vs_parent),
    )
    if dry_run:
        return record

    async with ontology_write_lock():
        # Refuse overwrite of an existing snapshot content graph.
        try:
            existing_shape = await load_ontology_shape(
                neptune, plan.snapshot_graph_uri
            )
            if existing_shape.types or existing_shape.attrs:
                raise OntologyGraphImmutable(plan.snapshot_graph_uri)
        except OntologyGraphImmutable:
            raise
        except Exception:
            pass  # empty / missing is fine

        # GraphStore path (ONTA-531): freeze the live shape into the companion bag.
        from infona_client.graph.store import GraphConfigError, get_graph_store

        try:
            get_graph_store()
            await _write_snapshot_graph_store(
                plan,
                record=record,
                publisher=publisher,
                change_summary=change_summary,
                stored_compat=stored_compat,
                timestamp=ts,
            )
        except GraphConfigError:
            await neptune.update(
                _copy_graph_sparql(plan.live_graph_uri, plan.snapshot_graph_uri)
            )
            meta = _release_metadata_triples(
                plan,
                publisher=publisher,
                change_summary=change_summary,
                compat_class=stored_compat,
                timestamp=ts,
            )
            await neptune.update(
                insert_triples(versions_graph_uri(plan.live_graph_uri), meta)
            )
        logger.info(
            "ontology_snapshot_written",
            live=plan.live_graph_uri,
            snapshot=plan.snapshot_graph_uri,
            version=plan.version,
            kind=plan.kind,
            fingerprint=plan.fingerprint,
            publisher=publisher,
            compat_class=stored_compat,
            declare_major=declare_major,
        )
    return record


async def _write_snapshot_graph_store(
    plan: SnapshotPlan,
    *,
    record: ReleaseRecord,
    publisher: str | None,
    change_summary: str | None,
    stored_compat: str | None,
    timestamp: str,
) -> None:
    """Materialize a frozen OntologyShape + release metadata on the companion bag."""
    from infona_client.graph.ontology_commit import shape_to_dict
    from infona_client.graph.ontology_companion import (
        get_ontology_companion,
        live_graph_uri,
    )

    live = live_graph_uri(plan.live_graph_uri)
    snap_uri = plan.snapshot_graph_uri.rstrip("/")
    bag = get_ontology_companion()

    # Freeze live shape.
    live_shape = await load_ontology_shape(None, live)
    bag.frozen_shapes[snap_uri] = shape_to_dict(live_shape)

    # Append release/revision metadata (newest last).
    meta = {
        "live_graph_uri": live,
        "snapshot_graph_uri": snap_uri,
        "version": plan.version,
        "kind": plan.kind,
        "layer": plan.layer,
        "fingerprint": plan.fingerprint,
        "parent_version": plan.parent_version,
        "publisher": publisher,
        "timestamp": timestamp,
        "change_summary": change_summary,
        "compat_class": stored_compat,
        "change_records": [
            r.model_dump(mode="json", exclude_none=True)
            for r in plan.change_records_vs_parent
        ],
    }
    bag.snapshots.setdefault(live, []).append(meta)


async def snapshot_ontology(
    neptune,
    live_graph_uri: str,
    *,
    kind: SnapshotKind = "release",
    version: int | None = None,
    dry_run: bool = False,
    publisher: str | None = None,
    change_summary: str | None = None,
    compat_class: str | None = None,
    declare_major: bool = False,
) -> ReleaseRecord:
    """Plan + execute a snapshot (convenience; mirrors attr_meta_migration).

    ``declare_major`` is required to publish a breaking release (ONTA-404).
    Free-form ``compat_class`` is ignored for releases — the classifier sets it.
    """
    plan = await plan_snapshot(
        neptune, live_graph_uri, kind=kind, version=version
    )
    # Load parent shape when a parent release exists so re-parent ancestry
    # can be classified correctly at the gate.
    parent_shape: OntologyShape | None = None
    if plan.parent_version is not None and plan.kind == "release":
        existing = await list_snapshots(neptune, live_graph_uri, kind=kind)
        for rec in existing:
            if rec.version == plan.parent_version:
                try:
                    parent_shape = await load_ontology_shape(
                        neptune, rec.snapshot_graph_uri
                    )
                except Exception:
                    parent_shape = None
                break
    return await execute_snapshot(
        neptune,
        plan,
        dry_run=dry_run,
        publisher=publisher,
        change_summary=change_summary,
        compat_class=compat_class,
        declare_major=declare_major,
        parent_shape=parent_shape,
    )

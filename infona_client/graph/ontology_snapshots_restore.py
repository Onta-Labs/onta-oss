"""Restore a live ontology from a snapshot + cleanup version artifacts."""

from __future__ import annotations

import structlog

from infona_client.graph.ontology_commit import (
    OntologyGraphImmutable,
    is_immutable_version_graph,
    load_ontology_shape,
    ontology_write_lock,
    release_graph_uri,
    revision_graph_uri,
    versions_graph_uri,
)
from infona_client.graph.ontology_snapshots_list import get_snapshot, list_snapshots
from infona_client.graph.ontology_snapshots_models import RestorePlan, SnapshotKind
from infona_client.graph.ontology_snapshots_sparql import (
    _clear_graph_sparql,
    _copy_graph_sparql,
    _drop_graph_sparql,
)

logger = structlog.stdlib.get_logger("infona.graph.ontology_snapshots")


async def plan_restore(
    neptune,
    live_graph_uri: str,
    version: int,
    *,
    kind: SnapshotKind = "release",
) -> RestorePlan:
    """Plan restoring ``live_graph_uri`` from snapshot ``version`` (no writes)."""
    if is_immutable_version_graph(live_graph_uri):
        raise OntologyGraphImmutable(live_graph_uri)

    rec = await get_snapshot(neptune, live_graph_uri, version, kind=kind)
    if rec is None:
        if kind == "revision":
            snap_uri = revision_graph_uri(live_graph_uri, version)
        else:
            snap_uri = release_graph_uri(live_graph_uri, version)
        snap_shape = await load_ontology_shape(neptune, snap_uri)
        if not snap_shape.types and not snap_shape.attrs:
            raise ValueError(
                f"no {kind} snapshot v{version} for {live_graph_uri!r}"
            )
        fp_after = snap_shape.fingerprint()
    else:
        snap_uri = rec.snapshot_graph_uri
        fp_after = rec.fingerprint

    fp_before = (await load_ontology_shape(neptune, live_graph_uri)).fingerprint()
    return RestorePlan(
        live_graph_uri=live_graph_uri,
        snapshot_graph_uri=snap_uri,
        version=version,
        kind=kind,
        fingerprint_before=fp_before,
        fingerprint_after=fp_after,
    )


async def execute_restore(
    neptune,
    plan: RestorePlan,
    *,
    dry_run: bool = False,
    actor: str | None = None,
) -> str:
    """Replace live ontology content with the snapshot. Returns fingerprint after.

    Clears the live graph then copies the snapshot. Does **not** write into the
    snapshot graph (immutability). With ``dry_run=True`` returns the planned
    fingerprint without writing.
    """
    if is_immutable_version_graph(plan.live_graph_uri):
        raise OntologyGraphImmutable(plan.live_graph_uri)
    if is_immutable_version_graph(plan.snapshot_graph_uri):
        # Snapshot is immutable — that's expected; we only READ it. Guard is
        # that we never pass snapshot as the *target* of clear/copy-into.
        pass

    if dry_run:
        return plan.fingerprint_after

    async with ontology_write_lock():
        from infona_client.graph.store import GraphConfigError, get_graph_store

        try:
            get_graph_store()
            after = await _execute_restore_graph_store(plan)
        except GraphConfigError:
            # Clear live, then copy snapshot → live. Never clear the snapshot.
            await neptune.update(_clear_graph_sparql(plan.live_graph_uri))
            await neptune.update(
                _copy_graph_sparql(plan.snapshot_graph_uri, plan.live_graph_uri)
            )
            after = (
                await load_ontology_shape(neptune, plan.live_graph_uri)
            ).fingerprint()
        logger.info(
            "ontology_restored",
            live=plan.live_graph_uri,
            snapshot=plan.snapshot_graph_uri,
            version=plan.version,
            fingerprint_before=plan.fingerprint_before,
            fingerprint_after=after,
            actor=actor,
        )
        return after


async def _execute_restore_graph_store(plan: RestorePlan) -> str:
    """Replace live catalog content with a frozen snapshot shape (ONTA-531)."""
    from infona_client.graph import ontology_catalog as oc
    from infona_client.graph.ontology_commit import (
        shape_from_dict,
        shape_to_dict,
    )
    from infona_client.graph.ontology_companion import (
        catalog_session_kwargs,
        catalog_target_from_graph_uri,
        get_ontology_companion,
        live_graph_uri,
    )

    live = live_graph_uri(plan.live_graph_uri)
    snap_uri = plan.snapshot_graph_uri.rstrip("/")
    bag = get_ontology_companion()
    frozen = bag.frozen_shapes.get(snap_uri)
    if frozen is None:
        raise ValueError(
            f"no frozen shape for snapshot {snap_uri!r}; cannot restore"
        )
    shape = shape_from_dict(frozen)
    target = catalog_target_from_graph_uri(live)
    cat_kw = catalog_session_kwargs(target)
    cat_read = catalog_session_kwargs(target, for_write=False)

    # Drop every live type/attr, then re-apply the frozen shape.
    for a in await oc.list_attributes(**cat_read):
        await oc.delete_attribute(a.domain, a.name, **cat_kw)
    for t in await oc.list_types(**cat_read):
        await oc.delete_type(t.name, **cat_kw)

    for t_name, desc in shape.types.items():
        parent = shape.parent_of.get(t_name)
        await oc.upsert_type(
            name=t_name,
            description=desc or "",
            parent_type=parent,
            clear_parent=parent is not None,
            **cat_kw,
        )
        dep_sup = shape.deprecated_types.get(t_name)
        if t_name in shape.deprecated_types:
            await oc.set_type_markers(
                t_name,
                deprecated_at="restored",
                superseded_by=dep_sup or None,
                **cat_kw,
            )

    core_set = {
        (tuple(c) if isinstance(c, list) else c) for c in shape.core_slots
    }
    for t_name, slots in shape.attrs.items():
        comments = shape.attr_comments.get(t_name) or {}
        for slot, dt in slots.items():
            await oc.upsert_attribute(
                type_name=t_name,
                attr_name=slot,
                description=comments.get(slot) or "",
                datatype=dt or "string",
                **cat_kw,
            )
            core = (t_name, slot) in core_set
            tk = shape.text_kinds.get((t_name, slot))
            dep_present = (t_name, slot) in shape.deprecated_slots
            dep = shape.deprecated_slots.get((t_name, slot)) if dep_present else None
            if core or tk or dep_present:
                await oc.set_attr_markers(
                    t_name,
                    slot,
                    core_slot=True if core else None,
                    text_kind=tk,
                    deprecated_at="restored" if dep_present else None,
                    superseded_by=dep or None,
                    **cat_kw,
                )

    # Restore alias map for this live graph from the frozen shape.
    bag.aliases[live] = dict(shape.alias_map or {})

    after_shape = await load_ontology_shape(None, live)
    # Re-freeze identity so a second restore is stable.
    bag.frozen_shapes[snap_uri] = shape_to_dict(after_shape)
    return after_shape.fingerprint()


async def restore_ontology(
    neptune,
    live_graph_uri: str,
    version: int,
    *,
    kind: SnapshotKind = "release",
    dry_run: bool = False,
    actor: str | None = None,
) -> str:
    """Plan + execute restore. Returns post-restore fingerprint."""
    plan = await plan_restore(neptune, live_graph_uri, version, kind=kind)
    return await execute_restore(neptune, plan, dry_run=dry_run, actor=actor)


# ---------------------------------------------------------------------------
# Cleanup — drop version artifacts when a tenant/live graph is deleted
# ---------------------------------------------------------------------------


async def list_version_artifact_uris(
    neptune, live_graph_uri: str
) -> list[str]:
    """All named graphs that are version artifacts of ``live_graph_uri``.

    Includes the versions companion, every recorded snapshot content graph,
    and (defensively) the changelog companion so a tenant wipe leaves no
    orphans. Does **not** include the live graph itself.
    """
    uris: list[str] = [
        versions_graph_uri(live_graph_uri),
        f"{live_graph_uri.rstrip('/')}/changelog",
    ]
    for rec in await list_snapshots(neptune, live_graph_uri):
        if rec.snapshot_graph_uri:
            uris.append(rec.snapshot_graph_uri)
    # De-dupe, preserve order.
    seen: set[str] = set()
    out: list[str] = []
    for u in uris:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


async def plan_cleanup_version_artifacts(
    neptune, live_graph_uri: str
) -> list[str]:
    """Return the list of graphs that would be dropped (dry-runable)."""
    return await list_version_artifact_uris(neptune, live_graph_uri)


async def cleanup_version_artifacts(
    neptune,
    live_graph_uri: str,
    *,
    dry_run: bool = False,
) -> list[str]:
    """DROP every version/revision/changelog companion of ``live_graph_uri``.

    Called when a tenant (or its ontology graph) is deleted so snapshot graphs
    cannot orphan. Safe to call when nothing exists (``DROP SILENT``).
    Returns the list of graphs dropped (or that would be, if ``dry_run``).
    """
    uris = await plan_cleanup_version_artifacts(neptune, live_graph_uri)
    if dry_run or not uris:
        return uris

    from infona_client.graph.store import GraphConfigError, get_graph_store

    try:
        get_graph_store()
        _cleanup_version_artifacts_graph_store(live_graph_uri)
    except GraphConfigError:
        # One DROP per graph — some stores reject multi-graph DROP lists.
        for u in uris:
            try:
                await neptune.update(_drop_graph_sparql(u))
            except Exception:
                logger.warning(
                    "version_artifact_drop_failed", graph_uri=u, exc_info=True
                )
    logger.info(
        "ontology_version_artifacts_cleaned",
        live=live_graph_uri,
        dropped=len(uris),
    )
    return uris


def _cleanup_version_artifacts_graph_store(live_graph_uri: str) -> None:
    """Clear companion bag entries for a live ontology graph (ONTA-531)."""
    from infona_client.graph.ontology_companion import (
        get_ontology_companion,
        live_graph_uri as _live,
    )

    live = _live(live_graph_uri)
    bag = get_ontology_companion()
    for rec in list(bag.snapshots.get(live) or []):
        snap = (rec.get("snapshot_graph_uri") or "").rstrip("/")
        if snap:
            bag.frozen_shapes.pop(snap, None)
    bag.snapshots.pop(live, None)
    bag.changelog.pop(live, None)
    bag.revisions.pop(live, None)
    bag.aliases.pop(live, None)


async def cleanup_tenant_version_artifacts(
    neptune, tenant_id: str, *, dry_run: bool = False
) -> list[str]:
    """Cleanup helper keyed by tenant id (workspace ontology graph)."""
    from infona_client.graph.queries import tenant_graph_uri

    return await cleanup_version_artifacts(
        neptune, tenant_graph_uri(tenant_id), dry_run=dry_run
    )

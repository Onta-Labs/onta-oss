"""GraphStore commit / apply-one path for ontology schema mutations.

Looks up patched names on :mod:`infona_client.graph.ontology_commit` via
``_host()``.
"""

from __future__ import annotations

from typing import Sequence
from uuid import uuid4

import structlog

from infona_client.graph.iri import GRAPH_URI_PREFIX
from infona_client.graph.ontology_commit_core import (
    OntologyVersionConflict,
    _GOV_NS,
)
from infona_client.models.ontology import (
    ChangeRecord,
    OntologyCommitResult,
    OntologyMutation,
    OntologyOpKind,
)

logger = structlog.stdlib.get_logger("infona.graph.ontology_commit")


def _host():
    """Call-time lookup of the public ontology_commit module (monkeypatch surface)."""
    from infona_client.graph import ontology_commit as _mod

    return _mod


async def _commit_ontology_graph_store(
    graph_uri: str,
    mutations: Sequence[OntologyMutation],
    *,
    expected_version: str | None = None,
    actor: str | None = None,
    message: str | None = None,
) -> OntologyCommitResult:
    """Apply schema mutations via GraphStore catalog (Neo4j product path).

    Full ONTA-531 surface: all 13 op kinds, real fingerprints, optimistic
    concurrency, revision bump, and changelog. Catalog scope is derived from
    the graph URI (global public/enhanced layers included).
    """
    from infona_client.graph.ontology_companion import (
        catalog_session_kwargs,
        catalog_target_from_graph_uri,
        get_ontology_companion,
    )

    target = catalog_target_from_graph_uri(graph_uri)
    live = target.live_graph_uri
    cat_kw = catalog_session_kwargs(target)

    version_before = await _host().fingerprint_ontology(None, live)
    if expected_version is not None and expected_version != version_before:
        raise OntologyVersionConflict(expected_version, version_before, graph_uri)

    applied: list[OntologyMutation] = []
    change_records: list[ChangeRecord] = []
    for mut in mutations:
        records = await _host()._apply_one_graph_store(
            mut, cat_kw=cat_kw, graph_uri=live
        )
        applied.append(mut)
        change_records.extend(records)

    version_after = (
        version_before
        if not applied
        else await _host().fingerprint_ontology(None, live)
    )

    revision: int | None = None
    if applied:
        revision = await _host()._bump_revision_graph_store(live)
        await _host()._emit_changelog_graph_store(
            live,
            version_before=version_before,
            version_after=version_after,
            actor=actor,
            message=message,
            change_records=change_records,
            revision=revision,
        )
        # Touch companion so hermetic stores keep a bag even with zero aliases.
        get_ontology_companion()
        logger.info(
            "ontology_committed_graph_store",
            graph_uri=live,
            n_mutations=len(applied),
            version_before=version_before,
            version_after=version_after,
            revision=revision,
            actor=actor,
            message=message,
            layer=target.layer,
        )

    return OntologyCommitResult(
        graph_uri=live,
        version_before=version_before,
        version_after=version_after,
        applied=list(applied),
        change_records=change_records,
    )


async def _apply_one_graph_store(
    mut: OntologyMutation,
    *,
    cat_kw: dict,
    graph_uri: str,
) -> list[ChangeRecord]:
    """Apply one mutation on the property-graph catalog + companion bag."""
    from infona_client.graph import ontology_catalog as oc
    from infona_client.models.ontology import ChangeKind, ChangeRecord

    op = mut.op
    if op is OntologyOpKind.UPSERT_TYPE:
        # Mirror SPARQL _apply_upsert_type: only set/replace parent when
        # parent_type is explicitly provided. Bare re-UPSERT_TYPE and
        # description-only updates must preserve existing SUBCLASS_OF
        # (SPARQL uses insert_type / upsert_type_comment, never clear).
        await oc.upsert_type(
            name=mut.type_name,
            description=mut.description or "",
            parent_type=mut.parent_type,
            clear_parent=False,
            **cat_kw,
        )
        records = [ChangeRecord(kind=ChangeKind.ADD_TYPE, type_name=mut.type_name)]
        if mut.parent_type:
            records.append(
                ChangeRecord(
                    kind=ChangeKind.ADD_SUBCLASS,
                    type_name=mut.type_name,
                    parent_type=mut.parent_type,
                )
            )
        if mut.description:
            records.append(
                ChangeRecord(
                    kind=ChangeKind.CHANGE_COMMENT,
                    type_name=mut.type_name,
                    new_value=mut.description,
                )
            )
        return records

    if op is OntologyOpKind.UPSERT_ATTRIBUTE:
        if not mut.slot_name:
            raise ValueError("UPSERT_ATTRIBUTE requires slot_name")
        datatype = mut.datatype or "string"
        await oc.upsert_attribute(
            type_name=mut.type_name,
            attr_name=mut.slot_name,
            description=mut.description or "",
            datatype=datatype,
            **cat_kw,
        )
        return [
            ChangeRecord(
                kind=ChangeKind.ADD_ATTRIBUTE,
                type_name=mut.type_name,
                slot_name=mut.slot_name,
                new_value=datatype,
            )
        ]

    if op is OntologyOpKind.UPSERT_RELATIONSHIP:
        if not mut.slot_name or not mut.target_type:
            raise ValueError("UPSERT_RELATIONSHIP requires slot_name and target_type")
        await oc.upsert_attribute(
            type_name=mut.type_name,
            attr_name=mut.slot_name,
            description=mut.description or "",
            datatype=mut.target_type,
            **cat_kw,
        )
        return [
            ChangeRecord(
                kind=ChangeKind.ADD_RELATIONSHIP
                if mut.description is not None
                else ChangeKind.CHANGE_RANGE,
                type_name=mut.type_name,
                slot_name=mut.slot_name,
                new_value=mut.target_type,
            )
        ]

    if op is OntologyOpKind.SET_SUBCLASS:
        if not mut.parent_type:
            raise ValueError("SET_SUBCLASS requires parent_type")
        await oc.upsert_type(
            name=mut.type_name,
            parent_type=mut.parent_type,
            clear_parent=True,
            **cat_kw,
        )
        return [
            ChangeRecord(
                kind=ChangeKind.ADD_SUBCLASS,
                type_name=mut.type_name,
                parent_type=mut.parent_type,
            )
        ]

    if op is OntologyOpKind.DELETE_ATTRIBUTE:
        if not mut.slot_name:
            raise ValueError("DELETE_ATTRIBUTE requires slot_name")
        await oc.delete_attribute(
            mut.type_name, mut.slot_name, **cat_kw
        )
        return [
            ChangeRecord(
                kind=ChangeKind.REMOVE_ATTRIBUTE,
                type_name=mut.type_name,
                slot_name=mut.slot_name,
            )
        ]

    if op is OntologyOpKind.DELETE_TYPE:
        await oc.delete_type(mut.type_name, **cat_kw)
        return [ChangeRecord(kind=ChangeKind.REMOVE_TYPE, type_name=mut.type_name)]

    if op is OntologyOpKind.SET_CORE_SLOT:
        if not mut.slot_name:
            raise ValueError("SET_CORE_SLOT requires slot_name")
        # Marker-only: do not upsert (B2 reserved leaves like `name` must not
        # be re-minted here; SPARQL mark_core_slot was also attach-only).
        await oc.set_attr_markers(
            mut.type_name,
            mut.slot_name,
            core_slot=False if mut.core_slot is False else True,
            **cat_kw,
        )
        return [
            ChangeRecord(
                kind=ChangeKind.CHANGE_CORE_SLOT,
                type_name=mut.type_name,
                slot_name=mut.slot_name,
                new_value="true" if mut.core_slot is not False else "false",
            )
        ]

    if op is OntologyOpKind.SET_TEXT_KIND:
        # ONTA-533 coherent path: dedicated set_attribute_text_kind (MERGE
        # stub + empty clears) + marker-cache invalidation so reconciler /
        # request path see the verdict immediately. set_attr_markers remains
        # for core_slot / deprecation companions (ONTA-531).
        if not mut.slot_name:
            raise ValueError("SET_TEXT_KIND requires slot_name")
        kind = mut.text_kind or ""
        await oc.set_attribute_text_kind(
            type_name=mut.type_name,
            attr_name=mut.slot_name,
            text_kind=kind,
            **cat_kw,
        )
        try:
            from infona_client.graph.text_markers import invalidate

            tid = cat_kw.get("tenant_id")
            if tid:
                invalidate(tid)
        except Exception:  # noqa: BLE001 — never fail a commit on cache
            pass
        return [
            ChangeRecord(
                kind=ChangeKind.CHANGE_TEXT_KIND,
                type_name=mut.type_name,
                slot_name=mut.slot_name,
                new_value=kind or None,
            )
        ]

    if op is OntologyOpKind.SET_COMMENT:
        if mut.slot_name:
            await oc.upsert_attribute(
                type_name=mut.type_name,
                attr_name=mut.slot_name,
                description=mut.description or "",
                datatype=mut.datatype or "string",
                **cat_kw,
            )
            return [
                ChangeRecord(
                    kind=ChangeKind.CHANGE_COMMENT,
                    type_name=mut.type_name,
                    slot_name=mut.slot_name,
                    new_value=mut.description,
                )
            ]
        # Type-level comment — never clear an existing parent edge.
        await oc.upsert_type(
            name=mut.type_name,
            description=mut.description or "",
            parent_type=None,
            clear_parent=False,
            **cat_kw,
        )
        # Also stamp description via markers path so empty-string clears work.
        await oc.set_type_markers(
            mut.type_name,
            description=mut.description or "",
            **cat_kw,
        )
        return [
            ChangeRecord(
                kind=ChangeKind.CHANGE_COMMENT,
                type_name=mut.type_name,
                new_value=mut.description,
            )
        ]

    if op is OntologyOpKind.REGISTER_ALIAS:
        return await _host()._apply_register_alias_graph_store(
            mut, graph_uri=graph_uri
        )

    if op is OntologyOpKind.RENAME_ATTRIBUTE:
        return await _host()._apply_rename_attribute_graph_store(
            mut, cat_kw=cat_kw, graph_uri=graph_uri
        )

    if op is OntologyOpKind.RETIRE_ALIAS:
        return await _host()._apply_retire_alias_graph_store(mut, graph_uri=graph_uri)

    if op is OntologyOpKind.DEPRECATE:
        return await _host()._apply_deprecate_graph_store(mut, cat_kw=cat_kw)

    raise ValueError(f"unknown ontology op: {op!r}")


async def _bump_revision_graph_store(graph_uri: str) -> int:
    from infona_client.graph.ontology_companion import get_ontology_companion

    bag = get_ontology_companion()
    nxt = int(bag.revisions.get(graph_uri, 0)) + 1
    bag.revisions[graph_uri] = nxt
    return nxt


async def _emit_changelog_graph_store(
    graph_uri: str,
    *,
    version_before: str,
    version_after: str,
    actor: str | None,
    message: str | None,
    change_records: list[ChangeRecord],
    revision: int,
) -> None:
    from infona_client.graph.ontology_changelog import serialize_change_records
    from infona_client.graph.ontology_companion import get_ontology_companion

    ts = _host().datetime.now(_host().timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry_uri = f"{_GOV_NS}log/{uuid4()}"
    tenant_id = ""
    prefix = GRAPH_URI_PREFIX
    if graph_uri.startswith(prefix):
        rest = graph_uri[len(prefix):]
        tenant_id = rest.split("/", 1)[0]
    entry = {
        "entry_uri": entry_uri,
        "action": "commit_ontology",
        "subject": graph_uri,
        "timestamp": ts,
        "tenant_id": tenant_id or None,
        "actor": actor,
        "message": message,
        "version_before": version_before,
        "version_after": version_after,
        "revision": revision,
        "delta": serialize_change_records(change_records),
    }
    bag = get_ontology_companion()
    bag.changelog.setdefault(graph_uri, []).insert(0, entry)

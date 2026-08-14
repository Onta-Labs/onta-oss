"""GraphStore alias / deprecate / reference-count ops for ontology commits.

Looks up patched names on :mod:`infona_client.graph.ontology_commit` via
``_host()``.
"""

from __future__ import annotations

import re

import structlog

from infona_client.models.ontology import ChangeRecord, OntologyMutation

logger = structlog.stdlib.get_logger("infona.graph.ontology_commit")


def _host():
    """Call-time lookup of the public ontology_commit module (monkeypatch surface)."""
    from infona_client.graph import ontology_commit as _mod

    return _mod


async def _apply_deprecate_graph_store(
    mut: OntologyMutation, *, cat_kw: dict
) -> list[ChangeRecord]:
    from infona_client.graph import ontology_catalog as oc
    from infona_client.models.ontology import ChangeKind, ChangeRecord

    if not mut.type_name:
        raise ValueError("DEPRECATE requires type_name")
    ts = _host().datetime.now(_host().timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sup_leaf = ""
    if mut.superseded_by:
        raw = mut.superseded_by.strip()
        sup_leaf = raw.rsplit("/", 1)[-1] if raw else ""

    if mut.slot_name:
        await oc.upsert_attribute(
            type_name=mut.type_name,
            attr_name=mut.slot_name,
            datatype=mut.datatype or "string",
            description=mut.description or "",
            **cat_kw,
        )
        await oc.set_attr_markers(
            mut.type_name,
            mut.slot_name,
            deprecated_at=ts,
            superseded_by=sup_leaf or None,
            **cat_kw,
        )
    else:
        await oc.upsert_type(
            name=mut.type_name,
            description=mut.description or "",
            clear_parent=False,
            **cat_kw,
        )
        await oc.set_type_markers(
            mut.type_name,
            deprecated_at=ts,
            superseded_by=sup_leaf or None,
            **cat_kw,
        )
    return [
        ChangeRecord(
            kind=ChangeKind.DEPRECATE,
            type_name=mut.type_name,
            slot_name=mut.slot_name,
            superseded_by=mut.superseded_by,
            new_value=ts,
        )
    ]


async def _apply_register_alias_graph_store(
    mut: OntologyMutation, *, graph_uri: str
) -> list[ChangeRecord]:
    from infona_client.graph.ontology_companion import get_ontology_companion
    from infona_client.models.ontology import ChangeKind, ChangeRecord

    if not mut.alias_from or not mut.alias_to:
        raise ValueError("REGISTER_ALIAS requires alias_from and alias_to")
    old_uri = _host()._resolve_attr_endpoint(
        mut.alias_from, type_name=mut.type_name, op_label="REGISTER_ALIAS",
    )
    new_uri = _host()._resolve_attr_endpoint(
        mut.alias_to,
        type_name=mut.type_name,
        target_type=mut.target_type,
        op_label="REGISTER_ALIAS",
    )
    if old_uri == new_uri:
        raise ValueError(
            f"alias must point to a different attribute, got {old_uri} -> itself"
        )
    bag = get_ontology_companion()
    bag.aliases.setdefault(graph_uri, {})[old_uri] = new_uri
    from_name = _host()._leaf_name(mut.alias_from, old_uri)
    to_name = _host()._leaf_name(mut.alias_to, new_uri)
    return [
        ChangeRecord(
            kind=ChangeKind.RENAME_WITH_ALIAS,
            type_name=mut.type_name or None,
            slot_name=from_name if not mut.alias_from.startswith("http") else None,
            from_name=from_name,
            to_name=to_name,
            old_value=old_uri,
            new_value=new_uri,
        )
    ]


async def _apply_rename_attribute_graph_store(
    mut: OntologyMutation, *, cat_kw: dict, graph_uri: str
) -> list[ChangeRecord]:
    from infona_client.graph import ontology_catalog as oc
    from infona_client.graph.ontology_companion import get_ontology_companion
    from infona_client.models.ontology import ChangeKind, ChangeRecord

    old_leaf = mut.alias_from or mut.slot_name
    if not old_leaf or not mut.alias_to:
        raise ValueError(
            "RENAME_ATTRIBUTE requires alias_from (or slot_name) and alias_to"
        )
    if not mut.type_name:
        raise ValueError("RENAME_ATTRIBUTE requires type_name")

    old_uri = _host()._resolve_attr_endpoint(
        old_leaf, type_name=mut.type_name, op_label="RENAME_ATTRIBUTE",
    )
    new_owner = (mut.target_type or mut.type_name).strip()
    new_uri = _host()._resolve_attr_endpoint(
        mut.alias_to,
        type_name=mut.type_name,
        target_type=mut.target_type,
        op_label="RENAME_ATTRIBUTE",
    )
    if old_uri == new_uri:
        raise ValueError(
            f"RENAME_ATTRIBUTE must change the attribute, got {old_uri} -> itself"
        )

    from_name = _host()._leaf_name(old_leaf, old_uri)
    to_name = _host()._leaf_name(mut.alias_to, new_uri)
    datatype = mut.datatype or "string"
    records: list[ChangeRecord] = []

    await oc.upsert_attribute(
        type_name=new_owner,
        attr_name=to_name,
        description=mut.description or "",
        datatype=datatype,
        **cat_kw,
    )
    records.append(
        ChangeRecord(
            kind=ChangeKind.ADD_ATTRIBUTE,
            type_name=new_owner,
            slot_name=to_name,
            new_value=datatype,
        )
    )

    if not old_leaf.startswith("http"):
        await oc.delete_attribute(mut.type_name, from_name, **cat_kw)
    records.append(
        ChangeRecord(
            kind=ChangeKind.REMOVE_ATTRIBUTE,
            type_name=mut.type_name,
            slot_name=from_name,
        )
    )

    bag = get_ontology_companion()
    bag.aliases.setdefault(graph_uri, {})[old_uri] = new_uri
    records.append(
        ChangeRecord(
            kind=ChangeKind.RENAME_WITH_ALIAS,
            type_name=mut.type_name,
            slot_name=from_name if not old_leaf.startswith("http") else None,
            from_name=from_name,
            to_name=to_name,
            old_value=old_uri,
            new_value=new_uri,
        )
    )
    return records


async def _apply_retire_alias_graph_store(
    mut: OntologyMutation, *, graph_uri: str
) -> list[ChangeRecord]:
    from infona_client.graph.ontology_companion import get_ontology_companion
    from infona_client.models.ontology import ChangeKind, ChangeRecord
    from infona_client.graph.aliases import AliasStillReferencedError

    old_leaf = mut.alias_from or mut.slot_name
    if not old_leaf:
        raise ValueError("RETIRE_ALIAS requires alias_from (or slot_name)")
    if not mut.data_graph_uri:
        raise ValueError(
            "RETIRE_ALIAS requires data_graph_uri for the instance reference check"
        )
    old_uri = _host()._resolve_attr_endpoint(
        old_leaf, type_name=mut.type_name, op_label="RETIRE_ALIAS",
    )
    remaining = await _host()._count_attr_references_graph_store(
        mut.data_graph_uri, old_uri
    )
    if remaining > 0:
        raise AliasStillReferencedError(old_uri, remaining, mut.data_graph_uri)

    bag = get_ontology_companion()
    amap = bag.aliases.get(graph_uri) or {}
    amap.pop(old_uri, None)
    if amap:
        bag.aliases[graph_uri] = amap
    else:
        bag.aliases.pop(graph_uri, None)

    from_name = _host()._leaf_name(old_leaf, old_uri)
    return [
        ChangeRecord(
            kind=ChangeKind.RENAME_WITH_ALIAS,
            type_name=mut.type_name or None,
            slot_name=from_name if not old_leaf.startswith("http") else None,
            from_name=from_name,
            to_name=None,
            old_value=old_uri,
            new_value=None,
        )
    ]


async def _count_attr_references_graph_store(
    data_graph_uri: str, attr_uri_s: str
) -> int:
    """Count instance facts that still use ``attr_uri_s`` (leaf property).

    Fail-closed: unparseable data graph URI or a failed store probe raises so
    ``RETIRE_ALIAS`` cannot succeed without a real zero-count check.
    """
    from infona_client.graph.store import get_graph_store
    from infona_client.graph.scope import GraphScope

    # leaf from …/attrs/<leaf> or …/properties/<leaf>
    leaf = attr_uri_s.rsplit("/", 1)[-1]
    # Parse tenant + kg from …/graphs/{tenant}/kg/{kg}
    m = re.search(r"/graphs/([^/]+)/kg/([^/]+)", data_graph_uri or "")
    if not m:
        m = re.search(r"/graphs/([^/]+)", data_graph_uri or "")
        if not m:
            raise ValueError(
                f"cannot parse tenant/kg from data_graph_uri={data_graph_uri!r} "
                f"for attribute reference count; refusing RETIRE_ALIAS"
            )
        tenant_id, kg = m.group(1), "main"
    else:
        tenant_id, kg = m.group(1), m.group(2)

    store = get_graph_store()
    # Prefer assertion / entity prop scan on MemoryGraphStore.
    n = 0
    entities = getattr(store, "_entities", None)
    if isinstance(entities, dict):
        for (t, k, _eid), row in entities.items():
            if t != tenant_id or k != kg:
                continue
            props = getattr(row, "props", None) or {}
            if leaf in props:
                n += 1
        if n:
            return n
    assertions = getattr(store, "_assertions", None)
    if isinstance(assertions, dict):
        prop_id_suffix = f"/{leaf}"
        for (t, k, _aid), row in assertions.items():
            if t != tenant_id or k != kg:
                continue
            pid = getattr(row, "property_id", "") or ""
            if pid.endswith(prop_id_suffix) or pid.rsplit("/", 1)[-1] == leaf:
                n += 1
        return n

    # Neo4j: COUNT entities that have the property key set.
    try:
        session = store.session(GraphScope.for_instance(tenant_id, kg))
        rows = await session.execute_read(
            "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
            f"WHERE e.`{leaf}` IS NOT NULL "
            "RETURN count(e) AS n",
            {},
        )
        if rows:
            return int(rows[0].get("n") or 0)
        return 0
    except Exception as exc:
        logger.warning(
            "attr_reference_count_failed",
            data_graph_uri=data_graph_uri,
            attr=attr_uri_s,
            exc_info=True,
        )
        raise RuntimeError(
            f"attribute reference count failed for {attr_uri_s!r} in "
            f"{data_graph_uri!r}; refusing RETIRE_ALIAS"
        ) from exc

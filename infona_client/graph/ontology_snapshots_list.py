"""List / get snapshot records (GraphStore + residual SPARQL)."""

from __future__ import annotations

import json

import structlog

from infona_client.graph.ontology_commit import (
    load_ontology_shape,
    versions_graph_uri,
)
from infona_client.graph.ontology_snapshots_models import (
    ReleaseRecord,
    SnapshotKind,
    _REL_COMPAT,
    _REL_DELTA,
    _REL_FINGERPRINT,
    _REL_KIND,
    _REL_LAYER,
    _REL_OF,
    _REL_PARENT,
    _REL_PUBLISHER,
    _REL_SNAPSHOT,
    _REL_SUMMARY,
    _REL_TIMESTAMP,
    _REL_VERSION,
    layer_for_graph,
)
from infona_client.graph.parser import parse_sparql_results
from infona_client.models.ontology import ChangeRecord

logger = structlog.stdlib.get_logger("infona.graph.ontology_snapshots")


def _parse_int_lit(val: str | None) -> int | None:
    if val is None:
        return None
    try:
        return int(str(val).split("^")[0].strip('"'))
    except (TypeError, ValueError):
        return None


def _parse_change_records(raw: str | None) -> tuple[ChangeRecord, ...]:
    if not raw:
        return ()
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return ()
    out: list[ChangeRecord] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or "kind" not in item:
            continue
        try:
            out.append(ChangeRecord.model_validate(item))
        except Exception:
            continue
    return tuple(out)


async def list_snapshots(
    neptune,
    live_graph_uri: str,
    *,
    kind: SnapshotKind | None = None,
) -> list[ReleaseRecord]:
    """List release/revision records for ``live_graph_uri`` (newest last)."""
    from infona_client.graph.store import GraphConfigError, get_graph_store

    try:
        get_graph_store()
        return _list_snapshots_graph_store(live_graph_uri, kind=kind)
    except GraphConfigError:
        pass
    except Exception:
        logger.warning(
            "list_snapshots_failed", graph_uri=live_graph_uri, exc_info=True
        )
        return []

    rev_graph = versions_graph_uri(live_graph_uri)
    q = (
        f"SELECT ?s ?version ?parent ?layer ?kind ?fp ?snap ?pub ?ts ?sum ?compat ?delta\n"
        f"FROM <{rev_graph}> WHERE {{\n"
        f"  ?s <{_REL_OF}> <{live_graph_uri}> ;\n"
        f"     <{_REL_VERSION}> ?version ;\n"
        f"     <{_REL_SNAPSHOT}> ?snap ;\n"
        f"     <{_REL_FINGERPRINT}> ?fp ;\n"
        f"     <{_REL_KIND}> ?kind ;\n"
        f"     <{_REL_LAYER}> ?layer .\n"
        f"  OPTIONAL {{ ?s <{_REL_PARENT}> ?parent }}\n"
        f"  OPTIONAL {{ ?s <{_REL_PUBLISHER}> ?pub }}\n"
        f"  OPTIONAL {{ ?s <{_REL_TIMESTAMP}> ?ts }}\n"
        f"  OPTIONAL {{ ?s <{_REL_SUMMARY}> ?sum }}\n"
        f"  OPTIONAL {{ ?s <{_REL_COMPAT}> ?compat }}\n"
        f"  OPTIONAL {{ ?s <{_REL_DELTA}> ?delta }}\n"
        f"}}"
    )
    try:
        raw = await neptune.query(q)
        _, rows = parse_sparql_results(raw)
    except Exception:
        logger.warning(
            "list_snapshots_failed", graph_uri=live_graph_uri, exc_info=True
        )
        return []

    out: list[ReleaseRecord] = []
    for row in rows:
        k = (row.get("kind") or "release").strip('"')
        if kind is not None and k != kind:
            continue
        ver = _parse_int_lit(row.get("version"))
        if ver is None:
            continue
        out.append(
            ReleaseRecord(
                live_graph_uri=live_graph_uri,
                snapshot_graph_uri=(row.get("snap") or "").strip('"'),
                version=ver,
                kind=k if k in ("release", "revision") else "release",  # type: ignore[arg-type]
                layer=(row.get("layer") or layer_for_graph(live_graph_uri)).strip('"'),  # type: ignore[arg-type]
                fingerprint=(row.get("fp") or "").strip('"'),
                parent_version=_parse_int_lit(row.get("parent")),
                publisher=(row.get("pub") or None),
                timestamp=(row.get("ts") or None),
                change_summary=(row.get("sum") or None),
                compat_class=(row.get("compat") or None),
                change_records=_parse_change_records(row.get("delta")),
            )
        )
    out.sort(key=lambda r: (r.kind, r.version))
    return out


def _list_snapshots_graph_store(
    live_graph_uri: str,
    *,
    kind: SnapshotKind | None = None,
) -> list[ReleaseRecord]:
    from infona_client.graph.ontology_companion import (
        get_ontology_companion,
        live_graph_uri as _live,
    )

    live = _live(live_graph_uri)
    bag = get_ontology_companion()
    out: list[ReleaseRecord] = []
    for row in bag.snapshots.get(live) or []:
        k = row.get("kind") or "release"
        if kind is not None and k != kind:
            continue
        ver = row.get("version")
        if ver is None:
            continue
        recs_raw = row.get("change_records") or []
        recs: list[ChangeRecord] = []
        for item in recs_raw:
            if isinstance(item, dict) and "kind" in item:
                try:
                    recs.append(ChangeRecord.model_validate(item))
                except Exception:
                    continue
        out.append(
            ReleaseRecord(
                live_graph_uri=live,
                snapshot_graph_uri=str(row.get("snapshot_graph_uri") or ""),
                version=int(ver),
                kind=k if k in ("release", "revision") else "release",  # type: ignore[arg-type]
                layer=str(row.get("layer") or layer_for_graph(live)),  # type: ignore[arg-type]
                fingerprint=str(row.get("fingerprint") or ""),
                parent_version=row.get("parent_version"),
                publisher=row.get("publisher"),
                timestamp=row.get("timestamp"),
                change_summary=row.get("change_summary"),
                compat_class=row.get("compat_class"),
                change_records=tuple(recs),
            )
        )
    out.sort(key=lambda r: (r.kind, r.version))
    return out


async def get_snapshot(
    neptune,
    live_graph_uri: str,
    version: int,
    *,
    kind: SnapshotKind = "release",
) -> ReleaseRecord | None:
    """Fetch one release/revision record, or None if absent."""
    for rec in await list_snapshots(neptune, live_graph_uri, kind=kind):
        if rec.version == version:
            return rec
    return None


async def read_snapshot_shape(neptune, snapshot_graph_uri: str) -> OntologyShape:
    """Load the ontology shape frozen in a snapshot content graph."""
    return await load_ontology_shape(neptune, snapshot_graph_uri)

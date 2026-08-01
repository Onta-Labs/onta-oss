"""Ontology snapshots, structural diff, and restore (ONTA-406).

Version the **graph name**, never the type IRI (plan §5). Published A/B releases
live at ``graphs/global/public/v{N}`` / ``graphs/global/enhanced/v{N}``; C
revisions materialize at ``graphs/{tenant}/revisions/r{N}``. Release/revision
**metadata** is RDF on the versions companion graph
(:func:`~cograph_client.graph.ontology_commit.versions_graph_uri`) — no Postgres
migration.

Diff produces the frozen :class:`~cograph_client.models.ontology.ChangeRecord`
vocabulary shared with ONTA-404. Symmetric by construction:
``diff(a, a) == []`` and ``invert(diff(a, b))`` multiset-equals ``diff(b, a)``.

Schema graphs only — not instance data — so this module is outside
``kg_writer`` (justified on the write-path allowlist).
"""


from __future__ import annotations

from cograph_client.graph.iri import ENHANCED_GRAPH_URI, IRI_BASE, PUBLIC_GRAPH_URI
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Sequence

import structlog

from cograph_client.graph.ontology_commit import (
    OntologyGraphImmutable,
    OntologyShape,
    is_immutable_version_graph,
    load_ontology_shape,
    ontology_write_lock,
    release_graph_uri,
    revision_graph_uri,
    versions_graph_uri,
)
from cograph_client.graph.ontology_compat import (
    assert_publishable,
    classify_diff,
)
from cograph_client.graph.ontology_queries import OMNIX_ONTO, XSD
from cograph_client.graph.parser import parse_sparql_results
from cograph_client.graph.queries import insert_triples
from cograph_client.models.ontology import ChangeKind, ChangeRecord

logger = structlog.stdlib.get_logger("cograph.graph.ontology_snapshots")

# ---------------------------------------------------------------------------
# Vocabulary — RDF release/revision records on the versions companion graph
# ---------------------------------------------------------------------------

_REL_NS = f"{OMNIX_ONTO}/"
_REL_TYPE = f"{_REL_NS}OntologyRelease"
_REL_OF = f"{_REL_NS}releaseOf"  # live graph this release versions
_REL_VERSION = f"{_REL_NS}version"
_REL_PARENT = f"{_REL_NS}parentVersion"
_REL_LAYER = f"{_REL_NS}layer"
_REL_KIND = f"{_REL_NS}snapshotKind"  # "release" | "revision"
_REL_PUBLISHER = f"{_REL_NS}publisher"
_REL_TIMESTAMP = f"{_REL_NS}timestamp"
_REL_SUMMARY = f"{_REL_NS}changeSummary"
_REL_COMPAT = f"{_REL_NS}compatClass"
_REL_FINGERPRINT = f"{_REL_NS}fingerprint"
_REL_SNAPSHOT = f"{_REL_NS}snapshotGraph"
_REL_DELTA = f"{_REL_NS}changeDelta"  # JSON ChangeRecord list vs parent

SnapshotKind = Literal["release", "revision"]
LayerName = Literal["public", "enhanced", "tenant"]

# XSD-ish / known literal datatypes — anything else is treated as a
# relationship range (bare type name) for ADD_ATTRIBUTE vs ADD_RELATIONSHIP.
_LITERAL_DATATYPES = frozenset({
    "string", "integer", "float", "boolean", "datetime", "uri", "geo",
    "double", "date", "decimal", "long", "int", "number", "anyURI",
    "dateTime", "time",
})


# ---------------------------------------------------------------------------
# Public models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReleaseRecord:
    """One immutable snapshot's metadata (A/B release or C revision)."""

    live_graph_uri: str
    snapshot_graph_uri: str
    version: int
    kind: SnapshotKind
    layer: LayerName
    fingerprint: str
    parent_version: int | None = None
    publisher: str | None = None
    timestamp: str | None = None
    change_summary: str | None = None
    compat_class: str | None = None
    change_records: tuple[ChangeRecord, ...] = ()


@dataclass
class SnapshotPlan:
    """Pure plan for materializing a snapshot (dry-runable)."""

    live_graph_uri: str
    snapshot_graph_uri: str
    version: int
    kind: SnapshotKind
    layer: LayerName
    fingerprint: str
    parent_version: int | None
    change_records_vs_parent: list[ChangeRecord] = field(default_factory=list)
    parent_fingerprint: str | None = None


@dataclass
class RestorePlan:
    """Pure plan for restoring a live graph from a snapshot (dry-runable)."""

    live_graph_uri: str
    snapshot_graph_uri: str
    version: int
    kind: SnapshotKind
    fingerprint_before: str
    fingerprint_after: str  # expected = snapshot fingerprint


# ---------------------------------------------------------------------------
# Layer / URI helpers
# ---------------------------------------------------------------------------


def layer_for_graph(graph_uri: str) -> LayerName:
    """Map a live ontology graph URI to its layer name."""
    g = graph_uri.rstrip("/")
    if g.endswith("/global/public") or g == PUBLIC_GRAPH_URI:
        return "public"
    if g.endswith("/global/enhanced") or g == ENHANCED_GRAPH_URI:
        return "enhanced"
    return "tenant"


def live_graph_from_snapshot(snapshot_graph_uri: str) -> str | None:
    """Inverse of release/revision URI minting; None if not a snapshot URI."""
    g = snapshot_graph_uri.rstrip("/")
    m = re.match(
        rf"^({re.escape(IRI_BASE)}/graphs/(?:global/(?:public|enhanced)|[^/]+))/v(\d+)$",
        g,
    )
    if m:
        return m.group(1)
    m = re.match(
        rf"^({re.escape(IRI_BASE)}/graphs/[^/]+)/revisions/r(\d+)$",
        g,
    )
    if m:
        return m.group(1)
    return None


def _release_subject(live_graph_uri: str, version: int, kind: SnapshotKind) -> str:
    tag = "r" if kind == "revision" else "v"
    return f"{live_graph_uri.rstrip('/')}/releases/{tag}{int(version)}"


# ---------------------------------------------------------------------------
# Diff (pure) — the ONTA-406 producer ONTA-404 will consume
# ---------------------------------------------------------------------------


def _is_literal_datatype(dt: str) -> bool:
    return (dt or "string") in _LITERAL_DATATYPES


def _add_slot_record(type_name: str, slot: str, datatype: str) -> ChangeRecord:
    if _is_literal_datatype(datatype):
        return ChangeRecord(
            kind=ChangeKind.ADD_ATTRIBUTE,
            type_name=type_name,
            slot_name=slot,
            new_value=datatype,
        )
    return ChangeRecord(
        kind=ChangeKind.ADD_RELATIONSHIP,
        type_name=type_name,
        slot_name=slot,
        new_value=datatype,
    )


def _remove_slot_record(type_name: str, slot: str, datatype: str) -> ChangeRecord:
    if _is_literal_datatype(datatype):
        return ChangeRecord(
            kind=ChangeKind.REMOVE_ATTRIBUTE,
            type_name=type_name,
            slot_name=slot,
            old_value=datatype,
        )
    return ChangeRecord(
        kind=ChangeKind.REMOVE_RELATIONSHIP,
        type_name=type_name,
        slot_name=slot,
        old_value=datatype,
    )


def diff_shapes(a: OntologyShape, b: OntologyShape) -> list[ChangeRecord]:
    """Structural diff ``a → b`` as a list of :class:`ChangeRecord`.

    Companions under ``attr_meta/`` are never present on either shape.
    Order is deterministic (sorted keys) so the same pair always yields the
    same list; invertibility is tested as a multiset.
    """
    records: list[ChangeRecord] = []

    types_a, types_b = set(a.types), set(b.types)
    for t in sorted(types_b - types_a):
        records.append(ChangeRecord(kind=ChangeKind.ADD_TYPE, type_name=t))
    for t in sorted(types_a - types_b):
        records.append(ChangeRecord(kind=ChangeKind.REMOVE_TYPE, type_name=t))

    # Type comments (shared types only — add/remove type covers birth/death).
    for t in sorted(types_a & types_b):
        old_c = a.types.get(t) or ""
        new_c = b.types.get(t) or ""
        if old_c != new_c:
            records.append(
                ChangeRecord(
                    kind=ChangeKind.CHANGE_COMMENT,
                    type_name=t,
                    old_value=old_c or None,
                    new_value=new_c or None,
                )
            )

    # Attributes / relationships.
    def _slots(shape: OntologyShape, t: str) -> dict[str, str]:
        return dict(shape.attrs.get(t) or {})

    all_types = types_a | types_b
    for t in sorted(all_types):
        sa, sb = _slots(a, t), _slots(b, t)
        for slot in sorted(set(sb) - set(sa)):
            records.append(_add_slot_record(t, slot, sb[slot]))
        for slot in sorted(set(sa) - set(sb)):
            records.append(_remove_slot_record(t, slot, sa[slot]))
        for slot in sorted(set(sa) & set(sb)):
            if sa[slot] != sb[slot]:
                records.append(
                    ChangeRecord(
                        kind=ChangeKind.CHANGE_RANGE,
                        type_name=t,
                        slot_name=slot,
                        old_value=sa[slot],
                        new_value=sb[slot],
                    )
                )

    # Attribute comments.
    def _attr_comments(shape: OntologyShape) -> dict[tuple[str, str], str]:
        out: dict[tuple[str, str], str] = {}
        for t, inner in (shape.attr_comments or {}).items():
            for attr, text in (inner or {}).items():
                if text:
                    out[(t, attr)] = text
        return out

    ac_a, ac_b = _attr_comments(a), _attr_comments(b)
    for key in sorted(set(ac_a) | set(ac_b)):
        old_v = ac_a.get(key)
        new_v = ac_b.get(key)
        if old_v != new_v:
            t, slot = key
            records.append(
                ChangeRecord(
                    kind=ChangeKind.CHANGE_COMMENT,
                    type_name=t,
                    slot_name=slot,
                    old_value=old_v,
                    new_value=new_v,
                )
            )

    # Subclass edges.
    for child in sorted(set(b.parent_of) - set(a.parent_of)):
        records.append(
            ChangeRecord(
                kind=ChangeKind.ADD_SUBCLASS,
                type_name=child,
                parent_type=b.parent_of[child],
            )
        )
    for child in sorted(set(a.parent_of) - set(b.parent_of)):
        records.append(
            ChangeRecord(
                kind=ChangeKind.REMOVE_SUBCLASS,
                type_name=child,
                parent_type=a.parent_of[child],
            )
        )
    for child in sorted(set(a.parent_of) & set(b.parent_of)):
        if a.parent_of[child] != b.parent_of[child]:
            records.append(
                ChangeRecord(
                    kind=ChangeKind.REMOVE_SUBCLASS,
                    type_name=child,
                    parent_type=a.parent_of[child],
                )
            )
            records.append(
                ChangeRecord(
                    kind=ChangeKind.ADD_SUBCLASS,
                    type_name=child,
                    parent_type=b.parent_of[child],
                )
            )

    # Core-slot markers.
    cs_a = set(a.core_slots)
    cs_b = set(b.core_slots)
    for t, slot in sorted(cs_b - cs_a):
        records.append(
            ChangeRecord(
                kind=ChangeKind.CHANGE_CORE_SLOT,
                type_name=t,
                slot_name=slot,
                old_value="false",
                new_value="true",
            )
        )
    for t, slot in sorted(cs_a - cs_b):
        records.append(
            ChangeRecord(
                kind=ChangeKind.CHANGE_CORE_SLOT,
                type_name=t,
                slot_name=slot,
                old_value="true",
                new_value="false",
            )
        )

    # Text-kind markers.
    tk_a, tk_b = a.text_kinds or {}, b.text_kinds or {}
    for key in sorted(set(tk_a) | set(tk_b)):
        old_v = tk_a.get(key)
        new_v = tk_b.get(key)
        if old_v != new_v:
            t, slot = key
            records.append(
                ChangeRecord(
                    kind=ChangeKind.CHANGE_TEXT_KIND,
                    type_name=t,
                    slot_name=slot,
                    old_value=old_v,
                    new_value=new_v,
                )
            )

    # Aliases (RENAME_WITH_ALIAS). Added edges set new_value only; removed
    # edges set old_value only — invert swaps the two for symmetry.
    am_a, am_b = a.alias_map or {}, b.alias_map or {}
    for old in sorted(set(am_b) - set(am_a)):
        records.append(
            ChangeRecord(
                kind=ChangeKind.RENAME_WITH_ALIAS,
                from_name=old.rsplit("/", 1)[-1],
                to_name=am_b[old].rsplit("/", 1)[-1],
                old_value=None,
                new_value=f"{old}->{am_b[old]}",
            )
        )
    for old in sorted(set(am_a) - set(am_b)):
        records.append(
            ChangeRecord(
                kind=ChangeKind.RENAME_WITH_ALIAS,
                from_name=old.rsplit("/", 1)[-1],
                to_name=am_a[old].rsplit("/", 1)[-1],
                old_value=f"{old}->{am_a[old]}",
                new_value=None,
            )
        )
    for old in sorted(set(am_a) & set(am_b)):
        if am_a[old] != am_b[old]:
            records.append(
                ChangeRecord(
                    kind=ChangeKind.RENAME_WITH_ALIAS,
                    from_name=old.rsplit("/", 1)[-1],
                    to_name=am_b[old].rsplit("/", 1)[-1],
                    old_value=f"{old}->{am_a[old]}",
                    new_value=f"{old}->{am_b[old]}",
                )
            )

    # Deprecations (ONTA-404). Newly marked types/slots → DEPRECATE; a change
    # of superseded_by on an already-deprecated subject also emits DEPRECATE.
    # Un-deprecate (marker removed) is not a ChangeKind today — fail-open as
    # absent rather than inventing a reverse op (publish gate only cares that
    # a new deprecation is DEPRECATING, not that un-deprecate is invisible).
    dt_a, dt_b = a.deprecated_types or {}, b.deprecated_types or {}
    for t in sorted(set(dt_b) - set(dt_a)):
        records.append(
            ChangeRecord(
                kind=ChangeKind.DEPRECATE,
                type_name=t,
                superseded_by=dt_b[t] or None,
                new_value=dt_b[t] or None,
            )
        )
    for t in sorted(set(dt_a) & set(dt_b)):
        if (dt_a[t] or "") != (dt_b[t] or ""):
            records.append(
                ChangeRecord(
                    kind=ChangeKind.DEPRECATE,
                    type_name=t,
                    superseded_by=dt_b[t] or None,
                    old_value=dt_a[t] or None,
                    new_value=dt_b[t] or None,
                )
            )
    ds_a, ds_b = a.deprecated_slots or {}, b.deprecated_slots or {}
    for key in sorted(set(ds_b) - set(ds_a)):
        t, slot = key
        records.append(
            ChangeRecord(
                kind=ChangeKind.DEPRECATE,
                type_name=t,
                slot_name=slot,
                superseded_by=ds_b[key] or None,
                new_value=ds_b[key] or None,
            )
        )
    for key in sorted(set(ds_a) & set(ds_b)):
        if (ds_a[key] or "") != (ds_b[key] or ""):
            t, slot = key
            records.append(
                ChangeRecord(
                    kind=ChangeKind.DEPRECATE,
                    type_name=t,
                    slot_name=slot,
                    superseded_by=ds_b[key] or None,
                    old_value=ds_a[key] or None,
                    new_value=ds_b[key] or None,
                )
            )

    return records


def invert_change(record: ChangeRecord) -> ChangeRecord:
    """Return the change that undoes ``record`` (ONTA-406 symmetry)."""
    kind = record.kind
    swap_kinds = {
        ChangeKind.ADD_TYPE: ChangeKind.REMOVE_TYPE,
        ChangeKind.REMOVE_TYPE: ChangeKind.ADD_TYPE,
        ChangeKind.ADD_ATTRIBUTE: ChangeKind.REMOVE_ATTRIBUTE,
        ChangeKind.REMOVE_ATTRIBUTE: ChangeKind.ADD_ATTRIBUTE,
        ChangeKind.ADD_RELATIONSHIP: ChangeKind.REMOVE_RELATIONSHIP,
        ChangeKind.REMOVE_RELATIONSHIP: ChangeKind.ADD_RELATIONSHIP,
        ChangeKind.ADD_SUBCLASS: ChangeKind.REMOVE_SUBCLASS,
        ChangeKind.REMOVE_SUBCLASS: ChangeKind.ADD_SUBCLASS,
    }
    if kind in swap_kinds:
        # For add/remove slot records, old_value/new_value also swap so a
        # REMOVE that carried old_value becomes an ADD with new_value.
        return record.model_copy(
            update={
                "kind": swap_kinds[kind],
                "old_value": record.new_value,
                "new_value": record.old_value,
            }
        )
    # Annotative / value changes (comment, range, core-slot, text-kind,
    # rename-with-alias add/remove encoded via old/new_value): same kind,
    # swap old/new. from_name/to_name stay put — they identify the edge,
    # not the direction of the mutation.
    return record.model_copy(
        update={
            "old_value": record.new_value,
            "new_value": record.old_value,
        }
    )


def invert_diff(records: Sequence[ChangeRecord]) -> list[ChangeRecord]:
    """Invert a whole diff (order reversed so a→b undoes in reverse apply order)."""
    return [invert_change(r) for r in reversed(records)]


def _record_key(r: ChangeRecord) -> tuple:
    """Canonical key for multiset equality (ignores list order; None-safe)."""
    d = r.model_dump()
    # Sort by field name; coerce None so heterogeneous values still order.
    return tuple(
        (k, "" if v is None else v)
        for k, v in sorted(d.items(), key=lambda kv: kv[0])
    )


def diffs_symmetric(a: OntologyShape, b: OntologyShape) -> bool:
    """True iff ``invert(diff(a,b))`` multiset-equals ``diff(b,a)``."""
    ab = diff_shapes(a, b)
    ba = diff_shapes(b, a)
    inv = invert_diff(ab)
    return sorted(_record_key(r) for r in inv) == sorted(_record_key(r) for r in ba)


async def diff_graphs(neptune, graph_a: str, graph_b: str) -> list[ChangeRecord]:
    """Load two ontology graphs and return ``diff_shapes(a, b)``."""
    a = await load_ontology_shape(neptune, graph_a)
    b = await load_ontology_shape(neptune, graph_b)
    return diff_shapes(a, b)


# ---------------------------------------------------------------------------
# Snapshot plan / execute
# ---------------------------------------------------------------------------


async def _next_release_version(neptune, live_graph_uri: str, kind: SnapshotKind) -> int:
    """Max recorded version for ``kind`` on this live graph + 1 (or 1)."""
    records = await list_snapshots(neptune, live_graph_uri, kind=kind)
    if not records:
        return 1
    return max(r.version for r in records) + 1


async def _current_revision_counter(neptune, live_graph_uri: str) -> int:
    """Read the ONTA-403 workspaceRevision counter (0 if absent)."""
    rev_graph = versions_graph_uri(live_graph_uri)
    pred = f"{OMNIX_ONTO}/workspaceRevision"
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


def _copy_graph_sparql(source: str, target: str) -> str:
    """INSERT-SELECT copy of every triple from ``source`` into ``target``."""
    return (
        f"INSERT {{ GRAPH <{target}> {{ ?s ?p ?o }} }}\n"
        f"WHERE {{ GRAPH <{source}> {{ ?s ?p ?o }} }}"
    )


def _clear_graph_sparql(graph_uri: str) -> str:
    return f"CLEAR SILENT GRAPH <{graph_uri}>"


def _drop_graph_sparql(graph_uri: str) -> str:
    return f"DROP SILENT GRAPH <{graph_uri}>"


def _release_metadata_triples(
    plan: SnapshotPlan,
    *,
    publisher: str | None,
    change_summary: str | None,
    compat_class: str | None,
    timestamp: str | None,
) -> list[tuple[str, str, str]]:
    subj = _release_subject(plan.live_graph_uri, plan.version, plan.kind)
    ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    triples: list[tuple[str, str, str]] = [
        (subj, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", _REL_TYPE),
        (subj, _REL_OF, plan.live_graph_uri),
        (subj, _REL_VERSION, f"{plan.version}^^{XSD}#integer"),
        (subj, _REL_LAYER, plan.layer),
        (subj, _REL_KIND, plan.kind),
        (subj, _REL_FINGERPRINT, plan.fingerprint),
        (subj, _REL_SNAPSHOT, plan.snapshot_graph_uri),
        (subj, _REL_TIMESTAMP, f"{ts}^^{XSD}#dateTime"),
    ]
    if plan.parent_version is not None:
        triples.append(
            (subj, _REL_PARENT, f"{plan.parent_version}^^{XSD}#integer")
        )
    if publisher:
        triples.append((subj, _REL_PUBLISHER, publisher))
    if change_summary:
        triples.append((subj, _REL_SUMMARY, change_summary))
    if compat_class:
        triples.append((subj, _REL_COMPAT, compat_class))
    if plan.change_records_vs_parent:
        delta = [
            {
                "kind": r.kind.value if hasattr(r.kind, "value") else str(r.kind),
                "type_name": r.type_name,
                "slot_name": r.slot_name,
                "parent_type": r.parent_type,
                "old_value": r.old_value,
                "new_value": r.new_value,
                "from_name": r.from_name,
                "to_name": r.to_name,
            }
            for r in plan.change_records_vs_parent
        ]
        triples.append(
            (
                subj,
                _REL_DELTA,
                json.dumps(delta, separators=(",", ":"), sort_keys=True),
            )
        )
    return triples


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


# ---------------------------------------------------------------------------
# Read / list
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


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
        # Clear live, then copy snapshot → live. Never clear the snapshot.
        await neptune.update(_clear_graph_sparql(plan.live_graph_uri))
        await neptune.update(
            _copy_graph_sparql(plan.snapshot_graph_uri, plan.live_graph_uri)
        )
        after = (await load_ontology_shape(neptune, plan.live_graph_uri)).fingerprint()
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


async def cleanup_tenant_version_artifacts(
    neptune, tenant_id: str, *, dry_run: bool = False
) -> list[str]:
    """Cleanup helper keyed by tenant id (workspace ontology graph)."""
    from cograph_client.graph.queries import tenant_graph_uri

    return await cleanup_version_artifacts(
        neptune, tenant_graph_uri(tenant_id), dry_run=dry_run
    )

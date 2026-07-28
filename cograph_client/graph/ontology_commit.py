"""Ontology schema commit API — one write path for schema mutations (ONTA-403).

Every process that mutates ontology *schema* (types, attributes, relationships,
subclass edges, core-slot markers, text-kinds) MUST call :func:`commit_ontology`
(or :func:`commit_ontology_unlocked` when the caller already holds
:func:`ontology_write_lock`). This is the schema-write analogue of
``kg_writer.insert_facts`` for instance data (ADR 0007) — a second, parallel
discipline.

Builders in :mod:`cograph_client.graph.ontology_queries` remain the SPARQL
construction layer; only this module may *apply* them in production. A
deny-by-default drift guard (``tests/test_ontology_commit_convergence.py``)
fails CI if a production module reintroduces a raw builder write.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence
from uuid import uuid4

import structlog

from cograph_client.graph.aliases import fetch_alias_map, register_alias
from cograph_client.graph.ontology_queries import (
    OMNIX_ONTO,
    XSD,
    attr_uri,
    delete_attribute_declaration,
    full_ontology_detail_query,
    insert_subtype,
    insert_type,
    mark_core_slot,
    ontology_version,
    parent_map_query,
    set_object_property_range,
    text_kind_map_query,
    type_uri,
    upsert_attribute,
    upsert_attribute_text_kind,
    upsert_type,
    upsert_type_comment,
)
from cograph_client.graph.parser import parse_sparql_results
from cograph_client.graph.queries import insert_triples
from cograph_client.models.ontology import (
    ChangeKind,
    ChangeRecord,
    OntologyCommitResult,
    OntologyMutation,
    OntologyOpKind,
)

logger = structlog.stdlib.get_logger("cograph.graph.ontology_commit")

# Shared serialization point for ALL ontology-schema writes. SchemaResolver
# defaults to this same lock (see its ``ontology_lock`` constructor arg) so
# concurrent ingest + REST mutations never interleave. asyncio.Lock is NOT
# reentrant: callers already holding it must use
# :func:`commit_ontology_unlocked`.
_ONTOLOGY_WRITE_LOCK = asyncio.Lock()

# Workspace revision counter lives on a companion named graph of the ontology
# graph (house encoding: companion-graph-per-data-graph). No durable Postgres
# revision store exists yet (plan §4); this RDF counter is the minimal
# monotonic bump ONTA-403 requires. ONTA-406 materializes revision snapshots
# and release records on the same companion.
_REV_PRED = f"{OMNIX_ONTO}/workspaceRevision"
_REV_GRAPH_SUFFIX = "/versions"

# Published A/B release graphs (`…/public/v{N}`, `…/enhanced/v{N}`) and C
# revision snapshot graphs (`…/revisions/r{N}`) are immutable. commit_ontology
# refuses them so a publish cannot be silently rewritten (ONTA-406).
_PUBLISHED_VERSION_GRAPH_RE = re.compile(
    r"^https://cograph\.tech/graphs/"
    r"(?:global/(?:public|enhanced)|[^/]+)"
    r"/v\d+$"
)
_REVISION_SNAPSHOT_GRAPH_RE = re.compile(
    r"^https://cograph\.tech/graphs/[^/]+/revisions/r\d+$"
)

# Changelog vocabulary — same GOV_* shape as resolver/governance.py so one
# reader can eventually cover both Global governance and workspace commits.
# Imported lazily / duplicated as constants to avoid a circular import through
# the governance module (which imports ontology_queries).
_GOV_NS = "https://cograph.tech/gov/"
_GOV_ACTION = f"{_GOV_NS}action"
_GOV_SUBJECT = f"{_GOV_NS}subject"
_GOV_TIMESTAMP = f"{_GOV_NS}timestamp"
_GOV_TENANT = f"{_GOV_NS}tenant"
_GOV_ACTOR = f"{_GOV_NS}actor"
_GOV_MESSAGE = f"{_GOV_NS}message"
_GOV_VERSION_BEFORE = f"{_GOV_NS}versionBefore"
_GOV_VERSION_AFTER = f"{_GOV_NS}versionAfter"
_GOV_DELTA = f"{_GOV_NS}delta"
_GOV_REVISION = f"{_GOV_NS}revision"


class OntologyVersionConflict(Exception):
    """Raised when ``expected_version`` does not match the live fingerprint."""

    def __init__(self, expected: str, actual: str, graph_uri: str):
        self.expected = expected
        self.actual = actual
        self.graph_uri = graph_uri
        super().__init__(
            f"ontology version conflict on {graph_uri!r}: "
            f"expected {expected!r}, actual {actual!r}"
        )


class OntologyOpNotSupported(ValueError):
    """Raised for ops reserved for later tickets (alias/deprecate)."""


class OntologyGraphImmutable(Exception):
    """Raised when a write targets a published version / revision snapshot graph.

    Version graphs (``…/v{N}``, ``…/revisions/r{N}``) are immutable by
    construction (ONTA-406). Restore and ordinary commits must target the live
    layer graph, never a snapshot URI.
    """

    def __init__(self, graph_uri: str):
        self.graph_uri = graph_uri
        super().__init__(
            f"refusing write into immutable ontology version graph {graph_uri!r}"
        )


def ontology_write_lock() -> asyncio.Lock:
    """The ONE process-wide ontology-schema write lock (ONTA-403 / ONTA-268).

    SchemaResolver and :func:`commit_ontology` share this so a REST mutation
    cannot interleave with an ingest type-mint. Pass it into every per-sub-query
    SchemaResolver in a discovery job so they serialize against each other.
    """
    return _ONTOLOGY_WRITE_LOCK


def versions_graph_uri(graph_uri: str) -> str:
    """Companion graph holding revision counters + release/revision records.

    House encoding: companion-graph-per-data-graph. Holds the monotonic
    ``workspaceRevision`` counter (ONTA-403) and the RDF release/revision
    metadata for snapshots (ONTA-406). Snapshot *content* lives at
    :func:`release_graph_uri` / :func:`revision_graph_uri`, not here.
    """
    return f"{graph_uri.rstrip('/')}{_REV_GRAPH_SUFFIX}"


def is_immutable_version_graph(graph_uri: str) -> bool:
    """True iff ``graph_uri`` is a published A/B release or C revision snapshot.

    Type IRIs are never versioned (plan §5 — version the graph name). These
    named graphs hold frozen ontology content and must not accept writes.
    """
    if not isinstance(graph_uri, str):
        return False
    g = graph_uri.rstrip("/")
    return bool(
        _PUBLISHED_VERSION_GRAPH_RE.match(g) or _REVISION_SNAPSHOT_GRAPH_RE.match(g)
    )


def release_graph_uri(live_graph_uri: str, version: int) -> str:
    """Published release snapshot graph for layer A or B: ``{live}/v{N}``.

    Example: ``https://cograph.tech/graphs/global/public/v3``.
    """
    if version < 1:
        raise ValueError(f"release version must be >= 1, got {version}")
    return f"{live_graph_uri.rstrip('/')}/v{int(version)}"


def revision_graph_uri(live_graph_uri: str, revision: int) -> str:
    """Materialized C revision snapshot: ``{live}/revisions/r{N}``.

    Revisions are a monotonic counter (ONTA-403); content is snapshotted only
    at job boundaries / named checkpoints (ONTA-406), not on every commit.
    """
    if revision < 1:
        raise ValueError(f"revision must be >= 1, got {revision}")
    return f"{live_graph_uri.rstrip('/')}/revisions/r{int(revision)}"


def changelog_graph_uri_for(graph_uri: str) -> str:
    """Append-only changelog companion for a workspace (or global) ontology graph.

    Global governance still writes to the fixed
    ``https://cograph.tech/graphs/global/changelog`` via
    :func:`cograph_client.resolver.governance.changelog_graph_uri`; workspace
    commits use a per-graph companion so tenant isolation is by named graph.
    """
    return f"{graph_uri.rstrip('/')}/changelog"


# ---------------------------------------------------------------------------
# Ontology shape (shared by fingerprint, diff, snapshot — ONTA-403/406)
# ---------------------------------------------------------------------------


@dataclass
class OntologyShape:
    """Identity-bearing ontology snapshot used by fingerprint + ONTA-406 diff.

    Companions under ``attr_meta/`` are never loaded here — they are not
    ontology content (plan §2).
    """

    types: dict[str, str] = field(default_factory=dict)  # name -> comment
    attrs: dict[str, dict[str, str]] = field(default_factory=dict)  # type -> attr -> dt
    parent_of: dict[str, str] = field(default_factory=dict)
    # Nested attr comments: {type: {attr: text}} — type comments live in types.
    attr_comments: dict[str, dict[str, str]] = field(default_factory=dict)
    core_slots: list[tuple[str, str]] = field(default_factory=list)
    text_kinds: dict[tuple[str, str], str] = field(default_factory=dict)
    alias_map: dict[str, str] = field(default_factory=dict)

    def fingerprint(self) -> str:
        """Same digest :func:`fingerprint_ontology` would return for this shape."""
        comments: dict = {}
        if self.attr_comments:
            comments.update(self.attr_comments)
        base = ontology_version(
            self.types,
            self.attrs,
            self.parent_of,
            comments=comments or None,
            core_slots=self.core_slots or None,
            text_kinds=self.text_kinds or None,
        )
        if not self.alias_map:
            return base
        h = hashlib.sha256()
        h.update(base.encode("utf-8"))
        h.update(b"\n")
        for old in sorted(self.alias_map):
            h.update(b"AL:")
            h.update(old.encode("utf-8"))
            h.update(b"=")
            h.update(self.alias_map[old].encode("utf-8"))
            h.update(b"\n")
        return h.hexdigest()[:16]


async def commit_ontology(
    neptune,
    graph_uri: str,
    mutations: Sequence[OntologyMutation],
    *,
    expected_version: str | None = None,
    actor: str | None = None,
    message: str | None = None,
) -> OntologyCommitResult:
    """Apply a batch of ontology schema mutations as one atomic commit.

    Parameters
    ----------
    neptune:
        Graph client (same object routes and the resolver already hold).
    graph_uri:
        Target named graph. Workspace writes always go to the tenant graph;
        only the governed promotion path may write a global layer, and only
        with consent (ONTA-402a).
    mutations:
        Ordered schema ops. Empty is a no-op that still returns the current
        fingerprint.
    expected_version:
        Optimistic-concurrency token from :func:`ontology_version` (extended
        by ONTA-403). ``None`` means "write unconditionally" (legacy callers).
        A mismatch raises :class:`OntologyVersionConflict`.
    actor:
        Optional identity for the changelog / provenance record.
    message:
        Optional human summary for the changelog entry.

    Returns
    -------
    OntologyCommitResult
        Fingerprint before/after plus the applied mutations and derived
        :class:`~cograph_client.models.ontology.ChangeRecord` list (the same
        vocabulary ONTA-406 diffs and ONTA-404 classifies).
    """
    async with _ONTOLOGY_WRITE_LOCK:
        return await commit_ontology_unlocked(
            neptune,
            graph_uri,
            mutations,
            expected_version=expected_version,
            actor=actor,
            message=message,
        )


async def commit_ontology_unlocked(
    neptune,
    graph_uri: str,
    mutations: Sequence[OntologyMutation],
    *,
    expected_version: str | None = None,
    actor: str | None = None,
    message: str | None = None,
) -> OntologyCommitResult:
    """Same as :func:`commit_ontology` but the caller already holds
    :func:`ontology_write_lock`.

    Used by SchemaResolver critical sections that already serialize under the
    shared lock (match-then-mint). Never call this without holding the lock —
    concurrent commits would race on the fingerprint.
    """
    if is_immutable_version_graph(graph_uri):
        raise OntologyGraphImmutable(graph_uri)
    version_before = await fingerprint_ontology(neptune, graph_uri)
    if expected_version is not None and expected_version != version_before:
        raise OntologyVersionConflict(expected_version, version_before, graph_uri)

    applied: list[OntologyMutation] = []
    change_records: list[ChangeRecord] = []
    for mut in mutations:
        records = await _apply_one(neptune, graph_uri, mut)
        applied.append(mut)
        change_records.extend(records)

    version_after = (
        version_before
        if not applied
        else await fingerprint_ontology(neptune, graph_uri)
    )

    revision: int | None = None
    if applied:
        revision = await _bump_revision(neptune, graph_uri)
        await _emit_changelog(
            neptune,
            graph_uri,
            version_before=version_before,
            version_after=version_after,
            actor=actor,
            message=message,
            change_records=change_records,
            revision=revision,
        )
        logger.info(
            "ontology_committed",
            graph_uri=graph_uri,
            n_mutations=len(applied),
            version_before=version_before,
            version_after=version_after,
            revision=revision,
            actor=actor,
        )

    return OntologyCommitResult(
        graph_uri=graph_uri,
        version_before=version_before,
        version_after=version_after,
        applied=list(applied),
        change_records=change_records,
    )


async def load_ontology_shape(neptune, graph_uri: str) -> OntologyShape:
    """Read the live ontology shape from ``graph_uri`` (ONTA-403/406).

    Shared by :func:`fingerprint_ontology` and the ONTA-406 diff/snapshot path
    so the two cannot disagree on what counts as ontology content.
    """
    types: dict[str, str] = {}
    attrs: dict[str, dict[str, str]] = {}
    attr_comments: dict[str, dict[str, str]] = {}
    core_slots: list[tuple[str, str]] = []

    try:
        raw = await neptune.query(full_ontology_detail_query(graph_uri))
        _, rows = parse_sparql_results(raw)
    except Exception:
        logger.warning("ontology_shape_fetch_failed", graph_uri=graph_uri, exc_info=True)
        rows = []

    for row in rows:
        tlabel = row.get("typeLabel") or ""
        if not tlabel:
            continue
        if tlabel not in types:
            types[tlabel] = row.get("typeComment") or ""
            attrs[tlabel] = {}
        # Prefer a non-empty type comment if a later row carries one.
        if row.get("typeComment") and not types[tlabel]:
            types[tlabel] = row["typeComment"]
        alabel = row.get("attrLabel") or ""
        if alabel:
            range_str = row.get("range") or ""
            datatype = _range_to_datatype(range_str)
            attrs[tlabel][alabel] = datatype
            ac = row.get("attrComment") or ""
            if ac:
                attr_comments.setdefault(tlabel, {})[alabel] = ac
            core = row.get("core") or ""
            if core and str(core).lower() in ("true", "1"):
                core_slots.append((tlabel, alabel))

    parent_of: dict[str, str] = {}
    try:
        raw_p = await neptune.query(parent_map_query(graph_uri))
        _, prows = parse_sparql_results(raw_p)
        for row in prows:
            child_uri = row.get("child") or ""
            parent_uri = row.get("parent") or ""
            if child_uri and parent_uri:
                child = child_uri.rsplit("/", 1)[-1]
                parent = parent_uri.rsplit("/", 1)[-1]
                if child and parent:
                    parent_of[child] = parent
    except Exception:
        logger.warning("ontology_shape_parent_map_failed", graph_uri=graph_uri, exc_info=True)

    text_kinds: dict[tuple[str, str], str] = {}
    try:
        raw_t = await neptune.query(text_kind_map_query(graph_uri))
        _, trows = parse_sparql_results(raw_t)
        for row in trows:
            a_uri = row.get("attr") or ""
            kind = row.get("kind") or ""
            if not a_uri or not kind:
                continue
            # attr URI: https://cograph.tech/types/<Type>/attrs/<leaf>
            parts = a_uri.split("/types/", 1)
            if len(parts) != 2 or "/attrs/" not in parts[1]:
                continue
            type_part, attr_part = parts[1].split("/attrs/", 1)
            # Strip layer prefixes (public/, x/) if present — bare name for fingerprint.
            type_name = type_part.rsplit("/", 1)[-1]
            attr_name = attr_part
            if type_name and attr_name:
                text_kinds[(type_name, attr_name)] = kind
    except Exception:
        logger.warning("ontology_shape_text_kinds_failed", graph_uri=graph_uri, exc_info=True)

    try:
        alias_map = await fetch_alias_map(neptune, graph_uri)
    except Exception:
        logger.warning("ontology_shape_aliases_failed", graph_uri=graph_uri, exc_info=True)
        alias_map = {}

    return OntologyShape(
        types=types,
        attrs=attrs,
        parent_of=parent_of,
        attr_comments=attr_comments,
        core_slots=core_slots,
        text_kinds=text_kinds,
        alias_map=alias_map or {},
    )


async def fingerprint_ontology(neptune, graph_uri: str) -> str:
    """Read the live ontology shape from ``graph_uri`` and return its fingerprint.

    Covers types, attributes (with ranges), subclass edges, comments, core-slot
    markers, text-kinds (ONTA-403 extended surface), and attribute aliases
    (ONTA-407a — alias registration must shift the concurrency token).
    """
    shape = await load_ontology_shape(neptune, graph_uri)
    return shape.fingerprint()


def _range_to_datatype(range_str: str) -> str:
    if not range_str:
        return "string"
    type_uri_prefix = "https://cograph.tech/types/"
    if range_str.startswith(type_uri_prefix):
        return range_str[len(type_uri_prefix):].rsplit("/", 1)[-1]
    if "#" in range_str:
        fragment = range_str.split("#")[-1]
        dt_map = {
            "string": "string",
            "integer": "integer",
            "float": "float",
            "double": "float",
            "boolean": "boolean",
            "dateTime": "datetime",
            "anyURI": "uri",
            "Resource": "uri",
            "wktLiteral": "geo",
        }
        return dt_map.get(fragment, "string")
    return "string"


async def _apply_one(
    neptune, graph_uri: str, mut: OntologyMutation,
) -> list[ChangeRecord]:
    """Apply one mutation via the existing SPARQL builders. Returns change records."""
    op = mut.op
    if op is OntologyOpKind.UPSERT_TYPE:
        return await _apply_upsert_type(neptune, graph_uri, mut)
    if op is OntologyOpKind.UPSERT_ATTRIBUTE:
        return await _apply_upsert_attribute(neptune, graph_uri, mut)
    if op is OntologyOpKind.UPSERT_RELATIONSHIP:
        return await _apply_upsert_relationship(neptune, graph_uri, mut)
    if op is OntologyOpKind.SET_SUBCLASS:
        if not mut.parent_type:
            raise ValueError("SET_SUBCLASS requires parent_type")
        await neptune.update(insert_subtype(graph_uri, mut.parent_type, mut.type_name))
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
        await neptune.update(
            delete_attribute_declaration(graph_uri, mut.type_name, mut.slot_name)
        )
        return [
            ChangeRecord(
                kind=ChangeKind.REMOVE_ATTRIBUTE,
                type_name=mut.type_name,
                slot_name=mut.slot_name,
            )
        ]
    if op is OntologyOpKind.DELETE_TYPE:
        # Best-effort: drop every triple whose subject is the type URI.
        # Attributes and reverse subClassOf edges are not cascaded (no production
        # caller uses DELETE_TYPE yet; ONTA-404/compat gate owns the policy).
        uri = type_uri(mut.type_name)
        await neptune.update(
            f"WITH <{graph_uri}>\n"
            f"DELETE {{ <{uri}> ?p ?o }} WHERE {{ <{uri}> ?p ?o }}"
        )
        return [ChangeRecord(kind=ChangeKind.REMOVE_TYPE, type_name=mut.type_name)]
    if op is OntologyOpKind.SET_CORE_SLOT:
        if not mut.slot_name:
            raise ValueError("SET_CORE_SLOT requires slot_name")
        if mut.core_slot is False:
            a_uri = f"https://cograph.tech/types/{mut.type_name}/attrs/{mut.slot_name}"
            await neptune.update(
                f"DELETE {{ GRAPH <{graph_uri}> {{ <{a_uri}> <{OMNIX_ONTO}/coreSlot> ?c }} }}\n"
                f"WHERE {{ GRAPH <{graph_uri}> {{ OPTIONAL {{ <{a_uri}> <{OMNIX_ONTO}/coreSlot> ?c }} }} }}"
            )
        else:
            await neptune.update(mark_core_slot(graph_uri, mut.type_name, mut.slot_name))
        return [
            ChangeRecord(
                kind=ChangeKind.CHANGE_CORE_SLOT,
                type_name=mut.type_name,
                slot_name=mut.slot_name,
                new_value="true" if mut.core_slot is not False else "false",
            )
        ]
    if op is OntologyOpKind.SET_TEXT_KIND:
        if not mut.slot_name:
            raise ValueError("SET_TEXT_KIND requires slot_name")
        kind = mut.text_kind or ""
        await neptune.update(
            upsert_attribute_text_kind(
                graph_uri, mut.type_name, mut.slot_name, text_kind=kind,
            )
        )
        return [
            ChangeRecord(
                kind=ChangeKind.CHANGE_TEXT_KIND,
                type_name=mut.type_name,
                slot_name=mut.slot_name,
                new_value=kind or None,
            )
        ]
    if op is OntologyOpKind.SET_COMMENT:
        # Type-level when no slot_name; attribute-level comment via upsert_attribute
        # range-preserving path is not a single builder — use upsert_type_comment
        # for types. Attribute comments go through UPSERT_ATTRIBUTE.
        if mut.slot_name:
            await neptune.update(
                upsert_attribute(
                    graph_uri,
                    mut.type_name,
                    mut.slot_name,
                    description=mut.description or "",
                    datatype=mut.datatype or "string",
                )
            )
            return [
                ChangeRecord(
                    kind=ChangeKind.CHANGE_COMMENT,
                    type_name=mut.type_name,
                    slot_name=mut.slot_name,
                    new_value=mut.description,
                )
            ]
        await neptune.update(
            upsert_type_comment(graph_uri, mut.type_name, mut.description or "")
        )
        return [
            ChangeRecord(
                kind=ChangeKind.CHANGE_COMMENT,
                type_name=mut.type_name,
                new_value=mut.description,
            )
        ]
    if op is OntologyOpKind.REGISTER_ALIAS:
        return await _apply_register_alias(neptune, graph_uri, mut)
    if op is OntologyOpKind.DEPRECATE:
        raise OntologyOpNotSupported(
            "DEPRECATE is owned by the compat/publish path (ONTA-404); not "
            "implemented in the ONTA-403 commit body."
        )
    raise ValueError(f"unknown ontology op: {op!r}")


def _resolve_attr_endpoint(
    name_or_uri: str,
    *,
    type_name: str | None,
    target_type: str | None = None,
) -> str:
    """Resolve a REGISTER_ALIAS endpoint to a full attribute IRI.

    Accepts a full ``http(s)://…`` IRI as-is, or a bare attribute leaf that is
    minted under ``types/<type>/attrs/<leaf>`` via :func:`attr_uri`. Bare leaves
    require ``type_name`` (or ``target_type`` when resolving the *new* side of a
    hierarchy move). Type-level renames (bare type IRIs without ``/attrs/``) are
    deferred to ONTA-407b.
    """
    s = (name_or_uri or "").strip()
    if not s:
        raise ValueError("REGISTER_ALIAS endpoint must be a non-empty leaf or IRI")
    if s.startswith("http://") or s.startswith("https://"):
        return s
    owner = (target_type or type_name or "").strip()
    if not owner:
        raise ValueError(
            "REGISTER_ALIAS bare leaf requires type_name (or target_type for the new side)"
        )
    # Reject accidental path fragments that are not full IRIs — 407a is
    # attribute-leaf only unless the caller passes a full IRI.
    if "/" in s:
        raise ValueError(
            f"REGISTER_ALIAS bare endpoint must be a leaf name or full IRI, got {s!r}"
        )
    return attr_uri(owner, s)


async def _apply_register_alias(
    neptune, graph_uri: str, mut: OntologyMutation,
) -> list[ChangeRecord]:
    """Author an ``old aliasOf new`` triple via :func:`register_alias` (ONTA-407a).

    The SPARQL INSERT still lives in ``graph/aliases.py`` — that module stays on
    the write-path allowlist until ONTA-407b consolidates it. This commit op is
    the production *authoring path* so aliases are no longer dead code: REST,
    agent, and other callers go through :func:`commit_ontology`.
    """
    if not mut.alias_from or not mut.alias_to:
        raise ValueError("REGISTER_ALIAS requires alias_from and alias_to")
    old_uri = _resolve_attr_endpoint(mut.alias_from, type_name=mut.type_name)
    new_uri = _resolve_attr_endpoint(
        mut.alias_to,
        type_name=mut.type_name,
        target_type=mut.target_type,
    )
    await register_alias(neptune, graph_uri, old_uri, new_uri)
    # from_name/to_name prefer bare leaves when the caller used leaves; fall
    # back to the full URI tail so the ChangeRecord is always informative.
    from_name = mut.alias_from if not mut.alias_from.startswith("http") else old_uri.rsplit("/", 1)[-1]
    to_name = mut.alias_to if not mut.alias_to.startswith("http") else new_uri.rsplit("/", 1)[-1]
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


async def _apply_upsert_type(
    neptune, graph_uri: str, mut: OntologyMutation,
) -> list[ChangeRecord]:
    records: list[ChangeRecord] = []
    # parent set → upsert_type (atomic class+label+comment+subClassOf replace).
    # description-only → upsert_type_comment so an existing subClassOf edge is
    # NEVER cleared (the new-parent-edge bug insert_type/upsert_type dual).
    # neither → non-destructive insert_type (class+label only).
    if mut.parent_type is not None:
        desc = mut.description if mut.description is not None else ""
        await neptune.update(
            upsert_type(graph_uri, mut.type_name, desc, mut.parent_type)
        )
        records.append(ChangeRecord(kind=ChangeKind.ADD_TYPE, type_name=mut.type_name))
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
    elif mut.description is not None:
        await neptune.update(
            upsert_type_comment(graph_uri, mut.type_name, mut.description)
        )
        records.append(ChangeRecord(kind=ChangeKind.ADD_TYPE, type_name=mut.type_name))
        if mut.description:
            records.append(
                ChangeRecord(
                    kind=ChangeKind.CHANGE_COMMENT,
                    type_name=mut.type_name,
                    new_value=mut.description,
                )
            )
    else:
        await neptune.update(insert_type(graph_uri, mut.type_name, ""))
        records.append(ChangeRecord(kind=ChangeKind.ADD_TYPE, type_name=mut.type_name))
    return records


async def _apply_upsert_attribute(
    neptune, graph_uri: str, mut: OntologyMutation,
) -> list[ChangeRecord]:
    if not mut.slot_name:
        raise ValueError("UPSERT_ATTRIBUTE requires slot_name")
    datatype = mut.datatype or "string"
    await neptune.update(
        upsert_attribute(
            graph_uri,
            mut.type_name,
            mut.slot_name,
            description=mut.description or "",
            datatype=datatype,
        )
    )
    return [
        ChangeRecord(
            kind=ChangeKind.ADD_ATTRIBUTE,
            type_name=mut.type_name,
            slot_name=mut.slot_name,
            new_value=datatype,
        )
    ]


async def _apply_upsert_relationship(
    neptune, graph_uri: str, mut: OntologyMutation,
) -> list[ChangeRecord]:
    if not mut.slot_name:
        raise ValueError("UPSERT_RELATIONSHIP requires slot_name")
    if not mut.target_type:
        raise ValueError("UPSERT_RELATIONSHIP requires target_type")
    # description=None → range-only upgrade (preserves human-authored comment).
    # description provided (including "") → full upsert_attribute.
    if mut.description is None:
        await neptune.update(
            set_object_property_range(
                graph_uri, mut.type_name, mut.slot_name, mut.target_type,
            )
        )
        return [
            ChangeRecord(
                kind=ChangeKind.CHANGE_RANGE,
                type_name=mut.type_name,
                slot_name=mut.slot_name,
                new_value=mut.target_type,
            )
        ]
    await neptune.update(
        upsert_attribute(
            graph_uri,
            mut.type_name,
            mut.slot_name,
            description=mut.description,
            datatype=mut.target_type,
        )
    )
    return [
        ChangeRecord(
            kind=ChangeKind.ADD_RELATIONSHIP,
            type_name=mut.type_name,
            slot_name=mut.slot_name,
            new_value=mut.target_type,
        )
    ]


async def _bump_revision(neptune, graph_uri: str) -> int:
    """Monotonic workspace revision counter on the versions companion graph.

    Minimal RDF counter (no Postgres store exists — plan §4). Read current,
    write current+1 as a single-valued replace. Concurrent commits are
    serialized by :func:`ontology_write_lock`, so lost updates cannot occur.
    """
    rev_graph = versions_graph_uri(graph_uri)
    subject = graph_uri
    current = 0
    try:
        raw = await neptune.query(
            f"SELECT ?r FROM <{rev_graph}> WHERE {{ <{subject}> <{_REV_PRED}> ?r }}"
        )
        _, rows = parse_sparql_results(raw)
        if rows and rows[0].get("r") is not None:
            try:
                current = int(str(rows[0]["r"]).split("^")[0])
            except (TypeError, ValueError):
                current = 0
    except Exception:
        current = 0
    nxt = current + 1
    sparql = (
        f"DELETE {{ GRAPH <{rev_graph}> {{ <{subject}> <{_REV_PRED}> ?old }} }}\n"
        f"INSERT {{ GRAPH <{rev_graph}> {{ "
        f'<{subject}> <{_REV_PRED}> "{nxt}"^^<{XSD}#integer> }} }}\n'
        f"WHERE {{ GRAPH <{rev_graph}> {{ "
        f"OPTIONAL {{ <{subject}> <{_REV_PRED}> ?old }} }} }}"
    )
    await neptune.update(sparql)
    return nxt


async def _emit_changelog(
    neptune,
    graph_uri: str,
    *,
    version_before: str,
    version_after: str,
    actor: str | None,
    message: str | None,
    change_records: list[ChangeRecord],
    revision: int,
) -> None:
    """One append-only changelog entry with a delta payload (ONTA-403).

    Shape mirrors :func:`cograph_client.resolver.governance.changelog_triples`
    (action / subject / timestamp / tenant) and extends it with version before/
    after, actor, message, revision, and a JSON delta of ChangeRecord kinds so
    ONTA-401 can describe a change without re-reading the graph.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = f"{_GOV_NS}log/{uuid4()}"
    # Tenant id is the last path segment of graphs/{tenant} (not a kg URI).
    tenant_id = ""
    prefix = "https://cograph.tech/graphs/"
    if graph_uri.startswith(prefix):
        rest = graph_uri[len(prefix):]
        tenant_id = rest.split("/", 1)[0]
    delta = [
        {
            "kind": r.kind.value if hasattr(r.kind, "value") else str(r.kind),
            "type_name": r.type_name,
            "slot_name": r.slot_name,
            "parent_type": r.parent_type,
            "old_value": r.old_value,
            "new_value": r.new_value,
        }
        for r in change_records
    ]
    triples: list[tuple[str, str, str]] = [
        (entry, _GOV_ACTION, "commit_ontology"),
        (entry, _GOV_SUBJECT, graph_uri),
        (entry, _GOV_TIMESTAMP, f"{ts}^^{XSD}#dateTime"),
        (entry, _GOV_VERSION_BEFORE, version_before),
        (entry, _GOV_VERSION_AFTER, version_after),
        (entry, _GOV_REVISION, f"{revision}^^{XSD}#integer"),
        (entry, _GOV_DELTA, json.dumps(delta, separators=(",", ":"), sort_keys=True)),
    ]
    if tenant_id:
        triples.append((entry, _GOV_TENANT, tenant_id))
    if actor:
        triples.append((entry, _GOV_ACTOR, actor))
    if message:
        triples.append((entry, _GOV_MESSAGE, message))
    await neptune.update(insert_triples(changelog_graph_uri_for(graph_uri), triples))

"""Lock, exceptions, URI helpers, and alias-endpoint resolution for ontology commits.

Looked up on :mod:`infona_client.graph.ontology_commit` at call time via
``_host()`` when a sibling needs a patchable name.
"""

from __future__ import annotations

import asyncio
import re

from infona_client.graph.iri import (
    GOV_NS,
    GRAPH_URI_PREFIX,  # noqa: F401 — re-exported via the facade
    IRI_BASE,
)
from infona_client.graph.ontology_queries import INFONA_ONTO, attr_uri


def _host():
    """Call-time lookup of the public ontology_commit module (monkeypatch surface)."""
    from infona_client.graph import ontology_commit as _mod

    return _mod


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
_REV_PRED = f"{INFONA_ONTO}/workspaceRevision"
_REV_GRAPH_SUFFIX = "/versions"

# Published A/B release graphs (`…/public/v{N}`, `…/enhanced/v{N}`) and C
# revision snapshot graphs (`…/revisions/r{N}`) are immutable. commit_ontology
# refuses them so a publish cannot be silently rewritten (ONTA-406).
_PUBLISHED_VERSION_GRAPH_RE = re.compile(
    rf"^{re.escape(IRI_BASE)}/graphs/"
    r"(?:global/(?:public|enhanced)|[^/]+)"
    r"/v\d+$"
)
_REVISION_SNAPSHOT_GRAPH_RE = re.compile(
    rf"^{re.escape(IRI_BASE)}/graphs/[^/]+/revisions/r\d+$"
)

# Changelog vocabulary — same GOV_* shape as resolver/governance.py so one
# reader can eventually cover both Global governance and workspace commits.
# Imported lazily / duplicated as constants to avoid a circular import through
# the governance module (which imports ontology_queries).
_GOV_NS = GOV_NS
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
    """Raised for ops that remain reserved / unimplemented."""


# Deprecation markers on type / attribute subjects (ONTA-404).
# Part of published schema identity — included in OntologyShape.fingerprint.
DEPRECATED_AT = f"{INFONA_ONTO}/deprecatedAt"
SUPERSEDED_BY = f"{INFONA_ONTO}/supersededBy"


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

    Example: ``https://graph.infona.ai/graphs/global/public/v3``.
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
    ``https://graph.infona.ai/graphs/global/changelog`` via
    :func:`infona_client.resolver.governance.changelog_graph_uri`; workspace
    commits use a per-graph companion so tenant isolation is by named graph.
    """
    return f"{graph_uri.rstrip('/')}/changelog"


def _resolve_attr_endpoint(
    name_or_uri: str,
    *,
    type_name: str | None,
    target_type: str | None = None,
    op_label: str = "alias",
) -> str:
    """Resolve an alias/rename endpoint to a full attribute IRI.

    Accepts a full ``http(s)://…`` IRI as-is, or a bare attribute leaf that is
    minted under ``types/<type>/attrs/<leaf>`` via :func:`attr_uri`. Bare leaves
    require ``type_name`` (or ``target_type`` when resolving the *new* side of a
    hierarchy move).

    **Type renames remain a gap (ONTA-407b):** bare type IRIs without
    ``/attrs/`` are rejected — renaming a type would also re-key
    ``entities/<Type>/…`` instance URIs and is intentionally out of scope.
    """
    s = (name_or_uri or "").strip()
    if not s:
        raise ValueError(f"{op_label} endpoint must be a non-empty leaf or IRI")
    if s.startswith("http://") or s.startswith("https://"):
        # Full attribute IRIs only — reject type-level URIs (no /attrs/).
        if "/attrs/" not in s and "/types/" in s:
            raise ValueError(
                f"{op_label}: type renames are not supported (got type IRI "
                f"{s!r}); only attribute aliases are implemented (ONTA-407b gap)"
            )
        return s
    owner = (target_type or type_name or "").strip()
    if not owner:
        raise ValueError(
            f"{op_label} bare leaf requires type_name (or target_type for the new side)"
        )
    # Reject accidental path fragments that are not full IRIs — attribute-leaf
    # only unless the caller passes a full attribute IRI.
    if "/" in s:
        raise ValueError(
            f"{op_label} bare endpoint must be a leaf name or full IRI, got {s!r}"
        )
    return attr_uri(owner, s)


def _leaf_name(name_or_uri: str, resolved_uri: str) -> str:
    """Prefer the caller's bare leaf; fall back to the IRI tail."""
    if name_or_uri and not name_or_uri.startswith("http"):
        return name_or_uri
    return resolved_uri.rsplit("/", 1)[-1]

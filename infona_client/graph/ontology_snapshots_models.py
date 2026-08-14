"""Release/revision records, plans, and URI helpers (ONTA-406)."""

from __future__ import annotations

from infona_client.graph.iri import ENHANCED_GRAPH_URI, IRI_BASE, PUBLIC_GRAPH_URI
import re
from dataclasses import dataclass, field
from typing import Literal

from infona_client.graph.ontology_queries_uris import INFONA_ONTO
from infona_client.models.ontology import ChangeRecord

# ---------------------------------------------------------------------------
# Vocabulary — RDF release/revision records on the versions companion graph
# ---------------------------------------------------------------------------

_REL_NS = f"{INFONA_ONTO}/"
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

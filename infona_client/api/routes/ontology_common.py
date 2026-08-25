"""Shared helpers for tenant ontology routes.

Look up patched entry points (``ensure_workspace_base_pin``,
``fetch_ontology_changelog``, ``_build_resolver``, ``commit_ontology``, …)
on the public ``ontology`` facade at call time via ``_host()``.

New siblings must not import the retired SPARQL client class — the residual
allowlist must not grow (ONTA-534).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from infona_client.graph.iri import IRI_BASE
from infona_client.graph.ontology_commit import release_graph_uri, revision_graph_uri
from infona_client.graph.queries import tenant_graph_uri
from infona_client.models.ontology import (
    AttributeDefinition,
    BasePinResponse,
    OntologyChangelogEntry,
    TypeResponse,
    WorkspaceOntologyType,
)

# Verdict cache lives alongside the app data (same path the ingest route uses);
# for ECS/Fargate this should be on an EFS mount or replaced with DynamoDB.
_VERDICT_CACHE_PATH = Path("/tmp/infona-verdict-cache.json")

# Absolute http(s) IRI for subject narrowing — same belt as history.py so a
# crafted `>` cannot inject GRAPH <other-tenant> into the query builder.
_ABS_IRI_RE = re.compile(r'^https?://[^\s<>"{}|\^`\\\x00-\x20]+$')
# Action is a short vocabulary token (commit_ontology, add_type, …) — reject
# anything that could break a SPARQL string literal or smuggle a FILTER.
_ACTION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def _host():
    """Call-time lookup of the public ontology routes module (monkeypatch surface)."""
    from infona_client.api.routes import ontology as _mod

    return _mod


def _type_response(t: WorkspaceOntologyType) -> TypeResponse:
    """Map a layered type onto the legacy TypeResponse contract."""
    return TypeResponse(
        name=t.name,
        description=t.description or "",
        parent_type=t.parent_type,
        attributes=[
            AttributeDefinition(
                name=a.name,
                description=a.description or "",
                datatype=a.datatype,
                kind="literal",
            )
            for a in t.attributes
        ]
        + [
            # Relationships still surface in ``attributes`` (legacy TypeResponse
            # has no separate relationships field) with the target type as
            # datatype. ``kind="relationship"`` lets clients skip pinning them
            # as literal chips next to the arrow chip from type-summary.
            AttributeDefinition(
                name=r.name,
                description=r.description or "",
                datatype=r.target_type,
                kind="relationship",
            )
            for r in t.relationships
        ],
        subtypes=list(t.subtypes),
        functions=[f.name for f in t.functions],
    )


def _changelog_entry_model(e) -> OntologyChangelogEntry:
    return OntologyChangelogEntry(
        entry_uri=e.entry_uri,
        action=e.action,
        subject=e.subject,
        timestamp=e.timestamp,
        tenant_id=e.tenant_id,
        actor=e.actor,
        message=e.message,
        version_before=e.version_before,
        version_after=e.version_after,
        revision=e.revision,
        changes=list(e.changes),
    )


def _base_pin_response(
    pin: Any,
    *,
    workspace_revision: int,
    latest_available: int | None,
) -> BasePinResponse:
    upgrade_available = False
    if latest_available is not None:
        if pin.base_version is None:
            upgrade_available = True  # live pin, a release exists
        elif latest_available > pin.base_version:
            upgrade_available = True
    return BasePinResponse(
        tenant_id=pin.tenant_id,
        base_layer=pin.base_layer,
        base_version=pin.base_version,
        is_live=pin.is_live,
        auto_upgrade=pin.auto_upgrade,
        previous_version=pin.previous_version,
        has_previous=pin.has_previous,
        updated_at=pin.updated_at,
        workspace_revision=workspace_revision,
        latest_available=latest_available,
        upgrade_available=upgrade_available,
    )


def _resolve_ontology_ref(
    ref: str,
    *,
    tenant_id: str,
    base_layer: str = "public",
) -> tuple[str, str]:
    """Map a version/revision ref string to ``(canonical_ref, graph_uri)``.

    Accepted forms:
    * ``current`` / ``live`` — tenant live ontology graph
    * bare integer / ``rN`` / ``revision:N`` / ``revision/N`` — workspace revision
    * ``release:N`` / ``vN`` — base-layer release snapshot for the pin's layer
    * absolute ``https://…`` graph URI — must stay under this tenant's graphs/
      or a global public/enhanced release path
    """
    raw = (ref or "").strip()
    if not raw:
        raise ValueError("version ref must be non-empty")

    live = tenant_graph_uri(tenant_id)
    lower = raw.lower()

    if lower in ("current", "live"):
        return ("current", live)

    # Absolute graph URI — tenant isolation + allowed global release graphs.
    if _ABS_IRI_RE.match(raw):
        g = raw.rstrip("/")
        tenant_prefix = f"{IRI_BASE}/graphs/{tenant_id}"
        global_ok = (
            g.startswith(f"{IRI_BASE}/graphs/global/public")
            or g.startswith(f"{IRI_BASE}/graphs/global/enhanced")
        )
        if not (g == live or g.startswith(tenant_prefix + "/") or global_ok):
            raise ValueError(
                "graph URI must be this tenant's ontology graph (or a global release)"
            )
        return (raw, g)

    # release:N / vN — base layer release
    m_rel = re.match(r"^(?:release:|v)(\d+)$", lower)
    if m_rel:
        n = int(m_rel.group(1))
        if n < 1:
            raise ValueError(f"release version must be >= 1, got {n}")
        from infona_client.graph.layers import enhanced_graph_uri, public_graph_uri

        base_live = (
            enhanced_graph_uri() if base_layer == "enhanced" else public_graph_uri()
        )
        uri = release_graph_uri(base_live, n)
        return (f"release:{n}", uri)

    # revision:N / rN / bare integer
    m_rev = re.match(r"^(?:revision[:/]|r)?(\d+)$", lower)
    if m_rev:
        n = int(m_rev.group(1))
        if n < 1:
            raise ValueError(f"revision must be >= 1, got {n}")
        uri = revision_graph_uri(live, n)
        return (f"revision:{n}", uri)

    raise ValueError(
        f"unrecognized version ref {ref!r}; use current, revision:N, release:N, or a graph URI"
    )

"""Ontology schema commit API — one write path for schema mutations (ONTA-403).

Every process that mutates ontology *schema* (types, attributes, relationships,
subclass edges, core-slot markers, text-kinds) MUST call :func:`commit_ontology`
(or :func:`commit_ontology_unlocked` when the caller already holds
:func:`ontology_write_lock`). This is the schema-write analogue of
``kg_writer.insert_facts`` for instance data (ADR 0007) — a second, parallel
discipline.

Builders in :mod:`infona_client.graph.ontology_queries` remain the SPARQL
construction layer; only this package may *apply* them in production. A
deny-by-default drift guard (``tests/test_ontology_commit_convergence.py``)
fails CI if a production module reintroduces a raw builder write.

Implementation lives in sibling ``ontology_commit_*.py`` modules. Every
previously importable name is re-exported here.
"""

from __future__ import annotations

from datetime import datetime, timezone  # noqa: F401 — tests patch ontology_commit.datetime
from typing import Sequence

import structlog

from infona_client.graph.iri import (  # noqa: F401 — public re-exports
    GOV_NS,
    GRAPH_URI_PREFIX,
    IRI_BASE,
    TYPE_URI_PREFIX,
)
from infona_client.graph.ontology_commit_core import (
    DEPRECATED_AT,
    SUPERSEDED_BY,
    OntologyGraphImmutable,
    OntologyOpNotSupported,
    OntologyVersionConflict,
    _GOV_ACTION,
    _GOV_ACTOR,
    _GOV_DELTA,
    _GOV_MESSAGE,
    _GOV_NS,
    _GOV_REVISION,
    _GOV_SUBJECT,
    _GOV_TENANT,
    _GOV_TIMESTAMP,
    _GOV_VERSION_AFTER,
    _GOV_VERSION_BEFORE,
    _ONTOLOGY_WRITE_LOCK,
    _PUBLISHED_VERSION_GRAPH_RE,
    _REV_GRAPH_SUFFIX,
    _REV_PRED,
    _REVISION_SNAPSHOT_GRAPH_RE,
    _leaf_name,
    _resolve_attr_endpoint,
    changelog_graph_uri_for,
    is_immutable_version_graph,
    ontology_write_lock,
    release_graph_uri,
    revision_graph_uri,
    versions_graph_uri,
)
from infona_client.graph.ontology_commit_shape import (
    OntologyShape,
    _flatten_alias_map,
    _load_ontology_shape_graph_store,
    _range_to_datatype,
    fingerprint_ontology,
    load_ontology_shape,
    shape_from_dict,
    shape_to_dict,
)
from infona_client.graph.ontology_commit_sparql import (
    _apply_one,
    _apply_upsert_attribute,
    _apply_upsert_relationship,
    _apply_upsert_type,
    _bump_revision,
    _emit_changelog,
)
from infona_client.graph.ontology_commit_sparql_ops import (
    _apply_deprecate,
    _apply_register_alias,
    _apply_rename_attribute,
    _apply_retire_alias,
)
from infona_client.graph.ontology_commit_store import (
    _apply_one_graph_store,
    _bump_revision_graph_store,
    _commit_ontology_graph_store,
    _emit_changelog_graph_store,
)
from infona_client.graph.ontology_commit_store_ops import (
    _apply_deprecate_graph_store,
    _apply_register_alias_graph_store,
    _apply_rename_attribute_graph_store,
    _apply_retire_alias_graph_store,
    _count_attr_references_graph_store,
)
from infona_client.models.ontology import (  # noqa: F401 — public re-exports
    ChangeKind,
    ChangeRecord,
    OntologyCommitResult,
    OntologyMutation,
    OntologyOpKind,
)

logger = structlog.stdlib.get_logger("infona.graph.ontology_commit")

__all__ = [
    "DEPRECATED_AT",
    "SUPERSEDED_BY",
    "OntologyGraphImmutable",
    "OntologyOpNotSupported",
    "OntologyShape",
    "OntologyVersionConflict",
    "changelog_graph_uri_for",
    "commit_ontology",
    "commit_ontology_unlocked",
    "fingerprint_ontology",
    "is_immutable_version_graph",
    "load_ontology_shape",
    "ontology_write_lock",
    "release_graph_uri",
    "revision_graph_uri",
    "shape_from_dict",
    "shape_to_dict",
    "versions_graph_uri",
]


def _host():
    """Call-time lookup of this module (monkeypatch surface)."""
    from infona_client.graph import ontology_commit as _mod

    return _mod


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
        :class:`~infona_client.models.ontology.ChangeRecord` list (the same
        vocabulary ONTA-406 diffs and ONTA-404 classifies).
    """
    async with _host().ontology_write_lock():
        return await _host().commit_ontology_unlocked(
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
    if _host().is_immutable_version_graph(graph_uri):
        raise OntologyGraphImmutable(graph_uri)

    # Neo4j product path: apply via ontology_catalog GraphStore writers when a
    # process GraphStore is available (prod + hermetic MemoryGraphStore). Prefer
    # store presence over backend string so mis-set SPARQL backends cannot force
    # dead Neptune after cutover.
    from infona_client.graph.store import GraphConfigError, get_graph_store

    try:
        get_graph_store()
        return await _host()._commit_ontology_graph_store(
            graph_uri,
            mutations,
            expected_version=expected_version,
            actor=actor,
            message=message,
        )
    except GraphConfigError:
        pass  # no GraphStore → legacy SPARQL path

    version_before = await _host().fingerprint_ontology(neptune, graph_uri)
    if expected_version is not None and expected_version != version_before:
        raise OntologyVersionConflict(expected_version, version_before, graph_uri)

    applied: list[OntologyMutation] = []
    change_records: list[ChangeRecord] = []
    for mut in mutations:
        records = await _host()._apply_one(neptune, graph_uri, mut)
        applied.append(mut)
        change_records.extend(records)

    version_after = (
        version_before
        if not applied
        else await _host().fingerprint_ontology(neptune, graph_uri)
    )

    revision: int | None = None
    if applied:
        revision = await _host()._bump_revision(neptune, graph_uri)
        await _host()._emit_changelog(
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

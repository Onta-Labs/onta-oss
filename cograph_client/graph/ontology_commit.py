"""Ontology schema commit API — Wave 0 signature freeze for ONTA-403.

Every process that mutates ontology *schema* (types, attributes, relationships,
subclass edges, core-slot markers, text-kinds, aliases, deprecations) will call
:func:`commit_ontology`. This is the schema-write analogue of
``kg_writer.insert_facts`` for instance data (ADR 0007) — a second, parallel
discipline. The ~40 mutation sites in ``schema_resolver``, the ontology REST
routes, and governance writers all converge here in ONTA-403.

**Wave 0 freezes the signature and the supporting types only.** Calling this
function raises :class:`NotImplementedError` until ONTA-403 lands the body.
Do not invent a parallel write path in the meantime.
"""

from __future__ import annotations

from typing import Sequence

from cograph_client.models.ontology import (
    OntologyCommitResult,
    OntologyMutation,
)


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
        fingerprint once implemented.
    expected_version:
        Optimistic-concurrency token from :func:`ontology_version` (extended
        by ONTA-403). ``None`` means "write unconditionally" (legacy callers).
        A mismatch rejects the commit rather than silently clobbering.
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

    Raises
    ------
    NotImplementedError
        Until ONTA-403 implements the body. Callers must not catch-and-ignore.
    """
    raise NotImplementedError(
        "commit_ontology is the Wave-0-frozen signature for ONTA-403; "
        "the body lands with that ticket. Do not invent a parallel schema "
        f"write path (graph_uri={graph_uri!r}, n_mutations={len(mutations)})."
    )

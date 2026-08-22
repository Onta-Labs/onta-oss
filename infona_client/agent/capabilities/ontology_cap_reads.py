"""The agent's tenant type list, read from the GraphStore catalog (ONTA-534).

``OntologyCapability.describe_ontology`` answers "show me the schema" / "what
types are there". With no type in scope it calls :func:`list_tenant_types`,
which used to be a bare ``ctx.neptune.query(list_types_query(...))`` inside
``ontology_cap`` — and the SPARQL HTTP client's ``query`` is RETIRED under the
Neo4j GraphStore (``_ensure_sparql_http_allowed`` raises ``SparqlClientRetired``
unless ``allow_http=True``, and ``api/app.py`` builds a bare client). The call
had no ``try`` and no store arm, so the exception escaped the capability and
every schema-inspection turn on ``/graphs/<t>/agent`` came back as a 500.

**Catalog first, SPARQL as a supplement.** The declarations come from
:func:`infona_client.graph.ontology_catalog.list_types` — the same catalog
``/ontology/types`` and the Explorer's ontology browser read, and the same one
``nlp/pipeline_ontology_catalog`` (#447) grounds the planner in, so the agent,
the browser and the planner cannot disagree about what a workspace declares.
The residual SPARQL arm is still consulted, so the dual-arm unit tests keep
exercising it, but its failure is swallowed once the catalog has answered.

**Empty is an ANSWER here, not a decline.** A workspace that declares no types
yet is an ordinary state (it is the state of every workspace before its first
ingest), and "you have not declared any types" is the correct reply — unlike
``pipeline_ontology_catalog``, where an empty schema would be handed to the
planner as authoritative grounding and had to decline instead.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.stdlib.get_logger("infona.agent.ontology")


async def _catalog_types(tenant_id: str) -> list[tuple[str, str]]:
    """``(name, description)`` per declared tenant type, or ``[]``.

    Best-effort: an unconfigured store or a catalog error yields ``[]`` so the
    caller falls through to its residual SPARQL arm rather than claiming the
    workspace declares nothing.
    """
    try:
        from infona_client.graph.ontology_catalog import list_types
        from infona_client.graph.store import get_optional_graph_store

        rows = await list_types(
            store=get_optional_graph_store(),
            layer="tenant",
            tenant_id=tenant_id,
        )
    except Exception as exc:  # noqa: BLE001 — fail soft onto the SPARQL arm
        logger.debug(
            "ontology_list_types_catalog_failed",
            tenant_id=tenant_id,
            error=str(exc),
        )
        return []
    return [
        (r.name, getattr(r, "description", "") or "")
        for r in rows or ()
        if getattr(r, "name", "")
    ]


async def list_tenant_types(neptune: Any, tenant_id: str) -> list[dict]:
    """The tenant's declared types as ``[{"name", "description"}, ...]``.

    Catalog rows first (in catalog order), then any SPARQL row the catalog did
    not already name. Never raises: a retired / failing SPARQL client leaves the
    catalog's answer standing instead of turning an answerable question into a
    500.
    """
    from infona_client.graph.ontology_queries import list_types_query
    from infona_client.graph.parser import parse_sparql_results
    from infona_client.graph.queries import tenant_graph_uri

    seen: set[str] = set()
    types: list[dict] = []
    for name, description in await _catalog_types(tenant_id):
        if name in seen:
            continue
        seen.add(name)
        types.append({"name": name, "description": description})

    try:
        _, rows = parse_sparql_results(
            await neptune.query(list_types_query(tenant_graph_uri(tenant_id)))
        )
    except Exception as exc:  # noqa: BLE001 — retired client must not 500
        logger.debug(
            "ontology_list_types_sparql_failed", tenant_id=tenant_id, error=str(exc)
        )
        return types

    for r in rows:
        label = r.get("label", "")
        if not label or label in seen:
            continue
        seen.add(label)
        types.append({"name": label, "description": r.get("comment", "")})
    return types


__all__ = ["list_tenant_types"]

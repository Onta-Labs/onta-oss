"""GraphStore arm of the workspace-wide instance probe (ONTA-534).

Split out of :mod:`infona_client.graph.kg_status` so that file stays inside its
size budget, the same move :mod:`ontology_base_pin_store` made for the base pin.
It holds ONE function because that is what the split needs; the older
``_kg_data_status_graph_store`` stays in ``kg_status`` rather than moving with
it, since it reads the KG_OK / KG_EMPTY / KG_MISSING verdicts defined there and
importing them back would close an import cycle.

Nothing here decides what a caller SAYS about the answer — that lives with the
caveat in :func:`infona_client.graph.kg_status.other_graphs_hold_instances`,
whose docstring also records what this measurement does and does not imply on
the property-graph backend.
"""

from __future__ import annotations

import structlog

logger = structlog.stdlib.get_logger("infona.graph.kg_status")


async def _graphs_hold_instances_store(
    tenant_id: str, targets: tuple[str, ...]
) -> bool | None:
    """GraphStore arm of :func:`other_graphs_hold_instances` (ONTA-534).

    ``True`` / ``False`` when the store ANSWERED; ``None`` when it could not,
    so the caller keeps the residual SPARQL arm (duck-typed doubles) and, past
    that, the fail-toward-silence rule. "Answered no" and "could not answer"
    stay distinguishable here for the same reason
    :meth:`~infona_client.nlp.pipeline_active_types.PipelineActiveTypesMixin._store_instance_types`
    keeps them apart: only the second may leave a positive claim unmade by
    accident, and only the first is a measurement.

    What a graph URI MEANS on the property graph
    --------------------------------------------
    There are no named graphs: instance data is ``:Entity`` nodes carrying
    ``(tenant_id, kg)``. Each URI the caller passes resolves to at most one such
    scope, and a URI that resolves to none cannot hold an instance at all.

    * per-KG URI (``…/graphs/<t>/kg/<kg>``) → :meth:`GraphScope.for_instance`.
    * bare tenant URI (``…/graphs/<t>``) → :meth:`GraphScope.for_catalog`
      (``layer='tenant'``, ``kg=__ontology__``). That IS the property-graph home
      of "base graph" instance data: an ingest with no ``kg_name`` leaves
      ``target_instance_graph = graph_uri`` (``resolver/schema_ingest.py``) and
      ``kg_writer``'s session resolution maps the bare tenant URI onto the
      tenant catalog scope, so the entities land under ``__ontology__``.
    * the shared Global layer URIs (``…/graphs/global/public|enhanced``) →
      NOTHING, by construction rather than by omission. ``graph/layer_content.py``
      permits only ontology content kinds on those layers, and
      ``GraphScope.for_instance`` refuses ``__global__`` outright, so there is no
      instance scope to read there and no reason to open a cross-workspace
      session to look. This is the half of the old SPARQL measurement (roughly a
      thousand typed subjects in the Global layers on Neptune) that has no
      property-graph counterpart — see :func:`other_graphs_hold_instances`.

    Scoped to the CALLER'S OWN workspace and nothing else: a URI naming another
    tenant is skipped, not read. Same rule, and the same reason, as
    ``nlp/pipeline_ontology_catalog.tenant_for_catalog_graph`` — deriving the
    scope from anything looser than the URI's own tenant is precisely the shape
    a cross-tenant read takes.
    """
    from infona_client.graph.queries import parse_kg_graph_uri, parse_tenant_graph_uri
    from infona_client.graph.scope import GraphScope
    from infona_client.graph.store import GraphConfigError, get_optional_graph_store

    try:
        store = get_optional_graph_store()
    except GraphConfigError:
        return None

    scopes: list[GraphScope] = []
    seen: set[tuple[str, str]] = set()
    for graph in targets:
        parsed = parse_kg_graph_uri(graph)
        try:
            if parsed is not None:
                if parsed[0] != tenant_id:
                    continue
                scope = GraphScope.for_instance(parsed[0], parsed[1])
            elif parse_tenant_graph_uri(graph) == tenant_id:
                scope = GraphScope.for_catalog(layer="tenant", tenant_id=tenant_id)
            else:
                continue
        except Exception:  # noqa: BLE001 — an unscopeable URI holds nothing here
            continue
        pair = (scope.tenant_id, scope.kg)
        if pair in seen:
            continue
        seen.add(pair)
        scopes.append(scope)
    if not scopes:
        # Not a failure: every URI resolved to a catalog-only or foreign scope,
        # so the store DID answer and the answer is "none of these can hold an
        # instance". Returning None here would hand the question to a SPARQL
        # client that is retired, and the warning it logs would be noise.
        return False

    try:
        from infona_client.graph.explore_store import count_entities_pg

        for scope in scopes:
            if int(await count_entities_pg(store.session(scope)) or 0) > 0:
                return True
    except Exception:  # noqa: BLE001 — unanswered, never answered "no"
        logger.warning(
            "other_graph_instance_store_probe_failed",
            tenant=tenant_id,
            exc_info=True,
        )
        return None
    return False

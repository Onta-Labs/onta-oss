"""One layer's declared schema, read from the GraphStore catalog (ONTA-534).

``NLQueryPipeline._fetch_ontology`` reads EVERY visible layer's schema so the
planner can see the whole ontology. It did that with
``get_full_ontology_query`` over the SPARQL HTTP client, which is RETIRED under
the shipped Neo4j GraphStore: on production every layer raised
``SparqlClientRetired``, the per-layer ``except: continue`` swallowed it, and
the fetch ended with an EMPTY ``types`` dict. That is what the
``layer_ontology_fetch_failed`` warnings on ``/graphs/<t>/agent`` are.

This module answers the same question against the catalog
(:mod:`infona_client.graph.ontology_catalog`) — the same reads
``api/routes/ontology_workspace.py::_workspace_ontology_store`` already uses to
build the layered Explorer ontology, so the planner and the ontology browser can
no longer disagree about what a workspace declares.

**Shape, not semantics.** The result is deliberately the SPARQL binding shape
(``typeLabel`` / ``attrLabel`` / ``range``) that ``get_full_ontology_query``
projects, so ``_fetch_ontology``'s long assembly loop — shadowing precedence,
``skip_invalid_type_name`` fail-soft, ``[no instances]`` annotation, enum
discovery — consumes store rows and SPARQL rows through ONE code path. Porting
the read must not fork the summary.

**Best-effort, and ``None`` means "the store had nothing to say"** — a missing
tenant scope, an unconfigured store, a store error, or zero declared types all
return ``None`` so the caller keeps its residual SPARQL arm rather than
inventing an answer. Mirrors
:meth:`infona_client.nlp.pipeline_active_types.PipelineActiveTypesMixin._store_instance_types`
and ``ontology_workspace._live_workspace_type_counts``.

Known gap, unchanged by this port: the catalog carries no ATTACHED FUNCTIONS,
so ``funcName`` is never projected here. ``_workspace_ontology_store`` has the
same gap ("GraphStore function attach still SPARQL-only") and on the shipped
backend the SPARQL arm returned no functions either — it raised — so this is
not a regression, just a hole that stays open.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from infona_client.graph.layers import Layer

logger = structlog.stdlib.get_logger("infona.nlp.pipeline")

#: ``get_full_ontology_query`` projects a literal attribute's ``?range`` as an
#: XSD datatype IRI, and ``_fetch_ontology`` reads the datatype back off the
#: fragment (``range_str.split("#")[-1]``). Catalog rows carry the bare name
#: (``string`` / ``integer`` / …), so re-mint the IRI rather than teaching the
#: consumer a second encoding.
XSD_PREFIX = "http://www.w3.org/2001/XMLSchema#"


def tenant_for_catalog_graph(onto_graph: str) -> str | None:
    """Workspace whose tenant catalog ``onto_graph`` names, or ``None``.

    Derived from the LAYER's own graph URI and nothing else. The looser
    alternative — falling back to the request's other graph URIs — would let a
    catalog read scope itself to a workspace the layer URI does not name, which
    is precisely the shape a cross-tenant read takes. ``None`` (and therefore
    the SPARQL arm) is the safe answer for anything that does not round-trip.
    """
    from infona_client.graph.queries import parse_kg_graph_uri, parse_tenant_graph_uri

    if not onto_graph:
        return None
    tenant = parse_tenant_graph_uri(onto_graph)
    if tenant:
        return tenant
    parsed = parse_kg_graph_uri(onto_graph)
    return parsed[0] if parsed else None


async def layer_ontology_bindings(
    layer: "Layer",
    *,
    onto_graph: str,
    store: Any = None,
) -> list[dict[str, str]] | None:
    """Declared types + attributes of ONE layer, in ``get_full_ontology_query`` shape.

    Returns one row per (type × attribute), plus a bare ``{"typeLabel": name}``
    row for a type that declares none — the same "an empty type still yields a
    row" contract the SPARQL builder gets from its OPTIONAL blocks, which is
    what keeps a declared-but-attributeless type VISIBLE (ONTA-258) instead of
    silently dropped.

    ``None`` when this layer cannot be read from the store; never raises.
    """
    from infona_client.graph.layers import Layer, layer_type_uri

    tenant_id: str | None = None
    if layer is Layer.TENANT:
        tenant_id = tenant_for_catalog_graph(onto_graph)
        if not tenant_id:
            # No workspace encoded in the layer graph URI: nothing to scope a
            # tenant-catalog session to.
            return None

    try:
        from infona_client.graph.ontology_catalog import (
            list_attributes as cat_list_attrs,
            list_types as cat_list_types,
        )

        scope: dict[str, Any] = {"store": store, "layer": layer.value}
        if tenant_id:
            scope["tenant_id"] = tenant_id
        type_rows = await cat_list_types(**scope)
        if not type_rows:
            # "This layer declares nothing" and "this store had nothing to say"
            # are indistinguishable here, and treating the second as the first
            # would hand the planner a confidently empty schema. Decline.
            return None
        attr_rows = await cat_list_attrs(type_name=None, **scope)
    except Exception as exc:  # noqa: BLE001 — fail soft, never break /ask
        logger.debug(
            "layer_ontology_catalog_read_failed",
            graph_uri=onto_graph,
            layer=layer.value,
            error=str(exc),
        )
        return None

    by_domain: dict[str, list[Any]] = {}
    for attr in attr_rows or ():
        domain = getattr(attr, "domain", "") or ""
        if domain:
            by_domain.setdefault(domain, []).append(attr)

    rows: list[dict[str, str]] = []
    for type_row in type_rows:
        name = getattr(type_row, "name", "") or ""
        if not name:
            continue
        emitted = 0
        for attr in by_domain.get(name, ()):
            leaf = getattr(attr, "name", "") or ""
            if not leaf:
                continue
            row = {"typeLabel": name, "attrLabel": leaf}
            if getattr(attr, "kind", "") == "relationship":
                target = getattr(attr, "range_type", None)
                if not target:
                    continue
                try:
                    # Same-layer namespace: the consumer runs the URI back
                    # through `type_name_from_uri`, which is what tells a
                    # RELATIONSHIP apart from a literal. A range it cannot mint
                    # (a corrupt stored leaf) drops that one attribute rather
                    # than mislabelling the edge as a string column.
                    row["range"] = layer_type_uri(layer, target)
                except Exception:
                    continue
            else:
                row["range"] = f"{XSD_PREFIX}{getattr(attr, 'datatype', None) or 'string'}"
            rows.append(row)
            emitted += 1
        if not emitted:
            rows.append({"typeLabel": name})
    return rows or None

"""Normalization inference's graph reads, on the GraphStore (ONTA-534).

:mod:`infona_client.normalization.inference` asks the graph two questions before
it can propose a single rule:

1. **which predicates does this type declare?** (``_list_predicates``)
2. **what do this predicate's values actually look like?** (``_sample_values``)

Both were SPARQL-only, and the SPARQL HTTP ``query`` is RETIRED under the shipped
Neo4j GraphStore. The first had no ``try`` at all, so ``POST /normalize/suggest``
— and the agent's clean / enrich planning, which grounds itself in the same read
via ``sample_predicate_values`` — raised ``SparqlClientRetired`` straight out of
the route as a **500**. The second was already wrapped, so it degraded silently:
had only the first been ported, every suggestion call would have come back a
confident empty ``[]`` ("no normalization needed") without a single value ever
being looked at. Porting one without the other trades a 500 for a wrong answer,
so both live here.

**Declarations** come from :func:`infona_client.graph.ontology_catalog.list_attributes`
— the same catalog ``/ontology/*``, the Explorer's ontology browser and
``nlp/pipeline_ontology_catalog`` (#447) read, so the rule inferencer cannot
disagree with the browser about what a type declares.

**Values** come from the ``entity_type_prop_distinct`` /
``entity_type_rel_target_distinct`` templates, the same two reads
``nlp/dim_registry_refresh`` samples dimension values with — so "what values does
this predicate hold" has ONE implementation, not one per consumer.

**Sampling shape.** The SPARQL arm pooled ``_NUM_SAMPLES`` independent draws at
staggered offsets/orderings to avoid always seeing the same head. The store
templates return DISTINCT values ordered ascending under a single ``LIMIT``, so
the port takes ONE draw of the same total budget (``_NUM_SAMPLES ×
_VALUES_PER_SAMPLE``) rather than faking three. Same value budget, same
distinctness; the deliberate loss is the DESC draw's tail, which mattered only
because each SPARQL draw was individually capped.

Every function returns ``None`` for "the store could not be consulted" (no
store, bad scope, store error) so callers keep their residual SPARQL arm, and a
real (possibly empty) list when the store answered.
"""

from __future__ import annotations

import structlog

logger = structlog.stdlib.get_logger("infona.normalization.inference")


async def declared_attributes(tenant_id: str, type_name: str):
    """``OntoAttrRecord`` rows a type declares in the tenant catalog, or ``None``.

    ``None`` = the catalog could not be consulted (unconfigured store, scope
    error, store failure). An empty list means the type genuinely declares
    nothing, which callers may treat as an answer.
    """
    try:
        from infona_client.graph.ontology_catalog import list_attributes
        from infona_client.graph.store import get_optional_graph_store

        rows = await list_attributes(
            store=get_optional_graph_store(),
            type_name=type_name,
            layer="tenant",
            tenant_id=tenant_id,
        )
    except Exception as exc:  # noqa: BLE001 — fail soft onto the SPARQL arm
        logger.debug(
            "inference_catalog_attrs_failed",
            tenant_id=tenant_id,
            type_name=type_name,
            error=str(exc),
        )
        return None
    return list(rows or ())


async def catalog_predicates(
    tenant_id: str, type_name: str
) -> list[tuple[str, str]] | None:
    """``[(predicate_uri, target_kind)]`` from the catalog, or ``None``.

    ``predicate_uri`` is the ONTOLOGY attr URI (``types/<T>/attrs/<leaf>``), the
    same form the SPARQL arm projects, so downstream leaf extraction and the
    instance-pattern UNION are unchanged. ``target_kind`` is ``"relationship"``
    when the declaration ranges over a type, else ``"attribute"``.
    """
    rows = await declared_attributes(tenant_id, type_name)
    if rows is None:
        return None
    from infona_client.graph.ontology_queries import attr_uri

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for rec in rows:
        leaf = getattr(rec, "name", "") or ""
        if not leaf:
            continue
        try:
            uri = attr_uri(type_name, leaf)
        except Exception:  # noqa: BLE001 — one corrupt leaf costs THAT leaf only
            continue
        if uri in seen:
            continue
        seen.add(uri)
        kind = (
            "relationship"
            if getattr(rec, "kind", "") == "relationship"
            else "attribute"
        )
        out.append((uri, kind))
    return out


async def sample_values(
    *,
    tenant_id: str,
    kg_name: str,
    type_name: str,
    predicate_leaf: str,
    target_kind: str,
    limit: int,
) -> list[str] | None:
    """Distinct values for one predicate of one type, or ``None``.

    Literals come back as their string form; a relationship's values are the
    TARGET entity's display name (falling back to its id) — the same "human
    value, not the URI" projection the SPARQL arm built with
    ``COALESCE(?lbl, REPLACE(STR(?o), "^.*/", ""))``.
    """
    if not (tenant_id and kg_name and type_name and predicate_leaf):
        return None
    try:
        from infona_client.graph.scope import GraphScope
        from infona_client.graph.store import get_optional_graph_store

        session = get_optional_graph_store().session(
            GraphScope.for_instance(tenant_id, kg_name)
        )
        if target_kind == "relationship":
            rows = await session.execute_template(
                "entity_type_rel_target_distinct",
                {
                    "primary_type": type_name,
                    "rel_attr": predicate_leaf,
                    "limit": int(limit),
                },
            )
        else:
            rows = await session.execute_template(
                "entity_type_prop_distinct",
                {
                    "primary_type": type_name,
                    "prop_key": predicate_leaf,
                    "limit": int(limit),
                },
            )
    except Exception as exc:  # noqa: BLE001 — fail soft onto the SPARQL arm
        logger.debug(
            "inference_store_sample_failed",
            tenant_id=tenant_id,
            kg=kg_name,
            type_name=type_name,
            predicate=predicate_leaf,
            error=str(exc),
        )
        return None

    out: list[str] = []
    seen: set[str] = set()
    for r in rows or ():
        d = r.to_dict() if hasattr(r, "to_dict") else dict(r)
        raw = d.get("value")
        if raw is None:
            continue
        val = str(raw).strip()
        if not val or val in seen:
            continue
        seen.add(val)
        out.append(val)
    return out


__all__ = ["catalog_predicates", "declared_attributes", "sample_values"]

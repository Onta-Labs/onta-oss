"""The rule-apply reads, on the GraphStore (ONTA-534).

``normalization/execute.py`` WRITES through the converged path
(``graph/kg_writer.py``) but still READ through the retired SPARQL HTTP client,
so on the shipped Neo4j-only backend every ``apply_rule`` raised
``SparqlClientRetired`` on its FIRST read: ``strip_emoji``, ``promote_to_node``
and BOTH ``list_explode`` shapes were dead. The route returns 202 and the apply
runs detached, so the only user-visible symptom was a rule that stayed
``confirmed`` forever (now ``failed`` + ``last_error``, #452) and a graph that
never changed.

Four reads, three templates (``graph/normalize_cypher.py``):

======================== =================================================
rule shape               read
======================== =================================================
``strip_emoji``          every literal of a leaf, KG-wide
``list_explode`` literal every literal of a leaf, KG-wide
``list_explode`` rel     every ``onto/<leaf>`` edge + its target's label
``promote_to_node``      every literal of a leaf **on one type**
orphan sweep             entities of a type with no inbound ``<leaf>`` edge
sweep target type        the leaf's declared range, from the ontology catalog
======================== =================================================

**Why not ``entity_literal_grep``?** It is paged (clamped to
``explore_store.MAX_PAGE_LIMIT``) and rejects an empty needle
(``GraphScopeError``). Apply must process EVERY row and ``strip_emoji`` has no
needle — emoji span too many codepoints for a substring pre-filter, which is
exactly why the SPARQL it replaces cleaned in Python. A paged read would
silently normalize the first 200 rows and report success.

**Predicate scope is preserved.** The SPARQL being ported filters on the
predicate alone, so ONE ``strip_emoji`` rule on ``skills`` cleans every type's
``skills`` literal. See ``graph/normalize_cypher.py`` for why narrowing that to
the rule's own type would have been a silent semantic regression.
``promote_to_node`` is the one genuinely type-scoped rule and passes its type.

Every function returns ``None`` for "the store could not be consulted" (no
store, bad scope, store error) so callers keep their residual SPARQL arm, and a
real (possibly empty) list when the store answered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.stdlib.get_logger("infona.normalization.execute")


@dataclass(frozen=True, slots=True)
class LiteralRow:
    """One literal value of the target leaf on one subject.

    A property-graph literal property may hold a LIST (the shape an
    already-exploded attribute takes), so one entity can yield several rows.
    ``value`` keeps the store's NATIVE type. Ingest writes a typed literal
    (``"4.6"^^xsd:float``) and the store holds a real float, so a caller that
    rewrites one value of a leaf must hand the untouched siblings back exactly as
    it found them — and a ``key_by="owner"`` promotion must land the measurement
    on the node without retyping it.
    """

    subject: str
    type_name: str | None
    value: Any


@dataclass(frozen=True, slots=True)
class RelRow:
    """One ``<leaf>`` edge and the display name / type of its target."""

    subject: str
    target: str
    target_name: str | None
    target_type: str | None


@dataclass(frozen=True, slots=True)
class OrphanRow:
    """One entity of the swept type with no inbound ``<leaf>`` edge."""

    uri: str
    name: str | None


def _session(kg_graph: str):
    """Scoped instance session for ``kg_graph``, or ``None``.

    ``None`` (rather than a raise) is the "could not be consulted" signal the
    callers' residual SPARQL arm keys on: a non-KG graph URI, an unconfigured
    store, or a scope that will not build.
    """
    try:
        from infona_client.graph.queries import parse_kg_graph_uri
        from infona_client.graph.scope import GraphScope
        from infona_client.graph.store import get_optional_graph_store

        pair = parse_kg_graph_uri(kg_graph)
        if pair is None:
            return None
        return get_optional_graph_store().session(GraphScope.for_instance(*pair))
    except Exception as exc:  # noqa: BLE001 — fail soft onto the SPARQL arm
        logger.debug("normalize_store_session_failed", graph=kg_graph, error=str(exc))
        return None


def _rows(record) -> dict:
    return record.to_dict() if hasattr(record, "to_dict") else dict(record)


async def literal_rows(
    kg_graph: str, predicate_leaf: str, *, type_name: str | None = None
) -> list[LiteralRow] | None:
    """Every literal of ``predicate_leaf``, KG-wide or scoped to ``type_name``.

    List-valued properties are EXPANDED, one :class:`LiteralRow` per value, in
    store order — so a caller that groups by subject sees the same multi-value
    set the graph holds and can rewrite it whole.
    """
    if not predicate_leaf:
        return None
    session = _session(kg_graph)
    if session is None:
        return None
    try:
        records = await session.execute_template(
            "entity_literals_by_prop",
            {"prop_key": predicate_leaf, "primary_type": type_name},
        )
    except Exception as exc:  # noqa: BLE001 — fail soft onto the SPARQL arm
        logger.debug(
            "normalize_store_literals_failed",
            graph=kg_graph,
            predicate=predicate_leaf,
            error=str(exc),
        )
        return None

    out: list[LiteralRow] = []
    for rec in records or ():
        d = _rows(rec)
        subject = str(d.get("entity_uri") or "")
        if not subject:
            continue
        raw = d.get("value")
        t = d.get("type")
        t = str(t) if t else None
        values = raw if isinstance(raw, (list, tuple)) else [raw]
        for v in values:
            if v is None or v == "":
                continue
            out.append(LiteralRow(subject=subject, type_name=t, value=v))
    return out


async def rel_rows(kg_graph: str, predicate_leaf: str) -> list[RelRow] | None:
    """Every ``predicate_leaf`` edge in the KG, with the target's name + type."""
    if not predicate_leaf:
        return None
    session = _session(kg_graph)
    if session is None:
        return None
    try:
        from infona_client.graph.facts import sanitize_rel_type

        records = await session.execute_template(
            "entity_rels_by_attr",
            {
                "rel_attr": predicate_leaf,
                "rel_type": sanitize_rel_type(predicate_leaf),
            },
        )
    except Exception as exc:  # noqa: BLE001 — fail soft onto the SPARQL arm
        logger.debug(
            "normalize_store_rels_failed",
            graph=kg_graph,
            predicate=predicate_leaf,
            error=str(exc),
        )
        return None

    out: list[RelRow] = []
    for rec in records or ():
        d = _rows(rec)
        subject = str(d.get("start_id") or "")
        target = str(d.get("end_id") or "")
        if not subject or not target:
            continue
        name = d.get("end_name")
        ttype = d.get("end_type")
        out.append(
            RelRow(
                subject=subject,
                target=target,
                target_name=str(name) if name else None,
                target_type=str(ttype) if ttype else None,
            )
        )
    return out


async def orphan_rows(
    kg_graph: str, type_name: str, predicate_leaf: str
) -> list[OrphanRow] | None:
    """Entities of ``type_name`` with no inbound ``predicate_leaf`` edge.

    Candidates only — the caller still narrows to the COMPOSITE-named ones
    before deleting. Scope is the session's ``(tenant_id, kg)``, which the
    template binds into every pattern, so a sweep can never reach another
    workspace's or another KG's nodes.
    """
    if not type_name or not predicate_leaf:
        return None
    session = _session(kg_graph)
    if session is None:
        return None
    try:
        from infona_client.graph.facts import sanitize_rel_type

        records = await session.execute_template(
            "entity_orphans_of_type",
            {
                "primary_type": type_name,
                "rel_attr": predicate_leaf,
                "rel_type": sanitize_rel_type(predicate_leaf),
            },
        )
    except Exception as exc:  # noqa: BLE001 — fail soft onto the SPARQL arm
        logger.debug(
            "normalize_store_orphans_failed",
            graph=kg_graph,
            type_name=type_name,
            predicate=predicate_leaf,
            error=str(exc),
        )
        return None

    out: list[OrphanRow] = []
    for rec in records or ():
        d = _rows(rec)
        uri = str(d.get("entity_uri") or "")
        if not uri:
            continue
        name = d.get("name")
        out.append(OrphanRow(uri=uri, name=str(name) if name else None))
    return out


async def catalog_range_types(
    onto_graph: str, domain_type: str, predicate_leaf: str
) -> set[str] | None:
    """The leaf's declared range type(s) for ``domain_type``, from the catalog.

    The ported form of the sweep's ``rdfs:range`` lookup: the ONE catalog
    ``/ontology/*``, the Explorer's ontology browser and the rule inferencer
    (``inference_reads.declared_attributes``) already read, so the sweep cannot
    disagree with the browser about what a predicate ranges over. A literal
    (``string`` / ``float`` / …) range yields the empty set — not an entity
    target — and the caller falls back to the composites it re-pointed.
    """
    if not domain_type or not predicate_leaf:
        return None
    try:
        from infona_client.graph.queries import parse_tenant_graph_uri

        tenant_id = parse_tenant_graph_uri(onto_graph)
        if not tenant_id:
            return None
        from infona_client.normalization.inference_reads import declared_attributes

        rows = await declared_attributes(tenant_id, domain_type)
    except Exception as exc:  # noqa: BLE001 — fail soft onto the SPARQL arm
        logger.debug(
            "normalize_store_range_failed",
            graph=onto_graph,
            type_name=domain_type,
            predicate=predicate_leaf,
            error=str(exc),
        )
        return None
    if rows is None:
        return None

    out: set[str] = set()
    for rec in rows:
        if getattr(rec, "name", "") != predicate_leaf:
            continue
        target = (getattr(rec, "range_type", "") or "").strip()
        if target:
            out.add(target)
    return out


__all__ = [
    "LiteralRow",
    "OrphanRow",
    "RelRow",
    "catalog_range_types",
    "literal_rows",
    "orphan_rows",
    "rel_rows",
]

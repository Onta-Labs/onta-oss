"""GraphStore ontology → Cypher planning text + record binding helpers."""

from __future__ import annotations

from typing import Any, Sequence

from infona_client.graph.store import GraphRecord

def format_schema_types_for_cypher(types: Sequence[Any]) -> str:
    """Render :class:`SchemaTypeSummary` rows as Cypher-oriented ontology text.

    Accepts any objects with ``name``, optional ``entity_count``,
    ``description``, ``parent_type``, and ``attributes`` (each with
    ``name`` / ``kind`` / ``datatype`` / ``range_type`` / ``prop_key``).

    Catalog-only (no per-slot inventory): type-level ``[no instances]`` only;
    declared slots on populated types are treated as usable. Prefer
    :func:`ontology_from_graph_store` (``prefer_populated=True``) for the
    planning path that demotes declared-empty leaves and surfaces
    instance-only predicates.
    """
    from infona_client.nlp.planning_schema import (
        format_planning_ontology,
        planning_types_from_schema_and_summaries,
    )

    planning = planning_types_from_schema_and_summaries(
        types,
        summaries_by_name=None,
        inventory_probed=False,
        max_empty_types=10_000,  # catalog-only: do not silently drop empties
    )
    # No planning preface on the bare catalog formatter (keeps unit tests and
    # non-ask consumers byte-stable aside from empty-type slot elision).
    return format_planning_ontology(planning, preface=False, max_empty_types=10_000)


async def ontology_from_graph_store(
    store: Any,
    *,
    tenant_id: str,
    kg: str,
    prefer_populated: bool = True,
    type_names: Sequence[str] | None = None,
    force_include: Sequence[str] | None = None,
    max_empty_types: int | None = None,
) -> tuple[str, list[str]]:
    """Load ontology text + type names from GraphStore for NL planning.

    When ``prefer_populated`` (default), merges tenant catalog declarations with
    per-type instance inventory (:func:`~infona_client.graph.explore_store.type_summary`)
    so populated leaves rank first and declared-but-empty edges/attrs are
    secondary ``[no instances]`` slots. Instance-only leaves (present in the KG
    but not in the catalog) are first-class primary context.

    ``type_names`` optionally scopes the text to a semantic top-K (plus 1-hop
    relationship neighbours from inventory). Instance-populated types in THIS
    kg are always kept even when they miss that top-K; if the requested names
    have zero overlap with the live inventory, the semantic/caller scope is
    ignored. Empty types are capped via ``max_empty_types`` to reduce
    tenant-ontology pollution in per-KG /ask.

    Returns ``("", [])`` on any failure so the pipeline can fall back.
    """
    if store is None or not tenant_id or not kg:
        return "", []
    try:
        from infona_client.graph.ontology_catalog import schema_types_for_kg
        from infona_client.nlp.planning_schema import (
            DEFAULT_MAX_EMPTY_TYPES,
            format_planning_ontology,
            planning_types_from_schema_and_summaries,
        )

        rows = await schema_types_for_kg(
            store, tenant_id=tenant_id, kg=kg, include_attrs=True
        )
        if not rows:
            return "", []

        empty_cap = (
            DEFAULT_MAX_EMPTY_TYPES if max_empty_types is None else int(max_empty_types)
        )
        force = list(force_include or ())
        wanted = {n for n in (type_names or ()) if n}
        force_set = set(force) | wanted

        if not prefer_populated:
            text = format_schema_types_for_cypher(rows)
            names = [r.name for r in rows if getattr(r, "name", None)]
            return text, names

        # Probe inventory for types that carry instances (and any force-include).
        summaries: dict[str, Any] = {}
        try:
            from infona_client.graph.explore_store import type_summary

            probe_names = [
                r.name
                for r in rows
                if getattr(r, "name", None)
                and (
                    int(getattr(r, "entity_count", 0) or 0) > 0
                    or r.name in force_set
                )
            ]
            # Also probe wanted names not in catalog (instance-only types).
            seen_probe = set(probe_names)
            for n in force_set:
                if n not in seen_probe:
                    probe_names.append(n)
                    seen_probe.add(n)

            async def _one(name: str) -> tuple[str, Any]:
                try:
                    row = await type_summary(
                        store=store,
                        tenant_id=tenant_id,
                        kg_name=kg,
                        type_name=name,
                    )
                    return name, row
                except Exception:
                    return name, None

            if probe_names:
                import asyncio

                results = await asyncio.gather(*[_one(n) for n in probe_names])
                for name, row in results:
                    if row is not None:
                        summaries[name] = row
        except Exception:
            summaries = {}

        planning = planning_types_from_schema_and_summaries(
            rows,
            summaries,
            max_empty_types=empty_cap if not wanted else 10_000,
            force_include=force_set or None,
            inventory_probed=True,
        )

        # Optional semantic / caller type filter + 1-hop neighbour expansion
        # via *populated* relationship ranges (planning truth, not dead edges).
        # Semantic/caller names rank and may add extra declared types; they
        # must NEVER hide a type that has instances in THIS kg. Zero overlap
        # with the live inventory means the retrieve hit leftover empty types
        # from another ingest — ignore that scope.
        if wanted:
            live = {t.name for t in planning if t.entity_count > 0}
            overlap = wanted & live
            if live and not overlap:
                expand = set(force) | live
            else:
                expand = set(wanted) | set(force) | live
            for t in planning:
                if t.name not in expand:
                    continue
                for s in t.slots:
                    if (
                        s.populated
                        and s.kind == "relationship"
                        and s.range_type
                    ):
                        expand.add(s.range_type)
            planning = [t for t in planning if t.name in expand]
            # Preserve population order from planning_types_from_schema_and_summaries
            # (already sorted); re-filter only.

        if not planning:
            # Fall back to catalog-only formatting rather than empty string so
            # callers still get *some* schema when filters were too aggressive.
            text = format_schema_types_for_cypher(rows)
            names = [r.name for r in rows if getattr(r, "name", None)]
            return text, names

        text = format_planning_ontology(
            planning,
            max_empty_types=empty_cap if not wanted else 10_000,
            force_include=force_set or None,
            preface=True,
        )
        try:
            from infona_client.nlp.populated_leaf_plan import (
                format_leaf_grounding_notes,
            )

            extra = format_leaf_grounding_notes(planning)
            if extra:
                text = f"{text}\n{extra}"
        except Exception:
            pass
        names = [t.name for t in planning]
        return text, names
    except Exception:
        return "", []


def records_to_bindings(records: list[GraphRecord]) -> tuple[list[str], list[dict[str, str]]]:
    """Convert GraphStore records to (variables, bindings) like SPARQL results.

    Values are stringified so :meth:`NLQueryPipeline._format_answer` can render
    them without a second code path.
    """
    if not records:
        return [], []
    # Union of keys in order of first appearance
    variables: list[str] = []
    seen: set[str] = set()
    for rec in records:
        for k in rec.keys():
            if k not in seen:
                seen.add(k)
                variables.append(str(k))
    bindings: list[dict[str, str]] = []
    for rec in records:
        row: dict[str, str] = {}
        for k in variables:
            v = rec.get(k)
            # Omit None so unbound_projection_vars can detect columns that
            # never bound (SPARQL OPTIONAL parity / ONTA-530 honesty).
            if v is None:
                continue
            row[k] = str(v)
        bindings.append(row)
    return variables, bindings


def neo4j_ask_enabled(*, explicit: bool | None = None) -> bool:
    """True when the NL path should generate Cypher — always, unless overridden.

    Neo4j is the only graph backend (ONTA-527 / ONTA-534), so the NL target
    language is Cypher. ``explicit=False`` is retained only as a fail-closed
    gate: :meth:`infona_client.nlp.pipeline.NLQueryPipeline.ask` raises
    :class:`~infona_client.nlp.pipeline.SparqlAskPathRetired` instead of
    running the retired SPARQL generator. Nothing in the product passes it.
    """
    if explicit is not None:
        return bool(explicit)
    return True


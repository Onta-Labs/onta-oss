"""list_explode handlers: composite relationship + packed-literal splits."""

from __future__ import annotations

from typing import Any

from infona_client.graph.iri import ENTITY_URI_PREFIX, ONTO_PRED_PREFIX
from infona_client.graph.ontology_queries import attr_uri, type_uri
from infona_client.normalization.execute_helpers import (
    ATTRS_INFIX,
    RDF_TYPE,
    RDFS_LABEL,
    RDFS_RANGE,
    _atom_uri,
    _decode_local_name,
    _group_sparql_literals,
    _group_store_literals,
    _host,
    _sparql_str,
    _split,
    _target_type_from_type_uri,
    _target_type_from_uri,
    logger,
)


def _has_delimiter(source: str, delimiters: list[str]) -> bool:
    """True when ``source`` packs several atoms behind a rule delimiter.

    The SPARQL arm pushes this down as ``CONTAINS(?cname, "…")``; the GraphStore
    read deliberately does not know about delimiters, so the check runs HERE for
    both arms. Skipping it on the store arm would let an atomic target whose IRI
    merely differs from its canonical slug (``…/Language/eng-1`` labelled
    ``English``) get re-pointed — a rename the SPARQL arm never performed.
    """
    return any(d in source for d in delimiters)


async def _explode_relationship(
    neptune: Any,
    kg_graph: str,
    onto_graph: str,
    domain_type: str,
    pred_leaf: str,
    delimiters: list[str],
) -> tuple[dict, list[str]]:
    """Split composite relationship targets into canonical atomic entities.

    Returns ``(summary, orphan_uris)`` — the composite subjects the final sweep
    removed, so the caller's single refresh can evict them from derived indexes.
    """
    onto_pred = ONTO_PRED_PREFIX + pred_leaf
    attr_pred_suffix = ATTRS_INFIX + pred_leaf  # any …/attrs/<leaf> form

    # 1) Find every (subject, predicate-as-used, composite) edge whose object is
    #    a composite entity, identified by its name/label containing a delimiter.
    #    GraphStore (ONTA-534) reads the `<leaf>` relationships directly; the
    #    residual SPARQL arm matches BOTH the onto/<leaf> predicate (the normal
    #    relationship form) and any types/<T>/attrs/<leaf> predicate (a predicate
    #    first seen as an attribute then carrying an entity object).
    rows = await _host().rel_rows(kg_graph, pred_leaf)
    if rows is None:
        delim_filter = " || ".join(
            f'CONTAINS(?cname, "{_sparql_str(d)}")' for d in delimiters
        )
        q = (
            f"SELECT ?s ?p ?composite ?clabel FROM <{kg_graph}> WHERE {{\n"
            f"  ?s ?p ?composite .\n"
            f"  FILTER(?p = <{onto_pred}> || STRENDS(STR(?p), \"{_sparql_str(attr_pred_suffix)}\"))\n"
            f'  FILTER(STRSTARTS(STR(?composite), "{ENTITY_URI_PREFIX}"))\n'
            f"  OPTIONAL {{ ?composite <{RDFS_LABEL}> ?clabel }}\n"
            f'  BIND(COALESCE(?clabel, REPLACE(STR(?composite), "^.*/", "")) AS ?cname)\n'
            f"  FILTER({delim_filter})\n"
            f"}}"
        )
        _, raw = _host().parse_sparql_results(await neptune.query(q))
        edges = [
            (r.get("s", ""), r.get("p", ""), r.get("composite", ""), r.get("clabel", ""))
            for r in raw
        ]
    else:
        # Always re-point via onto/<leaf>: on the store path a relationship IS
        # the onto form, and it is the only predicate the NL planner traverses.
        edges = [(r.subject, onto_pred, r.target, r.target_name or "") for r in rows]

    edges_to_delete: list[tuple[str, str, str]] = []
    edges_to_add: list[tuple[str, str, str]] = []
    atomic_triples: list[tuple[str, str, str]] = []
    atomic_seen: set[str] = set()
    composites_touched: set[str] = set()

    for s, p, composite, clabel in edges:
        if not s or not p or not composite:
            continue
        target_type = _target_type_from_uri(composite)
        if not target_type:
            continue
        # Prefer the rdfs:label (the human value) for the split; fall back to the
        # URL-decoded local-name. The "__" slug split recovers atoms from names.
        source = clabel or _decode_local_name(composite)
        if not _has_delimiter(source, delimiters):
            continue
        atoms = _split(source, delimiters)
        if not atoms:
            # Nothing to split (empty/whitespace-only source) — nothing to do.
            continue
        # Skip ONLY when the target is already a clean atomic node: a single atom
        # whose CANONICAL IRI is the composite's own IRI. That is the genuine
        # idempotency case (re-running on `…/Language/English` is a no-op). A
        # single atom whose canonical IRI DIFFERS from the composite's IRI means
        # the target carries a junk delimiter (leading/trailing/doubled, e.g.
        # `…/Industry/__Agriculture` → atom "Agriculture" → `…/Industry/Agriculture`)
        # and MUST be re-pointed to the clean node — same as the multi-atom path —
        # so the malformed node becomes a sweepable orphan (COG-118). The equality
        # uses the SAME minting helper as the re-point below, so the check is exact.
        if len(atoms) == 1 and _atom_uri(target_type, atoms[0]) == composite:
            continue
        composites_touched.add(composite)
        # Re-point the edge to one CANONICAL atomic entity per atom; the canonical
        # IRI is slug-derived so the same atom (e.g. "Russian") from any composite
        # maps to the SAME node. Always re-point using the onto/<leaf> predicate
        # (the proper relationship form) regardless of the predicate as-used.
        for atom in atoms:
            atom_uri = _atom_uri(target_type, atom)
            edges_to_add.append((s, onto_pred, atom_uri))
            if atom_uri not in atomic_seen:
                atomic_seen.add(atom_uri)
                atomic_triples.append((atom_uri, RDF_TYPE, type_uri(target_type)))
                atomic_triples.append((atom_uri, RDFS_LABEL, atom))
                # Mirror ingest: also store the human value under attrs/name so the
                # Explorer Data table shows it (see explore.get_type_records).
                atomic_triples.append(
                    (atom_uri, type_uri(target_type) + "/attrs/name", atom)
                )
        edges_to_delete.append((s, p, composite))

    # 2) Apply: add atomic entity triples + new edges, then delete composite edges.
    # E7: GraphStore once per write batch when neo4j backend is active.
    store = _host().resolve_optional_graph_store()
    if atomic_triples:
        await _host().insert_facts(neptune, kg_graph, atomic_triples, store=store)
    if edges_to_add:
        await _host().insert_facts(neptune, kg_graph, edges_to_add, store=store)
    if edges_to_delete:
        # Concrete-triple removal via the shared primitive (ADR 0007); delete_facts
        # batches internally (no oversized statement). These are edge drops — the
        # subject survives — so they are NOT deleted_subjects.
        await _host().delete_facts(
            neptune,
            kg_graph,
            triples=edges_to_delete,
            reason="normalization:list_explode composite-edge drop",
            store=store,
        )

    # 3) Final orphan sweep. After ALL edges for this predicate are re-pointed,
    #    delete EVERY composite node of the relationship's target type(s) that has
    #    no inbound onto/<pred> (or attrs/<pred>) edge left — keyed on graph state,
    #    not on the composites we happened to touch this pass. That makes it both
    #    complete (one pass per type catches the ones a per-edge drop misses)
    #    and re-runnable (a second apply still sweeps leftover orphans from a
    #    buggy earlier run, even when nothing was rewritten this pass).
    #    The target type comes from the ONTOLOGY (the predicate's declared range),
    #    a cheap bounded lookup that works on a pure re-run regardless of whether
    #    any edge was rewritten this pass (COG-118).
    target_types = await _composite_target_types(
        neptune, onto_graph, domain_type, pred_leaf, composites_touched
    )
    orphan_uris = await _sweep_orphan_composites(
        neptune, kg_graph, pred_leaf, target_types, delimiters
    )

    summary = {
        "edges_rewritten": len(edges_to_delete),
        "atomic_created": len(atomic_seen),
        "orphans_dropped": len(orphan_uris),
    }
    logger.info("explode_relationship_done", predicate=pred_leaf, **summary)
    return summary, orphan_uris


async def _composite_target_types(
    neptune: Any,
    onto_graph: str,
    domain_type: str,
    pred_leaf: str,
    composites: set[str],
) -> set[str]:
    """The relationship's target type(s), for scoping the final orphan sweep.

    PRIMARY path (COG-118): resolve the type from the ONTOLOGY — the predicate's
    declared range. This is a bounded single-attribute lookup in the tenant
    catalog — cheap, reliable, and INDEPENDENT of whether any edge was rewritten
    this pass, so a pure re-run (``edges_rewritten == 0``) still resolves the
    type and sweeps lingering orphans to zero. It replaces the old unbounded
    full-graph ``SELECT DISTINCT ?t`` scan that timed out on live data and
    silently skipped the sweep (logged ``composite_target_type_query_failed``).

    FALLBACK: if the ontology declares no entity range for the predicate
    (un-upgraded attribute, or range missing), derive the type(s) from the
    composites we re-pointed this pass — their IRI carries ``…/entities/
    <TargetType>/…``. This keeps the first-pass split path working even before the
    range is upgraded. Scoping to a real target type means the sweep never touches
    unrelated types.
    """
    onto_types = await _range_target_types(neptune, onto_graph, domain_type, pred_leaf)
    if onto_types:
        return onto_types

    # No usable ontology range — derive from this pass's re-pointed composites.
    types: set[str] = set()
    for composite in composites:
        t = _target_type_from_uri(composite)
        if t:
            types.add(t)
    if not types:
        # Nothing rewritten this pass AND no ontology range: we cannot scope a
        # sweep. Surface it (not a silent skip) so a missing range is visible.
        logger.warning(
            "sweep_target_type_unresolved",
            domain_type=domain_type,
            predicate=pred_leaf,
            note="no ontology range and no composites re-pointed this pass",
        )
    return types


async def _range_target_types(
    neptune: Any, onto_graph: str, domain_type: str, pred_leaf: str
) -> set[str]:
    """Read the predicate's declared range from the ontology → target type(s).

    GraphStore (ONTA-534): the ontology CATALOG's declaration for
    ``(domain_type, pred_leaf)`` — the same row ``/ontology/*``, the Explorer's
    ontology browser and the rule inferencer read, so the sweep cannot disagree
    with the browser about what the predicate ranges over. Bounded: one type's
    attribute list, never a scan of the KG data graph.

    Residual SPARQL arm: the ``rdfs:range`` triple on the property URI. Returns
    the set of target type NAMES; XSD/primitive ranges are ignored (not entity
    targets). A failure is logged and treated as "no range" so the caller falls
    back rather than crashing.
    """
    catalog = await _host().catalog_range_types(onto_graph, domain_type, pred_leaf)
    if catalog is not None:
        return catalog

    prop_uri = attr_uri(domain_type, pred_leaf)
    q = (
        f"SELECT ?range FROM <{onto_graph}> WHERE {{\n"
        f"  <{prop_uri}> <{RDFS_RANGE}> ?range .\n"
        f"}}"
    )
    try:
        _, rows = _host().parse_sparql_results(await neptune.query(q))
    except Exception:
        logger.warning(
            "sweep_range_lookup_failed",
            domain_type=domain_type,
            predicate=pred_leaf,
            exc_info=True,
        )
        return set()
    types: set[str] = set()
    for r in rows:
        t = _target_type_from_type_uri(r.get("range", ""))
        if t:
            types.add(t)
    return types


async def _sweep_orphan_composites(
    neptune: Any,
    kg_graph: str,
    pred_leaf: str,
    target_types: set[str],
    delimiters: list[str],
) -> list[str]:
    """Final, graph-state-keyed sweep of orphaned composite nodes.

    For each target type, delete ALL triples of every entity that is (a) of that
    type, (b) composite-named (local-name or label contains a rule delimiter),
    and (c) has ZERO inbound ``<pred>`` edges. One read per type resolves the
    orphan set (complete — catches every orphan a per-edge drop would miss;
    re-runnable — a later apply still sweeps leftovers), then the removal routes
    through the shared ``delete_facts`` primitive (ADR 0007) so a swept subject is
    evicted from the derived secondary indexes too — no ghost rows keyed to a
    deleted subject. Atomic nodes (no delimiter) and still-referenced composites
    are left untouched.

    **Scope.** The store read runs on a session pinned to this graph's
    ``(tenant_id, kg)`` and the template binds that scope into every pattern, so
    the candidate set — and therefore the delete — can never reach another
    workspace's or another KG's nodes even if a type name collides.

    Returns the URIs of the orphan composite subjects removed (the summary count
    is ``len(...)``, and the caller feeds them to ``refresh_after_write`` as
    ``deleted_subjects``).
    """
    if not target_types:
        return []

    onto_pred = ONTO_PRED_PREFIX + pred_leaf
    attr_pred_suffix = ATTRS_INFIX + pred_leaf
    delim_filter = " || ".join(
        f'CONTAINS(?cname, "{_sparql_str(d)}")' for d in delimiters
    )
    dropped: list[str] = []
    for target_type in sorted(target_types):
        try:
            rows = await _host().orphan_rows(kg_graph, target_type, pred_leaf)
        except Exception:  # noqa: BLE001 — one type's failure costs THAT type only
            logger.warning(
                "orphan_select_failed", target_type=target_type, exc_info=True
            )
            continue
        if rows is None:
            orphan_uris = await _sweep_candidates_sparql(
                neptune,
                kg_graph,
                target_type,
                onto_pred,
                attr_pred_suffix,
                delim_filter,
            )
            if orphan_uris is None:
                continue
        else:
            # The store read is delimiter-agnostic: narrow to COMPOSITE-named
            # candidates here, the same CONTAINS the SPARQL arm pushes down.
            orphan_uris = [
                r.uri
                for r in rows
                if _has_delimiter(r.name or _decode_local_name(r.uri), delimiters)
            ]
        if not orphan_uris:
            continue
        try:
            await _host().delete_facts(
                neptune,
                kg_graph,
                subjects=orphan_uris,
                touched_types=[target_type],
                reason="normalization:list_explode orphan-composite sweep",
                store=_host().resolve_optional_graph_store(),
            )
        except Exception:
            logger.warning(
                "orphan_sweep_failed", target_type=target_type, exc_info=True
            )
            continue
        dropped.extend(orphan_uris)
    return dropped


async def _sweep_candidates_sparql(
    neptune: Any,
    kg_graph: str,
    target_type: str,
    onto_pred: str,
    attr_pred_suffix: str,
    delim_filter: str,
) -> list[str] | None:
    """Residual SPARQL arm of the orphan scan; ``None`` when the SELECT failed."""
    t_uri = type_uri(target_type)
    orphan_where = (
        f"  ?c <{RDF_TYPE}> <{t_uri}> .\n"
        f'  FILTER(STRSTARTS(STR(?c), "{ENTITY_URI_PREFIX}"))\n'
        f"  OPTIONAL {{ ?c <{RDFS_LABEL}> ?clabel }}\n"
        f'  BIND(COALESCE(?clabel, REPLACE(STR(?c), "^.*/", "")) AS ?cname)\n'
        f"  FILTER({delim_filter})\n"
        f"  FILTER NOT EXISTS {{ ?s <{onto_pred}> ?c }}\n"
        f"  FILTER NOT EXISTS {{ ?s2 ?p2 ?c . "
        f"FILTER(STRENDS(STR(?p2), \"{_sparql_str(attr_pred_suffix)}\")) }}\n"
    )
    select_q = f"SELECT DISTINCT ?c FROM <{kg_graph}> WHERE {{\n{orphan_where}}}"
    try:
        _, rows = _host().parse_sparql_results(await neptune.query(select_q))
    except Exception:
        logger.warning("orphan_select_failed", target_type=target_type, exc_info=True)
        return None
    return [r["c"] for r in rows if r.get("c")]


async def _explode_literal(
    neptune: Any, kg_graph: str, rule, delimiters: list[str]
) -> tuple[dict, list[str]]:
    """Split packed attribute literals into N atomic literals.

    Returns ``(summary, [])`` — literal splits replace an attribute value on a
    surviving subject, so nothing here is a deleted subject.

    **Whole-leaf rewrite, delete BEFORE insert** — same reason as
    ``strip_emoji``: a property-graph literal delete is predicate-scoped, so the
    subject's untouched sibling values must be re-inserted alongside the atoms.
    """
    pred_leaf = rule.predicate
    onto_pred = ONTO_PRED_PREFIX + pred_leaf
    attr_pred_suffix = ATTRS_INFIX + pred_leaf

    rows = await _host().literal_rows(kg_graph, pred_leaf)
    if rows is None:
        delim_filter = " || ".join(
            f'CONTAINS(STR(?o), "{_sparql_str(d)}")' for d in delimiters
        )
        q = (
            f"SELECT ?s ?p ?o FROM <{kg_graph}> WHERE {{\n"
            f"  ?s ?p ?o .\n"
            f"  FILTER(?p = <{onto_pred}> || STRENDS(STR(?p), \"{_sparql_str(attr_pred_suffix)}\"))\n"
            f"  FILTER(isLiteral(?o))\n"
            f"  FILTER({delim_filter})\n"
            f"}}"
        )
        _, raw = _host().parse_sparql_results(await neptune.query(q))
        groups = _group_sparql_literals(raw)
    else:
        groups = _group_store_literals(rows, rule, pred_leaf)

    to_delete: list[tuple[str, str, Any]] = []
    to_add: list[tuple[str, str, Any]] = []
    rewritten = 0
    atomic_count = 0
    for (s, p), values in groups.items():
        replacement: list[Any] = []
        changed = 0
        for value in values:
            if not isinstance(value, str):
                # A typed numeric/boolean literal has nothing to split; pass it
                # through in its native type rather than stringifying it.
                replacement.append(value)
                continue
            atoms = _split(value, delimiters)
            if len(atoms) <= 1:
                replacement.append(value)  # already atomic — idempotent no-op
                continue
            changed += 1
            atomic_count += len(atoms)
            replacement.extend(atoms)
        if not changed:
            continue
        rewritten += changed
        to_delete.extend((s, p, v) for v in values)
        to_add.extend((s, p, v) for v in replacement)

    # E7: GraphStore once per write batch when neo4j backend is active.
    store = _host().resolve_optional_graph_store()
    if to_delete:
        await _host().delete_facts(
            neptune,
            kg_graph,
            triples=to_delete,
            reason="normalization:list_explode packed-literal replace",
            store=store,
        )
    if to_add:
        await _host().insert_facts(neptune, kg_graph, to_add, store=store)

    summary = {
        "edges_rewritten": rewritten,
        "atomic_created": atomic_count,
        "orphans_dropped": 0,
    }
    logger.info("explode_literal_done", predicate=pred_leaf, **summary)
    return summary, []

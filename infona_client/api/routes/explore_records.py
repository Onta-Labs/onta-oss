"""Paged type-records Explorer endpoint (COG-100).

GraphStore-first. Residual SPARQL arm is hermetic-test only.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import Depends, Query

from infona_client.api.deps import get_neptune_client
from infona_client.api.routes.explore_common import (
    RDF_PROPERTY,
    RDF_TYPE,
    RDFS,
    _PRIMARY_TYPE_GUARD,
    _STAT_ENTITY_COUNT,
    _esc,
    _stats_graph_uri,
    _to_float,
    _to_int,
)
from infona_client.graph.iri import ENTITY_URI_PREFIX, ONTO_PRED_PREFIX
from infona_client.graph.predicates import (
    companion_leaves as _companion_leaves,
    is_internal_predicate as _is_internal_predicate,
)
from infona_client.api.routes.explore_resolve import _resolve_layered_type
from infona_client.auth.api_keys import TenantContext, get_tenant
from infona_client.graph.ontology_queries import type_uri
from infona_client.graph.parser import parse_sparql_results
from infona_client.graph.queries import (
    kg_graph_uri,
    require_valid_type_name,
    tenant_graph_uri,
)


async def _records_from_explore_store(
    *,
    tenant_id: str,
    kg_name: str,
    type_name: str,
    limit: int,
    cursor: str | None,
) -> dict | None:
    """Build the records response via GraphStore, or ``None`` for SPARQL.

    Dual-backend (E9): when ``INFONA_GRAPH_BACKEND=neo4j`` (or an injected
    process store under that backend), list + detail come from
    :mod:`infona_client.graph.explore_store`. Same response shape as the
    SPARQL path so Explorer clients need no branch.

    **ONTA-535:** public properties already exclude internal/housekeeping keys
    (via :func:`~infona_client.graph.explore_store._public_properties`).
    Ontology-declared attributes always become columns (COG-112), even when
    empty on the current page — joined from the tenant catalog, same as the
    SPARQL branch's ``attr_def`` query. Declared *relationships* are columns
    too (one leaf, target display name) so Explorer browse can render a filled
    edge instead of an empty dash. The ``has_*`` alias is never minted.
    """
    from infona_client.graph.explore_store import (
        get_entity_detail as pg_entity_detail,
        list_entities_by_type as pg_list_entities,
        resolve_explore_session,
    )
    from infona_client.graph.facts import is_internal_property_key

    if resolve_explore_session(tenant_id=tenant_id, kg_name=kg_name) is None:
        return None

    page = await pg_list_entities(
        tenant_id=tenant_id,
        kg_name=kg_name,
        type_name=type_name,
        limit=limit,
        after_id=cursor,
    )
    if page is None:
        return None

    # Ontology-declared attributes are always columns (COG-112 / ONTA-535).
    # Exempt from the observed-column budget so a declared-but-empty enriched
    # attr stays visible in the Explorer table.
    declared_display: list[str] = []
    declared_set: set[str] = set()
    declared_rel_display: list[str] = []
    declared_rel_set: set[str] = set()
    try:
        from infona_client.graph.ontology_catalog import list_attributes as cat_list_attrs

        for a in await cat_list_attrs(
            layer="tenant",
            tenant_id=tenant_id,
            type_name=type_name,
        ):
            label = (a.name or "").strip()
            if not label or label == "name":
                continue
            if is_internal_property_key(label):
                continue
            # Object properties are relationship columns, not literal ones.
            # Folding them into the literal list made Explorer pin the same
            # leaf twice (``works_at`` + ``works_at →``). They still belong
            # in the table — Browse looks up rec[rel.label] — just once.
            if getattr(a, "kind", None) == "relationship":
                if label not in declared_rel_set:
                    declared_rel_set.add(label)
                    declared_rel_display.append(label)
                continue
            if label not in declared_set:
                declared_set.add(label)
                declared_display.append(label)
        declared_display.sort()
        declared_rel_display.sort()
    except Exception:
        # Catalog is best-effort: page-observed columns still work without it.
        declared_display = []
        declared_set = set()
        declared_rel_display = []
        declared_rel_set = set()

    _EMPTY = {
        "columns": ["name"] + declared_display + declared_rel_display,
        "rows": [],
        "total": 0,
        "next_cursor": None,
    }
    if not page.entities:
        return {**_EMPTY, "total": page.total}

    # Column budget for observed-but-undeclared extras only (mirrors SPARQL path).
    _MAX_EXTRA_COLS = 24
    n_declared = len(declared_set) + len(declared_rel_set)

    # Collect per-entity properties for table columns (page-sized, ≤ 200).
    col_set: set[str] = set(declared_set) | set(declared_rel_set)
    col_display: list[str] = list(declared_display) + list(declared_rel_display)
    rows_out: list[dict] = []
    for ent in page.entities:
        detail = await pg_entity_detail(
            tenant_id=tenant_id,
            kg_name=kg_name,
            entity_id=ent.id,
        )
        props = dict(detail.properties) if detail is not None else {}
        # Prefer detail.name over summary name (same precedence as SPARQL path:
        # human-readable name attribute / node name beats URI slug).
        name = (
            (detail.name if detail is not None else None)
            or ent.name
            or ent.id.rstrip("/").split("/")[-1]
        )
        row: dict = {"id": ent.id, "name": name}
        for k, v in props.items():
            if k == "name":
                continue
            display = str(k)
            # Defence in depth: entity detail already strips internals, but a
            # drifted store path must not re-introduce them as columns.
            if is_internal_property_key(display):
                continue
            if display not in col_set:
                # Declared attrs/rels are unlimited; extras cap at _MAX_EXTRA_COLS.
                if display not in declared_set and display not in declared_rel_set:
                    extras = len(col_set) - n_declared
                    if extras >= _MAX_EXTRA_COLS:
                        continue
                col_set.add(display)
                col_display.append(display)
            if isinstance(v, (list, tuple)):
                row[display] = ", ".join(str(x) for x in v)
            else:
                row[display] = "" if v is None else str(v)
        # Overlay relationship *target display names* onto the relationship
        # column. One column per leaf (declared object property or observed
        # edge). Never mint a ``has_*`` strip alias — that was the duplicate
        # #470 closed. Omitting the column entirely made Explorer render a
        # filled edge as empty dashes (Browse looks up rec[rel.label]).
        if detail is not None:
            for rel in getattr(detail, "outgoing", ()) or ():
                leaf = str(getattr(rel, "attr", None) or getattr(rel, "rel_type", "") or "")
                if not leaf:
                    continue
                if is_internal_property_key(leaf):
                    continue
                target = (
                    getattr(rel, "other_name", None)
                    or (getattr(rel, "other_id", "") or "").rstrip("/").split("/")[-1]
                    or ""
                )
                if not target:
                    continue
                if leaf not in col_set:
                    if leaf not in declared_set and leaf not in declared_rel_set:
                        extras = len(col_set) - n_declared
                        if extras >= _MAX_EXTRA_COLS:
                            continue
                    col_set.add(leaf)
                    col_display.append(leaf)
                prev = str(row.get(leaf) or "")
                if not prev or "___" in prev:
                    row[leaf] = target
                elif target not in prev.split(", "):
                    row[leaf] = f"{prev}, {target}"
        # Fill blanks for columns already discovered on earlier rows.
        for c in col_display:
            row.setdefault(c, "")
        rows_out.append(row)

    # Ensure every row has every column (later columns may appear mid-page).
    for row in rows_out:
        for c in col_display:
            row.setdefault(c, "")

    # Declared first (stable alpha), then observed extras (alpha) — matches
    # the SPARQL branch's "declared always shown" contract.
    extras = sorted(
        c for c in col_display if c not in declared_set and c not in declared_rel_set
    )
    columns = ["name"] + list(declared_display) + list(declared_rel_display) + extras
    return {
        "columns": columns,
        "rows": rows_out,
        "total": page.total,
        "next_cursor": page.next_cursor,
    }


async def get_type_records(
    kg_name: str,
    type_name: str,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
    tenant: TenantContext = Depends(get_tenant),
    client: Any = Depends(get_neptune_client),
):
    """Paged entity instances for the Explorer Data table (COG-100).

    Returns one page of instances of ``type_name``, ordered deterministically
    by entity URI (``ORDER BY ?e``) with keyset pagination via ``cursor`` (the
    last entity URI from the previous page).  For each entity the endpoint
    fetches all attribute values, excluding ``rdf:type`` and
    ``SYSTEM_PREDICATES``.  Attribute predicates are resolved to display names
    via the ontology (same ``attr_def`` query shape as ``get_type_summary``).
    The row ``name`` is the declared ``attrs/name`` attribute value when present
    (ingest stores the human-readable name there; ``rdfs:label`` holds the
    opaque entity-id slug), else ``rdfs:label``, else the entity-URI leaf.

    Response shape::

        {
            "columns": ["name", "<attr1>", ...],
            "rows": [{"id": "<uri>", "name": "...", "<attr1>": "...", ...}],
            "total": <int>,
            "next_cursor": "<uri>" | null,
        }

    Never errors on an empty/missing type; returns the empty sentinel instead.
    A type name that could not exist at all — one carrying a character no IRI may
    contain — is a different thing from a type with no rows, and is a 422
    (ONTA-425). The sentinel keeps covering every name that is merely absent.

    **Dual-backend (E9):** when ``INFONA_GRAPH_BACKEND=neo4j``, reads via
    :mod:`infona_client.graph.explore_store`. Default Neptune path unchanged.
    """
    require_valid_type_name(type_name)
    _EMPTY = {"columns": ["name"], "rows": [], "total": 0, "next_cursor": None}

    from fastapi import HTTPException

    from infona_client.graph.store import GraphConfigError

    try:
        pg = await _records_from_explore_store(
            tenant_id=tenant.tenant_id,
            kg_name=kg_name,
            type_name=type_name,
            limit=limit,
            cursor=cursor,
        )
    except GraphConfigError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "Graph store is not configured. Neo4j GraphStore is required "
                f"(ONTA-534). {exc}"
            ),
        ) from exc
    if pg is not None:
        return pg

    # ONTA-534: do not fall through to SPARQL when the store path returned None
    # under a configured GraphStore (unknown/empty type uses empty sentinel).
    # Residual SPARQL arm below remains for dual-arm archaeology only when
    # explicitly reached; production GraphStore always answers above.
    kg_graph = kg_graph_uri(tenant.tenant_id, kg_name)
    resolved = await _resolve_layered_type(client, tenant, type_name)
    if resolved is not None:
        t_uri, onto_graph, _layer = resolved
    else:
        onto_graph = tenant_graph_uri(tenant.tenant_id)
        t_uri = type_uri(type_name)

    # --- (1) attribute display-name map from ontology (same as get_type_summary) ---
    attr_def_sparql = (
        f"SELECT ?attr ?attrLabel ?range FROM <{onto_graph}> WHERE {{\n"
        f"  ?attr <{RDF_TYPE}> <{RDF_PROPERTY}> .\n"
        f"  ?attr <{RDFS}#domain> <{t_uri}> .\n"
        f"  ?attr <{RDFS}#label> ?attrLabel .\n"
        f"  OPTIONAL {{ ?attr <{RDFS}#range> ?range }}\n"
        f"}}"
    )

    # --- (2) entity page: keyset pagination ordered by ?e URI ---
    cursor_filter = f'  FILTER(STR(?e) > "{_esc(cursor)}")\n' if cursor else ""
    entities_sparql = (
        f"SELECT DISTINCT ?e FROM <{kg_graph}> WHERE {{\n"
        f"  ?e <{RDF_TYPE}> <{t_uri}> .\n"
        f"{_PRIMARY_TYPE_GUARD}"
        f"{cursor_filter}"
        f"}} ORDER BY ?e LIMIT {limit}"
    )

    # --- (3) total count: try stats graph first, fall back to COUNT query ---
    stats_graph = _stats_graph_uri(tenant.tenant_id, kg_name)
    total_sparql = (
        f"SELECT ?ec FROM <{stats_graph}> WHERE {{\n"
        f"  <{t_uri}> <{_STAT_ENTITY_COUNT}> ?ec\n"
        f"}}"
    )

    attr_def_raw, entity_raw, total_raw = await asyncio.gather(
        client.query(attr_def_sparql),
        client.query(entities_sparql),
        client.query(total_sparql),
    )

    _, attr_def_rows = parse_sparql_results(attr_def_raw)
    # Column budget.  Ontology-DECLARED attributes are always shown (they are the
    # type's schema — including enriched attrs like ``company`` that may sit on
    # only a handful of entities), so they are exempt from this cap.  The cap
    # only bounds the *extra* non-declared predicates discovered on the page, so
    # one rogue entity with dozens of ad-hoc predicates can't blow up the table.
    # Raised from 12 → 24 so a wide-but-legitimate declared schema isn't crowded
    # out and there's still headroom for a few observed-but-undeclared columns.
    _MAX_COLS = 24
    # Map ONTO pred URI → label.  We also need the instance predicate URI which
    # is `…/onto/<predLeaf>`.  Build both directions.  ``declared_display`` is the
    # ordered list of declared-attribute display labels that ALWAYS become
    # columns (deduped, alphabetical for a stable order — coverage isn't carried
    # by the attr-def query, so we don't pay an extra round-trip to rank by it).
    attr_label_by_onto: dict[str, str] = {}  # onto attr URI → label
    attr_label_by_pred: dict[str, str] = {}  # onto pred URI → label (instance triples)
    declared_display: list[str] = []
    declared_display_set: set[str] = set()
    for r in attr_def_rows:
        a_uri = r.get("attr", "")
        label = r.get("attrLabel") or a_uri.rstrip("/").split("/")[-1]
        if not a_uri:
            continue
        attr_label_by_onto[a_uri] = label
        # instance predicate URI: …/onto/<leaf>  where leaf is the last segment of
        # the attr URI (attrs/<leaf> → <leaf>)
        pred_leaf = a_uri.rstrip("/").split("/")[-1]
        inst_pred = ONTO_PRED_PREFIX + pred_leaf
        attr_label_by_pred[inst_pred] = label
        # ``name`` is rendered from rdfs:label as the first column; never let a
        # declared attribute literally named "name" duplicate it.
        if label != "name" and label not in declared_display_set:
            declared_display_set.add(label)
            declared_display.append(label)
    declared_display.sort()
    # LEGACY companion classification (ONTA-262): enrichment used to DECLARE the
    # per-attribute provenance companions (`<attr>_source_url` / `_provenance` /
    # `_verified_at`) as first-class schema, so on un-migrated KGs they'd become
    # always-shown declared columns. Classify them set-wise (`<base>_<suffix>`
    # with `<base>` present among declared labels + "name") and keep them out of
    # the table — they are metadata of the base attribute, not columns.
    legacy_companion_labels = _companion_leaves([*declared_display, "name"])
    if legacy_companion_labels:
        declared_display = [
            c for c in declared_display if c not in legacy_companion_labels
        ]
        declared_display_set -= legacy_companion_labels

    _, entity_rows = parse_sparql_results(entity_raw)
    entity_uris = [r.get("e", "") for r in entity_rows if r.get("e")]
    if not entity_uris:
        # No instances on this page — still need a total
        _, total_rows = parse_sparql_results(total_raw)
        total = _to_int(total_rows[0].get("ec") if total_rows else None)
        if not total:
            # Fall back to a COUNT query if stats absent
            count_sparql = (
                f"SELECT (COUNT(DISTINCT ?e) AS ?n) FROM <{kg_graph}> WHERE {{\n"
                f"  ?e <{RDF_TYPE}> <{t_uri}> .\n"
                f"{_PRIMARY_TYPE_GUARD}"
                f"}}"
            )
            _, cnt_rows = parse_sparql_results(await client.query(count_sparql))
            total = _to_int(cnt_rows[0].get("n") if cnt_rows else None)
        return {**_EMPTY, "total": total}

    # --- (4) fetch attribute values for the page entities ---
    uri_values = " ".join(f"<{u}>" for u in entity_uris)
    values_sparql = (
        f"SELECT ?e ?p ?o FROM <{kg_graph}> WHERE {{\n"
        f"  VALUES ?e {{ {uri_values} }}\n"
        f"  ?e ?p ?o .\n"
        f'  FILTER(?p != <{RDF_TYPE}>)\n'
        f"}}"
    )

    # Total count and attribute values fetched concurrently
    values_raw, total_raw2 = await asyncio.gather(
        client.query(values_sparql),
        client.query(total_sparql),
    )

    _, values_rows = parse_sparql_results(values_raw)

    # Determine total
    _, total_rows2 = parse_sparql_results(total_raw2)
    total = _to_int(total_rows2[0].get("ec") if total_rows2 else None)
    if not total:
        count_sparql = (
            f"SELECT (COUNT(DISTINCT ?e) AS ?n) FROM <{kg_graph}> WHERE {{\n"
            f"  ?e <{RDF_TYPE}> <{t_uri}> .\n"
            f"{_PRIMARY_TYPE_GUARD}"
            f"}}"
        )
        _, cnt_rows = parse_sparql_results(await client.query(count_sparql))
        total = _to_int(cnt_rows[0].get("n") if cnt_rows else None)

    # --- (5) assemble rows ---
    # Collect per-entity: label + attribute values keyed by display name.
    # ``_name_attr`` captures the instance value of the declared "name" attribute
    # (``…/onto/name`` ← ``attrs/name``): these entities carry their real,
    # human-readable name THERE. ``rdfs:label`` holds the opaque entity-id slug
    # (ingest writes ``(entity_uri, rdfs:label, entity.id)``), so attrs/name is
    # the PREFERRED name source — rdfs:label is only the fallback below it. We
    # don't render attrs/name as a SEPARATE column (it would duplicate the first
    # "name" column); its value feeds the first column instead.
    LABEL_PRED = f"{RDFS}#label"
    entity_data: dict[str, dict] = {
        u: {"_label": None, "_name_attr": None, "_attrs": {}} for u in entity_uris
    }
    # Column order: declared attributes ALWAYS first (schema columns, not subject
    # to the frequency cap), then any extra non-declared predicates observed on
    # the page — bounded by _MAX_COLS so a stray entity can't inflate the table.
    col_display: list[str] = list(declared_display)
    col_set: set[str] = set(declared_display)
    extra_count = 0

    def _display_of(p_uri: str) -> str:
        # Resolve display name: check attr_label_by_pred (instance pred) first,
        # then attr_label_by_onto (onto attr URI), then fall back to the URI leaf.
        return (
            attr_label_by_pred.get(p_uri)
            or attr_label_by_onto.get(p_uri)
            or p_uri.rstrip("/").split("/")[-1]
        )

    # LEGACY companion classification for OBSERVED (non-declared) predicates on
    # this page (ONTA-262): discovery used to stamp companions as ordinary
    # attribute-namespace instance triples, so an un-migrated KG surfaces them
    # here as extra columns. Classify set-wise over every literal-valued display
    # name observed on the page plus the declared labels (a companion's base may
    # be declared while the companion is only observed, or vice versa).
    observed_literal_displays = {
        _display_of(r.get("p", ""))
        for r in values_rows
        if r.get("p", "") not in (LABEL_PRED, RDF_TYPE)
        and not r.get("o", "").startswith(ENTITY_URI_PREFIX)
    }
    observed_companions = _companion_leaves(
        observed_literal_displays | declared_display_set | {"name"}
    )

    for r in values_rows:
        e_uri = r.get("e", "")
        p_uri = r.get("p", "")
        o_val = r.get("o", "")
        if not e_uri or e_uri not in entity_data:
            continue
        if p_uri == LABEL_PRED:
            entity_data[e_uri]["_label"] = o_val
            continue
        # Internal/housekeeping predicates (onto/batch_id, er/blockKey,
        # er/erSignal_*, rdf*/rdfs*) must never become data-table columns — same
        # filter the summary panel uses. rdfs:label is intercepted above (it is
        # the row name); the real attrs/name predicate (…/onto/name) is NOT
        # internal and still flows through to the name-precedence logic below.
        # An entity-valued object marks a relationship, exempt from the
        # literal-only housekeeping markers (FIX 2) so a real `onto/source` edge
        # to an entity isn't hidden from the table.
        is_rel = o_val.startswith(ENTITY_URI_PREFIX)
        if _is_internal_predicate(p_uri, is_relationship=is_rel):
            continue
        display = _display_of(p_uri)
        # Legacy per-attribute provenance companions are metadata, not columns
        # (literal-valued only — a relationship can never be misclassified).
        if not is_rel and display in observed_companions:
            continue
        # "name" is rendered in the first column; a declared/instance predicate
        # named "name" (e.g. …/onto/name ← attrs/name) must not become a SEPARATE
        # column. But its value is the entity's real, human-readable name —
        # capture it so the first column can PREFER it over the slug-shaped
        # rdfs:label.
        if display == "name":
            if entity_data[e_uri]["_name_attr"] is None:
                entity_data[e_uri]["_name_attr"] = o_val
            continue
        if display not in col_set and extra_count < _MAX_COLS:
            col_set.add(display)
            col_display.append(display)
            extra_count += 1
        entity_data[e_uri]["_attrs"][display] = o_val

    columns = ["name"] + col_display
    rows = []
    for u in entity_uris:
        d = entity_data[u]
        # Name precedence: the declared "name" attribute's value (attrs/name)
        # FIRST, else rdfs:label, else the URI slug. Ingest writes
        # `(entity_uri, rdfs:label, entity.id)` — i.e. rdfs:label IS the opaque
        # entity-id slug — while the human-readable name lives in attrs/name. So
        # attrs/name must win over rdfs:label, otherwise the row degrades to the
        # slug (e.g. "4akvVWgTcS") even when a real name is present.
        label = d["_name_attr"] or d["_label"] or u.rstrip("/").split("/")[-1]
        row: dict = {"id": u, "name": label}
        for col in col_display:
            # Declared columns with no value on this entity render blank.
            row[col] = d["_attrs"].get(col, "")
        rows.append(row)

    next_cursor = entity_uris[-1] if len(entity_uris) == limit else None

    return {
        "columns": columns,
        "rows": rows,
        "total": total,
        "next_cursor": next_cursor,
    }



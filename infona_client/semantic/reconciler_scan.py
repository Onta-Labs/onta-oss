"""KG scan (GraphStore first, SPARQL fallback) for the semantic reconciler.

Patchable ``_ASSERTION_HISTORY_HARD_CAP`` and ``_MAX_SCAN_PAGES`` are read
via ``_host()``.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from infona_client.semantic.extract import _local_name
from infona_client.semantic.reconciler_common import _host
from infona_client.semantic.reconciler_const import Triple, _RDF_TYPE
from infona_client.semantic.reconciler_env import _scan_page_size
from infona_client.semantic.reconciler_keys import _assertion_row_to_semantic_triples


def _sparql_string_literal(value: str) -> str:
    """Escape a Python string for embedding in a double-quoted SPARQL literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _scan_query(
    kg_graph: str,
    predicates: Sequence[str],
    limit: int,
    after_entity: Optional[str] = None,
) -> str:
    """One keyset-paginated scan page: entities strictly after
    ``after_entity`` (the last COMPLETELY-scanned entity of the previous page),
    in stable ``?e ?p ?o`` order. Keyset instead of OFFSET so page N costs the
    same as page 1 — OFFSET makes the store re-walk (and re-sort) every
    already-scanned row, an O(pages²) total scan."""
    values = " ".join(f"<{p}>" for p in predicates)
    entity_filter = (
        f'  FILTER(STR(?e) > "{_sparql_string_literal(after_entity)}")\n'
        if after_entity
        else ""
    )
    return (
        f"SELECT ?e ?p ?o FROM <{kg_graph}> WHERE {{\n"
        f"  VALUES ?p {{ {values} }}\n"
        f"  ?e ?p ?o .\n"
        f"{entity_filter}"
        f"}} ORDER BY ?e ?p ?o LIMIT {limit}"
    )


def _store_property_ids_for_scan(predicates: Sequence[str]) -> list[str]:
    """Map RDF/attrs scan predicates onto GraphStore ``property_id`` IRIs.

    Assertion SoT uses ``https://graph.infona.ai/properties/<leaf>`` (and the
    well-known type-membership property for ``rdf:type``). The reconciler's
    scan predicate set is attrs/RDF-shaped; history is property-scoped.
    """
    from infona_client.graph.assertion_model import (
        property_uri,
        type_membership_property_id,
    )

    prop_ids: set[str] = set()
    type_prop = type_membership_property_id()
    for p in predicates:
        if not isinstance(p, str) or not p:
            continue
        if p == _RDF_TYPE or p == type_prop or p.endswith("/properties/rdf_type"):
            prop_ids.add(type_prop)
            continue
        if "/properties/" in p:
            prop_ids.add(p)
            continue
        leaf = _local_name(p, lower=False)
        if not leaf:
            continue
        try:
            prop_ids.add(property_uri(leaf))
        except Exception:  # noqa: BLE001 — skip unmappable foreign preds
            continue
    return sorted(prop_ids)


async def _scan_triples_store(
    tenant_id: str, kg_name: str, predicates: Sequence[str]
) -> tuple[list[Triple], bool] | None:
    """GraphStore scan of Assertions for the requested predicates.

    Returns ``None`` when no GraphStore / history seam is available.

    **Hard-cap fail-closed (ONTA-533 BLOCKER):** Memory and Neo4j clamp
    ``read_assertion_history(limit=…)`` at
    :data:`_ASSERTION_HISTORY_HARD_CAP` (10000). A naïve
    ``truncated = len(rows) > (page_size * max_pages)`` is always False because
    the store never returns more than the hard cap — and ghost-delete would
    then wipe healthy docs past the silent cutoff. A full hard-cap page is
    therefore treated as ``truncated=True``.

    **Predicate-scoped pages:** each scan property is fetched with its own
    hard-cap budget (not one global 10k window that may never include free-text
    rows). Full entity-keyset pagination is not available on the history API
    (``since`` is verified_at-only); incomplete pages fail closed.
    """
    from infona_client.graph.scope import GraphScope
    from infona_client.graph.store import GraphConfigError

    h = _host()
    try:
        from infona_client.graph.store import get_optional_graph_store

        store = get_optional_graph_store()
    except GraphConfigError:
        return None
    if store is None:
        return None
    session = store.session(GraphScope.for_instance(tenant_id, kg_name))
    history = getattr(session, "read_assertion_history", None)
    if not callable(history):
        return None

    prop_ids = _store_property_ids_for_scan(predicates)
    if not prop_ids:
        return [], False

    hard_cap = max(1, int(h._ASSERTION_HISTORY_HARD_CAP))
    triples: list[Triple] = []
    truncated = False
    hit_cap_props: list[str] = []
    for prop_id in prop_ids:
        # Request exactly the hard cap: the store clamps higher values to the
        # same ceiling, so over-fetch (limit+1) cannot observe "more than cap".
        rows = await history(prop_id=prop_id, limit=hard_cap)
        if len(rows) >= hard_cap:
            # Full hard-cap page ⇒ store may hold more for this property.
            # Fail closed: partial expected set must not drive ghost deletes.
            truncated = True
            hit_cap_props.append(prop_id)
        for row in rows:
            if not isinstance(row, dict):
                continue
            triples.extend(_assertion_row_to_semantic_triples(row))

    if truncated:
        h.logger.warning(
            "semantic_scan_truncated",
            tenant_id=tenant_id,
            kg_name=kg_name,
            path="graph_store",
            hard_cap=hard_cap,
            properties_at_cap=hit_cap_props,
            reason=(
                "read_assertion_history hit the store hard cap for one or more "
                "scan properties; expected set is partial so ghost deletion "
                "must be skipped"
            ),
        )
    triples.sort()
    return triples, truncated


async def _scan_triples_sparql(
    neptune: Any, kg_graph: str, predicates: Sequence[str]
) -> tuple[list[Triple], bool]:
    """SPARQL keyset-paginated scan (hermetic FakeNeptune / legacy)."""
    from infona_client.graph.parser import parse_sparql_results

    h = _host()
    page = _scan_page_size()
    triples: list[Triple] = []
    after: Optional[str] = None  # last completely-scanned entity
    max_pages = h._MAX_SCAN_PAGES
    for _page_ix in range(max_pages):
        sparql = _scan_query(kg_graph, predicates, page, after_entity=after)
        _, rows = parse_sparql_results(await neptune.query(sparql))
        page_triples: list[Triple] = []
        for row in rows:
            e, p = row.get("e", ""), row.get("p", "")
            if e and p:
                page_triples.append((e, p, row.get("o", "")))
        if len(rows) < page:
            triples.extend(page_triples)
            return triples, False
        if not page_triples:
            continue
        last_entity = page_triples[-1][0]
        complete = [t for t in page_triples if t[0] != last_entity]
        if complete:
            triples.extend(complete)
            after = complete[-1][0]
        else:
            h.logger.warning(
                "semantic_scan_entity_exceeds_page",
                kg_graph=kg_graph,
                entity_uri=last_entity,
                page_size=page,
            )
            triples.extend(page_triples)
            after = last_entity
    h.logger.warning(
        "semantic_scan_truncated",
        kg_graph=kg_graph,
        pages=max_pages,
        page_size=page,
    )
    return triples, True


async def _scan_triples(
    neptune: Any, kg_graph: str, predicates: Sequence[str]
) -> tuple[list[Triple], bool]:
    """Scan ``?e ?p ?o`` for the given predicates.

    **GraphStore first (ONTA-533):** when a process store is configured, the
    Assertion SoT is the product path — vestigial SPARQL clients must not win
    over Neo4j. Predicate-scoped history with hard-cap truncation detection
    (see :func:`_scan_triples_store`).

    **SPARQL fallback:** hermetic FakeNeptune tests (and any environment with
    no GraphStore) use keyset pagination by entity, bounded by the page cap.
    Each full page holds back its trailing entity group so every entity's
    rows arrive CONTIGUOUS AND COMPLETE for ``extract_semantic_chunks``.

    Returns ``(triples, truncated)``. ``truncated=True`` means the scan is
    PARTIAL — the caller must NOT ghost-delete against it (a partial expected
    set would mass-delete perfectly healthy docs).
    """
    from infona_client.graph.queries import parse_kg_graph_uri

    # Prefer GraphStore whenever a process store is available (production
    # Neo4j). An empty complete store scan falls through to SPARQL so
    # FakeNeptune hermetic tests that seed only SPARQL keep working under
    # conftest's empty MemoryGraphStore.
    scope = parse_kg_graph_uri(kg_graph)
    if scope is not None:
        tenant_id, kg_name = scope
        try:
            store_result = await _scan_triples_store(
                tenant_id, kg_name, predicates
            )
        except Exception:  # noqa: BLE001 — fall through to SPARQL
            store_result = None
        if store_result is not None:
            store_triples, store_truncated = store_result
            if store_triples or store_truncated:
                return store_result
            # Empty complete store scan: try SPARQL for hermetic FakeNeptune.
            # Production empty KGs get empty SPARQL too (or an exception).

    if neptune is not None and hasattr(neptune, "query"):
        try:
            return await _scan_triples_sparql(neptune, kg_graph, predicates)
        except Exception:
            pass

    # Store was empty/unavailable and SPARQL failed or was absent.
    if scope is not None:
        try:
            store_result = await _scan_triples_store(
                scope[0], scope[1], predicates
            )
            if store_result is not None:
                return store_result
        except Exception:  # noqa: BLE001
            pass
    return [], False

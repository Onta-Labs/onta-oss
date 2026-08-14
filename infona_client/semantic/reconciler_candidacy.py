"""Marker fetch + default text-kind candidacy for the semantic reconciler.

Schema writes go through ``commit_ontology`` (SET_TEXT_KIND). Patchable
``_MAX_CANDIDACY_ATTRS_PER_RUN`` and ``random.shuffle`` are read via ``_host()``.
"""

from __future__ import annotations

from typing import Any

from infona_client.semantic.extract import _local_name
from infona_client.semantic.reconciler_common import _host
from infona_client.semantic.reconciler_const import (
    TEXT_KIND_NOT_TEXT,
    _ATTR_URI_RE,
    _CANDIDACY_SAMPLE_SIZE,
)


async def _fetch_marker_map(neptune: Any, tenant_id: str) -> dict[str, bool]:
    """Uncached ``{attr URI -> is_free_text}`` fetch that RAISES on failure.

    Deliberately NOT :func:`~infona_client.graph.text_markers.get_free_text_map`:
    that request-path helper is best-effort (returns ``{}`` on a hiccup), which
    is right for query routing but catastrophic here — an empty map is
    indistinguishable from "no markers", and reconciling against it would
    ghost-delete the whole KG's index and let the heuristic overwrite
    REASON-layer verdicts. A correctness worker must abort (the runner retries
    on the next cadence) rather than act on a maybe-empty map.

    **Primary source (ONTA-533):** GraphStore catalog ``:OntoAttr.text_kind``.
    SPARQL remains a secondary source so FakeNeptune-seeded markers in hermetic
    tests still surface (catalog wins on conflict — production writes go there).
    """
    from infona_client.graph.ontology_queries import (
        TEXT_KIND_FREE_TEXT,
        text_kind_map_query,
    )
    from infona_client.graph.parser import parse_sparql_results
    from infona_client.graph.queries import tenant_graph_uri
    from infona_client.graph.text_markers import marker_map_from_catalog

    marker_map: dict[str, bool] = {}
    catalog_err: Exception | None = None
    try:
        catalog = await marker_map_from_catalog(tenant_id)
        if catalog is not None:
            marker_map.update(catalog)
    except Exception as exc:  # noqa: BLE001 — try SPARQL before aborting
        catalog_err = exc

    sparql_err: Exception | None = None
    if neptune is not None and hasattr(neptune, "query"):
        try:
            raw = await neptune.query(text_kind_map_query(tenant_graph_uri(tenant_id)))
            _, bindings = parse_sparql_results(raw)
            for row in bindings:
                attr = row.get("attr")
                if not attr:
                    continue
                # Catalog wins: only fill gaps from SPARQL.
                marker_map.setdefault(attr, row.get("kind") == TEXT_KIND_FREE_TEXT)
        except Exception as exc:  # noqa: BLE001
            sparql_err = exc

    if not marker_map and catalog_err is not None and sparql_err is not None:
        raise catalog_err
    if not marker_map and catalog_err is not None and neptune is None:
        raise catalog_err
    if not marker_map and sparql_err is not None and catalog_err is None:
        # Catalog returned empty (or unavailable) and SPARQL failed — raise so
        # we don't reconcile against a maybe-stale empty map.
        # When catalog is available and empty, empty is a legitimate answer.
        from infona_client.graph.store import get_optional_graph_store

        if get_optional_graph_store() is None:
            raise sparql_err
    return marker_map


async def _catalog_domain_for_attr(tenant_id: str, attr_leaf: str) -> str | None:
    """Best-effort type domain for an attribute leaf from the catalog."""
    try:
        from infona_client.graph.ontology_catalog import list_attributes
        from infona_client.graph.store import get_optional_graph_store

        store = get_optional_graph_store()
        if store is None:
            return None
        attrs = await list_attributes(tenant_id=tenant_id, store=store)
        for a in attrs:
            if a.name == attr_leaf:
                return a.domain
    except Exception:  # noqa: BLE001
        return None
    return None


async def _distinct_literal_predicates_store(
    tenant_id: str, kg_name: str
) -> list[str] | None:
    """GraphStore path: every property_id carrying a literal Assertion."""
    from infona_client.graph.scope import GraphScope
    from infona_client.graph.store import GraphConfigError, get_optional_graph_store

    store = get_optional_graph_store()
    if store is None:
        try:
            from infona_client.graph.store import get_graph_store

            store = get_graph_store()
        except GraphConfigError:
            return None
    session = store.session(GraphScope.for_instance(tenant_id, kg_name))
    history = getattr(session, "read_assertion_history", None)
    if not callable(history):
        return None
    # Cap is large: this is a DISTINCT over property_ids, not a full scan.
    rows = await history(limit=100_000)
    preds: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("literal_value") is None:
            continue
        p = row.get("property_id")
        if p:
            preds.add(str(p))
    return sorted(preds)


async def _distinct_literal_predicates(neptune: Any, kg_graph: str) -> list[str]:
    """Every predicate carrying at least one literal object in the KG graph.

    Prefers SPARQL when the client can answer (hermetic FakeNeptune tests);
    falls back to GraphStore Assertion history for the Neo4j product path
    (ONTA-533 — the vestigial SPARQL client cannot SPARQL).
    """
    from infona_client.graph.parser import parse_sparql_results
    from infona_client.graph.queries import parse_kg_graph_uri

    if neptune is not None and hasattr(neptune, "query"):
        try:
            sparql = (
                f"SELECT DISTINCT ?p FROM <{kg_graph}> WHERE {{\n"
                f"  ?e ?p ?o .\n"
                f"  FILTER(isLiteral(?o))\n"
                f"}}"
            )
            _, bindings = parse_sparql_results(await neptune.query(sparql))
            return [row["p"] for row in bindings if row.get("p")]
        except Exception:
            pass  # fall through to store
    scope = parse_kg_graph_uri(kg_graph)
    if scope is None:
        return []
    tenant_id, kg_name = scope
    store_preds = await _distinct_literal_predicates_store(tenant_id, kg_name)
    return store_preds if store_preds is not None else []


async def _sample_literal_values(
    neptune: Any, kg_graph: str, predicate: str, *, limit: int
) -> list[str]:
    """Sample literal values for one predicate (SPARQL or GraphStore)."""
    from infona_client.graph.parser import parse_sparql_results
    from infona_client.graph.queries import parse_kg_graph_uri

    if neptune is not None and hasattr(neptune, "query"):
        try:
            sample_sparql = (
                f"SELECT ?o FROM <{kg_graph}> WHERE {{\n"
                f"  ?e <{predicate}> ?o .\n"
                f"  FILTER(isLiteral(?o))\n"
                f"}} LIMIT {limit}"
            )
            _, rows = parse_sparql_results(await neptune.query(sample_sparql))
            return [row.get("o", "") for row in rows]
        except Exception:
            pass
    scope = parse_kg_graph_uri(kg_graph)
    if scope is None:
        return []
    tenant_id, kg_name = scope
    from infona_client.graph.scope import GraphScope
    from infona_client.graph.store import GraphConfigError, get_optional_graph_store

    store = get_optional_graph_store()
    if store is None:
        try:
            from infona_client.graph.store import get_graph_store

            store = get_graph_store()
        except GraphConfigError:
            return []
    session = store.session(GraphScope.for_instance(tenant_id, kg_name))
    history = getattr(session, "read_assertion_history", None)
    if not callable(history):
        return []
    rows = await history(prop_id=predicate, limit=limit)
    out: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        lit = row.get("literal_value")
        if lit is not None:
            out.append(str(lit))
    return out


async def _apply_default_candidacy(
    neptune: Any,
    tenant_id: str,
    kg_graph: str,
    literal_predicates: list[str],
    marker_map: dict[str, bool],
) -> dict[str, int]:
    """Write durable textKind verdicts for attributes with NO verdict yet.

    The ONTA-177 hand-off: schema-pass-mapped attributes arrive with a marker,
    but client-mapped CSV rows and enrichment-minted attributes never met the
    schema pass — this is their (name-blind) candidacy path. Only predicates in
    the canonical ``types/{T}/attrs/{a}`` shape are considered; verdicts go
    through ``commit_ontology`` (SET_TEXT_KIND; single-valued, idempotent) and
    the tenant's marker cache is invalidated so query-side consumers see them.
    """
    from infona_client.graph.ontology_commit import commit_ontology
    from infona_client.graph.ontology_queries import TEXT_KIND_FREE_TEXT
    from infona_client.graph.queries import tenant_graph_uri
    from infona_client.graph.text_markers import (
        TextCandidacy,
        classify_text_candidacy,
        invalidate,
    )
    from infona_client.models.ontology import OntologyMutation, OntologyOpKind

    h = _host()
    # Local names already marked free_text: the extractor's documented
    # conflation marks that local name on EVERY type, so a same-named attr on
    # another type is already covered — re-classifying it as "undecided" would
    # fight the existing verdict.
    marked_locals = {_local_name(u) for u, ft in marker_map.items() if ft}
    # Already-decided locals (free_text OR not_text) — property IRIs from the
    # GraphStore path only carry the leaf, so we also skip those that match a
    # decided marker's local name (ONTA-533).
    decided_locals = {_local_name(u) for u in marker_map}

    undecided: list[tuple[str, str, str]] = []  # (pred_uri, type_name, attr_name)
    for pred in literal_predicates:
        if pred in marker_map:
            continue  # decided (free_text or decided-no)
        m = _ATTR_URI_RE.match(pred)
        if m is not None:
            if m.group("attr").lower() in marked_locals:
                continue
            if _local_name(pred) in decided_locals:
                continue
            undecided.append((pred, m.group("type"), m.group("attr")))
            continue
        # GraphStore Assertion property IRIs (…/properties/<leaf>) — resolve
        # the domain type from the catalog when possible so SET_TEXT_KIND still
        # lands on a real :OntoAttr (ONTA-533).
        leaf = _local_name(pred, lower=False)
        if not leaf or not pred.endswith(f"/properties/{leaf}"):
            continue  # system/foreign predicate — never carries a verdict
        if leaf.lower() in decided_locals or leaf.lower() in marked_locals:
            continue
        domain = await _catalog_domain_for_attr(tenant_id, leaf)
        if domain is None:
            # No catalog domain yet — still commit under a best-effort type
            # so the verdict is durable (stub OntoAttr is MERGEd by SET_TEXT_KIND).
            domain = "Entity"
        undecided.append((pred, domain, leaf))

    counters = {"attrs_marked_free_text": 0, "attrs_marked_not_text": 0}
    if not undecided:
        return counters
    cap = h._MAX_CANDIDACY_ATTRS_PER_RUN
    if len(undecided) > cap:
        h.logger.info(
            "semantic_candidacy_capped",
            undecided=len(undecided),
            cap=cap,
        )
        # Fairness guard: a deterministic prefix would starve everything past
        # the cap FOREVER when more than the cap stay perpetually AMBIGUOUS
        # (heuristic verdicts are durable, but AMBIGUOUS attrs are re-sampled
        # every run, so a stable order re-samples the same head each time).
        # Randomizing before truncation gives every undecided attr a chance of
        # being sampled on each run, so all of them are eventually classified.
        h.random.shuffle(undecided)
        undecided = undecided[:cap]

    onto_graph = tenant_graph_uri(tenant_id)
    wrote = False
    for pred, type_name, attr_name in undecided:
        sample_values = await _sample_literal_values(
            neptune, kg_graph, pred, limit=_CANDIDACY_SAMPLE_SIZE
        )
        verdict = classify_text_candidacy(sample_values)
        if verdict is TextCandidacy.AMBIGUOUS:
            # Needs the LLM REASON layer (name-aware) — not available in a
            # background worker; stays undecided and is re-sampled next run.
            continue
        kind = (
            TEXT_KIND_FREE_TEXT
            if verdict is TextCandidacy.FREE_TEXT
            else TEXT_KIND_NOT_TEXT
        )
        await commit_ontology(
            neptune,
            onto_graph,
            [OntologyMutation(
                op=OntologyOpKind.SET_TEXT_KIND,
                type_name=type_name,
                slot_name=attr_name,
                text_kind=kind,
            )],
            message="semantic reconciler text candidacy",
        )
        wrote = True
        if verdict is TextCandidacy.FREE_TEXT:
            counters["attrs_marked_free_text"] += 1
        else:
            counters["attrs_marked_not_text"] += 1
    if wrote:
        # Make the fresh verdicts visible to the request path immediately (the
        # TTL remains the cross-process backstop).
        invalidate(tenant_id)
    return counters

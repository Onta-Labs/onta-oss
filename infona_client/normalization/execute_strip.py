"""strip_emoji handler — clean pictographic junk from literals."""

from __future__ import annotations

from typing import Any

from infona_client.graph.iri import ONTO_PRED_PREFIX
from infona_client.normalization.execute_helpers import (
    ATTRS_INFIX,
    _group_sparql_literals,
    _group_store_literals,
    _host,
    _sparql_str,
    _strip_emoji_value,
    logger,
)


async def _strip_emoji(neptune: Any, kg_graph: str, rule) -> tuple[dict, list[str]]:
    """Strip emoji/junk from this predicate's literals; rewrite only what changed.

    Reads every literal of the predicate (GraphStore, ONTA-534; residual SPARQL
    when the store cannot be consulted), cleans each value, and — for the
    SUBJECTS whose values actually changed — deletes the old literals and
    re-inserts the cleaned ones. Unchanged literals (already emoji-free) and
    non-literal objects are left alone, so the pass is idempotent. ``targets`` in
    params is reserved for future relationship-label cleaning; v1 cleans
    attribute literals.

    **Whole-leaf rewrite, delete BEFORE insert.** A property-graph literal is
    keyed by ``(subject, leaf)`` and a delete is predicate-scoped (``pg_ops.
    delete_literals`` drops the KEY, not one value of it), so a per-value
    delete-after-insert would erase the cleaned value it had just written, and a
    per-value delete would take the subject's UNCHANGED siblings with it. We
    therefore delete the leaf and re-insert every cleaned value of it, changed or
    not — a no-op for the untouched ones on both arms. The counters still report
    VALUES cleaned, not subjects.
    """
    pred_leaf = rule.predicate
    onto_pred = ONTO_PRED_PREFIX + pred_leaf
    attr_pred_suffix = ATTRS_INFIX + pred_leaf

    rows = await _host().literal_rows(kg_graph, pred_leaf)
    if rows is None:
        # Residual SPARQL arm. Pull every literal for the predicate (both
        # predicate forms). No CONTAINS pre-filter — emoji are spread across many
        # codepoints, so we clean in Python and only rewrite the rows that change
        # (the SELECT is bounded by predicate).
        q = (
            f"SELECT ?s ?p ?o FROM <{kg_graph}> WHERE {{\n"
            f"  ?s ?p ?o .\n"
            f"  FILTER(?p = <{onto_pred}> || STRENDS(STR(?p), \"{_sparql_str(attr_pred_suffix)}\"))\n"
            f"  FILTER(isLiteral(?o))\n"
            f"}}"
        )
        _, raw = _host().parse_sparql_results(await neptune.query(q))
        groups = _group_sparql_literals(raw)
    else:
        groups = _group_store_literals(rows, rule, pred_leaf)

    to_delete: list[tuple[str, str, Any]] = []
    to_add: list[tuple[str, str, Any]] = []
    literals_cleaned = 0
    for (s, p), values in groups.items():
        # A non-text literal (the store keeps a typed `"4.6"^^xsd:float` as a real
        # float) carries no emoji and is passed through UNTOUCHED — stringifying
        # it while rewriting a text sibling would retype the column silently.
        cleaned = [_strip_emoji_value(v) if isinstance(v, str) else v for v in values]
        changed = sum(1 for old, new in zip(values, cleaned) if old != new)
        if not changed:
            continue  # no emoji / already clean — idempotent no-op
        literals_cleaned += changed
        to_delete.extend((s, p, v) for v in values)
        # A value that cleans to empty (pure-emoji) is dropped entirely.
        to_add.extend((s, p, v) for v in cleaned if v not in ("", None))

    # E7: GraphStore once per write batch when neo4j backend is active.
    store = _host().resolve_optional_graph_store()
    if to_delete:
        await _host().delete_facts(
            neptune,
            kg_graph,
            triples=to_delete,
            reason="normalization:strip_emoji literal cleanup",
            store=store,
        )
    if to_add:
        await _host().insert_facts(neptune, kg_graph, to_add, store=store)

    summary = {
        "literals_cleaned": literals_cleaned,
        "triples_rewritten": literals_cleaned,
    }
    logger.info("strip_emoji_done", predicate=pred_leaf, **summary)
    return summary, []

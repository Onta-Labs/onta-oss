"""strip_emoji handler — clean pictographic junk from literals."""

from __future__ import annotations

from typing import Any

from infona_client.graph.iri import ONTO_PRED_PREFIX
from infona_client.normalization.execute_helpers import (
    ATTRS_INFIX,
    _host,
    _sparql_str,
    _strip_emoji_value,
    logger,
)


async def _strip_emoji(neptune: Any, kg_graph: str, rule) -> tuple[dict, list[str]]:
    """Strip emoji/junk from this predicate's literals; rewrite only what changed.

    Selects every ``attrs/<leaf>`` (or ``onto/<leaf>``) literal for the
    predicate, cleans each value, and — for the literals that actually changed —
    deletes the old triple and (unless the cleaned value is empty) inserts the
    cleaned one. Unchanged literals (already emoji-free) and non-literal objects
    are left alone, so the pass is idempotent. ``targets`` in params is reserved
    for future relationship-label cleaning; v1 cleans attribute literals.
    """
    pred_leaf = rule.predicate
    onto_pred = ONTO_PRED_PREFIX + pred_leaf
    attr_pred_suffix = ATTRS_INFIX + pred_leaf

    # Pull every literal for the predicate (both predicate forms). No CONTAINS
    # pre-filter — emoji are spread across many codepoints, so we clean in Python
    # and only rewrite the rows that change (the SELECT is bounded by predicate).
    q = (
        f"SELECT ?s ?p ?o FROM <{kg_graph}> WHERE {{\n"
        f"  ?s ?p ?o .\n"
        f"  FILTER(?p = <{onto_pred}> || STRENDS(STR(?p), \"{_sparql_str(attr_pred_suffix)}\"))\n"
        f"  FILTER(isLiteral(?o))\n"
        f"}}"
    )
    _, rows = _host().parse_sparql_results(await neptune.query(q))

    to_delete: list[tuple[str, str, str]] = []
    to_add: list[tuple[str, str, str]] = []
    literals_cleaned = 0
    for r in rows:
        s = r.get("s", "")
        p = r.get("p", "")
        o = r.get("o", "")
        if not s or not p:
            continue
        cleaned = _strip_emoji_value(o)
        if cleaned == o:
            continue  # no emoji / already clean — idempotent no-op
        literals_cleaned += 1
        to_delete.append((s, p, o))
        if cleaned:
            to_add.append((s, p, cleaned))
        # else: cleaned is empty (pure-emoji value) — drop the triple entirely.

    # E7: GraphStore once per write batch when neo4j backend is active.
    store = _host().resolve_optional_graph_store()
    if to_add:
        await _host().insert_facts(neptune, kg_graph, to_add, store=store)
    if to_delete:
        await _host().delete_facts(
            neptune,
            kg_graph,
            triples=to_delete,
            reason="normalization:strip_emoji literal cleanup",
            store=store,
        )

    summary = {
        "literals_cleaned": literals_cleaned,
        "triples_rewritten": len(to_delete),
    }
    logger.info("strip_emoji_done", predicate=pred_leaf, **summary)
    return summary, []

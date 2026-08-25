"""Current-fact filter for GraphStore reads (valid-time / ONTA-277).

A fact is CURRENT iff it has no ``:ValidityInterval`` carrying ``valid_to``.
No interval at all → current (legacy unannotated). Closed intervals stay in
the store; this module only HIDES them on literal templates and Explorer
property dumps.

Reads reuse session ``read_validity_intervals``. No second writer.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

# OPTIONAL MATCH identity is (tenant, kg, subject) plus WHERE on predicate /
# object_repr. Predicate is the RDF attr URI on the interval, while Property.id
# is ``property_uri(leaf)`` — match exact or shared leaf. ``valid_to`` absent
# or empty → current (Neo4j null, MemoryGraphStore "").
# Bind object to Assertion SoT or Entity denorm: when ``a`` is null, a closed
# scalar still sitting on ``e[$prop_key]`` must match the interval (Memory
# already uses ``_current_denorm_literal``). ``p`` is also null then, so the
# leaf also compares to ``$prop_key``.
CURRENT_INTERVAL_OPTIONAL_CYPHER = """
OPTIONAL MATCH (v:ValidityInterval {
  tenant_id: $tenant_id, kg: $kg, subject: e.id
})
WHERE (
    v.predicate = p.id
    OR last(split(v.predicate, '/')) = p.name
    OR last(split(v.predicate, '/')) = $prop_key
  )
  AND (
    v.object_repr = toString(coalesce(a.literal_value, e[$prop_key]))
    OR split(toString(v.object_repr), '^^')[0]
      = toString(coalesce(a.literal_value, e[$prop_key]))
  )
""".strip()

CURRENT_INTERVAL_KEEP_CYPHER = (
    "(v IS NULL OR v.valid_to IS NULL OR v.valid_to = '')"
)


def current_interval_keep_cypher(alias: str = "v") -> str:
    """``CURRENT_INTERVAL_KEEP_CYPHER`` for a non-``v`` OPTIONAL MATCH alias."""
    return (
        f"({alias} IS NULL OR {alias}.valid_to IS NULL OR {alias}.valid_to = '')"
    )


def current_interval_scan_cypher(
    *,
    leaf: str,
    value: str = "val",
    alias: str = "v",
) -> str:
    """OPTIONAL MATCH a ValidityInterval for one denorm leaf/value.

    ``leaf`` / ``value`` are Cypher expressions (``prop_key``, ``$prop_key``,
    ``e[$group_key]``). Last-segment predicate match lines RDF attr URIs up
    with Entity property keys. No Assertion ``a`` / Property ``p``.
    """
    return f"""
OPTIONAL MATCH ({alias}:ValidityInterval {{
  tenant_id: $tenant_id, kg: $kg, subject: e.id
}})
WHERE last(split({alias}.predicate, '/')) = {leaf}
  AND (
    {alias}.object_repr = toString({value})
    OR split(toString({alias}.object_repr), '^^')[0] = toString({value})
  )
""".strip()


def build_entity_literal_grep_cypher(excluded_key_list: str, er_prefix: str) -> str:
    """Index-free Entity property substring scan; closed valid-time terms drop."""
    return f"""
MATCH (e:Entity {{tenant_id: $tenant_id, kg: $kg}})
WHERE ($type_name IS NULL OR e.primary_type = $type_name OR EXISTS {{
  MATCH (e)-[:INSTANCE_OF]->(c:Class {{tenant_id: $tenant_id, kg: $kg}})
  WHERE c.name = $type_name OR c.id = $type_name
}})
WITH e, [k IN keys(e) WHERE NOT k IN [
  {excluded_key_list}
] AND NOT k STARTS WITH '{er_prefix}'] AS prop_keys
UNWIND prop_keys AS prop_key
WITH e, prop_key, e[prop_key] AS val
{current_interval_scan_cypher(leaf="prop_key")}
WITH e, prop_key, val, v
WHERE val IS NOT NULL
  AND {CURRENT_INTERVAL_KEEP_CYPHER}
  AND ($predicate_leaf IS NULL OR prop_key = $predicate_leaf)
  AND (
    ($case_sensitive = true AND toString(val) CONTAINS $needle)
    OR ($case_sensitive = false AND toLower(toString(val)) CONTAINS toLower($needle))
  )
RETURN e.id AS entity_uri, e.name AS label, e.primary_type AS type,
       prop_key AS attr, toString(val) AS value
ORDER BY e.id, prop_key
LIMIT $limit
""".strip()


# Distinct literal values for one type+prop (dim registry). Closed terms drop
# before DISTINCT so a superseded HQ cannot remain a dim value.
ENTITY_TYPE_PROP_DISTINCT_CYPHER = f"""
MATCH (e:Entity {{tenant_id: $tenant_id, kg: $kg}})-[:INSTANCE_OF]->(c:Class {{
  tenant_id: $tenant_id, kg: $kg
}})
WHERE c.name = $primary_type OR c.id = $primary_type
WITH e, e[$prop_key] AS val
WHERE val IS NOT NULL
{current_interval_scan_cypher(leaf="$prop_key")}
WITH val, v
WHERE {CURRENT_INTERVAL_KEEP_CYPHER}
WITH DISTINCT toString(val) AS value
RETURN value
ORDER BY value ASC
LIMIT $limit
""".strip()


def term_key(term: Any) -> str:
    """Lexical form for matching: strip a typed-literal ``^^`` tail if present."""
    text = "" if term is None else str(term)
    if "^^" in text:
        return text.rsplit("^^", 1)[0]
    return text


def predicate_leaf(predicate: str) -> str:
    """Last path segment of an RDF predicate / Property id / Entity prop key."""
    if not predicate:
        return ""
    return predicate.rstrip("/").rsplit("/", 1)[-1]


def predicate_matches_leaf(predicate: str, prop_key: str) -> bool:
    """True if ``predicate`` is ``prop_key`` or they share a leaf."""
    if not predicate or not prop_key:
        return False
    if predicate == prop_key:
        return True
    return predicate_leaf(predicate) == predicate_leaf(prop_key)


def interval_is_closed(valid_to: Any) -> bool:
    """``valid_to`` present → CLOSED (not current)."""
    return bool(valid_to)


def _as_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return row
    to_dict = getattr(row, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    if hasattr(row, "keys"):
        return dict(row)
    return {}


def closed_by_leaf(rows: Iterable[Any] | None) -> dict[str, set[str]]:
    """Map predicate leaf → closed object term-keys from validity rows."""
    out: dict[str, set[str]] = {}
    for row in rows or ():
        data = _as_dict(row)
        if not interval_is_closed(data.get("valid_to")):
            continue
        obj = data.get("object_repr")
        if obj is None or obj == "":
            continue
        leaf = predicate_leaf(str(data.get("predicate") or ""))
        if not leaf:
            continue
        out.setdefault(leaf, set()).add(term_key(obj))
    return out


def closed_terms_for_prop(
    rows: Iterable[Any] | None, prop_key: str
) -> set[str]:
    """Closed object term-keys for one property leaf (or full predicate URI)."""
    if not prop_key:
        return set()
    leaf = predicate_leaf(prop_key)
    closed: set[str] = set()
    for key, terms in closed_by_leaf(rows).items():
        if key == leaf or predicate_matches_leaf(key, prop_key):
            closed.update(terms)
    return closed


def drop_closed_value(value: Any, closed: set[str]) -> Any:
    """Drop closed terms from a scalar or list; unwrap a leftover singleton.

    Empty after filter → ``None`` (caller omits the key).
    """
    if value is None or not closed:
        return value
    if isinstance(value, (list, tuple)):
        kept = [v for v in value if term_key(v) not in closed]
        if not kept:
            return None
        if len(kept) == 1:
            return kept[0]
        return kept
    if term_key(value) in closed:
        return None
    return value


def drop_closed_literals(
    props: Mapping[str, Any] | None,
    closed: Mapping[str, set[str]] | None,
) -> dict[str, Any]:
    """Copy ``props`` omitting values whose validity interval is closed."""
    if not props:
        return {}
    if not closed:
        return dict(props)
    out: dict[str, Any] = {}
    for key, value in props.items():
        leaf = predicate_leaf(str(key))
        dropped = drop_closed_value(value, closed.get(leaf) or set())
        if dropped is not None:
            out[key] = dropped
    return out


async def closed_literals_for_subject(session: Any, subject: str) -> dict[str, set[str]]:
    """Closed object terms for ``subject``, keyed by predicate leaf.

    Reuses ``session.read_validity_intervals``. Missing native / read failure
    → ``{}`` (legacy: do not hide facts we could not ask about).
    """
    native = getattr(session, "read_validity_intervals", None)
    if not callable(native) or not subject:
        return {}
    try:
        rows = await native(subject=subject)
    except Exception:  # noqa: BLE001 — explore dump is best-effort
        return {}
    return closed_by_leaf(rows)


__all__ = [
    "CURRENT_INTERVAL_KEEP_CYPHER",
    "CURRENT_INTERVAL_OPTIONAL_CYPHER",
    "ENTITY_TYPE_PROP_DISTINCT_CYPHER",
    "build_entity_literal_grep_cypher",
    "closed_by_leaf",
    "closed_literals_for_subject",
    "closed_terms_for_prop",
    "current_interval_keep_cypher",
    "current_interval_scan_cypher",
    "drop_closed_literals",
    "drop_closed_value",
    "interval_is_closed",
    "predicate_leaf",
    "predicate_matches_leaf",
    "term_key",
]

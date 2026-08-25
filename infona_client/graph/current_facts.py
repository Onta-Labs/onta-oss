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
CURRENT_INTERVAL_OPTIONAL_CYPHER = """
OPTIONAL MATCH (v:ValidityInterval {
  tenant_id: $tenant_id, kg: $kg, subject: e.id
})
WHERE (v.predicate = p.id OR last(split(v.predicate, '/')) = p.name)
  AND (
    v.object_repr = toString(a.literal_value)
    OR split(toString(v.object_repr), '^^')[0] = toString(a.literal_value)
  )
""".strip()

CURRENT_INTERVAL_KEEP_CYPHER = (
    "(v IS NULL OR v.valid_to IS NULL OR v.valid_to = '')"
)


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
    "closed_by_leaf",
    "closed_literals_for_subject",
    "closed_terms_for_prop",
    "drop_closed_literals",
    "drop_closed_value",
    "interval_is_closed",
    "predicate_leaf",
    "predicate_matches_leaf",
    "term_key",
]

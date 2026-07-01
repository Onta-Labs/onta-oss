"""Read enrichment strategy from the ontology graph.

Strategy lives as triples on type and attribute URIs. See module docstring
in the issue for the predicate set. The executor reads these and merges
with the EnrichRequest defaults at job start. Request values override
ontology values; ontology values override hardcoded defaults.
"""
from __future__ import annotations

import structlog
from pydantic import BaseModel, Field

from cograph_client.graph.client import NeptuneClient
from cograph_client.graph.ontology_queries import list_types_query
from cograph_client.graph.parser import parse_sparql_results
from cograph_client.graph.queries import tenant_graph_uri

logger = structlog.stdlib.get_logger("cograph.enrichment.strategy")


ONTO = "https://cograph.tech/onto"
TYPES_PREFIX = "https://cograph.tech/types"


class AttributeStrategy(BaseModel):
    sources: list[str] = Field(default_factory=list)
    confidence_min: float | None = None
    id_pattern: str | None = None
    canonicalizer: str | None = None
    aliases: dict[str, str] = Field(default_factory=dict)  # "KN" -> "K&N"
    conflict_policy: str | None = None


class TypeStrategy(BaseModel):
    type_name: str
    match_key: str | None = None
    lookup_priority: int | None = None
    attributes: dict[str, AttributeStrategy] = Field(default_factory=dict)


def _type_uri(type_name: str) -> str:
    return f"{TYPES_PREFIX}/{type_name}"


def _attr_prefix(type_name: str) -> str:
    return f"{TYPES_PREFIX}/{type_name}/attrs/"


def _attr_name_from_uri(attr_uri: str, type_name: str) -> str | None:
    prefix = _attr_prefix(type_name)
    if not attr_uri.startswith(prefix):
        return None
    name = attr_uri[len(prefix):]
    return name or None


def _parse_alias(raw: str) -> tuple[str, str] | None:
    """Parse 'KN->K&N' (arrow or =>) into (KN, K&N). Returns None if malformed."""
    if not raw:
        return None
    for sep in ("→", "=>"):  # U+2192 RIGHTWARDS ARROW
        if sep in raw:
            left, _, right = raw.partition(sep)
            left = left.strip()
            right = right.strip()
            if left and right:
                return left, right
            return None
    return None


def _build_strategy_query(graph_uri: str, type_name: str) -> str:
    type_uri = _type_uri(type_name)
    attr_prefix = _attr_prefix(type_name)
    return (
        f"SELECT ?subj ?p ?o FROM <{graph_uri}> WHERE {{\n"
        f"  {{\n"
        f"    BIND(<{type_uri}> AS ?subj)\n"
        f"    <{type_uri}> ?p ?o .\n"
        f"    FILTER(?p IN ("
        f"<{ONTO}/matchKey>, <{ONTO}/lookupPriority>"
        f"))\n"
        f"  }} UNION {{\n"
        f"    ?subj ?p ?o .\n"
        f"    FILTER(STRSTARTS(STR(?subj), \"{attr_prefix}\"))\n"
        f"    FILTER(?p IN ("
        f"<{ONTO}/enrichmentSource>, <{ONTO}/confidenceMin>, "
        f"<{ONTO}/idPattern>, <{ONTO}/canonicalizer>, "
        f"<{ONTO}/alias>, <{ONTO}/conflictPolicy>"
        f"))\n"
        f"  }}\n"
        f"}}"
    )


_TYPE_PREDICATES = {
    f"{ONTO}/matchKey",
    f"{ONTO}/lookupPriority",
}

_ATTR_PREDICATES = {
    f"{ONTO}/enrichmentSource",
    f"{ONTO}/confidenceMin",
    f"{ONTO}/idPattern",
    f"{ONTO}/canonicalizer",
    f"{ONTO}/alias",
    f"{ONTO}/conflictPolicy",
}


async def load_strategy(
    client: NeptuneClient, tenant_id: str, type_name: str
) -> TypeStrategy:
    """Read the strategy for a type from the tenant's ontology graph.

    Missing fields are left None / empty. Always returns a TypeStrategy
    (never raises) so callers can merge with defaults safely.
    """
    strategy = TypeStrategy(type_name=type_name)
    type_uri = _type_uri(type_name)
    try:
        graph_uri = tenant_graph_uri(tenant_id)
        query = _build_strategy_query(graph_uri, type_name)
        raw = await client.query(query)
        _, bindings = parse_sparql_results(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "strategy_load_failed", tenant_id=tenant_id, type_name=type_name, error=str(exc)
        )
        return strategy

    for row in bindings:
        subj = row.get("subj", "")
        pred = row.get("p", "")
        obj = row.get("o", "")
        if not pred or obj is None:
            continue

        if subj == type_uri and pred in _TYPE_PREDICATES:
            if pred == f"{ONTO}/matchKey":
                strategy.match_key = obj or None
            elif pred == f"{ONTO}/lookupPriority":
                try:
                    strategy.lookup_priority = int(obj)
                except (TypeError, ValueError):
                    continue
            continue

        if pred in _ATTR_PREDICATES:
            attr_name = _attr_name_from_uri(subj, type_name)
            if not attr_name:
                continue
            attr = strategy.attributes.get(attr_name)
            if attr is None:
                attr = AttributeStrategy()
                strategy.attributes[attr_name] = attr

            if pred == f"{ONTO}/enrichmentSource":
                if obj and obj not in attr.sources:
                    attr.sources.append(obj)
            elif pred == f"{ONTO}/confidenceMin":
                try:
                    attr.confidence_min = float(obj)
                except (TypeError, ValueError):
                    continue
            elif pred == f"{ONTO}/idPattern":
                attr.id_pattern = obj or None
            elif pred == f"{ONTO}/canonicalizer":
                attr.canonicalizer = obj or None
            elif pred == f"{ONTO}/alias":
                parsed = _parse_alias(obj)
                if parsed is not None:
                    left, right = parsed
                    attr.aliases[left] = right
            elif pred == f"{ONTO}/conflictPolicy":
                attr.conflict_policy = obj or None

    return strategy


# ── Type-name resolution ─────────────────────────────────────────────────────
# Root-cause guard for the "job Completed but enriched nothing" no-op: the entity
# SELECT keys on ``?e a <types/Name>`` case-sensitively, so a lowercase
# ``organization`` against a declared PascalCase ``Organization`` matches zero
# entities and the run silently finishes empty. These resolve a requested type to
# the tenant's canonical declared name (auto-correcting case) and let callers
# reject a type that truly doesn't exist. Both the enrich route (up-front 422)
# and the executor (safety net for schedules / actions) use them.


async def list_declared_types(client: NeptuneClient, tenant_id: str) -> list[str]:
    """The tenant's declared type names — the local part of each ``rdfs:Class``
    URI in the ontology graph (e.g. ``Organization`` from
    ``…/types/Organization``).

    A single bounded ontology read — the SAME query :func:`load_strategy` and the
    ``/ontology/types`` route use, never an instance scan (COG-112 safe). Keys on
    the class URI (not ``rdfs:label``) because that local part is exactly what
    :func:`_type_uri` / the entity SELECT match on. Returns ``[]`` on any read
    error so callers fail open (an unavailable ontology must never block a job).
    """
    try:
        onto_graph = tenant_graph_uri(tenant_id)
        _, rows = parse_sparql_results(await client.query(list_types_query(onto_graph)))
    except Exception:  # noqa: BLE001 — a type-list read must never break a job
        logger.warning("enrich_list_types_failed", tenant_id=tenant_id, exc_info=True)
        return []
    prefix = f"{TYPES_PREFIX}/"
    seen: set[str] = set()
    names: list[str] = []
    for row in rows:
        uri = (row.get("type") or "").strip()
        if not uri.startswith(prefix):
            continue
        name = uri[len(prefix):]
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


async def resolve_type_name(
    client: NeptuneClient, tenant_id: str, requested: str
) -> tuple[str | None, list[str]]:
    """Resolve ``requested`` to the tenant's canonical declared type name.

    Matching is exact first, then case-insensitive — so a lowercase
    ``organization`` resolves to the declared ``Organization`` (the reported
    root-cause: a miscased type selected zero entities → a silent no-op run).

    Returns ``(canonical, known)``:

    - ``known`` — the declared type names; ``[]`` means the ontology read failed
      or the tenant declared none, i.e. "cannot judge" → callers MUST fail open
      and keep ``requested`` unchanged.
    - ``canonical`` — the matched declared name, or ``None`` when ``known`` is
      non-empty but nothing matches (a genuinely unknown type).
    """
    known = await list_declared_types(client, tenant_id)
    if not known:
        return None, []
    if requested in known:
        return requested, known
    lowered = requested.strip().lower()
    for name in known:
        if name.lower() == lowered:
            return name, known
    return None, known


def unknown_type_message(requested: str, known: list[str]) -> str:
    """Actionable error for an enrich job whose type doesn't exist in the graph."""
    preview = ", ".join(sorted(known)[:10])
    more = "" if len(known) <= 10 else f" (+{len(known) - 10} more)"
    return (
        f"Type '{requested}' doesn't exist in this graph. "
        f"Available types: {preview}{more}."
    )

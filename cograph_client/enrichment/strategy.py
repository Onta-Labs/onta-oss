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

"""Read enrichment strategy from the ontology graph.

Strategy lives as triples on type and attribute URIs. The executor reads these
and merges with the EnrichRequest defaults at job start. Request values override
ontology values; ontology values override hardcoded defaults.

ONTA-527: production is Neo4j-only. This module reads GraphStore / the ontology
catalog. There is no SPARQL fallback — a retired ``NeptuneClient.query`` used
to fail-open into ``SparqlClientRetired`` and return an empty TypeStrategy while
logging ``strategy_load_failed``. Missing strategy triples are a legitimate
empty TypeStrategy; a store error logs loudly and still returns empty (callers
merge with request defaults).
"""

from __future__ import annotations

from typing import Any, Iterable

import structlog
from pydantic import BaseModel, Field

from infona_client.graph.client import NeptuneClient
from infona_client.graph.iri import IRI_BASE, ONTO_BASE

logger = structlog.stdlib.get_logger("infona.enrichment.strategy")


ONTO = ONTO_BASE
TYPES_PREFIX = f"{IRI_BASE}/types"


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

# Flattened GraphStore property keys (classify_triple maps …/onto/<leaf> → leaf).
_TYPE_PROP_KEYS = {
    "matchKey": f"{ONTO}/matchKey",
    "lookupPriority": f"{ONTO}/lookupPriority",
}
_ATTR_PROP_KEYS = {
    "enrichmentSource": f"{ONTO}/enrichmentSource",
    "confidenceMin": f"{ONTO}/confidenceMin",
    "idPattern": f"{ONTO}/idPattern",
    "canonicalizer": f"{ONTO}/canonicalizer",
    "alias": f"{ONTO}/alias",
    "conflictPolicy": f"{ONTO}/conflictPolicy",
}


def _as_value_list(raw: Any) -> list[str]:
    """Normalize a GraphStore property / assertion value to a string list."""
    if raw is None or raw == "":
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw if x is not None and x != ""]
    return [str(raw)]


def apply_strategy_triple(
    strategy: TypeStrategy, subj: str, pred: str, obj: str, type_name: str
) -> None:
    """Fold one (subject, predicate, object) strategy triple into ``strategy``.

    Parser helper — kept so tests can exercise the encoding without SPARQL.
    Unknown / malformed triples are skipped.
    """
    if not pred or obj is None:
        return
    obj_s = str(obj)
    type_uri = _type_uri(type_name)

    if subj == type_uri and pred in _TYPE_PREDICATES:
        if pred == f"{ONTO}/matchKey":
            strategy.match_key = obj_s or None
        elif pred == f"{ONTO}/lookupPriority":
            try:
                strategy.lookup_priority = int(obj_s)
            except (TypeError, ValueError):
                return
        return

    if pred in _ATTR_PREDICATES:
        attr_name = _attr_name_from_uri(subj, type_name)
        if not attr_name:
            return
        attr = strategy.attributes.get(attr_name)
        if attr is None:
            attr = AttributeStrategy()
            strategy.attributes[attr_name] = attr

        if pred == f"{ONTO}/enrichmentSource":
            if obj_s and obj_s not in attr.sources:
                attr.sources.append(obj_s)
        elif pred == f"{ONTO}/confidenceMin":
            try:
                attr.confidence_min = float(obj_s)
            except (TypeError, ValueError):
                return
        elif pred == f"{ONTO}/idPattern":
            attr.id_pattern = obj_s or None
        elif pred == f"{ONTO}/canonicalizer":
            attr.canonicalizer = obj_s or None
        elif pred == f"{ONTO}/alias":
            parsed = _parse_alias(obj_s)
            if parsed is not None:
                left, right = parsed
                attr.aliases[left] = right
        elif pred == f"{ONTO}/conflictPolicy":
            attr.conflict_policy = obj_s or None


def _triples_from_props(
    subject_uri: str, props: dict[str, Any], *, type_level: bool
) -> Iterable[tuple[str, str, str]]:
    """Project GraphStore entity props into (subj, pred, obj) strategy triples."""
    key_map = _TYPE_PROP_KEYS if type_level else _ATTR_PROP_KEYS
    for key, pred in key_map.items():
        for obj in _as_value_list(props.get(key)):
            yield subject_uri, pred, obj


async def _entity_props(session: Any, entity_id: str) -> dict[str, Any]:
    """Literal props + assertion values for one ontology-scope subject."""
    out: dict[str, Any] = {}
    try:
        rows = await session.execute_template("entity_detail", {"id": entity_id})
    except Exception:  # noqa: BLE001 — a missing subject is not a store outage
        rows = []
    if rows:
        detail = (
            rows[0].to_dict() if hasattr(rows[0], "to_dict") else dict(rows[0])
        )
        raw = detail.get("props") or {}
        if isinstance(raw, dict):
            out.update(raw)

    reader = getattr(session, "read_assertions_for_subject", None)
    if not callable(reader):
        return out
    try:
        assertions = await reader(entity_id)
    except Exception:  # noqa: BLE001
        return out
    for row in assertions or []:
        data = row.to_dict() if hasattr(row, "to_dict") else dict(row)
        prop_id = str(data.get("property_id") or data.get("prop_id") or "")
        leaf = prop_id.rsplit("/", 1)[-1] if prop_id else ""
        val = data.get("literal_value")
        if not leaf or val is None or val == "":
            continue
        existing = out.get(leaf)
        if existing is None:
            out[leaf] = val
        else:
            merged = _as_value_list(existing)
            for item in _as_value_list(val):
                if item not in merged:
                    merged.append(item)
            out[leaf] = merged if len(merged) > 1 else merged[0]
    return out


async def load_strategy(
    client: NeptuneClient, tenant_id: str, type_name: str
) -> TypeStrategy:
    """Read the strategy for a type from the tenant's ontology GraphStore.

    Strategy triples live on the type URI (``…/types/<Type>``) and attribute
    URIs (``…/types/<Type>/attrs/<leaf>``) as ``onto/matchKey``,
    ``onto/enrichmentSource``, etc. They are written the same way every other
    ontology fact is written (``insert_facts`` into the tenant ontology graph).
    Nothing in production writes them today — an empty TypeStrategy is the
    correct GraphStore answer, not a reason to SPARQL.

    ``client`` is unused (kept so callers do not change). Always returns a
    TypeStrategy (never raises) so callers can merge with defaults.
    """
    del client  # GraphStore-only; NeptuneClient.query is retired (ONTA-534).
    strategy = TypeStrategy(type_name=type_name)
    try:
        from infona_client.graph.ontology_catalog import list_attributes
        from infona_client.graph.scope import GraphScope
        from infona_client.graph.store import GraphConfigError, get_optional_graph_store

        store = get_optional_graph_store()
        session = store.session(
            GraphScope.for_catalog(layer="tenant", tenant_id=tenant_id)
        )
    except GraphConfigError:
        logger.error(
            "strategy_load_no_store",
            tenant_id=tenant_id,
            type_name=type_name,
        )
        return strategy
    except Exception:  # noqa: BLE001
        logger.exception(
            "strategy_load_failed", tenant_id=tenant_id, type_name=type_name
        )
        return strategy

    type_uri = _type_uri(type_name)
    try:
        type_props = await _entity_props(session, type_uri)
        for subj, pred, obj in _triples_from_props(
            type_uri, type_props, type_level=True
        ):
            apply_strategy_triple(strategy, subj, pred, obj, type_name)

        attr_records = await list_attributes(
            store=store, tenant_id=tenant_id, type_name=type_name, layer="tenant"
        )
        for rec in attr_records:
            name = (getattr(rec, "name", None) or "").strip()
            if not name:
                continue
            attr_uri = f"{_attr_prefix(type_name)}{name}"
            attr_props = await _entity_props(session, attr_uri)
            for subj, pred, obj in _triples_from_props(
                attr_uri, attr_props, type_level=False
            ):
                apply_strategy_triple(strategy, subj, pred, obj, type_name)
    except Exception:  # noqa: BLE001
        logger.exception(
            "strategy_load_failed", tenant_id=tenant_id, type_name=type_name
        )
        return strategy

    return strategy


# ── Type-name resolution ─────────────────────────────────────────────────────
# Root-cause guard for the "job Completed but enriched nothing" no-op: the entity
# SELECT keys on the declared type name case-sensitively, so a lowercase
# ``organization`` against a declared PascalCase ``Organization`` matches zero
# entities and the run silently finishes empty. These resolve a requested type to
# the tenant's canonical declared name (auto-correcting case) and let callers
# reject a type that truly doesn't exist. Both the enrich route (up-front 422)
# and the executor (safety net for schedules / actions) use them.


async def list_declared_types(client: NeptuneClient, tenant_id: str) -> list[str]:
    """The tenant's declared type names — the local part of each catalog type.

    GraphStore / Neo4j (ONTA-534 / ONTA-527): the SAME ``list_types`` the
    ``/ontology/types`` route uses. No SPARQL arm. ``client`` is unused (kept
    so callers do not change).

    Returns ``[]`` when the catalog is empty or the store is unavailable so
    callers fail open (an unavailable ontology must never block a job). A
    store error is logged at error, not swallowed as "maybe SPARQL will work".
    """
    del client
    try:
        from infona_client.graph.ontology_catalog import list_types as cat_list_types
        from infona_client.graph.store import GraphConfigError, get_optional_graph_store

        store = get_optional_graph_store()
        records = await cat_list_types(
            store=store, tenant_id=tenant_id, layer="tenant"
        )
    except GraphConfigError:
        logger.error("enrich_list_types_no_store", tenant_id=tenant_id)
        return []
    except Exception:  # noqa: BLE001 — a type-list read must never break a job
        logger.exception("enrich_list_types_failed", tenant_id=tenant_id)
        return []

    seen: set[str] = set()
    names: list[str] = []
    for rec in records:
        name = (getattr(rec, "name", None) or "").strip()
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

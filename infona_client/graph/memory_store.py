"""In-memory :class:`GraphStore` for hermetic unit tests.

Implements the same scope-enforcement surface as :class:`Neo4jGraphStore` so
isolation tests do not need a live database. Supports:

* Entity MERGE / MATCH by ``(tenant_id, kg, id)`` (templates)
* Entity list filtered by ``INSTANCE_OF`` → Class (ADR 0013; not primary_type alone)
* Domain-label SET via :func:`infona_client.graph.labels.set_entity_type_labels`
* Native writer methods used by :mod:`infona_client.graph.pg_ops` (E3):
  literals (list-union), typed rels (B4 MERGE key), delete, rewrite, ProvEvent
* Ontology catalog (E4): ``:OntoType`` / ``:OntoAttr`` + hierarchy / DECLARES /
  RANGE_TYPE templates
* Explore reads (E5): paged list-by-type, entity detail + rels, type / total counts

Anything outside the smoke Cypher subset raises :class:`GraphQueryError` —
prefer the native ``write_*`` methods for instance mutations.

Implementation lives in sibling ``memory_store_*.py`` modules. Every
previously importable name is re-exported here.
"""

from __future__ import annotations

from typing import Any, Sequence

from infona_client.graph.memory_store_assertions import MemoryAssertionsMixin
from infona_client.graph.memory_store_entities import MemoryEntitiesMixin
from infona_client.graph.memory_store_execute import MemoryExecuteMixin
from infona_client.graph.memory_store_explore import MemoryExploreMixin
from infona_client.graph.memory_store_listing import MemoryListingMixin
from infona_client.graph.memory_store_literals import MemoryLiteralsMixin
from infona_client.graph.memory_store_normalize import MemoryNormalizeMixin
from infona_client.graph.memory_store_norms import (  # noqa: F401 — public re-exports
    _COUNT_BY_TYPE_SINGLE_NORM,
    _COUNT_TOTAL_NORM,
    _DETAIL_NORM,
    _ENTITIES_OF_TYPE_COUNT_NORM,
    _ENTITIES_OF_TYPE_NORM,
    _ENTITY_COUNT_BY_TYPE_NORM,
    _FILTER_PROP_EQ_NORM,
    _GET_NORM,
    _HOP_OUT_NORM,
    _LIST_NORM,
    _LIST_PAGE_NORM,
    _LITERAL_AGGREGATE_NORM,
    _LITERAL_COMPARE_NORM,
    _LITERAL_GREP_NORM,
    _LITERAL_VALUES_NORM,
    _MERGE_NORM,
    _ONTO_ATTR_DELETE_NORM,
    _ONTO_ATTR_LIST_NORM,
    _ONTO_ATTR_RANGE_NORM,
    _ONTO_ATTR_SET_MARKERS_NORM,
    _ONTO_ATTR_SET_TEXT_KIND_NORM,
    _ONTO_ATTR_UPSERT_NORM,
    _ONTO_SUBCLASS_CLEAR_NORM,
    _ONTO_SUBCLASS_SET_NORM,
    _ONTO_TYPE_DELETE_NORM,
    _ONTO_TYPE_GET_NORM,
    _ONTO_TYPE_LIST_NORM,
    _ONTO_TYPE_SET_MARKERS_NORM,
    _ONTO_TYPE_UPSERT_NORM,
    _RELATED_ENTITIES_NORM,
    _RELATED_ENTITY_NAME_FILTER_NORM,
    _RELS_NORM,
    _SUBCLASS_OF_CLOSURE_NORM,
    _TYPE_ATTR_COVERAGE_NORM,
    _TYPE_PROP_DISTINCT_NORM,
    _TYPE_REL_COVERAGE_NORM,
    _TYPE_REL_TARGET_DISTINCT_NORM,
    _norm_cypher,
)
from infona_client.graph.memory_store_ontology import MemoryOntologyMixin
from infona_client.graph.memory_store_rels import MemoryRelsMixin
from infona_client.graph.memory_store_suppression import MemorySuppressionMixin
from infona_client.graph.memory_store_validity import MemoryValidityMixin
from infona_client.graph.memory_store_rows import (  # noqa: F401 — public re-exports
    _AssertionRow,
    _CitationRow,
    _ClassRow,
    _EntityRow,
    _OntoAttrRow,
    _OntoTypeRow,
    _PropertyRow,
    _ProvRow,
    _RelRow,
    _SuppressionRow,
    _ValidityRow,
    _ValueHistoryRow,
)
from infona_client.graph.memory_store_session import MemoryGraphSession
from infona_client.graph.schema_bootstrap import bootstrap_schema_statements
from infona_client.graph.scope import GraphScope, GraphScopeError
from infona_client.graph.store import GraphSession


class MemoryGraphStore(
    MemoryEntitiesMixin,
    MemoryAssertionsMixin,
    MemoryOntologyMixin,
    MemoryListingMixin,
    MemoryLiteralsMixin,
    MemoryRelsMixin,
    MemoryExploreMixin,
    MemorySuppressionMixin,
    MemoryValidityMixin,
    MemoryNormalizeMixin,
    MemoryExecuteMixin,
):
    """Process-local fake store; not safe for concurrent multi-process use."""

    def __init__(self) -> None:
        # key: (tenant_id, kg, id)
        self._entities: dict[tuple[str, str, str], _EntityRow] = {}
        # B4 key: (tenant_id, kg, start_id, end_id, rel_type)
        self._rels: dict[tuple[str, str, str, str, str], _RelRow] = {}
        self._prov: list[_ProvRow] = []
        # ValueHistory (ONTA-236/536): ordered old→new transitions
        self._value_history: list[_ValueHistoryRow] = []
        # Suppression MERGE key: (tenant_id, kg, mark_id) — ONTA-279 sticky marks
        self._suppressions: dict[tuple[str, str, str], _SuppressionRow] = {}
        # Validity MERGE key: (tenant_id, kg, interval_id) — ONTA-277 intervals
        self._validity: dict[tuple[str, str, str], _ValidityRow] = {}
        # AttrCitation MERGE key: (tenant_id, kg, entity_id, attr, value_hash)
        self._citations: dict[tuple[str, str, str, str, str], _CitationRow] = {}
        # ADR 0013: Class / Property / Assertion
        self._classes: dict[tuple[str, str, str], _ClassRow] = {}
        self._properties: dict[tuple[str, str, str], _PropertyRow] = {}
        self._assertions: dict[tuple[str, str, str], _AssertionRow] = {}
        # SUBCLASS_OF: (tenant, kg, child_id) → parent_id
        self._subclass_of: dict[tuple[str, str, str], str] = {}
        # SUBPROPERTY_OF: (tenant, kg, child_id) → parent_id
        self._subproperty_of: dict[tuple[str, str, str], str] = {}
        # INSTANCE_OF cache: (tenant, kg, entity_id) → set(class_id)
        self._instance_of: dict[tuple[str, str, str], set[str]] = {}
        # Catalog: (tenant_id, kg, layer, name)
        self._onto_types: dict[tuple[str, str, str, str], _OntoTypeRow] = {}
        # Catalog: (tenant_id, kg, layer, domain, name)
        self._onto_attrs: dict[tuple[str, str, str, str, str], _OntoAttrRow] = {}
        # KG registry: (tenant_id, name) → {name, description, triple_count}
        self._kg_registry: dict[tuple[str, str], dict[str, Any]] = {}
        # Blueprint install lock: (tenant_id, blueprint_id) → payload dict
        self._blueprint_locks: dict[tuple[str, str], dict[str, Any]] = {}
        self._bootstrapped: list[str] = []

    def session(self, scope: GraphScope) -> GraphSession:
        if not isinstance(scope, GraphScope):
            raise GraphScopeError("session() requires a GraphScope instance")
        return MemoryGraphSession(self, scope)

    async def health(self) -> bool:
        return True

    async def bootstrap_schema(self) -> Sequence[str]:
        if not self._bootstrapped:
            self._bootstrapped = [name for name, _ in bootstrap_schema_statements()]
        return list(self._bootstrapped)

    async def close(self) -> None:
        self._entities.clear()
        self._rels.clear()
        self._prov.clear()
        self._value_history.clear()
        self._suppressions.clear()
        self._validity.clear()
        self._citations.clear()
        self._classes.clear()
        self._properties.clear()
        self._assertions.clear()
        self._subclass_of.clear()
        self._subproperty_of.clear()
        self._instance_of.clear()
        self._onto_types.clear()
        self._onto_attrs.clear()
        self._kg_registry.clear()
        self._blueprint_locks.clear()
        self._bootstrapped.clear()
        bag = getattr(self, "_ontology_companion", None)
        if bag is not None and hasattr(bag, "clear"):
            bag.clear()

    async def kg_registry_list(self, tenant_id: str) -> list[dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for (t, name), row in self._kg_registry.items():
            if t == tenant_id:
                out[name] = dict(row)
        for (t, k, _), _ in self._entities.items():
            if t == tenant_id and k and k not in out:
                out[k] = {"name": k, "description": "", "triple_count": 0}
        return [out[n] for n in sorted(out)]

    async def kg_registry_upsert(
        self,
        tenant_id: str,
        name: str,
        *,
        description: str = "",
        triple_count: int | None = None,
        only_if_absent: bool = False,
    ) -> dict[str, Any]:
        key = (tenant_id, name)
        existing = self._kg_registry.get(key)
        if existing is not None and only_if_absent:
            return dict(existing)
        if existing is None:
            row = {
                "name": name,
                "description": description or "",
                "triple_count": int(triple_count or 0),
            }
        else:
            row = dict(existing)
            if description:
                row["description"] = description
            if triple_count is not None:
                row["triple_count"] = int(triple_count)
        self._kg_registry[key] = row
        return dict(row)

    async def kg_registry_delete(self, tenant_id: str, name: str) -> None:
        self._kg_registry.pop((tenant_id, name), None)

    async def blueprint_lock_get(
        self, tenant_id: str, blueprint_id: str
    ) -> dict[str, Any] | None:
        row = self._blueprint_locks.get((tenant_id, blueprint_id))
        return dict(row) if row is not None else None

    async def blueprint_lock_list(self, tenant_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for (tid, _), row in self._blueprint_locks.items()
            if tid == tenant_id
        ]

    async def blueprint_lock_put(
        self, tenant_id: str, blueprint_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        stored = dict(payload)
        stored["tenant_id"] = tenant_id
        stored["blueprint_id"] = blueprint_id
        self._blueprint_locks[(tenant_id, blueprint_id)] = stored
        return dict(stored)

    async def blueprint_lock_delete(self, tenant_id: str, blueprint_id: str) -> bool:
        return self._blueprint_locks.pop((tenant_id, blueprint_id), None) is not None

    def assertion_count(
        self, *, tenant_id: str | None = None, kg: str | None = None
    ) -> int:
        n = 0
        for (t, k, _), _ in self._assertions.items():
            if tenant_id is not None and t != tenant_id:
                continue
            if kg is not None and k != kg:
                continue
            n += 1
        return n


__all__ = [
    "MemoryGraphSession",
    "MemoryGraphStore",
    "_AssertionRow",
    "_CitationRow",
    "_ClassRow",
    "_EntityRow",
    "_OntoAttrRow",
    "_OntoTypeRow",
    "_PropertyRow",
    "_ProvRow",
    "_RelRow",
    "_SuppressionRow",
    "_ValidityRow",
    "_ValueHistoryRow",
    "_norm_cypher",
]

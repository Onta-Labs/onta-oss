"""Reusable RDF-semantic query helpers (ADR 0013 §9 / golden-query harness).

Compose these from NL planners and app code. Helpers are **scope-bound** and
return answer rows as plain dicts — never SPARQL strings, never free-form
Assertion scans without subject/type constraint as the default tool.

Two surfaces (same semantics, different backends):

1. **AssertionMemoryStore** helpers (hermetic golden suite) — take an explicit
   store + tenant_id/kg.
2. **Cypher templates** (+ optional GraphSession natives) for Neo4j /
   MemoryGraphStore explore paths — registered in
   :mod:`infona_client.graph.schema_bootstrap`.

Golden cases compare **answers**, not query text.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from infona_client.graph.assertion_model import (
    AssertionNode,
    canonical_literal,
    type_membership_property_id,
)
from infona_client.graph.scope import GraphScopeError

if TYPE_CHECKING:
    from infona_client.graph.assertion_memory import AssertionMemoryStore
    from infona_client.graph.store import GraphSession

# ---------------------------------------------------------------------------
# Template names (ADR 0013 — NL fixtures + schema_bootstrap registry keys)
# ---------------------------------------------------------------------------

TEMPLATE_ENTITIES_OF_TYPE = "entities_of_type"
TEMPLATE_ENTITIES_OF_TYPE_COUNT = "entities_of_type_count"
TEMPLATE_LITERAL_VALUES = "literal_values"
TEMPLATE_LITERAL_COMPARE = "literal_compare"
TEMPLATE_RELATED_ENTITIES = "related_entities"
TEMPLATE_RELATED_ENTITY_NAME_FILTER = "related_entity_name_filter"
TEMPLATE_ASSERTIONS_FOR_SUBJECT = "assertions_for_subject"
TEMPLATE_SUBCLASS_OF_CLOSURE = "subclass_of_closure"

# ---------------------------------------------------------------------------
# Cypher templates (ADR 0013 helpers — allowlisted in schema_bootstrap)
# ---------------------------------------------------------------------------
# Defined FIRST so memory_store / schema_bootstrap can import these constants
# without pulling AssertionMemoryStore (breaks import cycles).
# Parameter names match MemoryGraphStore native implementations.

# Semantic membership via INSTANCE_OF → Class (ADR 0013). ``$type_names`` is the
# subclass-expanded list of Class **names** (leaves) and/or Class IRIs; callers
# (NL fixtures) expand via type_names_with_subclasses / Class SUBCLASS_OF.
# ``primary_type`` is a denorm cache only — never the sole type filter.
ENTITIES_OF_TYPE_CYPHER = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE (c.name IN $type_names OR c.id IN $type_names)
  AND ($after_id IS NULL OR e.id > $after_id)
RETURN DISTINCT e.id AS id, e.tenant_id AS tenant_id, e.kg AS kg,
       e.primary_type AS primary_type, e.name AS name, e.source AS source
ORDER BY e.id
LIMIT $limit
""".strip()

ENTITIES_OF_TYPE_COUNT_CYPHER = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names OR c.id IN $type_names
RETURN count(DISTINCT e) AS n
""".strip()

# Prefer Assertion literal SoT; Entity property cache is secondary (dual-written
# after Assertion by apply_facts / assert_fact).
# Equality matches raw values OR normalized forms: strip SPARQL-era
# ``lexical^^xsd-uri`` suffixes (legacy graphs), then string-compare the
# lexical half and allow toFloat equality when both sides are numeric so
# native store numbers still match string $prop_value from NL fixtures.
LITERAL_VALUES_CYPHER = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names OR c.id IN $type_names
OPTIONAL MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg, subject_id: e.id})
  -[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg})
WHERE p.name = $prop_key AND (
  a.literal_value = $prop_value
  OR (
    CASE
      WHEN toString(a.literal_value) CONTAINS '^^'
        THEN split(toString(a.literal_value), '^^')[0]
      ELSE toString(a.literal_value)
    END
    =
    CASE
      WHEN toString($prop_value) CONTAINS '^^'
        THEN split(toString($prop_value), '^^')[0]
      ELSE toString($prop_value)
    END
  )
  OR (
    toFloat(
      CASE
        WHEN toString(a.literal_value) CONTAINS '^^'
          THEN split(toString(a.literal_value), '^^')[0]
        ELSE toString(a.literal_value)
      END
    ) =
    toFloat(
      CASE
        WHEN toString($prop_value) CONTAINS '^^'
          THEN split(toString($prop_value), '^^')[0]
        ELSE toString($prop_value)
      END
    )
  )
)
WITH DISTINCT e, a
WHERE a IS NOT NULL OR (
  e[$prop_key] = $prop_value
  OR (
    CASE
      WHEN toString(e[$prop_key]) CONTAINS '^^'
        THEN split(toString(e[$prop_key]), '^^')[0]
      ELSE toString(e[$prop_key])
    END
    =
    CASE
      WHEN toString($prop_value) CONTAINS '^^'
        THEN split(toString($prop_value), '^^')[0]
      ELSE toString($prop_value)
    END
  )
  OR (
    toFloat(
      CASE
        WHEN toString(e[$prop_key]) CONTAINS '^^'
          THEN split(toString(e[$prop_key]), '^^')[0]
        ELSE toString(e[$prop_key])
      END
    ) =
    toFloat(
      CASE
        WHEN toString($prop_value) CONTAINS '^^'
          THEN split(toString($prop_value), '^^')[0]
        ELSE toString($prop_value)
      END
    )
  )
)
RETURN e.id AS id, e.name AS name, e.primary_type AS primary_type,
       coalesce(a.literal_value, e[$prop_key]) AS literal_value
ORDER BY e.id
LIMIT $limit
""".strip()

# Numeric / inequality compare on a datatype property. Handles both native
# store numbers and legacy SPARQL-era ``lexical^^xsd-uri`` strings still in
# older graphs (split off the suffix before toFloat).
LITERAL_COMPARE_CYPHER = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names OR c.id IN $type_names
OPTIONAL MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg, subject_id: e.id})
  -[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg})
WHERE p.name = $prop_key
WITH e, coalesce(a.literal_value, e[$prop_key]) AS raw
WHERE raw IS NOT NULL
WITH e, raw,
  toFloat(
    CASE
      WHEN toString(raw) CONTAINS '^^' THEN split(toString(raw), '^^')[0]
      ELSE toString(raw)
    END
  ) AS num
WHERE num IS NOT NULL AND (
  ($op = 'lt' AND num < $threshold) OR
  ($op = 'le' AND num <= $threshold) OR
  ($op = 'gt' AND num > $threshold) OR
  ($op = 'ge' AND num >= $threshold) OR
  ($op = 'eq' AND num = $threshold)
)
RETURN e.id AS id, e.name AS name, e.primary_type AS primary_type,
       coalesce(e.title, e.name) AS title, num AS value
ORDER BY num, e.id
LIMIT $limit
""".strip()

# Filter subjects of a type by a related entity's display name / name
# (e.g. Book --HAS_GENRE--> Genre{display_name: "Classic Fiction"}).

# Reverse of related_entity_name_filter: "products made by Acme" when the
# edge is Organization-[:makes]->Product (object is the thing we return).
RELATED_ENTITY_NAME_FILTER_INVERSE_CYPHER = """
MATCH (maker:Entity {tenant_id: $tenant_id, kg: $kg})
WHERE (
    toLower(coalesce(maker.display_name, '')) = toLower($target_name)
    OR toLower(coalesce(maker.name, '')) = toLower($target_name)
    OR toLower(replace(coalesce(maker.name, ''), '_', ' ')) = toLower($target_name)
    OR toLower(coalesce(maker.display_name, maker.name, '')) CONTAINS toLower($target_name)
  )
MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg})-[:SUBJECT]->(maker)
MATCH (a)-[:OBJECT]->(e:Entity {tenant_id: $tenant_id, kg: $kg})
MATCH (a)-[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg})
WHERE p.name = $rel_attr
MATCH (e)-[:INSTANCE_OF]->(c:Class {tenant_id: $tenant_id, kg: $kg})
WHERE c.name IN $type_names OR c.id IN $type_names
RETURN DISTINCT e.id AS id, coalesce(e.title, e.display_name, e.name) AS title,
       e.primary_type AS primary_type,
       coalesce(maker.display_name, maker.name) AS related_name
ORDER BY e.id
LIMIT $limit
""".strip()

RELATED_ENTITY_NAME_FILTER_CYPHER = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names OR c.id IN $type_names
MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg})-[:SUBJECT]->(e)
MATCH (a)-[:OBJECT]->(t:Entity {tenant_id: $tenant_id, kg: $kg})
MATCH (a)-[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg})
WHERE p.name = $rel_attr
  AND (
    toLower(coalesce(t.display_name, '')) = toLower($target_name)
    OR toLower(coalesce(t.name, '')) = toLower($target_name)
    OR toLower(replace(coalesce(t.name, ''), '_', ' ')) = toLower($target_name)
    OR toLower(coalesce(t.display_name, t.name, '')) CONTAINS toLower($target_name)
  )
RETURN DISTINCT e.id AS id, coalesce(e.title, e.name) AS title,
       e.primary_type AS primary_type,
       coalesce(t.display_name, t.name) AS related_name
ORDER BY e.id
LIMIT $limit
""".strip()

# Object Assertions are SoT (SUBJECT → PREDICATE → OBJECT). Typed shortcut
# relationships are a **derived** dual-write from assert_fact / apply_facts
# (not independent truth); this template reads Assertions, not arbitrary rels.
RELATED_ENTITIES_CYPHER = """
MATCH (from_e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(fc:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE fc.name IN $from_types OR fc.id IN $from_types
MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg})-[:SUBJECT]->(from_e)
MATCH (a)-[:OBJECT]->(to_e:Entity {tenant_id: $tenant_id, kg: $kg})
MATCH (a)-[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg})
WHERE ($rel_attr IS NULL OR p.name = $rel_attr)
OPTIONAL MATCH (to_e)-[:INSTANCE_OF]->(tc:Class {tenant_id: $tenant_id, kg: $kg})
WITH DISTINCT from_e, to_e, p,
     collect(DISTINCT tc.name) AS tc_names,
     collect(DISTINCT tc.id) AS tc_ids
WHERE $to_types IS NULL
   OR any(n IN tc_names WHERE n IN $to_types)
   OR any(i IN tc_ids WHERE i IN $to_types)
   OR to_e.primary_type IN $to_types
RETURN from_e.id AS from_id, from_e.name AS from_name, from_e.primary_type AS from_type,
       to_e.id AS to_id, to_e.name AS to_name, to_e.primary_type AS to_type,
       p.name AS rel_type, p.name AS attr
ORDER BY from_id, to_id
LIMIT $limit
""".strip()

ASSERTIONS_FOR_SUBJECT_CYPHER = """
MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg, subject_id: $entity_id})
WHERE $prop_id IS NULL OR a.property_id = $prop_id
OPTIONAL MATCH (a)-[:OBJECT]->(o:Entity)
OPTIONAL MATCH (a)-[:OBJECT_CLASS]->(oc:Class)
RETURN a.id AS assertion_id,
       a.subject_id AS subject_id,
       a.property_id AS property_id,
       a.literal_value AS literal_value,
       a.literal_datatype AS literal_datatype,
       a.source_url AS source_url,
       a.verified_at AS verified_at,
       a.run_id AS run_id,
       a.confidence AS confidence,
       a.provenance AS provenance,
       o.id AS object_id,
       oc.id AS object_class_id
ORDER BY a.property_id, a.id
""".strip()

# Prefer :Class hierarchy (ADR 0013). ``$layer`` filters Class.layer when set.
# OntoType remains dual-written for legacy catalog readers until cutover.
SUBCLASS_OF_CLOSURE_CYPHER = """
MATCH (c:Class {tenant_id: $tenant_id, kg: $kg})
WHERE ($layer IS NULL OR c.layer = $layer)
  AND (
    c.name = $type_name
    OR (c)-[:SUBCLASS_OF*1..]->(:Class {
      tenant_id: $tenant_id, kg: $kg, name: $type_name
    })
  )
RETURN DISTINCT c.name AS type_name
ORDER BY c.name
""".strip()

# Class-node form (ADR 0013) — descendants of class_id including self.
CLASS_SUBCLASS_DESCENDANTS_CYPHER = """
MATCH (c:Class {tenant_id: $tenant_id, kg: $kg})
WHERE c.id = $class_id
   OR (c)-[:SUBCLASS_OF*1..]->(:Class {tenant_id: $tenant_id, kg: $kg, id: $class_id})
RETURN DISTINCT c.id AS id
""".strip()

SUBPROPERTY_DESCENDANTS_CYPHER = """
MATCH (p:Property {tenant_id: $tenant_id, kg: $kg})
WHERE p.id = $prop_id
   OR (p)-[:SUBPROPERTY_OF*1..]->(:Property {tenant_id: $tenant_id, kg: $kg, id: $prop_id})
RETURN DISTINCT p.id AS id
""".strip()


def descendants_of(root: str, child_to_parent: Mapping[str, str]) -> list[str]:
    """Return ``root`` plus all names that transitively subclass it.

    ``child_to_parent`` maps child leaf → parent leaf (OntoType / Class names).
    Stable order: root first, then remaining names sorted alphabetically
    (matches NL fixture expectations for deterministic ``$type_names``).
    """
    if not root:
        return []
    children: dict[str, list[str]] = {}
    for child, parent in child_to_parent.items():
        if parent:
            children.setdefault(str(parent), []).append(str(child))
    seen: set[str] = {root}
    stack = list(children.get(root, []))
    while stack:
        c = stack.pop()
        if c in seen:
            continue
        seen.add(c)
        stack.extend(children.get(c, []))
    rest = sorted(n for n in seen if n != root)
    return [root, *rest]


_TYPE_LINE_RE = re.compile(
    r"(?im)^\s*Type:\s*([A-Za-z][A-Za-z0-9_]*)\b.*$"
)
_PARENT_FIELD_RE = re.compile(r"(?im)^\s*parent:\s*([A-Za-z][A-Za-z0-9_]*)\b")


def extract_subclass_map_from_ontology(ontology_summary: str) -> dict[str, str]:
    """Parse ``Type: Child`` + ``parent: Parent`` lines into child→parent map."""
    child_to_parent: dict[str, str] = {}
    if not ontology_summary:
        return child_to_parent
    current: str | None = None
    for line in ontology_summary.splitlines():
        tm = _TYPE_LINE_RE.match(line)
        if tm:
            current = tm.group(1)
            continue
        if current is None:
            continue
        pm = _PARENT_FIELD_RE.match(line)
        if pm:
            child_to_parent[current] = pm.group(1)
            current = None
    return child_to_parent


def type_names_with_subclasses(
    type_name: str,
    *,
    ontology_summary: str = "",
    child_to_parent: Mapping[str, str] | None = None,
    include_subclasses: bool = True,
) -> list[str]:
    """Expand a type leaf to the list bound as ``$type_names`` for NL helpers."""
    if not type_name:
        return []
    if not include_subclasses:
        return [type_name]
    mapping = (
        dict(child_to_parent)
        if child_to_parent is not None
        else extract_subclass_map_from_ontology(ontology_summary)
    )
    if not mapping:
        return [type_name]
    return descendants_of(type_name, mapping)


def semantic_templates() -> dict[str, tuple[str, bool]]:
    """Map template name → (cypher, writing) for registry / tests."""
    return {
        TEMPLATE_ENTITIES_OF_TYPE: (ENTITIES_OF_TYPE_CYPHER, False),
        TEMPLATE_ENTITIES_OF_TYPE_COUNT: (ENTITIES_OF_TYPE_COUNT_CYPHER, False),
        TEMPLATE_LITERAL_VALUES: (LITERAL_VALUES_CYPHER, False),
        TEMPLATE_RELATED_ENTITIES: (RELATED_ENTITIES_CYPHER, False),
        TEMPLATE_ASSERTIONS_FOR_SUBJECT: (ASSERTIONS_FOR_SUBJECT_CYPHER, False),
        TEMPLATE_SUBCLASS_OF_CLOSURE: (SUBCLASS_OF_CLOSURE_CYPHER, False),
    }


def subclass_of(
    store: AssertionMemoryStore,
    class_id: str,
    *,
    tenant_id: str,
    kg: str,
    direction: str = "ancestors",
    transitive: bool = True,
) -> list[str]:
    """Walk ``SUBCLASS_OF`` from ``class_id``.

    Parameters
    ----------
    direction:
        ``ancestors`` — parents of this class (toward superclasses).
        ``descendants`` — classes that subclass this class (transitive children).
    transitive:
        When False, only the immediate parent (ancestors) or children
        (descendants).
    """
    if direction not in ("ancestors", "descendants"):
        raise GraphScopeError("direction must be ancestors|descendants")
    if direction == "ancestors":
        out: list[str] = []
        cur = class_id
        seen: set[str] = set()
        while True:
            parent = store.subclass_parent(tenant_id, kg, cur)
            if not parent or parent in seen:
                break
            out.append(parent)
            seen.add(parent)
            if not transitive:
                break
            cur = parent
        return out

    # descendants: invert the parent map
    children: dict[str, list[str]] = {}
    for cid in store.all_class_ids(tenant_id, kg):
        parent = store.subclass_parent(tenant_id, kg, cid)
        if parent:
            children.setdefault(parent, []).append(cid)

    out = []
    stack = list(children.get(class_id, []))
    seen = set()
    while stack:
        c = stack.pop()
        if c in seen:
            continue
        seen.add(c)
        out.append(c)
        if transitive:
            stack.extend(children.get(c, []))
    return out


def subclass_of_closure(
    store: AssertionMemoryStore,
    class_ids: Sequence[str],
    *,
    tenant_id: str,
    kg: str,
    include_self: bool = True,
) -> set[str]:
    """Set of Class ids in the descendant closure of ``class_ids`` (for type query).

    ``entities_of_type(T, include_subclasses=True)`` uses this: an entity whose
    asserted type is a **descendant** of T matches T.
    """
    result: set[str] = set()
    for cid in class_ids:
        if include_self:
            result.add(cid)
        for d in subclass_of(
            store, cid, tenant_id=tenant_id, kg=kg, direction="descendants", transitive=True
        ):
            result.add(d)
    return result


def asserted_types(
    store: AssertionMemoryStore,
    entity_id: str,
    *,
    tenant_id: str,
    kg: str,
) -> list[dict[str, Any]]:
    """Asserted type Class ids / names for one entity (no ancestor fill)."""
    type_prop = type_membership_property_id()
    rows: list[dict[str, Any]] = []
    for a in store.list_assertions(tenant_id=tenant_id, kg=kg):
        if a.subject_id != entity_id:
            continue
        if a.property_id != type_prop:
            continue
        cid = a.object_class_id
        if not cid:
            continue
        rows.append(
            {
                "entity_id": entity_id,
                "class_id": cid,
                "type_name": store.class_name(tenant_id, kg, cid),
            }
        )
    return rows


def entities_of_type(
    store: AssertionMemoryStore,
    class_id_or_name: str,
    *,
    tenant_id: str,
    kg: str,
    include_subclasses: bool = True,
) -> list[dict[str, Any]]:
    """Entities with a type Assertion matching ``class_id`` (optional subclass fill).

    Uses type Assertions / INSTANCE_OF cache; never invents ancestor type
    Assertions. When ``include_subclasses`` is True, an entity asserted as a
    **descendant** of the query class is included.
    """
    class_id = store.resolve_class_id(tenant_id, kg, class_id_or_name)
    if not class_id:
        return []

    if include_subclasses:
        allowed = subclass_of_closure(
            store, [class_id], tenant_id=tenant_id, kg=kg, include_self=True
        )
    else:
        allowed = {class_id}

    type_prop = type_membership_property_id()
    # entity_id → set of matching class ids from type assertions
    matched: dict[str, set[str]] = {}
    for a in store.list_assertions(tenant_id=tenant_id, kg=kg):
        if a.property_id != type_prop:
            continue
        if not a.object_class_id or a.object_class_id not in allowed:
            continue
        matched.setdefault(a.subject_id, set()).add(a.object_class_id)

    # Cross-check INSTANCE_OF cache (derived) — Assertions win if skew, but
    # include cache-only only when assertion already listed (never invent).
    rows: list[dict[str, Any]] = []
    for eid, cids in sorted(matched.items()):
        ent = store.get_entity(tenant_id, kg, eid)
        rows.append(
            {
                "entity_id": eid,
                "name": ent.name if ent else None,
                "type_ids": sorted(cids),
                "type_names": sorted(
                    n
                    for n in (
                        store.class_name(tenant_id, kg, c) for c in cids
                    )
                    if n
                ),
            }
        )
    return rows


def count_entities_of_type(
    store: AssertionMemoryStore,
    class_id_or_name: str,
    *,
    tenant_id: str,
    kg: str,
    include_subclasses: bool = True,
) -> list[dict[str, Any]]:
    """``[{count: N}]`` — answer shape for GQ-01 / GQ-02."""
    ents = entities_of_type(
        store,
        class_id_or_name,
        tenant_id=tenant_id,
        kg=kg,
        include_subclasses=include_subclasses,
    )
    return [{"count": len(ents)}]


def assertions_for_subject(
    store: AssertionMemoryStore,
    entity_id: str,
    *,
    tenant_id: str,
    kg: str,
    property_id: str | None = None,
    property_name: str | None = None,
) -> list[dict[str, Any]]:
    """Assertion rows for one subject, optionally filtered by property."""
    prop_filter: str | None = property_id
    if prop_filter is None and property_name:
        prop_filter = store.resolve_property_id(tenant_id, kg, property_name)

    rows: list[dict[str, Any]] = []
    for a in store.list_assertions(tenant_id=tenant_id, kg=kg):
        if a.subject_id != entity_id:
            continue
        if prop_filter is not None and a.property_id != prop_filter:
            continue
        rows.append(_project_assertion(store, a, tenant_id=tenant_id, kg=kg))
    return rows


def literal_value(assertion: AssertionNode | MappingLike) -> Any:
    """Project the datatype object of an Assertion."""
    if isinstance(assertion, AssertionNode):
        return assertion.literal_value
    return assertion.get("literal_value")  # type: ignore[union-attr]


def object_value(assertion: AssertionNode | MappingLike) -> str | None:
    """Project the object Entity id of an object-property Assertion."""
    if isinstance(assertion, AssertionNode):
        return assertion.object_id
    return assertion.get("object_id")  # type: ignore[union-attr]


def fact_provenance(
    store: AssertionMemoryStore,
    assertion_id: str,
    *,
    tenant_id: str,
    kg: str,
) -> list[dict[str, Any]]:
    """Provenance fields for one Assertion (GQ-06)."""
    a = store.get_assertion(tenant_id, kg, assertion_id)
    if a is None:
        return []
    return [
        {
            "assertion_id": a.id,
            "subject_id": a.subject_id,
            "property": store.property_name(tenant_id, kg, a.property_id)
            or a.property_id,
            "property_id": a.property_id,
            "value": a.literal_value if a.literal_value is not None else a.object_id,
            "source_url": a.source_url,
            "verified_at": a.verified_at,
            "run_id": a.run_id,
            "confidence": a.confidence,
            "provenance": a.provenance,
        }
    ]


def reverse_object_assertions(
    store: AssertionMemoryStore,
    object_entity_id: str,
    *,
    tenant_id: str,
    kg: str,
    property_id: str | None = None,
    property_name: str | None = None,
) -> list[dict[str, Any]]:
    """Subjects that point at ``object_entity_id`` via an object property (GQ-05)."""
    prop_filter: str | None = property_id
    if prop_filter is None and property_name:
        prop_filter = store.resolve_property_id(tenant_id, kg, property_name)

    rows: list[dict[str, Any]] = []
    for a in store.list_assertions(tenant_id=tenant_id, kg=kg):
        if a.object_id != object_entity_id:
            continue
        if prop_filter is not None and a.property_id != prop_filter:
            continue
        rows.append(
            {
                "subject_id": a.subject_id,
                "object_id": a.object_id,
                "property": store.property_name(tenant_id, kg, a.property_id)
                or a.property_id,
            }
        )
    return rows


def parent_classes(
    store: AssertionMemoryStore,
    class_id_or_name: str,
    *,
    tenant_id: str,
    kg: str,
    transitive: bool = True,
) -> list[dict[str, Any]]:
    """Parent Class ids/names for catalog hierarchy (GQ-10)."""
    class_id = store.resolve_class_id(tenant_id, kg, class_id_or_name)
    if not class_id:
        return []
    parents = subclass_of(
        store,
        class_id,
        tenant_id=tenant_id,
        kg=kg,
        direction="ancestors",
        transitive=transitive,
    )
    return [
        {
            "class_id": pid,
            "type_name": store.class_name(tenant_id, kg, pid),
        }
        for pid in parents
    ]


def entities_with_literal_filter(
    store: AssertionMemoryStore,
    class_id_or_name: str,
    property_name: str,
    *,
    tenant_id: str,
    kg: str,
    op: str = ">",
    value: Any = None,
    include_subclasses: bool = True,
) -> list[dict[str, Any]]:
    """Compose type membership + literal assertion filter (GQ-12)."""
    candidates = entities_of_type(
        store,
        class_id_or_name,
        tenant_id=tenant_id,
        kg=kg,
        include_subclasses=include_subclasses,
    )
    prop_id = store.resolve_property_id(tenant_id, kg, property_name)
    if not prop_id:
        return []

    out: list[dict[str, Any]] = []
    for row in candidates:
        eid = row["entity_id"]
        for a in store.list_assertions(tenant_id=tenant_id, kg=kg):
            if a.subject_id != eid or a.property_id != prop_id:
                continue
            lit = a.literal_value
            if lit is None:
                continue
            if _compare(lit, op, value):
                out.append({"entity_id": eid, "value": lit})
                break
    return out


# --- internal ---------------------------------------------------------------

# Typing alias for duck-typed assertion maps
MappingLike = Any


def _project_assertion(
    store: AssertionMemoryStore,
    a: AssertionNode,
    *,
    tenant_id: str,
    kg: str,
) -> dict[str, Any]:
    return {
        "assertion_id": a.id,
        "subject_id": a.subject_id,
        "entity_id": a.subject_id,
        "property_id": a.property_id,
        "property": store.property_name(tenant_id, kg, a.property_id) or a.property_id,
        "literal_value": a.literal_value,
        "value": a.literal_value if a.literal_value is not None else a.object_id,
        "object_id": a.object_id,
        "object_class_id": a.object_class_id,
        "source_url": a.source_url,
        "verified_at": a.verified_at,
        "run_id": a.run_id,
        "confidence": a.confidence,
        "provenance": a.provenance,
    }


def _compare(left: Any, op: str, right: Any) -> bool:
    try:
        if op == ">":
            return left > right
        if op == ">=":
            return left >= right
        if op == "<":
            return left < right
        if op == "<=":
            return left <= right
        if op in ("=", "==", "eq"):
            return canonical_literal(left) == canonical_literal(right)
        if op in ("!=", "ne"):
            return canonical_literal(left) != canonical_literal(right)
    except TypeError:
        return False
    raise GraphScopeError(f"unsupported compare op {op!r}")


def project_rows(
    rows: Sequence[dict[str, Any]],
    columns: Sequence[str] | None,
) -> list[dict[str, Any]]:
    """Strip helper debug columns to the gold-defined column set."""
    if not columns:
        return [dict(r) for r in rows]
    return [{c: r.get(c) for c in columns} for r in rows]


# ---------------------------------------------------------------------------
# GraphSession async helpers (Memory / Neo4j — native methods preferred)
# ---------------------------------------------------------------------------


async def subclass_closure(
    session: "GraphSession",
    class_id: str,
    *,
    include_self: bool = True,
) -> list[str]:
    """Class IRIs: query class plus descendants (Person ⊑ Agent → Person in Agent)."""
    native = getattr(session, "read_subclass_closure", None)
    if callable(native):
        ids = list(await native(class_id))
    else:
        rows = await session.execute_read(
            CLASS_SUBCLASS_DESCENDANTS_CYPHER, {"class_id": class_id}
        )
        ids = [str(r.get("id")) for r in rows if r.get("id")]
    if include_self and class_id not in ids:
        ids = [class_id, *ids]
    if not include_self:
        ids = [i for i in ids if i != class_id]
    return ids


async def subproperty_closure(
    session: "GraphSession",
    prop_id: str,
    *,
    include_self: bool = True,
) -> list[str]:
    """Property IRIs: query property plus sub-properties."""
    native = getattr(session, "read_subproperty_closure", None)
    if callable(native):
        ids = list(await native(prop_id))
    else:
        rows = await session.execute_read(
            SUBPROPERTY_DESCENDANTS_CYPHER, {"prop_id": prop_id}
        )
        ids = [str(r.get("id")) for r in rows if r.get("id")]
    if include_self and prop_id not in ids:
        ids = [prop_id, *ids]
    if not include_self:
        ids = [i for i in ids if i != prop_id]
    return ids


async def session_entities_of_type(
    session: "GraphSession",
    class_id: str,
    *,
    include_subclasses: bool = True,
) -> list[str]:
    """Entity IRIs typed as ``class_id`` (optional subclass closure)."""
    if include_subclasses:
        class_ids = await subclass_closure(session, class_id, include_self=True)
    else:
        class_ids = [class_id]
    native = getattr(session, "read_entities_of_type", None)
    if callable(native):
        return list(await native(class_ids))
    return []


async def session_assertions_for_subject(
    session: "GraphSession",
    entity_id: str,
    *,
    prop_id: str | None = None,
) -> list[dict[str, Any]]:
    """Assertion dicts for one subject within session scope."""
    native = getattr(session, "read_assertions_for_subject", None)
    if callable(native):
        return list(await native(entity_id, prop_id=prop_id))
    rows = await session.execute_read(
        ASSERTIONS_FOR_SUBJECT_CYPHER,
        {"entity_id": entity_id, "prop_id": prop_id},
    )
    return [r.to_dict() if hasattr(r, "to_dict") else dict(r) for r in rows]


def assertion_to_history_row(row: MappingLike) -> dict[str, Any]:
    """Project an Assertion provenance dict into the GET ``/history`` change shape.

    Neo4j has no companion value-history graph yet (temporal ``old → new``
    :ValueHistory is deferred). Until that lands, the store path treats
    **current Assertion provenance** as the history feed:

    * ``subject`` / ``predicate`` — Assertion subject + property IRIs
    * ``new_value`` — current literal / object id
    * ``old_value`` — empty (no prior-value log on Assertion SoT alone)
    * ``changed_at`` — ``verified_at`` when present, else empty

    Same keys as :class:`infona_client.graph.history.ValueChange` so the dual-
    backend route can reuse one response builder.
    """
    if hasattr(row, "to_dict"):
        row = row.to_dict()  # type: ignore[assignment]
    data = dict(row) if not isinstance(row, dict) else row
    value = data.get("literal_value")
    if value is None:
        value = data.get("object_id") or data.get("object_class_id") or ""
    return {
        "subject": str(data.get("subject_id") or ""),
        "predicate": str(data.get("property_id") or ""),
        "old_value": "",
        "new_value": "" if value is None else str(value),
        "changed_at": str(data.get("verified_at") or ""),
        # Provenance extras (route may omit; helpers / clients may use):
        "source_url": data.get("source_url"),
        "provenance": data.get("provenance"),
        "assertion_id": data.get("assertion_id") or data.get("id"),
    }


def _since_passes(verified_at: str | None, since: str | None) -> bool:
    """``True`` when the row is strictly after ``since`` (Neptune FILTER ``>``)."""
    if not since:
        return True
    va = (verified_at or "").strip()
    if not va:
        return False
    return va > since


async def session_assertion_history(
    session: "GraphSession",
    *,
    entity_id: str | None = None,
    prop_id: str | None = None,
    since: str | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """List Assertion provenance as history-shaped rows (neo4j dual-backend).

    Preferred path: subject-scoped via :func:`session_assertions_for_subject`
    (or native ``read_assertion_history`` when the session implements a full-KG
    scan). Without a subject and without a native scan, returns ``[]`` — never
    invents cross-scope rows.
    """
    lim = max(1, min(int(limit), 10000))
    native = getattr(session, "read_assertion_history", None)
    raw_rows: list[Any]
    if callable(native):
        raw_rows = list(
            await native(
                entity_id=entity_id,
                prop_id=prop_id,
                since=since,
                limit=lim,
            )
        )
    elif entity_id:
        raw_rows = await session_assertions_for_subject(
            session, entity_id, prop_id=prop_id
        )
    else:
        return []

    projected: list[dict[str, Any]] = []
    for r in raw_rows:
        d = r.to_dict() if hasattr(r, "to_dict") else dict(r)
        if prop_id is not None and d.get("property_id") != prop_id:
            continue
        if not _since_passes(d.get("verified_at"), since):
            continue
        projected.append(assertion_to_history_row(d))

    projected.sort(
        key=lambda x: (
            x.get("changed_at") or "",
            x.get("predicate") or "",
            x.get("subject") or "",
            str(x.get("assertion_id") or ""),
        )
    )
    return projected[:lim]


async def session_literal_values(
    session: "GraphSession",
    entity_id: str,
    prop_id: str,
) -> list[Any]:
    rows = await session_assertions_for_subject(session, entity_id, prop_id=prop_id)
    return [r["literal_value"] for r in rows if r.get("literal_value") is not None]


async def session_object_values(
    session: "GraphSession",
    entity_id: str,
    prop_id: str,
) -> list[str]:
    rows = await session_assertions_for_subject(session, entity_id, prop_id=prop_id)
    return [str(r["object_id"]) for r in rows if r.get("object_id")]


__all__ = [
    "ASSERTIONS_FOR_SUBJECT_CYPHER",
    "CLASS_SUBCLASS_DESCENDANTS_CYPHER",
    "ENTITIES_OF_TYPE_COUNT_CYPHER",
    "ENTITIES_OF_TYPE_CYPHER",
    "LITERAL_VALUES_CYPHER",
    "RELATED_ENTITIES_CYPHER",
    "RELATED_ENTITY_NAME_FILTER_CYPHER",
    "RELATED_ENTITY_NAME_FILTER_INVERSE_CYPHER",
    "SUBCLASS_OF_CLOSURE_CYPHER",
    "SUBPROPERTY_DESCENDANTS_CYPHER",
    "TEMPLATE_ASSERTIONS_FOR_SUBJECT",
    "TEMPLATE_ENTITIES_OF_TYPE",
    "TEMPLATE_ENTITIES_OF_TYPE_COUNT",
    "TEMPLATE_LITERAL_COMPARE",
    "TEMPLATE_LITERAL_VALUES",
    "TEMPLATE_RELATED_ENTITIES",
    "TEMPLATE_RELATED_ENTITY_NAME_FILTER",
    "TEMPLATE_SUBCLASS_OF_CLOSURE",
    "LITERAL_COMPARE_CYPHER",
    "assertion_to_history_row",
    "assertions_for_subject",
    "asserted_types",
    "count_entities_of_type",
    "descendants_of",
    "entities_of_type",
    "entities_with_literal_filter",
    "extract_subclass_map_from_ontology",
    "fact_provenance",
    "literal_value",
    "object_value",
    "parent_classes",
    "project_rows",
    "reverse_object_assertions",
    "semantic_templates",
    "session_assertion_history",
    "session_assertions_for_subject",
    "session_entities_of_type",
    "session_literal_values",
    "session_object_values",
    "subclass_closure",
    "subclass_of",
    "subclass_of_closure",
    "subproperty_closure",
    "type_names_with_subclasses",
]

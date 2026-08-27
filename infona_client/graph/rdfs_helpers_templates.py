"""ADR 0013 Cypher templates + type-name / subclass expansion helpers.

Defined here (not on the facade) so memory_store / schema_bootstrap can
import the constants without pulling AssertionMemoryStore (import cycles).

Implementation sibling of :mod:`infona_client.graph.rdfs_helpers`.
"""

from __future__ import annotations

import re
from typing import Mapping, Sequence

from infona_client.graph.current_facts import (
    CURRENT_INTERVAL_KEEP_CYPHER,
    CURRENT_INTERVAL_OPTIONAL_CYPHER,
    current_interval_keep_cypher,
    current_interval_scan_cypher,
)

# ---------------------------------------------------------------------------
# Template names (ADR 0013 — NL fixtures + schema_bootstrap registry keys)
# ---------------------------------------------------------------------------

TEMPLATE_ENTITIES_OF_TYPE = "entities_of_type"
TEMPLATE_ENTITIES_OF_TYPE_COUNT = "entities_of_type_count"
TEMPLATE_LITERAL_VALUES = "literal_values"
TEMPLATE_LITERAL_VALUES_COUNT = "literal_values_count"
TEMPLATE_LITERAL_COMPARE = "literal_compare"
TEMPLATE_LITERAL_COMPARE_COUNT = "literal_compare_count"
TEMPLATE_RELATED_ENTITY_NAME_FILTER_INVERSE = "related_entity_name_filter_inverse"
TEMPLATE_LITERAL_AGGREGATE = "literal_aggregate"
TEMPLATE_LITERAL_ARGMAX_BY_DIM = "literal_argmax_by_dim"
TEMPLATE_LITERAL_DISTINCT_COUNT = "literal_distinct_count"
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
_LITERAL_VALUES_MATCH = (
    """
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
"""
    + CURRENT_INTERVAL_OPTIONAL_CYPHER
    + """
WITH DISTINCT e, a, v
WHERE """
    + CURRENT_INTERVAL_KEEP_CYPHER
    + """ AND (
  a IS NOT NULL OR (
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
)
"""
).strip()

LITERAL_VALUES_CYPHER = (
    _LITERAL_VALUES_MATCH
    + """
RETURN e.id AS id, e.name AS name, e.primary_type AS primary_type,
       coalesce(a.literal_value, e[$prop_key]) AS literal_value
ORDER BY e.id
LIMIT $limit
"""
).strip()

LITERAL_VALUES_COUNT_CYPHER = (
    _LITERAL_VALUES_MATCH
    + """
RETURN count(DISTINCT e) AS n
"""
).strip()

# Shared Assertion SoT + current-interval filter for numeric/agg helpers.
# Closed ValidityInterval rows drop out; missing interval stays current.
_LITERAL_RAW_CURRENT = (
    """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names OR c.id IN $type_names
OPTIONAL MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg, subject_id: e.id})
  -[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg})
WHERE p.name = $prop_key
"""
    + CURRENT_INTERVAL_OPTIONAL_CYPHER
    + """
WITH e, a, v, coalesce(a.literal_value, e[$prop_key]) AS raw
WHERE raw IS NOT NULL
  AND """
    + CURRENT_INTERVAL_KEEP_CYPHER
).strip()

# Numeric / inequality compare on a datatype property. Handles both native
# store numbers and legacy SPARQL-era ``lexical^^xsd-uri`` strings still in
# older graphs (split off the suffix before toFloat).
_LITERAL_COMPARE_MATCH = (
    _LITERAL_RAW_CURRENT
    + """
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
"""
).strip()

LITERAL_COMPARE_CYPHER = (
    _LITERAL_COMPARE_MATCH
    + """
RETURN e.id AS id, e.name AS name, e.primary_type AS primary_type,
       coalesce(e.title, e.name) AS title, num AS value
ORDER BY num, e.id
LIMIT $limit
"""
).strip()

LITERAL_COMPARE_COUNT_CYPHER = (
    _LITERAL_COMPARE_MATCH
    + """
RETURN count(DISTINCT e) AS n
"""
).strip()


# Aggregate (sum/avg/min/max) over a datatype property — Assertion SoT + denorm.
# $agg_op is one of sum|avg|min|max (allowlisted by the fixture, never free text).
LITERAL_AGGREGATE_CYPHER = (
    _LITERAL_RAW_CURRENT
    + """
WITH e, toFloat(
  CASE
    WHEN toString(raw) CONTAINS '^^' THEN split(toString(raw), '^^')[0]
    ELSE toString(raw)
  END
) AS num
WHERE num IS NOT NULL
WITH e, max(num) AS num
RETURN CASE
  WHEN $agg_op = 'sum' THEN sum(num)
  WHEN $agg_op = 'avg' THEN avg(num)
  WHEN $agg_op = 'min' THEN min(num)
  WHEN $agg_op = 'max' THEN max(num)
  ELSE null
END AS value
"""
).strip()

# Group by a datatype leaf, SUM a measure, return the dim with the max sum.
# No equality filter in this helper (constrain first, or free-form).
# Group key uses the same current-interval filter as the measure (closed HQ
# must not remain a dim bucket after Austin beats SF).
LITERAL_ARGMAX_BY_DIM_CYPHER = (
    _LITERAL_RAW_CURRENT
    + """
WITH e, toFloat(
  CASE
    WHEN toString(raw) CONTAINS '^^' THEN split(toString(raw), '^^')[0]
    ELSE toString(raw)
  END
) AS num
WHERE num IS NOT NULL
WITH e, max(num) AS num
"""
    + current_interval_scan_cypher(
        leaf="$group_key", value="e[$group_key]", alias="gv"
    )
    + """
WITH e, num, e[$group_key] AS grp_raw, gv
WHERE """
    + current_interval_keep_cypher("gv")
    + """
WITH coalesce(toString(grp_raw), '') AS grp, sum(num) AS total
WHERE grp <> ''
RETURN grp AS name, total AS value
ORDER BY total DESC, grp ASC
LIMIT 1
"""
).strip()

# Distinct non-empty values of a datatype leaf (not DISTINCT entities).
LITERAL_DISTINCT_COUNT_CYPHER = (
    _LITERAL_RAW_CURRENT
    + """
WITH e, CASE
  WHEN toString(raw) CONTAINS '^^' THEN split(toString(raw), '^^')[0]
  ELSE toString(raw)
END AS val
WHERE val <> ''
WITH e, max(val) AS val
RETURN count(DISTINCT val) AS n
"""
).strip()

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
RETURN DISTINCT e.id AS id, coalesce(e.display_name, e.title, e.name) AS title,
       e.primary_type AS primary_type,
       coalesce(t.display_name, t.title, t.name) AS related_name
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
RETURN from_e.id AS from_id,
       coalesce(from_e.display_name, from_e.title, from_e.name) AS from_name,
       from_e.primary_type AS from_type,
       to_e.id AS to_id,
       coalesce(to_e.display_name, to_e.title, to_e.name) AS to_name,
       to_e.primary_type AS to_type,
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


_ATTR_DASH_RE = re.compile(
    r"(?im)^\s*-\s*([A-Za-z_][A-Za-z0-9_]*)\s*:"
)
_ATTR_KEY_RE = re.compile(r"\bkey=([A-Za-z_][A-Za-z0-9_]*)")


def declared_attr_leaves_by_type(ontology_summary: str) -> dict[str, set[str]]:
    """Parse ``Type:`` blocks into type → declared literal leaf names.

    Used so Person.first_name binds Contact.first_name but not Staff.first_name
    when Staff only declared ``name`` (INF-599).
    """
    out: dict[str, set[str]] = {}
    current: str | None = None
    for line in (ontology_summary or "").splitlines():
        tm = _TYPE_LINE_RE.match(line)
        if tm:
            current = tm.group(1)
            out.setdefault(current, set())
            continue
        if current is None:
            continue
        am = _ATTR_DASH_RE.match(line)
        if am:
            out[current].add(am.group(1))
        for km in _ATTR_KEY_RE.finditer(line):
            out[current].add(km.group(1))
    return out


def subclass_attribute_predicates(
    parent_type: str,
    attr_leaf: str,
    *,
    child_to_parent: Mapping[str, str] | None = None,
    ontology_summary: str = "",
    declared_by_type: Mapping[str, Sequence[str]] | None = None,
) -> list[str]:
    """Bind ``types/<Child>/attrs/<leaf>`` for a parent and its descendants.

    Instance triples stay on the asserted leaf (ADR 0001): Contact.first_name
    is ``types/Contact/attrs/first_name``, never rewritten onto Person. A
    Person ask that only binds ``types/Person/attrs/first_name`` returns
    empty names. Callers must bind the child predicates too.

    When ``declared_by_type`` / ``ontology_summary`` is provided, a descendant
    is included only if it actually declares ``attr_leaf`` — Staff with
    ``name`` (not ``first_name``) is omitted.

    Order matches :func:`type_names_with_subclasses` (parent first, then
    children alphabetically).
    """
    from infona_client.graph.ontology_queries_uris import attr_uri

    if not parent_type or not attr_leaf:
        return []
    types = type_names_with_subclasses(
        parent_type,
        ontology_summary=ontology_summary,
        child_to_parent=child_to_parent,
        include_subclasses=True,
    )
    declared = declared_by_type
    if declared is None and ontology_summary:
        declared = declared_attr_leaves_by_type(ontology_summary)
    leaf_l = attr_leaf.strip().lower()
    out: list[str] = []
    for t in types:
        if declared is not None:
            leaves = {str(x).strip().lower() for x in (declared.get(t) or ())}
            if leaf_l not in leaves:
                continue
        try:
            out.append(attr_uri(t, attr_leaf))
        except ValueError:
            continue
    return out


def bind_subclass_attribute(
    parent_type: str,
    attr_leaf: str,
    *,
    child_to_parent: Mapping[str, str] | None = None,
    ontology_summary: str = "",
    declared_by_type: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, object]:
    """Planner bind for a parent-type attribute ask (count/list/filter).

    ``type_names`` drives subclass-closure membership (how many people?).
    ``predicates`` is the SPARQL-era attr IRI list the NL layer must use
    instead of only ``types/<Parent>/attrs/<leaf>``. ``prop_key`` is the
    Neo4j leaf (property-graph Facts flatten the type segment).
    """
    types = type_names_with_subclasses(
        parent_type,
        ontology_summary=ontology_summary,
        child_to_parent=child_to_parent,
        include_subclasses=True,
    )
    predicates = subclass_attribute_predicates(
        parent_type,
        attr_leaf,
        child_to_parent=child_to_parent,
        ontology_summary=ontology_summary,
        declared_by_type=declared_by_type,
    )
    return {
        "type_names": types,
        "attr_leaf": attr_leaf,
        "predicates": predicates,
        "prop_key": attr_leaf,
    }


def semantic_templates() -> dict[str, tuple[str, bool]]:
    """Map template name → (cypher, writing) for registry / tests."""
    return {
        TEMPLATE_ENTITIES_OF_TYPE: (ENTITIES_OF_TYPE_CYPHER, False),
        TEMPLATE_ENTITIES_OF_TYPE_COUNT: (ENTITIES_OF_TYPE_COUNT_CYPHER, False),
        TEMPLATE_LITERAL_VALUES: (LITERAL_VALUES_CYPHER, False),
        TEMPLATE_LITERAL_VALUES_COUNT: (LITERAL_VALUES_COUNT_CYPHER, False),
        TEMPLATE_LITERAL_COMPARE: (LITERAL_COMPARE_CYPHER, False),
        TEMPLATE_LITERAL_COMPARE_COUNT: (LITERAL_COMPARE_COUNT_CYPHER, False),
        TEMPLATE_LITERAL_AGGREGATE: (LITERAL_AGGREGATE_CYPHER, False),
        TEMPLATE_LITERAL_ARGMAX_BY_DIM: (LITERAL_ARGMAX_BY_DIM_CYPHER, False),
        TEMPLATE_LITERAL_DISTINCT_COUNT: (LITERAL_DISTINCT_COUNT_CYPHER, False),
        TEMPLATE_RELATED_ENTITIES: (RELATED_ENTITIES_CYPHER, False),
        TEMPLATE_RELATED_ENTITY_NAME_FILTER: (RELATED_ENTITY_NAME_FILTER_CYPHER, False),
        TEMPLATE_RELATED_ENTITY_NAME_FILTER_INVERSE: (
            RELATED_ENTITY_NAME_FILTER_INVERSE_CYPHER,
            False,
        ),
        TEMPLATE_ASSERTIONS_FOR_SUBJECT: (ASSERTIONS_FOR_SUBJECT_CYPHER, False),
        TEMPLATE_SUBCLASS_OF_CLOSURE: (SUBCLASS_OF_CLOSURE_CYPHER, False),
    }


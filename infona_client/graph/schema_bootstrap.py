"""Idempotent Neo4j constraints + indexes (ADR 0013 RDF-semantic model).

Apply once per database before instance writes that rely on uniqueness (G7).
Statement names are stable so re-runs are no-ops via ``IF NOT EXISTS``.

**ADR 0013 labels:** ``Entity``, ``Class``, ``Property``, ``Assertion`` with
unique ``(tenant_id, kg, id)``. Legacy ``OntoType`` / ``OntoAttr`` catalog
constraints remain for E4 readers until catalog fully maps onto Class/Property.

Callable from tests and process startup via :meth:`GraphStore.bootstrap_schema`
or :func:`bootstrap_schema_statements` for inspection / alternate drivers.

Also owns the **allowlisted Cypher template registry** used by
:meth:`GraphSession.execute_template` — the safe path for application writers
(Wave 1 isolation story: free-form ``execute_read``/``execute_write`` remain
for admin/bootstrap/tests only; see ``docs/neo4j-local.md``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Sequence

from infona_client.graph.facts import (
    ER_SIGNAL_PROPERTY_KEY_PREFIX,
    INTERNAL_PROPERTY_KEYS,
)
from infona_client.graph.rdfs_helpers import (
    ASSERTIONS_FOR_SUBJECT_CYPHER,
    ENTITIES_OF_TYPE_COUNT_CYPHER,
    ENTITIES_OF_TYPE_CYPHER,
    LITERAL_COMPARE_CYPHER,
    LITERAL_AGGREGATE_CYPHER,
    LITERAL_VALUES_CYPHER,
    RELATED_ENTITIES_CYPHER,
    RELATED_ENTITY_NAME_FILTER_CYPHER,
    RELATED_ENTITY_NAME_FILTER_INVERSE_CYPHER,
    SUBCLASS_OF_CLOSURE_CYPHER,
)

#: Bare-identifier guard for property-key names interpolated into a template.
_SAFE_PROPERTY_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# (name, cypher) — name is returned by bootstrap for logging / tests.
# Cypher uses Neo4j 5 IF NOT EXISTS so Community + Aura both accept it.
SCHEMA_STATEMENTS: tuple[tuple[str, str], ...] = (
    # --- Uniqueness (ADR 0013 §12) ---
    (
        "entity_tenant_kg_id_unique",
        "CREATE CONSTRAINT entity_tenant_kg_id_unique IF NOT EXISTS "
        "FOR (e:Entity) REQUIRE (e.tenant_id, e.kg, e.id) IS UNIQUE",
    ),
    (
        "class_tenant_kg_id_unique",
        "CREATE CONSTRAINT class_tenant_kg_id_unique IF NOT EXISTS "
        "FOR (c:Class) REQUIRE (c.tenant_id, c.kg, c.id) IS UNIQUE",
    ),
    (
        "property_tenant_kg_id_unique",
        "CREATE CONSTRAINT property_tenant_kg_id_unique IF NOT EXISTS "
        "FOR (p:Property) REQUIRE (p.tenant_id, p.kg, p.id) IS UNIQUE",
    ),
    (
        "assertion_tenant_kg_id_unique",
        "CREATE CONSTRAINT assertion_tenant_kg_id_unique IF NOT EXISTS "
        "FOR (a:Assertion) REQUIRE (a.tenant_id, a.kg, a.id) IS UNIQUE",
    ),
    # --- Legacy catalog (E4) ---
    (
        "onto_type_scope_unique",
        "CREATE CONSTRAINT onto_type_scope_unique IF NOT EXISTS "
        "FOR (t:OntoType) REQUIRE (t.tenant_id, t.kg, t.layer, t.name) IS UNIQUE",
    ),
    (
        "onto_attr_scope_unique",
        "CREATE CONSTRAINT onto_attr_scope_unique IF NOT EXISTS "
        "FOR (a:OntoAttr) REQUIRE (a.tenant_id, a.kg, a.layer, a.domain, a.name) IS UNIQUE",
    ),
    # --- Indexes ---
    (
        "entity_tenant_kg_primary_type",
        "CREATE INDEX entity_tenant_kg_primary_type IF NOT EXISTS "
        "FOR (e:Entity) ON (e.tenant_id, e.kg, e.primary_type)",
    ),
    (
        "entity_tenant_kg_name",
        "CREATE INDEX entity_tenant_kg_name IF NOT EXISTS "
        "FOR (e:Entity) ON (e.tenant_id, e.kg, e.name)",
    ),
    (
        "entity_tenant_kg_source",
        "CREATE INDEX entity_tenant_kg_source IF NOT EXISTS "
        "FOR (e:Entity) ON (e.tenant_id, e.kg, e.source)",
    ),
    (
        "assertion_subject_lookup",
        "CREATE INDEX assertion_subject_lookup IF NOT EXISTS "
        "FOR (a:Assertion) ON (a.tenant_id, a.kg, a.subject_id)",
    ),
    (
        "assertion_property_lookup",
        "CREATE INDEX assertion_property_lookup IF NOT EXISTS "
        "FOR (a:Assertion) ON (a.tenant_id, a.kg, a.property_id)",
    ),
    (
        "assertion_object_lookup",
        "CREATE INDEX assertion_object_lookup IF NOT EXISTS "
        "FOR (a:Assertion) ON (a.tenant_id, a.kg, a.object_id)",
    ),
    (
        "class_name_lookup",
        "CREATE INDEX class_name_lookup IF NOT EXISTS "
        "FOR (c:Class) ON (c.tenant_id, c.kg, c.name)",
    ),
    (
        "property_name_lookup",
        "CREATE INDEX property_name_lookup IF NOT EXISTS "
        "FOR (p:Property) ON (p.tenant_id, p.kg, p.name)",
    ),
    (
        "attr_citation_lookup",
        "CREATE INDEX attr_citation_lookup IF NOT EXISTS "
        "FOR (c:AttrCitation) ON (c.tenant_id, c.kg, c.entity_id)",
    ),
    (
        "prov_event_subject",
        "CREATE INDEX prov_event_subject IF NOT EXISTS "
        "FOR (p:ProvEvent) ON (p.tenant_id, p.kg, p.subject_id)",
    ),
    (
        "onto_type_layer_name",
        "CREATE INDEX onto_type_layer_name IF NOT EXISTS "
        "FOR (t:OntoType) ON (t.layer, t.name)",
    ),
)


def bootstrap_schema_statements() -> Sequence[tuple[str, str]]:
    """Return the ordered (name, cypher) pairs applied by bootstrap.

    Pure function for tests that assert the constraint plan without a driver.
    """
    return SCHEMA_STATEMENTS


# Minimal Cypher templates used by smoke tests + future kg_writer ports.
# Always include $tenant_id / $kg so session scope enforcement accepts them.
# Prefer session.execute_template(name, params) over pasting these strings.

ENTITY_MERGE_CYPHER = """
MERGE (e:Entity {tenant_id: $tenant_id, kg: $kg, id: $id})
ON CREATE SET
  e.primary_type = $primary_type,
  e.name = $name,
  e.source = $source,
  e.created_at = $ts,
  e.updated_at = $ts
ON MATCH SET
  e.primary_type = coalesce($primary_type, e.primary_type),
  e.name = coalesce($name, e.name),
  e.source = coalesce($source, e.source),
  e.updated_at = $ts
RETURN e.id AS id, e.tenant_id AS tenant_id, e.kg AS kg,
       e.primary_type AS primary_type, e.name AS name, e.source AS source
""".strip()

ENTITY_GET_CYPHER = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg, id: $id})
RETURN e.id AS id, e.tenant_id AS tenant_id, e.kg AS kg,
       e.primary_type AS primary_type, e.name AS name, e.source AS source
""".strip()

# Explore type filter via INSTANCE_OF → Class (ADR 0013). Param name
# ``primary_type`` is historical API; match is Class.name / Class.id, not the
# denorm Entity.primary_type property alone.
ENTITY_LIST_BY_TYPE_CYPHER = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name = $primary_type OR c.id = $primary_type
RETURN DISTINCT e.id AS id, e.tenant_id AS tenant_id, e.kg AS kg,
       e.primary_type AS primary_type, e.name AS name, e.source AS source
ORDER BY e.id
""".strip()

# --- Ontology catalog (E4 / model §5) ----------------------------------------

ONTO_TYPE_UPSERT_CYPHER = """
MERGE (t:OntoType {tenant_id: $tenant_id, kg: $kg, layer: $layer, name: $name})
ON CREATE SET
  t.description = $description,
  t.label_token = $label_token,
  t.uri = $uri
ON MATCH SET
  t.description = CASE WHEN $description = '' THEN t.description ELSE $description END,
  t.label_token = coalesce($label_token, t.label_token),
  t.uri = coalesce($uri, t.uri)
RETURN t.name AS name, t.layer AS layer, t.description AS description,
       t.label_token AS label_token, t.uri AS uri,
       t.tenant_id AS tenant_id, t.kg AS kg
""".strip()

ONTO_SUBCLASS_SET_CYPHER = """
MATCH (child:OntoType {tenant_id: $tenant_id, kg: $kg, layer: $layer, name: $name})
OPTIONAL MATCH (child)-[old:SUBCLASS_OF]->()
DELETE old
WITH child
MERGE (parent:OntoType {tenant_id: $tenant_id, kg: $kg, layer: $layer, name: $parent_name})
ON CREATE SET parent.label_token = $parent_label_token
MERGE (child)-[:SUBCLASS_OF]->(parent)
RETURN child.name AS name, parent.name AS parent_type
""".strip()

ONTO_SUBCLASS_CLEAR_CYPHER = """
MATCH (child:OntoType {tenant_id: $tenant_id, kg: $kg, layer: $layer, name: $name})
OPTIONAL MATCH (child)-[old:SUBCLASS_OF]->()
DELETE old
RETURN child.name AS name, null AS parent_type
""".strip()

ONTO_TYPE_LIST_CYPHER = """
MATCH (t:OntoType {tenant_id: $tenant_id, kg: $kg})
WHERE $layer IS NULL OR t.layer = $layer
OPTIONAL MATCH (t)-[:SUBCLASS_OF]->(p:OntoType)
RETURN t.name AS name, t.layer AS layer, coalesce(t.description, '') AS description,
       t.label_token AS label_token, t.uri AS uri,
       p.name AS parent_type, t.tenant_id AS tenant_id, t.kg AS kg,
       t.deprecated_at AS deprecated_at, t.superseded_by AS superseded_by
ORDER BY t.name
""".strip()

ONTO_TYPE_GET_CYPHER = """
MATCH (t:OntoType {tenant_id: $tenant_id, kg: $kg, layer: $layer, name: $name})
OPTIONAL MATCH (t)-[:SUBCLASS_OF]->(p:OntoType)
RETURN t.name AS name, t.layer AS layer, coalesce(t.description, '') AS description,
       t.label_token AS label_token, t.uri AS uri,
       p.name AS parent_type, t.tenant_id AS tenant_id, t.kg AS kg,
       t.deprecated_at AS deprecated_at, t.superseded_by AS superseded_by
""".strip()

ONTO_ATTR_UPSERT_CYPHER = """
MERGE (a:OntoAttr {
  tenant_id: $tenant_id, kg: $kg, layer: $layer, domain: $domain, name: $name
})
ON CREATE SET
  a.kind = $kind,
  a.datatype = $datatype,
  a.range_type = $range_type,
  a.cardinality = $cardinality,
  a.description = $description,
  a.prop_key = $prop_key
ON MATCH SET
  a.kind = $kind,
  a.datatype = $datatype,
  a.range_type = $range_type,
  a.cardinality = coalesce($cardinality, a.cardinality),
  a.description = CASE WHEN $description = '' THEN a.description ELSE $description END,
  a.prop_key = coalesce($prop_key, a.prop_key)
WITH a
MERGE (t:OntoType {tenant_id: $tenant_id, kg: $kg, layer: $layer, name: $domain})
ON CREATE SET t.label_token = $domain_label_token
MERGE (t)-[:DECLARES]->(a)
RETURN a.name AS name, a.domain AS domain, a.kind AS kind,
       a.datatype AS datatype, a.range_type AS range_type,
       a.cardinality AS cardinality, coalesce(a.description, '') AS description,
       a.prop_key AS prop_key, a.layer AS layer,
       a.tenant_id AS tenant_id, a.kg AS kg,
       a.text_kind AS text_kind
""".strip()

ONTO_ATTR_RANGE_TYPE_CYPHER = """
MATCH (a:OntoAttr {
  tenant_id: $tenant_id, kg: $kg, layer: $layer, domain: $domain, name: $name
})
OPTIONAL MATCH (a)-[old:RANGE_TYPE]->()
DELETE old
WITH a
MERGE (rt:OntoType {tenant_id: $tenant_id, kg: $kg, layer: $layer, name: $range_type})
ON CREATE SET rt.label_token = $range_label_token
MERGE (a)-[:RANGE_TYPE]->(rt)
RETURN a.name AS name, rt.name AS range_type
""".strip()

# ONTA-533: durable free-text candidacy on :OntoAttr (SET_TEXT_KIND / reconciler).
# Empty $text_kind clears the marker (candidacy becomes undecided again).
ONTO_ATTR_SET_TEXT_KIND_CYPHER = """
MERGE (a:OntoAttr {
  tenant_id: $tenant_id, kg: $kg, layer: $layer, domain: $domain, name: $name
})
ON CREATE SET
  a.kind = 'literal',
  a.datatype = 'string',
  a.cardinality = '1:1',
  a.description = '',
  a.text_kind = CASE WHEN $text_kind = '' THEN null ELSE $text_kind END
ON MATCH SET
  a.text_kind = CASE WHEN $text_kind = '' THEN null ELSE $text_kind END
WITH a
MERGE (t:OntoType {tenant_id: $tenant_id, kg: $kg, layer: $layer, name: $domain})
ON CREATE SET t.label_token = $domain_label_token
MERGE (t)-[:DECLARES]->(a)
RETURN a.name AS name, a.domain AS domain, a.kind AS kind,
       a.datatype AS datatype, a.range_type AS range_type,
       a.cardinality AS cardinality, coalesce(a.description, '') AS description,
       a.prop_key AS prop_key, a.layer AS layer,
       a.tenant_id AS tenant_id, a.kg AS kg,
       a.text_kind AS text_kind
""".strip()

ONTO_ATTR_LIST_CYPHER = """
MATCH (a:OntoAttr {tenant_id: $tenant_id, kg: $kg})
WHERE ($domain IS NULL OR a.domain = $domain)
  AND ($layer IS NULL OR a.layer = $layer)
RETURN a.name AS name, a.domain AS domain, a.kind AS kind,
       a.datatype AS datatype, a.range_type AS range_type,
       a.cardinality AS cardinality, coalesce(a.description, '') AS description,
       a.prop_key AS prop_key, a.layer AS layer,
       a.tenant_id AS tenant_id, a.kg AS kg,
       coalesce(a.core_slot, false) AS core_slot,
       a.text_kind AS text_kind,
       a.deprecated_at AS deprecated_at,
       a.superseded_by AS superseded_by
ORDER BY a.domain, a.name
""".strip()

# ONTA-531 — deletes + marker updates (schema governance on Neo4j)
ONTO_ATTR_DELETE_CYPHER = """
MATCH (a:OntoAttr {
  tenant_id: $tenant_id, kg: $kg, layer: $layer, domain: $domain, name: $name
})
DETACH DELETE a
RETURN $name AS name, $domain AS domain
""".strip()

ONTO_TYPE_DELETE_CYPHER = """
MATCH (t:OntoType {tenant_id: $tenant_id, kg: $kg, layer: $layer, name: $name})
DETACH DELETE t
RETURN $name AS name
""".strip()

ONTO_ATTR_SET_MARKERS_CYPHER = """
MATCH (a:OntoAttr {
  tenant_id: $tenant_id, kg: $kg, layer: $layer, domain: $domain, name: $name
})
SET a.core_slot = CASE WHEN $core_slot IS NULL THEN a.core_slot ELSE $core_slot END,
    a.text_kind = CASE
      WHEN $clear_text_kind THEN null
      WHEN $text_kind IS NULL THEN a.text_kind
      ELSE $text_kind
    END,
    a.deprecated_at = CASE
      WHEN $clear_deprecation THEN null
      WHEN $deprecated_at IS NULL THEN a.deprecated_at
      ELSE $deprecated_at
    END,
    a.superseded_by = CASE
      WHEN $clear_deprecation THEN null
      WHEN $superseded_by IS NULL THEN a.superseded_by
      ELSE $superseded_by
    END
RETURN a.name AS name, a.domain AS domain,
       coalesce(a.core_slot, false) AS core_slot, a.text_kind AS text_kind,
       a.deprecated_at AS deprecated_at, a.superseded_by AS superseded_by
""".strip()

ONTO_TYPE_SET_MARKERS_CYPHER = """
MATCH (t:OntoType {tenant_id: $tenant_id, kg: $kg, layer: $layer, name: $name})
SET t.description = CASE
      WHEN $description IS NULL THEN t.description
      ELSE $description
    END,
    t.deprecated_at = CASE
      WHEN $clear_deprecation THEN null
      WHEN $deprecated_at IS NULL THEN t.deprecated_at
      ELSE $deprecated_at
    END,
    t.superseded_by = CASE
      WHEN $clear_deprecation THEN null
      WHEN $superseded_by IS NULL THEN t.superseded_by
      ELSE $superseded_by
    END
RETURN t.name AS name, coalesce(t.description, '') AS description,
       t.deprecated_at AS deprecated_at, t.superseded_by AS superseded_by
""".strip()

# Per-Class instance counts via INSTANCE_OF (Explorer type-stats). Column name
# ``primary_type`` kept for template/row compat; value is Class.name.
ENTITY_COUNT_BY_PRIMARY_TYPE_CYPHER = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IS NOT NULL
RETURN c.name AS primary_type, count(DISTINCT e) AS n
ORDER BY primary_type
""".strip()

# --- Explore / KG-admin reads (E5) -------------------------------------------

ENTITY_LIST_BY_TYPE_PAGE_CYPHER = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE (c.name = $primary_type OR c.id = $primary_type)
  AND ($after_id IS NULL OR e.id > $after_id)
RETURN DISTINCT e.id AS id, e.tenant_id AS tenant_id, e.kg AS kg,
       e.primary_type AS primary_type, e.name AS name, e.source AS source
ORDER BY e.id
LIMIT $limit
""".strip()

ENTITY_COUNT_BY_TYPE_CYPHER = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name = $primary_type OR c.id = $primary_type
RETURN count(DISTINCT e) AS n
""".strip()

ENTITY_COUNT_TOTAL_CYPHER = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})
RETURN count(*) AS n
""".strip()

ENTITY_DETAIL_CYPHER = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg, id: $id})
RETURN e.id AS id, e.tenant_id AS tenant_id, e.kg AS kg,
       e.primary_type AS primary_type, e.name AS name, e.source AS source,
       labels(e) AS labels, properties(e) AS props
""".strip()

ENTITY_RELS_CYPHER = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg, id: $id})-[r]->(o:Entity {tenant_id: $tenant_id, kg: $kg})
WHERE r.tenant_id = $tenant_id AND r.kg = $kg
RETURN coalesce(r.attr, type(r)) AS attr, type(r) AS rel_type,
       o.id AS other_id, o.name AS other_name, o.primary_type AS other_type,
       'out' AS direction
UNION ALL
MATCH (o:Entity {tenant_id: $tenant_id, kg: $kg})-[r]->(e:Entity {tenant_id: $tenant_id, kg: $kg, id: $id})
WHERE r.tenant_id = $tenant_id AND r.kg = $kg
RETURN coalesce(r.attr, type(r)) AS attr, type(r) AS rel_type,
       o.id AS other_id, o.name AS other_name, o.primary_type AS other_type,
       'in' AS direction
""".strip()

# --- Type summary live scan (P-A1a / vis drill-in) ----------------------------
# Per-type attribute coverage: how many entities of ``$primary_type`` carry each
# Entity property key. Callers filter reserved / internal keys post-scan.
# Instance membership is ``INSTANCE_OF`` → Class (ADR 0013), matching type-counts.
ENTITY_TYPE_ATTR_COVERAGE_CYPHER = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name = $primary_type OR c.id = $primary_type
WITH e, keys(e) AS ks
UNWIND ks AS k
WITH k, count(DISTINCT e) AS n
WHERE k IS NOT NULL AND k <> ''
RETURN k AS attr, n
ORDER BY n DESC, attr ASC
""".strip()

# Per-type outgoing relationship coverage + edge totals for avg_degree.
ENTITY_TYPE_REL_COVERAGE_CYPHER = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name = $primary_type OR c.id = $primary_type
MATCH (e)-[r]->(o:Entity {tenant_id: $tenant_id, kg: $kg})
WHERE r.tenant_id = $tenant_id AND r.kg = $kg
WITH coalesce(r.attr, type(r)) AS attr,
     count(DISTINCT e) AS n,
     count(r) AS rel_total,
     collect(DISTINCT o.primary_type)[0] AS target_type
RETURN attr, n, rel_total, target_type
ORDER BY n DESC, attr ASC
""".strip()

# Property keys a literal grep must never return, pushed INTO the scan.
#
# Two groups, and the second is the load-bearing one:
#   * store plumbing (`id`, `tenant_id`, `kg`, …) — never was domain data;
#   * INTERNAL keys (`blockKey`, `batch_id`, …) — the flattened form of the
#     predicates `predicates.is_internal_predicate` hides on every other
#     surface, taken from `facts.INTERNAL_PROPERTY_KEYS` rather than restated.
#
# Excluding them HERE rather than after the scan is what keeps `LIMIT` honest:
# these keys sort ahead of most domain attributes (`aliasOf` < `batch_id` <
# `blockKey` < … < `name`), so a post-filter would let housekeeping rows eat the
# caller's page and hand back a short one with `truncated: false`.
#
# `name` is deliberately absent — grep exists to find things by displayed name.
_GREP_EXCLUDED_PROPERTY_KEYS: tuple[str, ...] = tuple(
    sorted(
        {
            "id",
            "tenant_id",
            "kg",
            "primary_type",
            "source",
            "created_at",
            "updated_at",
            "elementId",
            "labels",
            "props",
        }
        | set(INTERNAL_PROPERTY_KEYS)
    )
)
# These are code constants, never caller input, but they are interpolated into
# Cypher rather than bound as parameters (a list literal inside a comprehension
# cannot be a parameter without changing every caller's params dict). Fail at
# import if one ever stops being a bare identifier, so a quote can never reach
# the query text.
if not all(
    _SAFE_PROPERTY_KEY.match(k)
    for k in (*_GREP_EXCLUDED_PROPERTY_KEYS, ER_SIGNAL_PROPERTY_KEY_PREFIX)
):
    raise ValueError(
        "grep property-key exclusions must be bare identifiers: "
        f"{_GREP_EXCLUDED_PROPERTY_KEYS!r} / {ER_SIGNAL_PROPERTY_KEY_PREFIX!r}"
    )
_GREP_EXCLUDED_KEY_LIST = ", ".join(f"'{k}'" for k in _GREP_EXCLUDED_PROPERTY_KEYS)

# Index-free literal substring scan over Entity properties (grep dual-backend).
# System + internal keys are excluded before UNWIND so neither tenant_id/kg/id
# nor ER/ingest housekeeping ever appears as a "match" — or consumes the LIMIT.
# ``$type_name`` / ``$predicate_leaf`` may be null (no filter). Over-fetch with
# ``LIMIT $limit`` (caller passes limit+1 for honest truncation).
ENTITY_LITERAL_GREP_CYPHER = f"""
MATCH (e:Entity {{tenant_id: $tenant_id, kg: $kg}})
WHERE ($type_name IS NULL OR e.primary_type = $type_name OR EXISTS {{
  MATCH (e)-[:INSTANCE_OF]->(c:Class {{tenant_id: $tenant_id, kg: $kg}})
  WHERE c.name = $type_name OR c.id = $type_name
}})
WITH e, [k IN keys(e) WHERE NOT k IN [
  {_GREP_EXCLUDED_KEY_LIST}
] AND NOT k STARTS WITH '{ER_SIGNAL_PROPERTY_KEY_PREFIX}'] AS prop_keys
UNWIND prop_keys AS prop_key
WITH e, prop_key, e[prop_key] AS val
WHERE val IS NOT NULL
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

# --- NL→Cypher fixtures (E6 quality) -----------------------------------------

ENTITY_FILTER_PROP_EQ_CYPHER = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})
WHERE e.primary_type = $primary_type
  AND e[$prop_key] = $prop_value
RETURN e.id AS id, e.name AS name, e.primary_type AS primary_type
ORDER BY e.id
LIMIT $limit
""".strip()

ENTITY_1HOP_OUT_CYPHER = """
MATCH (a:Entity {tenant_id: $tenant_id, kg: $kg})-[r]->(b:Entity {tenant_id: $tenant_id, kg: $kg})
WHERE a.primary_type = $from_type
  AND r.tenant_id = $tenant_id AND r.kg = $kg
  AND ($to_type IS NULL OR b.primary_type = $to_type)
  AND ($rel_attr IS NULL OR r.attr = $rel_attr OR type(r) = $rel_attr)
RETURN a.id AS from_id, a.name AS from_name, a.primary_type AS from_type,
       b.id AS to_id, b.name AS to_name, b.primary_type AS to_type,
       type(r) AS rel_type, coalesce(r.attr, type(r)) AS attr
ORDER BY a.id, b.id
LIMIT $limit
""".strip()

# --- ADR 0013 semantic helpers (rdfs_helpers) --------------------------------
# Prefer these names for NL fixtures and new app code. Wave‑1 explore paths
# may still use entity_* templates above; both are allowlisted.
# Cypher strings are imported at module top from rdfs_helpers.


@dataclass(frozen=True, slots=True)
class CypherTemplate:
    """Allowlisted Cypher statement registered for :meth:`execute_template`."""

    name: str
    cypher: str
    writing: bool
    #: When True, session fails closed if ``id`` is missing/blank before run.
    require_entity_id: bool = False


# Registry keyed by stable template name. Application code should only run
# Cypher from this map (or future kg_writer ports that register here).
TEMPLATES: Mapping[str, CypherTemplate] = {
    "entity_merge": CypherTemplate(
        name="entity_merge",
        cypher=ENTITY_MERGE_CYPHER,
        writing=True,
        require_entity_id=True,
    ),
    "entity_get": CypherTemplate(
        name="entity_get",
        cypher=ENTITY_GET_CYPHER,
        writing=False,
        require_entity_id=False,
    ),
    "entity_list_by_type": CypherTemplate(
        name="entity_list_by_type",
        cypher=ENTITY_LIST_BY_TYPE_CYPHER,
        writing=False,
        require_entity_id=False,
    ),
    "onto_type_upsert": CypherTemplate(
        name="onto_type_upsert",
        cypher=ONTO_TYPE_UPSERT_CYPHER,
        writing=True,
    ),
    "onto_subclass_set": CypherTemplate(
        name="onto_subclass_set",
        cypher=ONTO_SUBCLASS_SET_CYPHER,
        writing=True,
    ),
    "onto_subclass_clear": CypherTemplate(
        name="onto_subclass_clear",
        cypher=ONTO_SUBCLASS_CLEAR_CYPHER,
        writing=True,
    ),
    "onto_type_list": CypherTemplate(
        name="onto_type_list",
        cypher=ONTO_TYPE_LIST_CYPHER,
        writing=False,
    ),
    "onto_type_get": CypherTemplate(
        name="onto_type_get",
        cypher=ONTO_TYPE_GET_CYPHER,
        writing=False,
    ),
    "onto_attr_upsert": CypherTemplate(
        name="onto_attr_upsert",
        cypher=ONTO_ATTR_UPSERT_CYPHER,
        writing=True,
    ),
    "onto_attr_range_type": CypherTemplate(
        name="onto_attr_range_type",
        cypher=ONTO_ATTR_RANGE_TYPE_CYPHER,
        writing=True,
    ),
    "onto_attr_set_text_kind": CypherTemplate(
        name="onto_attr_set_text_kind",
        cypher=ONTO_ATTR_SET_TEXT_KIND_CYPHER,
        writing=True,
    ),
    "onto_attr_list": CypherTemplate(
        name="onto_attr_list",
        cypher=ONTO_ATTR_LIST_CYPHER,
        writing=False,
    ),
    "onto_attr_delete": CypherTemplate(
        name="onto_attr_delete",
        cypher=ONTO_ATTR_DELETE_CYPHER,
        writing=True,
    ),
    "onto_type_delete": CypherTemplate(
        name="onto_type_delete",
        cypher=ONTO_TYPE_DELETE_CYPHER,
        writing=True,
    ),
    "onto_attr_set_markers": CypherTemplate(
        name="onto_attr_set_markers",
        cypher=ONTO_ATTR_SET_MARKERS_CYPHER,
        writing=True,
    ),
    "onto_type_set_markers": CypherTemplate(
        name="onto_type_set_markers",
        cypher=ONTO_TYPE_SET_MARKERS_CYPHER,
        writing=True,
    ),
    "entity_count_by_primary_type": CypherTemplate(
        name="entity_count_by_primary_type",
        cypher=ENTITY_COUNT_BY_PRIMARY_TYPE_CYPHER,
        writing=False,
    ),
    "entity_list_by_type_page": CypherTemplate(
        name="entity_list_by_type_page",
        cypher=ENTITY_LIST_BY_TYPE_PAGE_CYPHER,
        writing=False,
    ),
    "entity_count_by_type": CypherTemplate(
        name="entity_count_by_type",
        cypher=ENTITY_COUNT_BY_TYPE_CYPHER,
        writing=False,
    ),
    "entity_count_total": CypherTemplate(
        name="entity_count_total",
        cypher=ENTITY_COUNT_TOTAL_CYPHER,
        writing=False,
    ),
    "entity_detail": CypherTemplate(
        name="entity_detail",
        cypher=ENTITY_DETAIL_CYPHER,
        writing=False,
    ),
    "entity_rels": CypherTemplate(
        name="entity_rels",
        cypher=ENTITY_RELS_CYPHER,
        writing=False,
    ),
    "entity_type_attr_coverage": CypherTemplate(
        name="entity_type_attr_coverage",
        cypher=ENTITY_TYPE_ATTR_COVERAGE_CYPHER,
        writing=False,
    ),
    "entity_type_rel_coverage": CypherTemplate(
        name="entity_type_rel_coverage",
        cypher=ENTITY_TYPE_REL_COVERAGE_CYPHER,
        writing=False,
    ),
    "entity_literal_grep": CypherTemplate(
        name="entity_literal_grep",
        cypher=ENTITY_LITERAL_GREP_CYPHER,
        writing=False,
    ),
    "entity_filter_prop_eq": CypherTemplate(
        name="entity_filter_prop_eq",
        cypher=ENTITY_FILTER_PROP_EQ_CYPHER,
        writing=False,
    ),
    "entity_1hop_out": CypherTemplate(
        name="entity_1hop_out",
        cypher=ENTITY_1HOP_OUT_CYPHER,
        writing=False,
    ),
    # ADR 0013 semantic helper names (compose these from NL / app code)
    "entities_of_type": CypherTemplate(
        name="entities_of_type",
        cypher=ENTITIES_OF_TYPE_CYPHER,
        writing=False,
    ),
    "entities_of_type_count": CypherTemplate(
        name="entities_of_type_count",
        cypher=ENTITIES_OF_TYPE_COUNT_CYPHER,
        writing=False,
    ),
    "literal_values": CypherTemplate(
        name="literal_values",
        cypher=LITERAL_VALUES_CYPHER,
        writing=False,
    ),
    "literal_compare": CypherTemplate(
        name="literal_compare",
        cypher=LITERAL_COMPARE_CYPHER,
        writing=False,
    ),
    "literal_aggregate": CypherTemplate(
        name="literal_aggregate",
        cypher=LITERAL_AGGREGATE_CYPHER,
        writing=False,
    ),
    "related_entities": CypherTemplate(
        name="related_entities",
        cypher=RELATED_ENTITIES_CYPHER,
        writing=False,
    ),
    "related_entity_name_filter": CypherTemplate(
        name="related_entity_name_filter",
        cypher=RELATED_ENTITY_NAME_FILTER_CYPHER,
        writing=False,
    ),
    "related_entity_name_filter_inverse": CypherTemplate(
        name="related_entity_name_filter_inverse",
        cypher=RELATED_ENTITY_NAME_FILTER_INVERSE_CYPHER,
        writing=False,
    ),
    "assertions_for_subject": CypherTemplate(
        name="assertions_for_subject",
        cypher=ASSERTIONS_FOR_SUBJECT_CYPHER,
        writing=False,
    ),
    "subclass_of_closure": CypherTemplate(
        name="subclass_of_closure",
        cypher=SUBCLASS_OF_CLOSURE_CYPHER,
        writing=False,
    ),
}


def get_template(name: str) -> CypherTemplate:
    """Look up an allowlisted template; raise :class:`KeyError` if unknown."""
    try:
        return TEMPLATES[name]
    except KeyError as exc:
        known = ", ".join(sorted(TEMPLATES))
        raise KeyError(
            f"Unknown Cypher template {name!r}; allowlisted: {known}"
        ) from exc

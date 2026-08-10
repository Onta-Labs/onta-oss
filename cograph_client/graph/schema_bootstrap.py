"""Idempotent Neo4j constraints + indexes (property-graph model §7).

Apply once per database before instance writes that rely on uniqueness (G7).
Statement names are stable so re-runs are no-ops via ``IF NOT EXISTS``.

Callable from tests and process startup via :meth:`GraphStore.bootstrap_schema`
or :func:`bootstrap_schema_statements` for inspection / alternate drivers.

Also owns the **allowlisted Cypher template registry** used by
:meth:`GraphSession.execute_template` — the safe path for application writers
(Wave 1 isolation story: free-form ``execute_read``/``execute_write`` remain
for admin/bootstrap/tests only; see ``docs/neo4j-local.md``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

# (name, cypher) — name is returned by bootstrap for logging / tests.
# Cypher uses Neo4j 5 IF NOT EXISTS so Community + Aura both accept it.
SCHEMA_STATEMENTS: tuple[tuple[str, str], ...] = (
    # --- Uniqueness / existence (model §7.1) ---
    (
        "entity_tenant_kg_id_unique",
        "CREATE CONSTRAINT entity_tenant_kg_id_unique IF NOT EXISTS "
        "FOR (e:Entity) REQUIRE (e.tenant_id, e.kg, e.id) IS UNIQUE",
    ),
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
    # --- Property indexes (model §7.2) ---
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

ENTITY_LIST_BY_TYPE_CYPHER = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})
WHERE e.primary_type = $primary_type
RETURN e.id AS id, e.tenant_id AS tenant_id, e.kg AS kg,
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
       p.name AS parent_type, t.tenant_id AS tenant_id, t.kg AS kg
ORDER BY t.name
""".strip()

ONTO_TYPE_GET_CYPHER = """
MATCH (t:OntoType {tenant_id: $tenant_id, kg: $kg, layer: $layer, name: $name})
OPTIONAL MATCH (t)-[:SUBCLASS_OF]->(p:OntoType)
RETURN t.name AS name, t.layer AS layer, coalesce(t.description, '') AS description,
       t.label_token AS label_token, t.uri AS uri,
       p.name AS parent_type, t.tenant_id AS tenant_id, t.kg AS kg
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
       a.tenant_id AS tenant_id, a.kg AS kg
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

ONTO_ATTR_LIST_CYPHER = """
MATCH (a:OntoAttr {tenant_id: $tenant_id, kg: $kg})
WHERE ($domain IS NULL OR a.domain = $domain)
  AND ($layer IS NULL OR a.layer = $layer)
RETURN a.name AS name, a.domain AS domain, a.kind AS kind,
       a.datatype AS datatype, a.range_type AS range_type,
       a.cardinality AS cardinality, coalesce(a.description, '') AS description,
       a.prop_key AS prop_key, a.layer AS layer,
       a.tenant_id AS tenant_id, a.kg AS kg
ORDER BY a.domain, a.name
""".strip()

ENTITY_COUNT_BY_PRIMARY_TYPE_CYPHER = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})
WHERE e.primary_type IS NOT NULL
RETURN e.primary_type AS primary_type, count(*) AS n
ORDER BY e.primary_type
""".strip()

# --- Explore / KG-admin reads (E5) -------------------------------------------

ENTITY_LIST_BY_TYPE_PAGE_CYPHER = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})
WHERE e.primary_type = $primary_type
  AND ($after_id IS NULL OR e.id > $after_id)
RETURN e.id AS id, e.tenant_id AS tenant_id, e.kg AS kg,
       e.primary_type AS primary_type, e.name AS name, e.source AS source
ORDER BY e.id
LIMIT $limit
""".strip()

ENTITY_COUNT_BY_TYPE_CYPHER = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})
WHERE e.primary_type = $primary_type
RETURN count(*) AS n
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
    "onto_attr_list": CypherTemplate(
        name="onto_attr_list",
        cypher=ONTO_ATTR_LIST_CYPHER,
        writing=False,
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

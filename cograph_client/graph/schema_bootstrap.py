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

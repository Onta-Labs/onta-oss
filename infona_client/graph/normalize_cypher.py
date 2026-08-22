"""Allowlisted Cypher for the normalization rule-apply READS (ONTA-534).

``normalization/execute.py`` applies a confirmed rule by first asking the graph
"which values does this predicate hold?" and then rewriting them through the
converged write path (``graph/kg_writer.py``). The write half was ported to the
GraphStore long ago; the READ half was still residual SPARQL, so on the shipped
Neo4j-only backend ``apply_rule`` raised ``SparqlClientRetired`` on its FIRST
read and every rule type died before touching the graph.

Three reads cover all four rule shapes; they live here rather than in
:mod:`schema_bootstrap` only because that module is at its file-size pin.
:data:`NORMALIZE_READ_CYPHER` is spliced into ``schema_bootstrap.TEMPLATES``.

**Predicate-scoped, not type-scoped — deliberate.** The SPARQL these replace
filter on the PREDICATE alone::

    FILTER(?p = <onto/leaf> || STRENDS(STR(?p), "/attrs/leaf"))

with no ``?s rdf:type <types/T>`` constraint, so one ``strip_emoji`` /
``list_explode`` rule on ``skills`` cleans ``Mentor.skills`` AND
``Coach.skills`` AND any ``onto/skills`` literal. Narrowing the port to the
rule's own type would silently stop cleaning every other type's values — no
error, no migration, a rule that used to work quietly doing less. So
``$primary_type`` is OPTIONAL: pass ``None`` for the predicate-scoped reads and
a type name only where the rule is genuinely type-scoped
(``promote_to_node``, which flips ONE type's declared ``rdfs:range`` and must
not promote a different type's literals — see ``execute_promote.py``).

**No LIMIT, by design.** Apply must process EVERY matching row; a paged read
would silently normalize the first page and leave the rest. That also rules out
reusing ``entity_literal_grep`` (capped at ``MAX_PAGE_LIMIT``, and it requires a
non-empty needle — ``strip_emoji`` has none, emoji span too many codepoints for
a substring pre-filter).
"""

from __future__ import annotations

from typing import Mapping

# Every literal value of one attribute leaf, KG-wide (or one type when
# ``$primary_type`` is given). ``value`` may be a LIST — the property-graph
# multi-value shape an already-exploded attribute takes — so callers must expand
# it rather than assume a scalar. Type membership accepts the INSTANCE_OF →
# Class edge OR the denormalized ``primary_type`` (same tolerance as
# ``entity_literal_grep``): a composite minted by an earlier rail may carry only
# one of the two, and missing it would leave the value un-normalized.
ENTITY_LITERALS_BY_PROP_CYPHER = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})
WHERE $primary_type IS NULL
   OR e.primary_type = $primary_type
   OR EXISTS {
     MATCH (e)-[:INSTANCE_OF]->(c:Class {tenant_id: $tenant_id, kg: $kg})
     WHERE c.name = $primary_type OR c.id = $primary_type
   }
WITH e, e[$prop_key] AS val
WHERE val IS NOT NULL
RETURN e.id AS entity_uri, e.primary_type AS type, val AS value
ORDER BY e.id
""".strip()

# Every relationship edge carrying one attribute leaf, KG-wide, with the target's
# display name + type so the caller can decide whether the target is a COMPOSITE
# (its label packs several atoms behind a delimiter). ``$rel_type`` is the
# sanitized upper-snake Neo4j type for the same leaf — both are matched because
# ``merge_rel`` stores the original leaf on ``r.attr`` while the relationship
# TYPE is the sanitized token.
ENTITY_RELS_BY_ATTR_CYPHER = """
MATCH (a:Entity {tenant_id: $tenant_id, kg: $kg})-[r]->(b:Entity {
  tenant_id: $tenant_id, kg: $kg
})
WHERE r.tenant_id = $tenant_id AND r.kg = $kg
  AND (r.attr = $rel_attr OR type(r) = $rel_type)
RETURN a.id AS start_id, b.id AS end_id, b.name AS end_name,
       b.primary_type AS end_type
ORDER BY a.id, b.id
""".strip()

# Entities of one type with NO inbound edge carrying the attribute leaf — the
# candidate set for ``list_explode``'s orphan-composite sweep. Keyed on GRAPH
# STATE (not on the edges this pass happened to rewrite), so the sweep stays
# complete and re-runnable. The caller still filters candidates down to the
# COMPOSITE-named ones before deleting anything; this read deliberately does not
# know about delimiters.
ENTITY_ORPHANS_OF_TYPE_CYPHER = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})
WHERE (e.primary_type = $primary_type
   OR EXISTS {
     MATCH (e)-[:INSTANCE_OF]->(c:Class {tenant_id: $tenant_id, kg: $kg})
     WHERE c.name = $primary_type OR c.id = $primary_type
   })
  AND NOT EXISTS {
    MATCH (s:Entity {tenant_id: $tenant_id, kg: $kg})-[r]->(e)
    WHERE r.tenant_id = $tenant_id AND r.kg = $kg
      AND (r.attr = $rel_attr OR type(r) = $rel_type)
  }
RETURN e.id AS entity_uri, e.name AS name
ORDER BY e.id
""".strip()

#: ``{template_name: cypher}`` spliced into ``schema_bootstrap.TEMPLATES``.
#: All read-only (``writing=False``) — apply's WRITES stay on ``kg_writer``.
NORMALIZE_READ_CYPHER: Mapping[str, str] = {
    "entity_literals_by_prop": ENTITY_LITERALS_BY_PROP_CYPHER,
    "entity_rels_by_attr": ENTITY_RELS_BY_ATTR_CYPHER,
    "entity_orphans_of_type": ENTITY_ORPHANS_OF_TYPE_CYPHER,
}

__all__ = [
    "ENTITY_LITERALS_BY_PROP_CYPHER",
    "ENTITY_ORPHANS_OF_TYPE_CYPHER",
    "ENTITY_RELS_BY_ATTR_CYPHER",
    "NORMALIZE_READ_CYPHER",
]

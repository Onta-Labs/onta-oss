"""Execute a confirmed normalization rule.

:func:`apply_rule` rewrites a KG graph in place. Three rule types ship today.

``list_explode`` splits collapsed multi-value cells into atomic ones. Two shapes:

* **relationship, target=entity** (the ``speaks`` case): an edge points at a
  COMPOSITE entity whose local-name/label packs several atomic values joined by
  a delimiter (``…/entities/Language/English__Russian__Ukrainian``). We split
  it, mint a CANONICAL atomic entity IRI per atomic value (slug-derived, so
  "Russian" from any composite maps to the SAME node — free dedup, no ER pass),
  re-point the edge at the atomic entities, drop the composite edge, and finally
  run a single graph-state-keyed orphan sweep that deletes EVERY composite
  entity of the target type left with no inbound edge for this predicate. The
  sweep is keyed on graph state (not the edges this pass touched), so it is
  complete (catches composites an inline per-edge drop would miss) and
  re-runnable (a later apply still sweeps leftovers from a buggy earlier run).
  The sweep's target type is resolved from the ONTOLOGY — the predicate's
  declared ``rdfs:range`` (``speaks → Language``), a bounded single-subject
  lookup — so a pure re-run with ``edges_rewritten == 0`` still resolves the type
  and sweeps lingering orphans to zero, with no unbounded full-graph scan
  (COG-118).

* **attribute, target=literal** (the skills/disciplines case): a literal packs
  several items with a delimiter. We split into atomic literals, write N triples,
  and remove the original packed literal.

``strip_emoji`` removes emoji / pictographic junk characters from text literals
(the ``skills = "🎨 design"`` case): for each matching ``attrs/<pred>`` (or
``onto/<pred>``) literal we strip emoji codepoints + variation selectors + ZWJ +
skin-tone modifiers, collapse the leftover whitespace, and rewrite ONLY the
literals that actually changed. A value with no emoji is untouched (idempotent
re-run is a no-op). A literal that becomes empty after stripping (a pure-emoji
value) is dropped. It operates per-literal, so it works whether ``skills`` is
still one packed literal or already exploded into atomic literals.

``promote_to_node`` PROMOTES a literal-valued attribute into entity NODES — the
"escape hatch" that makes a literal-by-default modeling choice safe: a column
first ingested as a plain literal (``specialty = "Cardiology"``,
``rating = "4.6"``) can be turned into a first-class entity later, without
re-ingesting. For every ``(?s, attrs/<leaf>, ?literal)`` triple we mint a node,
add a RELATIONSHIP edge ``(?s, onto/<leaf>, node)``, clear the old literal
(predicate-scoped, datatype-agnostic), and flip the attribute's declared
``rdfs:range`` from the XSD primitive to the target entity type. The result is the
SAME relationship shape ingest writes for a native relationship — ``onto/<leaf>``
instance edges + an ``rdfs:range`` pointing at a ``types/`` URI + a first-class
``rdfs:Class`` target — so a promoted attribute is indistinguishable from one that
was node-valued from the start, and the NL planner (which queries ``onto/<leaf>``
for a type-ranged attribute) traverses it correctly. Two node-identity strategies
via ``params.key_by``:

* **``"value"``** (categoricals — Specialty / Category / City): the node IRI is
  ``…/entities/<TargetType>/<slug(value)>``, SHARED across every owner with the
  same value — so two Doctors with ``specialty = "Cardiology"`` point at ONE
  ``Cardiology`` node (free dedup, exactly like ``list_explode``'s atomic-entity
  minting). The human value is also stored under ``attrs/name`` (Explorer Data
  table). ``params.split`` may be set (reuses the ``list_explode`` delimiters) so
  a multi-valued literal ``"A, B"`` becomes MULTIPLE value-keyed nodes/edges.
  ``params.extract="bracket_id"`` pulls the id out of ``Name [id]``;
  ``params.key_map`` remaps a display atom to an existing row id (strip +
  casefold) so ``entity_uri`` matches the already-ingested node;
  ``params.link_existing`` is per-atom: a joined atom writes only the
  ``onto/<leaf>`` edge (does not rewrite the target's label); an unmatched
  atom mints a typed node. When ``params.delimiters`` is set, that set is
  exclusive.
* **``"owner"``** (measurements — Rating / Price / Score): the node IRI is
  ``…/entities/<TargetType>/<slug(owner_local_id)>-<leaf>``, one node PER OWNER —
  two shops rated ``4.6`` are NOT the same ``Rating``. The original literal is
  PRESERVED losslessly as the node's ``value`` attribute
  (``<node> <types/<TargetType>/attrs/value> "4.6"``) alongside ``rdfs:label``.
  ``split`` is ignored (a measurement is one value).

All four shapes READ through the GraphStore (ONTA-534,
``normalization/execute_reads.py``) and WRITE through the converged path
(``graph/kg_writer.py``). Before the read port the first ``await
neptune.query(...)`` raised ``SparqlClientRetired`` on the shipped Neo4j-only
backend, so every apply died before touching the graph. Each read keeps a
residual SPARQL arm for when no store can be consulted.

Idempotent: re-running finds nothing to change (values are already atomic /
emoji-free; a promoted object is a URI, not a literal, so the ``isLiteral(?o)``
filter returns nothing) and is a no-op. ``list_explode`` returns
``{edges_rewritten, atomic_created, orphans_dropped}``; ``strip_emoji`` returns
``{literals_cleaned, triples_rewritten}``; ``promote_to_node`` returns
``{nodes_created, edges_added, literals_promoted}``.
"""


from __future__ import annotations

from infona_client.graph.client import NeptuneClient
from infona_client.graph.iri import ENTITY_URI_PREFIX, ONTO_PRED_PREFIX  # noqa: F401
from infona_client.graph.kg_writer import (  # noqa: F401 — monkeypatch surface
    delete_facts,
    insert_facts,
    refresh_after_write,
)
from infona_client.graph.ontology_commit import commit_ontology  # noqa: F401
from infona_client.graph.ontology_queries import (  # noqa: F401
    RDF,
    RDFS,
    TYPE_URI_PREFIX,
    _safe_id,
    attr_uri,
    entity_uri,
    type_uri,
)
from infona_client.graph.parser import parse_sparql_results  # noqa: F401
from infona_client.graph.queries import kg_graph_uri, tenant_graph_uri
from infona_client.graph.store import resolve_optional_graph_store  # noqa: F401
from infona_client.models.ontology import OntologyMutation, OntologyOpKind  # noqa: F401
from infona_client.normalization.execute_helpers import (  # noqa: F401
    ATTRS_INFIX,
    NAME_ATTR_SUFFIX,
    RDF_TYPE,
    RDFS_LABEL,
    RDFS_RANGE,
    _EMOJI_PATTERN,
    _FALLBACK_DELIMITERS,
    _WS_PATTERN,
    _affected_types,
    _atom_uri,
    _decode_local_name,
    _delimiters,
    _extract_atom,
    _host,
    _join_atom_key,
    _list_explode_as_promotion,
    _node_uri_owner,
    _node_uri_value,
    _resolve_atom_key,
    _sparql_str,
    _split,
    _strip_emoji_value,
    _subject_local_id,
    _summary_mutated,
    _target_type_from_type_uri,
    _target_type_from_uri,
    _title_type,
    logger,
)
from infona_client.normalization.execute_explode import (  # noqa: F401
    _composite_target_types,
    _explode_literal,
    _explode_relationship,
    _range_target_types,
    _sweep_orphan_composites,
)
from infona_client.normalization.execute_promote import _promote_to_node  # noqa: F401
from infona_client.normalization.execute_reads import (  # noqa: F401 — monkeypatch surface
    catalog_range_types,
    literal_rows,
    orphan_rows,
    rel_rows,
)
from infona_client.normalization.execute_strip import _strip_emoji  # noqa: F401


async def apply_rule(neptune: NeptuneClient, tenant_id: str, rule) -> dict:
    """Apply a confirmed rule (``list_explode`` / ``strip_emoji`` / ``promote_to_node``).

    Returns a summary. On any apply that actually mutates the graph, fire the same
    fire-and-forget type-stats recompute enrichment uses (``schedule_recompute``)
    so the Explorer's precomputed counts don't go stale (COG-118). A pure no-op
    (idempotent re-run that changed nothing) skips it.
    """
    if rule.rule_type not in ("list_explode", "strip_emoji", "promote_to_node"):
        raise ValueError(
            f"unsupported rule_type {rule.rule_type!r} "
            f"(supported: list_explode, strip_emoji, promote_to_node)"
        )

    kg_graph = kg_graph_uri(tenant_id, rule.kg_name)
    onto_graph = tenant_graph_uri(tenant_id)

    summary, deleted_subjects = await _dispatch(neptune, kg_graph, onto_graph, rule)

    if _summary_mutated(summary):
        # Shared post-write housekeeping (graph/kg_writer.py) — same path every
        # KG writer uses. deleted_subjects carries any orphan composites the sweep
        # removed (ADR 0007) so the SAME refresh evicts them from the derived
        # secondary indexes — no ghost rows left behind.
        #
        # affected_types: list_explode / strip_emoji change instance data + counts
        # but NEVER the type SCHEMA, so they pass () (no re-embed needed; the
        # refresh still invalidates the NL-planning cache and recomputes stats).
        # promote_to_node is the exception — it CHANGES THE SCHEMA (the attribute's
        # range flips literal->TargetType, and TargetType is a brand-new node
        # type), so both the owning type and the minted target type need
        # re-embedding for semantic retrieval to see the new relationship. We pass
        # both.
        affected_types = _affected_types(rule)
        await _host().refresh_after_write(
            neptune,
            tenant_id=tenant_id,
            kg_name=rule.kg_name,
            affected_types=affected_types,
            deleted_subjects=deleted_subjects,
        )
    return summary


async def _dispatch(
    neptune: NeptuneClient, kg_graph: str, onto_graph: str, rule
) -> tuple[dict, list[str]]:
    """Route to the rule-type handler; return ``(summary, deleted_subjects)``.

    ``deleted_subjects`` are the whole-entity URIs the handler removed (the orphan
    sweep's swept composites) so ``apply_rule``'s single refresh can evict them
    from derived indexes. Attribute/edge-level deletes (a subject survives, only
    some triples go) are NOT subjects here.
    """
    if rule.rule_type == "strip_emoji":
        return await _strip_emoji(neptune, kg_graph, rule)

    if rule.rule_type == "promote_to_node":
        return await _promote_to_node(neptune, kg_graph, onto_graph, rule)

    delimiters = _delimiters(rule)
    target = (rule.params or {}).get("target")
    pred_leaf = rule.predicate

    if rule.target_kind == "relationship" or target == "entity":
        if rule.target_kind == "attribute" and target == "entity":
            # attribute -> atomic ENTITIES: promote the literal to value-keyed
            # nodes (the follow-up that was previously a no-op stub). A packed
            # literal like "A, B" splits into MULTIPLE shared value-keyed nodes,
            # matching what `list_explode target=entity` on a RELATIONSHIP does but
            # for a literal-valued attribute. A list_explode rule carries no
            # `target_type` (that param is new with promote_to_node), so
            # _list_explode_as_promotion derives one from the predicate leaf
            # (title-cased: specialty -> Specialty) when unset, and forces
            # key_by="value" + split=True (the multi-value-cell semantics).
            promote_rule = _list_explode_as_promotion(rule)
            return await _promote_to_node(
                neptune, kg_graph, onto_graph, promote_rule
            )
        return await _explode_relationship(
            neptune, kg_graph, onto_graph, rule.type_name, pred_leaf, delimiters
        )
    return await _explode_literal(neptune, kg_graph, rule, delimiters)


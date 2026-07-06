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
* **``"owner"``** (measurements — Rating / Price / Score): the node IRI is
  ``…/entities/<TargetType>/<slug(owner_local_id)>-<leaf>``, one node PER OWNER —
  two shops rated ``4.6`` are NOT the same ``Rating``. The original literal is
  PRESERVED losslessly as the node's ``value`` attribute
  (``<node> <types/<TargetType>/attrs/value> "4.6"``) alongside ``rdfs:label``.
  ``split`` is ignored (a measurement is one value).

Idempotent: re-running finds nothing to change (values are already atomic /
emoji-free; a promoted object is a URI, not a literal, so the ``isLiteral(?o)``
filter returns nothing) and is a no-op. ``list_explode`` returns
``{edges_rewritten, atomic_created, orphans_dropped}``; ``strip_emoji`` returns
``{literals_cleaned, triples_rewritten}``; ``promote_to_node`` returns
``{nodes_created, edges_added, literals_promoted}``.
"""

from __future__ import annotations

import re

import structlog

from cograph_client.graph.client import NeptuneClient
from cograph_client.graph.kg_writer import delete_facts, insert_facts, refresh_after_write
from cograph_client.graph.ontology_queries import (
    RDF,
    RDFS,
    _safe_id,
    attr_uri,
    entity_uri,
    set_object_property_range,
    type_uri,
    upsert_type,
)
from cograph_client.graph.parser import parse_sparql_results
from cograph_client.graph.queries import (
    kg_graph_uri,
    tenant_graph_uri,
)

logger = structlog.stdlib.get_logger("cograph.normalization.execute")

RDF_TYPE = f"{RDF}#type"
RDFS_LABEL = f"{RDFS}#label"
RDFS_RANGE = f"{RDFS}#range"
ENTITY_URI_PREFIX = "https://cograph.tech/entities/"
ATTRS_INFIX = "/attrs/"
ONTO_PRED_PREFIX = "https://cograph.tech/onto/"
NAME_ATTR_SUFFIX = "/attrs/name"

# Slug-aware delimiters: the slug "__" is the de-slugified form of a source-list
# separator (", " etc.). We keep it last so we try the longer composite-name
# split form too. Each is a literal substring to split on.
_FALLBACK_DELIMITERS = [", ", "; ", " / ", " | ", " - ", "__"]

# Emoji / pictographic / junk codepoints to strip from text literals
# (strip_emoji). Scoped to the symbol/pictograph blocks so ordinary letters
# (incl. accented), digits, and real-skill-name punctuation (& + - / # . etc.)
# are left ALONE — e.g. "c++", "C#", "Node.js", "café", "R&D" survive intact.
#   U+200D            zero-width joiner (binds emoji sequences)
#   U+FE0E/U+FE0F     variation selectors (text/emoji presentation)
#   U+1F3FB–U+1F3FF   skin-tone modifiers
#   U+1F1E6–U+1F1FF   regional-indicator letters (flags)
#   U+2600–U+27BF     Misc Symbols + Dingbats
#   U+2B00–U+2BFF     Misc Symbols & Arrows (incl. ⭐ stars, ✅-adjacent)
#   U+1F000–U+1FAFF   the emoji/pictograph planes (Emoticons, Misc Symbols &
#                     Pictographs, Transport & Map, Supplemental, Symbols &
#                     Pictographs Extended-A, etc.)
#   U+2190–U+21FF     Arrows (decorative junk that shows up in scraped text)
#   U+2300–U+23FF     Misc Technical (⌚⏰ etc.)
#   U+2B50 etc. fall inside the ranges above.
_EMOJI_PATTERN = re.compile(
    "["
    "\U0000200d"
    "\U0000fe0e\U0000fe0f"
    "\U0001f3fb-\U0001f3ff"
    "\U0001f1e6-\U0001f1ff"
    "\U00002190-\U000021ff"
    "\U00002300-\U000023ff"
    "\U00002600-\U000027bf"
    "\U00002b00-\U00002bff"
    "\U0001f000-\U0001faff"
    "]+"
)
# Collapse the whitespace left behind once emoji are removed.
_WS_PATTERN = re.compile(r"\s+")


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
        await refresh_after_write(
            neptune,
            tenant_id=tenant_id,
            kg_name=rule.kg_name,
            affected_types=affected_types,
            deleted_subjects=deleted_subjects,
        )
    return summary


def _affected_types(rule) -> tuple[str, ...]:
    """Types whose SCHEMA this rule changed (for ``refresh_after_write`` re-embed).

    Only ``promote_to_node`` alters the schema — it flips an attribute's
    ``rdfs:range`` from a literal type to an entity type and introduces that
    entity type as a new node type — so it returns ``(owning_type, target_type)``.
    ``list_explode`` / ``strip_emoji`` touch only instance data, so they return
    ``()`` (unchanged behavior — no re-embed).
    """
    if rule.rule_type != "promote_to_node":
        return ()
    target_type = str((rule.params or {}).get("target_type") or "").strip()
    types = [rule.type_name]
    if target_type:
        types.append(target_type)
    return tuple(t for t in types if t)


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
    return await _explode_literal(neptune, kg_graph, pred_leaf, delimiters)


def _list_explode_as_promotion(rule):
    """Adapt a ``list_explode`` (attribute, target=entity) rule to a
    ``promote_to_node`` value-keyed, split promotion.

    A ``list_explode`` rule's ``params`` has no ``target_type`` (that concept is
    new with ``promote_to_node``), so we derive the node type name from the
    predicate leaf (``specialty`` -> ``Specialty``) unless the caller already put
    a ``target_type`` in params. ``key_by`` is forced to ``"value"`` and ``split``
    to ``True`` — a multi-valued cell exploded into SHARED categorical nodes is
    exactly the value-keyed-with-split shape.

    Returns a shallow copy with ``rule_type="promote_to_node"`` and the derived
    params, so the original rule object is left untouched.
    """
    params = dict(rule.params or {})
    target_type = str(params.get("target_type") or "").strip() or _title_type(
        rule.predicate
    )
    params["target_type"] = target_type
    params["key_by"] = "value"
    params["split"] = True
    return rule.model_copy(update={"rule_type": "promote_to_node", "params": params})


def _title_type(pred_leaf: str) -> str:
    """Best-effort node TYPE name from a predicate leaf: ``specialty`` ->
    ``Specialty``, ``home_city`` -> ``HomeCity``.

    Only used for the ``list_explode target=entity`` back-compat path, where no
    explicit ``target_type`` is supplied. Splits on non-alphanumeric runs and
    title-cases each token; falls back to a capitalised leaf, then ``"Value"``.
    """
    tokens = [t for t in re.split(r"[^A-Za-z0-9]+", pred_leaf) if t]
    if not tokens:
        return "Value"
    return "".join(t[:1].upper() + t[1:] for t in tokens)


def _summary_mutated(summary: dict) -> bool:
    """True iff this apply actually changed the graph (so a recompute is worth it).

    Covers every summary shape: list_explode's counters, strip_emoji's, and
    promote_to_node's. A purely idempotent re-run reports all-zero and we skip the
    recompute (and, for promote_to_node, the schema re-embed).
    """
    return any(
        int(summary.get(k, 0))
        for k in (
            "edges_rewritten",
            "atomic_created",
            "orphans_dropped",
            "triples_rewritten",
            "literals_cleaned",
            "nodes_created",
            "edges_added",
            "literals_promoted",
        )
    )


def _delimiters(rule) -> list[str]:
    delims = list((rule.params or {}).get("delimiters") or [])
    # Always include the slug "__" — composite entity names use it even when the
    # source literal used ", " (the slugifier maps both to "__").
    for d in _FALLBACK_DELIMITERS:
        if d not in delims:
            delims.append(d)
    # Longest-first so " / " is tried before "/" etc. — avoids splitting inside a
    # token that legitimately contains the shorter delimiter.
    return sorted(set(delims), key=len, reverse=True)


def _split(value: str, delimiters: list[str]) -> list[str]:
    """Split ``value`` on any of the delimiters into trimmed, de-duped atoms.

    Returns the original single-element list when no delimiter is present (i.e.
    the value is already atomic — the idempotency guarantee).
    """
    # Build one regex alternation of the (escaped) delimiters, longest first.
    pattern = "|".join(re.escape(d) for d in sorted(delimiters, key=len, reverse=True))
    if not pattern:
        return [value.strip()] if value.strip() else []
    parts = re.split(pattern, value)
    atoms: list[str] = []
    seen: set[str] = set()
    for p in parts:
        a = p.strip()
        if a and a not in seen:
            seen.add(a)
            atoms.append(a)
    return atoms


def _atom_uri(target_type: str, atom: str) -> str:
    """Canonical atomic entity IRI for ``atom`` of ``target_type``:
    ``…/entities/<TargetType>/<slug>``.

    Minted through the ONE shared ``entity_uri`` (graph/ontology_queries) so an
    atom's IRI is byte-identical to how ingestion/discovery mint the composite's
    own IRI. Used both to RE-POINT an edge at the clean atomic node and to decide
    idempotency — the skip check compares an atom's canonical IRI to the
    composite's own IRI, so they MUST be minted the same way for the equality to
    be exact (COG-118). Sharing the minter makes that guarantee structural.
    """
    return entity_uri(target_type, atom)


def _target_type_from_uri(composite_uri: str) -> str | None:
    """``…/entities/<TargetType>/<slug>`` → ``<TargetType>``."""
    if not composite_uri.startswith(ENTITY_URI_PREFIX):
        return None
    tail = composite_uri[len(ENTITY_URI_PREFIX):]
    head = tail.split("/", 1)[0]
    return head or None


async def _explode_relationship(
    neptune: NeptuneClient,
    kg_graph: str,
    onto_graph: str,
    domain_type: str,
    pred_leaf: str,
    delimiters: list[str],
) -> tuple[dict, list[str]]:
    """Split composite relationship targets into canonical atomic entities.

    Returns ``(summary, orphan_uris)`` — the composite subjects the final sweep
    removed, so the caller's single refresh can evict them from derived indexes.
    """
    onto_pred = ONTO_PRED_PREFIX + pred_leaf
    attr_pred_suffix = ATTRS_INFIX + pred_leaf  # any …/attrs/<leaf> form

    # 1) Find every (subject, predicate-as-used, composite) edge whose object is a
    #    composite entity. We match BOTH the onto/<leaf> predicate (the normal
    #    relationship form) and any types/<T>/attrs/<leaf> predicate (a predicate
    #    first seen as an attribute then carrying an entity object). The composite
    #    is identified by its name/label containing a delimiter.
    delim_filter = " || ".join(
        f'CONTAINS(?cname, "{_sparql_str(d)}")' for d in delimiters
    )
    q = (
        f"SELECT ?s ?p ?composite ?clabel FROM <{kg_graph}> WHERE {{\n"
        f"  ?s ?p ?composite .\n"
        f"  FILTER(?p = <{onto_pred}> || STRENDS(STR(?p), \"{_sparql_str(attr_pred_suffix)}\"))\n"
        f'  FILTER(STRSTARTS(STR(?composite), "{ENTITY_URI_PREFIX}"))\n'
        f"  OPTIONAL {{ ?composite <{RDFS_LABEL}> ?clabel }}\n"
        f'  BIND(COALESCE(?clabel, REPLACE(STR(?composite), "^.*/", "")) AS ?cname)\n'
        f"  FILTER({delim_filter})\n"
        f"}}"
    )
    _, rows = parse_sparql_results(await neptune.query(q))

    edges_to_delete: list[tuple[str, str, str]] = []
    edges_to_add: list[tuple[str, str, str]] = []
    atomic_triples: list[tuple[str, str, str]] = []
    atomic_seen: set[str] = set()
    composites_touched: set[str] = set()

    for r in rows:
        s = r.get("s", "")
        p = r.get("p", "")
        composite = r.get("composite", "")
        if not s or not p or not composite:
            continue
        target_type = _target_type_from_uri(composite)
        if not target_type:
            continue
        # Prefer the rdfs:label (the human value) for the split; fall back to the
        # URL-decoded local-name. The "__" slug split recovers atoms from names.
        clabel = r.get("clabel", "")
        source = clabel or _decode_local_name(composite)
        atoms = _split(source, delimiters)
        if not atoms:
            # Nothing to split (empty/whitespace-only source) — nothing to do.
            continue
        # Skip ONLY when the target is already a clean atomic node: a single atom
        # whose CANONICAL IRI is the composite's own IRI. That is the genuine
        # idempotency case (re-running on `…/Language/English` is a no-op). A
        # single atom whose canonical IRI DIFFERS from the composite's IRI means
        # the target carries a junk delimiter (leading/trailing/doubled, e.g.
        # `…/Industry/__Agriculture` → atom "Agriculture" → `…/Industry/Agriculture`)
        # and MUST be re-pointed to the clean node — same as the multi-atom path —
        # so the malformed node becomes a sweepable orphan (COG-118). The equality
        # uses the SAME minting helper as the re-point below, so the check is exact.
        if len(atoms) == 1 and _atom_uri(target_type, atoms[0]) == composite:
            continue
        composites_touched.add(composite)
        # Re-point the edge to one CANONICAL atomic entity per atom; the canonical
        # IRI is slug-derived so the same atom (e.g. "Russian") from any composite
        # maps to the SAME node. Always re-point using the onto/<leaf> predicate
        # (the proper relationship form) regardless of the predicate as-used.
        for atom in atoms:
            atom_uri = _atom_uri(target_type, atom)
            edges_to_add.append((s, onto_pred, atom_uri))
            if atom_uri not in atomic_seen:
                atomic_seen.add(atom_uri)
                atomic_triples.append((atom_uri, RDF_TYPE, type_uri(target_type)))
                atomic_triples.append((atom_uri, RDFS_LABEL, atom))
                # Mirror ingest: also store the human value under attrs/name so the
                # Explorer Data table shows it (see explore.get_type_records).
                atomic_triples.append(
                    (atom_uri, type_uri(target_type) + "/attrs/name", atom)
                )
        edges_to_delete.append((s, p, composite))

    # 2) Apply: add atomic entity triples + new edges, then delete composite edges.
    if atomic_triples:
        await insert_facts(neptune, kg_graph, atomic_triples)
    if edges_to_add:
        await insert_facts(neptune, kg_graph, edges_to_add)
    if edges_to_delete:
        # Concrete-triple removal via the shared primitive (ADR 0007); delete_facts
        # batches internally (no oversized statement). These are edge drops — the
        # subject survives — so they are NOT deleted_subjects.
        await delete_facts(
            neptune,
            kg_graph,
            triples=edges_to_delete,
            reason="normalization:list_explode composite-edge drop",
        )

    # 3) Final orphan sweep. After ALL edges for this predicate are re-pointed,
    #    delete EVERY composite node of the relationship's target type(s) that has
    #    no inbound onto/<pred> (or attrs/<pred>) edge left — keyed on graph state,
    #    not on the composites we happened to touch this pass. That makes it both
    #    complete (one DELETE/WHERE per type catches the ones a per-edge drop
    #    misses) and re-runnable (a second apply still sweeps leftover orphans
    #    from a buggy earlier run, even when nothing was rewritten this pass).
    #    The target type comes from the ONTOLOGY (the predicate's rdfs:range), a
    #    cheap single-subject lookup that works on a pure re-run regardless of
    #    whether any edge was rewritten this pass (COG-118).
    target_types = await _composite_target_types(
        neptune, onto_graph, domain_type, pred_leaf, composites_touched
    )
    orphan_uris = await _sweep_orphan_composites(
        neptune, kg_graph, onto_pred, attr_pred_suffix, target_types, delimiters
    )

    summary = {
        "edges_rewritten": len(edges_to_delete),
        "atomic_created": len(atomic_seen),
        "orphans_dropped": len(orphan_uris),
    }
    logger.info("explode_relationship_done", predicate=pred_leaf, **summary)
    return summary, orphan_uris


async def _composite_target_types(
    neptune: NeptuneClient,
    onto_graph: str,
    domain_type: str,
    pred_leaf: str,
    composites: set[str],
) -> set[str]:
    """The relationship's target type(s), for scoping the final orphan sweep.

    PRIMARY path (COG-118): resolve the type from the ONTOLOGY — the predicate's
    declared ``rdfs:range``. The relationship property is
    ``<types/<domain_type>/attrs/<pred_leaf>>`` and its range is the target type's
    ``types/<TargetType>`` URI (the same range the Explorer summary / type-edges
    read). This is a bounded single-subject lookup in the tenant ontology graph —
    cheap, reliable, and INDEPENDENT of whether any edge was rewritten this pass,
    so a pure re-run (``edges_rewritten == 0``) still resolves the type and sweeps
    lingering orphans to zero. It replaces the old unbounded full-graph
    ``SELECT DISTINCT ?t`` scan that timed out on live data and silently skipped
    the sweep (logged ``composite_target_type_query_failed``).

    FALLBACK: if the ontology declares no ``types/`` range for the predicate
    (un-upgraded attribute, or range missing), derive the type(s) from the
    composites we re-pointed this pass — their IRI carries ``…/entities/
    <TargetType>/…``. This keeps the first-pass split path working even before the
    range is upgraded. Scoping to a real target type means the sweep never touches
    unrelated types.
    """
    onto_types = await _range_target_types(neptune, onto_graph, domain_type, pred_leaf)
    if onto_types:
        return onto_types

    # No usable ontology range — derive from this pass's re-pointed composites.
    types: set[str] = set()
    for composite in composites:
        t = _target_type_from_uri(composite)
        if t:
            types.add(t)
    if not types:
        # Nothing rewritten this pass AND no ontology range: we cannot scope a
        # sweep. Surface it (not a silent skip) so a missing range is visible.
        logger.warning(
            "sweep_target_type_unresolved",
            domain_type=domain_type,
            predicate=pred_leaf,
            note="no ontology rdfs:range and no composites re-pointed this pass",
        )
    return types


async def _range_target_types(
    neptune: NeptuneClient, onto_graph: str, domain_type: str, pred_leaf: str
) -> set[str]:
    """Read the predicate's ``rdfs:range`` from the ontology → its target type(s).

    Bounded single-subject query: the relationship property's URI is fully known
    (``attr_uri(domain_type, pred_leaf)``), so this never scans the KG data graph.
    Returns the set of target type NAMES whose range URI is a ``types/`` URI
    (a relationship range); XSD/primitive ranges are ignored (not entity targets).
    A query error is logged and treated as "no range" so the caller falls back
    rather than crashing.
    """
    prop_uri = attr_uri(domain_type, pred_leaf)
    q = (
        f"SELECT ?range FROM <{onto_graph}> WHERE {{\n"
        f"  <{prop_uri}> <{RDFS_RANGE}> ?range .\n"
        f"}}"
    )
    try:
        _, rows = parse_sparql_results(await neptune.query(q))
    except Exception:
        logger.warning(
            "sweep_range_lookup_failed",
            domain_type=domain_type,
            predicate=pred_leaf,
            exc_info=True,
        )
        return set()
    types: set[str] = set()
    for r in rows:
        t = _target_type_from_type_uri(r.get("range", ""))
        if t:
            types.add(t)
    return types


async def _sweep_orphan_composites(
    neptune: NeptuneClient,
    kg_graph: str,
    onto_pred: str,
    attr_pred_suffix: str,
    target_types: set[str],
    delimiters: list[str],
) -> list[str]:
    """Final, graph-state-keyed sweep of orphaned composite nodes.

    For each target type, delete ALL triples of every entity that is (a) of that
    type, (b) composite-named (local-name or rdfs:label contains a rule
    delimiter), and (c) has ZERO inbound ``onto/<pred>`` (or ``…/attrs/<pred>``)
    edges. One SELECT per type resolves the orphan set (complete — catches every
    orphan a per-edge drop would miss; re-runnable — a later apply still sweeps
    leftovers), then the removal routes through the shared ``delete_facts``
    primitive (ADR 0007) so a swept subject is evicted from the derived secondary
    indexes too — no ghost rows keyed to a deleted subject. Atomic nodes (no
    delimiter) and still-referenced composites are left untouched.

    Returns the URIs of the orphan composite subjects removed (the summary count
    is ``len(...)``, and the caller feeds them to ``refresh_after_write`` as
    ``deleted_subjects``).
    """
    if not target_types:
        return []

    delim_filter = " || ".join(
        f'CONTAINS(?cname, "{_sparql_str(d)}")' for d in delimiters
    )
    dropped: list[str] = []
    for target_type in sorted(target_types):
        t_uri = type_uri(target_type)
        # An orphaned composite ?c of this type. SELECT the subjects, then remove
        # them by URI via delete_facts (subject-scoped) — so the set that is
        # evicted from derived indexes is exactly the set removed from Neptune.
        orphan_where = (
            f"  ?c <{RDF_TYPE}> <{t_uri}> .\n"
            f'  FILTER(STRSTARTS(STR(?c), "{ENTITY_URI_PREFIX}"))\n'
            f"  OPTIONAL {{ ?c <{RDFS_LABEL}> ?clabel }}\n"
            f'  BIND(COALESCE(?clabel, REPLACE(STR(?c), "^.*/", "")) AS ?cname)\n'
            f"  FILTER({delim_filter})\n"
            f"  FILTER NOT EXISTS {{ ?s <{onto_pred}> ?c }}\n"
            f"  FILTER NOT EXISTS {{ ?s2 ?p2 ?c . "
            f"FILTER(STRENDS(STR(?p2), \"{_sparql_str(attr_pred_suffix)}\")) }}\n"
        )
        select_q = (
            f"SELECT DISTINCT ?c FROM <{kg_graph}> WHERE {{\n"
            f"{orphan_where}"
            f"}}"
        )
        try:
            _, rows = parse_sparql_results(await neptune.query(select_q))
            orphan_uris = [r["c"] for r in rows if r.get("c")]
        except Exception:
            logger.warning(
                "orphan_select_failed", target_type=target_type, exc_info=True
            )
            continue
        if not orphan_uris:
            continue
        try:
            await delete_facts(
                neptune,
                kg_graph,
                subjects=orphan_uris,
                touched_types=[target_type],
                reason="normalization:list_explode orphan-composite sweep",
            )
        except Exception:
            logger.warning(
                "orphan_sweep_failed", target_type=target_type, exc_info=True
            )
            continue
        dropped.extend(orphan_uris)
    return dropped


def _target_type_from_type_uri(t_uri: str) -> str | None:
    """``https://cograph.tech/types/<TargetType>`` → ``<TargetType>``."""
    prefix = type_uri("")
    if not t_uri.startswith(prefix):
        return None
    tail = t_uri[len(prefix):].strip("/")
    return tail or None


async def _explode_literal(
    neptune: NeptuneClient, kg_graph: str, pred_leaf: str, delimiters: list[str]
) -> tuple[dict, list[str]]:
    """Split packed attribute literals into N atomic literals.

    Returns ``(summary, [])`` — literal splits replace an attribute value on a
    surviving subject, so nothing here is a deleted subject.
    """
    onto_pred = ONTO_PRED_PREFIX + pred_leaf
    attr_pred_suffix = ATTRS_INFIX + pred_leaf

    delim_filter = " || ".join(
        f'CONTAINS(STR(?o), "{_sparql_str(d)}")' for d in delimiters
    )
    q = (
        f"SELECT ?s ?p ?o FROM <{kg_graph}> WHERE {{\n"
        f"  ?s ?p ?o .\n"
        f"  FILTER(?p = <{onto_pred}> || STRENDS(STR(?p), \"{_sparql_str(attr_pred_suffix)}\"))\n"
        f"  FILTER(isLiteral(?o))\n"
        f"  FILTER({delim_filter})\n"
        f"}}"
    )
    _, rows = parse_sparql_results(await neptune.query(q))

    to_delete: list[tuple[str, str, str]] = []
    to_add: list[tuple[str, str, str]] = []
    rewritten = 0
    atomic_count = 0
    for r in rows:
        s = r.get("s", "")
        p = r.get("p", "")
        o = r.get("o", "")
        if not s or not p:
            continue
        atoms = _split(o, delimiters)
        if len(atoms) <= 1:
            continue  # already atomic — idempotent no-op
        for atom in atoms:
            to_add.append((s, p, atom))
            atomic_count += 1
        to_delete.append((s, p, o))
        rewritten += 1

    if to_add:
        await insert_facts(neptune, kg_graph, to_add)
    if to_delete:
        await delete_facts(
            neptune,
            kg_graph,
            triples=to_delete,
            reason="normalization:list_explode packed-literal replace",
        )

    summary = {
        "edges_rewritten": rewritten,
        "atomic_created": atomic_count,
        "orphans_dropped": 0,
    }
    logger.info("explode_literal_done", predicate=pred_leaf, **summary)
    return summary, []


def _subject_local_id(subject_uri: str) -> str:
    """The part of a subject URI after the last ``/`` — the owner's local id.

    Used only by the ``key_by="owner"`` node-identity strategy: the Rating node
    for ``…/entities/CoffeeShop/shop-1`` keys on ``shop-1`` so each owner gets its
    OWN measurement node. Trailing slashes are stripped first so a URI that ends
    in ``/`` still yields its real last segment.
    """
    return subject_uri.rstrip("/").rsplit("/", 1)[-1]


def _node_uri_value(target_type: str, value: str) -> str:
    """Value-keyed node IRI: ``…/entities/<TargetType>/<slug(value)>``.

    SHARED across every owner with the same value (free dedup) — the categorical
    strategy. Minted through the ONE shared ``entity_uri`` (graph/ontology_queries),
    the SAME minter ``_atom_uri`` / ``list_explode`` use, so a promoted categorical
    node coincides exactly with the node ``list_explode`` would mint for the same
    value (cross-rail consistency)."""
    return entity_uri(target_type, value)


def _node_uri_owner(target_type: str, subject_uri: str, pred_leaf: str) -> str:
    """Owner-keyed node IRI: ``…/entities/<TargetType>/<slug(owner_id)>-<leaf>``.

    One node PER OWNER (two shops rated 4.6 are NOT the same Rating). The owner's
    local id disambiguates, and the ``-<leaf>`` suffix keeps two owner-keyed
    promotions on DIFFERENT predicates of the same owner from colliding (a shop's
    ``rating`` node vs its ``price`` node). The base ``…/entities/<TargetType>/
    <slug(owner_id)>`` is the shared ``entity_uri`` (so it coincides with every
    other rail's node for that owner id); the ``-<slug(leaf)>`` suffix is appended
    exactly as before — byte-identical to the old ``ENTITY_URI_PREFIX`` + ``_slug``
    form."""
    return f"{entity_uri(target_type, _subject_local_id(subject_uri))}-{_safe_id(pred_leaf)}"


async def _promote_to_node(
    neptune: NeptuneClient, kg_graph: str, onto_graph: str, rule
) -> tuple[dict, list[str]]:
    """Promote a literal-valued attribute into entity NODES (``promote_to_node``).

    Mirrors :func:`_explode_relationship`'s structure — query → mint node triples
    → add edges → ``delete_facts`` the old literals → ontology range flip → return
    a summary — but the source is a LITERAL attribute (not a composite edge) and
    the outcome is a node-valued attribute. Node identity is chosen by
    ``params.key_by``:

    * ``"value"`` (default) — one SHARED node per distinct value
      (``…/entities/<TargetType>/<slug(value)>``); the value is stored as
      ``rdfs:label`` AND ``attrs/name`` (the categorical / Explorer-Data shape).
      With ``params.split`` a multi-valued literal explodes into several nodes.
    * ``"owner"`` — one node PER OWNER
      (``…/entities/<TargetType>/<slug(owner)>-<leaf>``); the original literal is
      PRESERVED losslessly as the node's ``value`` attribute
      (``<node> <attr_uri(TargetType,"value")> literal``) alongside ``rdfs:label``.

    Returns ``(summary, [])`` — a promotion re-points an attribute on a SURVIVING
    owner (the literal object is replaced by a node edge), so nothing here is a
    deleted subject. Idempotency: the query filters ``isLiteral(?o)``, so once
    promoted the object is a URI and the next run selects nothing → all-zero
    summary → no mutation, no refresh, no schema re-embed.
    """
    params = rule.params or {}
    type_name = rule.type_name
    pred_leaf = rule.predicate
    target_type = str(params.get("target_type") or "").strip()
    if not target_type:
        raise ValueError("promote_to_node requires params.target_type")
    key_by = str(params.get("key_by") or "value").strip().lower()
    if key_by not in ("value", "owner"):
        raise ValueError(
            f"promote_to_node key_by must be 'value' or 'owner', got {key_by!r}"
        )
    # split only makes sense for value-keyed categoricals; a measurement is one
    # value, so owner-keyed ignores it.
    split = bool(params.get("split", False)) and key_by == "value"
    delimiters = _delimiters(rule) if split else []

    prim_pred = attr_uri(type_name, pred_leaf)  # the TYPE-SCOPED attrs/<leaf> predicate

    # 1) Every (?s, <types/<type>/attrs/<leaf>>, ?literal) for THIS type. The
    #    predicate IRI already embeds the type name, so matching it EXACTLY keeps
    #    the promotion scoped to type_name's own instances. That scoping is
    #    load-bearing: a rule is per (type, predicate), and the ontology range flip
    #    below only touches type_name — so a broader predicate match (onto/<leaf>,
    #    or any OTHER type's …/attrs/<leaf>) would promote a different type's
    #    literals to node edges while leaving that type's declared range a stale
    #    literal. ``isLiteral`` is the idempotency guard: once promoted the object
    #    is a URI, so a re-run selects nothing. No delimiter CONTAINS — promotion
    #    applies to ALL literals. The marker comment self-identifies the query in
    #    log traces.
    q = (
        f"SELECT ?s ?p ?o FROM <{kg_graph}> WHERE {{\n"
        f"  # promote_to_node: literals of {pred_leaf} on {type_name}\n"
        f"  ?s ?p ?o .\n"
        f"  FILTER(?p = <{prim_pred}>)\n"
        f"  FILTER(isLiteral(?o))\n"
        f"}}"
    )
    _, rows = parse_sparql_results(await neptune.query(q))

    onto_pred = ONTO_PRED_PREFIX + pred_leaf  # onto/<leaf> — the relationship edge form

    node_triples: list[tuple[str, str, str]] = []
    edges_to_add: list[tuple[str, str, str]] = []
    subjects_to_clear: set[str] = set()
    nodes_seen: set[str] = set()
    edges_seen: set[tuple[str, str, str]] = set()
    literals_promoted = 0

    t_uri = type_uri(target_type)
    value_attr = attr_uri(target_type, "value")  # owner-keyed lossless store
    name_attr = attr_uri(target_type, "name")  # value-keyed Explorer-Data label

    for r in rows:
        s = r.get("s", "")
        o = r.get("o", "")
        if not s or o is None or o == "":
            continue
        # The atoms to promote: split a multi-value literal (value-keyed only) or
        # take the whole literal as one value. _split trims + de-dupes; a value
        # with no delimiter comes back as a single-element list (idempotent).
        atoms = _split(o, delimiters) if split else [o.strip()]
        atoms = [a for a in atoms if a]
        if not atoms:
            continue
        for atom in atoms:
            if key_by == "value":
                node_uri = _node_uri_value(target_type, atom)
                new_triples = [
                    (node_uri, RDF_TYPE, t_uri),
                    (node_uri, RDFS_LABEL, atom),
                    # Mirror ingest / list_explode: store the value under attrs/name
                    # so the Explorer Data table renders it.
                    (node_uri, name_attr, atom),
                ]
            else:  # owner-keyed measurement
                node_uri = _node_uri_owner(target_type, s, pred_leaf)
                new_triples = [
                    (node_uri, RDF_TYPE, t_uri),
                    (node_uri, RDFS_LABEL, atom),
                    # PRESERVE the original literal losslessly as the node's value.
                    (node_uri, value_attr, atom),
                ]
            if node_uri not in nodes_seen:
                nodes_seen.add(node_uri)
                node_triples.extend(new_triples)
            # Re-point the edge via the onto/<leaf> RELATIONSHIP predicate — the
            # form the NL planner queries for a type-ranged attribute, and the form
            # ingest (schema_resolver) + _explode_relationship use for relationship
            # instances. An attrs/<leaf> edge would be invisible to NL queries once
            # the range flips to an entity type. Keeping the edge on a DIFFERENT
            # predicate than the old literal also lets the clear below be a clean
            # predicate-scoped delete of attrs/<leaf> that never touches this edge.
            edge = (s, onto_pred, node_uri)
            if edge not in edges_seen:
                edges_seen.add(edge)
                edges_to_add.append(edge)
        # Clear this subject's attrs/<leaf> with a PREDICATE-SCOPED delete (below):
        # every literal object of it, DATATYPE-AGNOSTICALLY. Reconstructing the
        # exact literal from the SELECT's lexical value would MISS a typed original
        # ("4.6"^^xsd:float) — the delete would serialize a plain xsd:string that
        # never matches the typed triple, leaving the old literal behind and
        # breaking idempotency. A predicate-scoped clear removes it whatever its
        # datatype.
        subjects_to_clear.add(s)
        literals_promoted += 1

    # 2) Apply through the converged write path, INSERT-FIRST for crash safety:
    #    nodes, then the onto/<leaf> edges, THEN clear the attrs/<leaf> literals. A
    #    crash between the edge insert and the clear converges on re-run — the node
    #    URIs are deterministic (re-mint the identical node/edge, idempotent INSERT)
    #    and the surviving literal is re-selected and re-cleared. The clear is a
    #    PREDICATE-SCOPED delete (o=None) of each subject's attrs/<leaf>: it removes
    #    every literal object regardless of datatype and never hits the onto/<leaf>
    #    edge (a different predicate), all through the shared delete_facts (batched,
    #    provenance tombstone, ADR 0007).
    if node_triples:
        await insert_facts(neptune, kg_graph, node_triples)
    if edges_to_add:
        await insert_facts(neptune, kg_graph, edges_to_add)
    if subjects_to_clear:
        await delete_facts(
            neptune,
            kg_graph,
            triples=[(s, prim_pred, None) for s in sorted(subjects_to_clear)],
            reason="normalization:promote_to_node literal->node",
        )

    # 3) ONTOLOGY (only when something was promoted — a pure re-run stays a total
    #    no-op, schema included). Two idempotent upserts:
    #    (a) declare the target as a first-class rdfs:Class (upsert_type) — a bare
    #        instance-level rdf:type is not enough: the type grid + embed_types key
    #        on the Class declaration, so without it the new type is invisible +
    #        the target-type re-embed is a no-op; and
    #    (b) flip the attribute's rdfs:range xsd->types/<target> via
    #        set_object_property_range — a RANGE-ONLY replace that preserves any
    #        human-authored rdfs:comment (upsert_attribute with an empty description
    #        would silently clear it). The attribute is now a proper
    #        relationship-ranged property matching its onto/<leaf> instance edges.
    if literals_promoted:
        await neptune.update(upsert_type(onto_graph, target_type))
        await neptune.update(
            set_object_property_range(onto_graph, type_name, pred_leaf, target_type)
        )

    summary = {
        "nodes_created": len(nodes_seen),
        "edges_added": len(edges_to_add),
        "literals_promoted": literals_promoted,
    }
    logger.info(
        "promote_to_node_done",
        predicate=pred_leaf,
        target_type=target_type,
        key_by=key_by,
        split=split,
        **summary,
    )
    return summary, []


def _strip_emoji_value(value: str) -> str:
    """Remove emoji / pictographic junk from one text value, collapse whitespace.

    Pure + deterministic. ``"🎨 design"`` → ``"design"``; ``"design 🚀"`` →
    ``"design"``; ``"ai 🚀 growth"`` → ``"ai growth"``; a pure-emoji value → ``""``
    (the caller drops empties). A value with no emoji is returned UNCHANGED after
    a no-op whitespace collapse, so re-running is idempotent and ordinary names
    (``"c++"``, ``"café"``, ``"R&D"``) are never touched.
    """
    stripped = _EMOJI_PATTERN.sub(" ", value)
    return _WS_PATTERN.sub(" ", stripped).strip()


async def _strip_emoji(neptune: NeptuneClient, kg_graph: str, rule) -> tuple[dict, list[str]]:
    """Strip emoji/junk from this predicate's literals; rewrite only what changed.

    Selects every ``attrs/<leaf>`` (or ``onto/<leaf>``) literal for the
    predicate, cleans each value, and — for the literals that actually changed —
    deletes the old triple and (unless the cleaned value is empty) inserts the
    cleaned one. Unchanged literals (already emoji-free) and non-literal objects
    are left alone, so the pass is idempotent. ``targets`` in params is reserved
    for future relationship-label cleaning; v1 cleans attribute literals.
    """
    pred_leaf = rule.predicate
    onto_pred = ONTO_PRED_PREFIX + pred_leaf
    attr_pred_suffix = ATTRS_INFIX + pred_leaf

    # Pull every literal for the predicate (both predicate forms). No CONTAINS
    # pre-filter — emoji are spread across many codepoints, so we clean in Python
    # and only rewrite the rows that change (the SELECT is bounded by predicate).
    q = (
        f"SELECT ?s ?p ?o FROM <{kg_graph}> WHERE {{\n"
        f"  ?s ?p ?o .\n"
        f"  FILTER(?p = <{onto_pred}> || STRENDS(STR(?p), \"{_sparql_str(attr_pred_suffix)}\"))\n"
        f"  FILTER(isLiteral(?o))\n"
        f"}}"
    )
    _, rows = parse_sparql_results(await neptune.query(q))

    to_delete: list[tuple[str, str, str]] = []
    to_add: list[tuple[str, str, str]] = []
    literals_cleaned = 0
    for r in rows:
        s = r.get("s", "")
        p = r.get("p", "")
        o = r.get("o", "")
        if not s or not p:
            continue
        cleaned = _strip_emoji_value(o)
        if cleaned == o:
            continue  # no emoji / already clean — idempotent no-op
        literals_cleaned += 1
        to_delete.append((s, p, o))
        if cleaned:
            to_add.append((s, p, cleaned))
        # else: cleaned is empty (pure-emoji value) — drop the triple entirely.

    if to_add:
        await insert_facts(neptune, kg_graph, to_add)
    if to_delete:
        await delete_facts(
            neptune,
            kg_graph,
            triples=to_delete,
            reason="normalization:strip_emoji literal cleanup",
        )

    summary = {
        "literals_cleaned": literals_cleaned,
        "triples_rewritten": len(to_delete),
    }
    logger.info("strip_emoji_done", predicate=pred_leaf, **summary)
    return summary, []


def _decode_local_name(uri: str) -> str:
    """The local-name of an entity URI, percent-decoded (best-effort)."""
    from urllib.parse import unquote

    tail = uri.rstrip("/").split("/")[-1]
    return unquote(tail)


def _sparql_str(s: str) -> str:
    """Escape a Python string for embedding inside a SPARQL double-quoted literal.

    Used for CONTAINS/STRENDS argument literals — the only place we splice a
    delimiter/suffix into a query. Escapes backslash, quote, and newline.
    """
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

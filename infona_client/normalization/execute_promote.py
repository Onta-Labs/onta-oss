"""promote_to_node handler — literal attribute → entity nodes."""

from __future__ import annotations

from typing import Any

from infona_client.graph.iri import ONTO_PRED_PREFIX
from infona_client.graph.ontology_queries import attr_uri, type_uri
from infona_client.models.ontology import OntologyMutation, OntologyOpKind
from infona_client.normalization.execute_helpers import (
    RDF_TYPE,
    RDFS_LABEL,
    _EXTRACT_KINDS,
    _delimiters,
    _host,
    _node_uri_owner,
    _node_uri_value,
    _resolve_atom_key,
    _split,
    logger,
)


async def _promote_to_node(
    neptune: Any, kg_graph: str, onto_graph: str, rule
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
    extract = str(params.get("extract") or "").strip().lower()
    if extract and extract not in _EXTRACT_KINDS:
        raise ValueError(
            f"promote_to_node extract must be one of {sorted(_EXTRACT_KINDS)}, "
            f"got {extract!r}"
        )
    raw_map = params.get("key_map") or {}
    if raw_map and not isinstance(raw_map, dict):
        raise ValueError("promote_to_node key_map must be an object of atom→id")
    key_map = {str(k): str(v) for k, v in dict(raw_map).items() if k and v}
    # Join to an already-ingested type (Staff, Tag CSV): mint the SAME
    # entity_uri, write the onto/<leaf> edge, do NOT rewrite the target's
    # label/name (that would clobber Ada Lovelace with the extracted id).
    link_existing = bool(params.get("link_existing", False))
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
    store_rows = await _host().literal_rows(
        kg_graph, pred_leaf, type_name=type_name
    )
    if store_rows is None:
        q = (
            f"SELECT ?s ?p ?o FROM <{kg_graph}> WHERE {{\n"
            f"  # promote_to_node: literals of {pred_leaf} on {type_name}\n"
            f"  ?s ?p ?o .\n"
            f"  FILTER(?p = <{prim_pred}>)\n"
            f"  FILTER(isLiteral(?o))\n"
            f"}}"
        )
        _, raw = _host().parse_sparql_results(await neptune.query(q))
        rows = [(r.get("s", ""), r.get("o", "")) for r in raw]
    else:
        rows = [(r.subject, r.value) for r in store_rows]

    onto_pred = ONTO_PRED_PREFIX + pred_leaf  # onto/<leaf> — the relationship edge form

    node_triples: list[tuple[str, str, Any]] = []
    edges_to_add: list[tuple[str, str, str]] = []
    subjects_to_clear: set[str] = set()
    nodes_seen: set[str] = set()
    edges_seen: set[tuple[str, str, str]] = set()
    literals_promoted = 0

    t_uri = type_uri(target_type)
    value_attr = attr_uri(target_type, "value")  # owner-keyed lossless store
    name_attr = attr_uri(target_type, "name")  # value-keyed Explorer-Data label

    for s, o_raw in rows:
        if not s or o_raw is None or o_raw == "":
            continue
        # `o_raw` keeps the store's native type — ingest writes `"4.6"^^xsd:float`
        # and the store holds a real float — so the owner-keyed `value` attribute
        # can preserve the measurement EXACTLY. `o` is the text form the node
        # identity + label are derived from.
        o = o_raw if isinstance(o_raw, str) else str(o_raw)
        # The atoms to promote: split a multi-value literal (value-keyed only) or
        # take the whole literal as one value. _split trims + de-dupes; a value
        # with no delimiter comes back as a single-element list (idempotent).
        atoms = _split(o, delimiters) if split else [o.strip()]
        atoms = [a for a in atoms if a]
        if not atoms:
            continue
        for atom in atoms:
            key = _resolve_atom_key(atom, extract, key_map)
            if not key:
                continue
            if key_by == "value":
                node_uri = _node_uri_value(target_type, key)
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
                    # PRESERVE the original literal losslessly as the node's
                    # value — the RAW term, so a typed float stays a float.
                    (node_uri, value_attr, atom if isinstance(o_raw, str) else o_raw),
                ]
            if node_uri not in nodes_seen:
                nodes_seen.add(node_uri)
                if not link_existing:
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
    #
    #    The edges are then RE-INSERTED after the clear. On the property-graph
    #    store an Assertion is keyed by (subject, PROPERTY IRI, object) and
    #    ``object_property_iri(leaf) == datatype_property_iri(leaf)`` — one IRI per
    #    leaf, whatever the range — so the clear's Assertion cleanup
    #    (``pg_ops.delete_literals`` → ``delete_assertions_for_subject(property_id
    #    =property_uri(leaf))``) matches the OBJECT assertion we just wrote as well
    #    as the literal one it is meant to remove. The relationship itself survives
    #    (the clear never touches rels), so the damage is silent: Explorer would
    #    still render the edge while the Assertion-backed NL reads stopped seeing
    #    it — precisely the "looks like it works, invisible to queries" failure
    #    class. Re-asserting is idempotent (same MERGE key) and keeps the
    #    insert-FIRST crash ordering: a crash before the clear converges on re-run
    #    because the literal is still there to re-select.
    # E7: GraphStore once per write batch when neo4j backend is active.
    store = _host().resolve_optional_graph_store()
    if node_triples:
        await _host().insert_facts(neptune, kg_graph, node_triples, store=store)
    if edges_to_add:
        await _host().insert_facts(neptune, kg_graph, edges_to_add, store=store)
    if subjects_to_clear:
        await _host().delete_facts(
            neptune,
            kg_graph,
            triples=[(s, prim_pred, None) for s in sorted(subjects_to_clear)],
            reason="normalization:promote_to_node literal->node",
            store=store,
        )
        if edges_to_add:
            await _host().insert_facts(neptune, kg_graph, edges_to_add, store=store)

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
        # One commit: mint target type + flip attribute range to relationship
        # (range-only upgrade preserves any human-authored rdfs:comment).
        await _host().commit_ontology(
            neptune,
            onto_graph,
            [
                OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name=target_type),
                OntologyMutation(
                    op=OntologyOpKind.UPSERT_RELATIONSHIP,
                    type_name=type_name,
                    slot_name=pred_leaf,
                    target_type=target_type,
                    description=None,  # range-only
                ),
            ],
            message="normalization:promote_to_node",
        )

    summary = {
        "nodes_created": 0 if link_existing else len(nodes_seen),
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


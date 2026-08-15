"""SPARQL builders that mutate ontology *schema* (types, attrs, markers).

Construction only — application is exclusive to ``ontology_commit``
(ONTA-403). These strings contain ``DELETE {`` / ``INSERT DATA`` and are
allowlisted as schema builders, not instance writers.
"""

from infona_client.graph.ontology_queries_uris import (
    INFONA_ONTO,
    RDF,
    RDFS,
    TEXT_KIND_FREE_TEXT,
    XSD,
    _datatype_to_xsd,
    _esc,
    attr_uri,
    type_uri,
)


def insert_type(graph_uri: str, name: str, description: str = "", parent_type: str | None = None) -> str:
    uri = type_uri(name)
    triples = [
        f'  <{uri}> <{RDF}#type> <{RDFS}#Class> .',
        f'  <{uri}> <{RDFS}#label> "{_esc(name)}" .',
    ]
    if description:
        triples.append(f'  <{uri}> <{RDFS}#comment> "{_esc(description)}" .')
    if parent_type:
        triples.append(f'  <{uri}> <{RDFS}#subClassOf> <{type_uri(parent_type)}> .')
    body = "\n".join(triples)
    return f"INSERT DATA {{\n  GRAPH <{graph_uri}> {{\n{body}\n  }}\n}}"


def insert_attribute(graph_uri: str, type_name: str, attr_name: str, description: str = "", datatype: str = "string") -> str:
    t_uri = type_uri(type_name)
    a_uri = attr_uri(type_name, attr_name)
    xsd_type = _datatype_to_xsd(datatype)
    triples = [
        f'  <{a_uri}> <{RDF}#type> <{RDF}#Property> .',
        f'  <{a_uri}> <{RDFS}#label> "{_esc(attr_name)}" .',
        f'  <{a_uri}> <{RDFS}#domain> <{t_uri}> .',
        f'  <{a_uri}> <{RDFS}#range> <{xsd_type}> .',
    ]
    if description:
        triples.append(f'  <{a_uri}> <{RDFS}#comment> "{_esc(description)}" .')
    body = "\n".join(triples)
    return f"INSERT DATA {{\n  GRAPH <{graph_uri}> {{\n{body}\n  }}\n}}"


def delete_attribute_declaration(graph_uri: str, type_name: str, attr_name: str) -> str:
    """Remove one attribute's ONTOLOGY declaration (rdf:Property + label/domain/
    range/comment — every triple whose subject is the attr URI).

    Built for the attr_meta companion migration (ONTA-262): enrichment used to
    declare per-attribute provenance companions (``<attr>_source_url`` /
    ``_provenance`` / ``_verified_at``) as first-class schema; those declarations
    are what rendered companions as sibling columns, and the migration purges
    them after re-keying the instance triples. Schema-graph only — instance
    triples are untouched (they move via ``kg_writer.rewrite_predicates``).
    Idempotent: deleting an absent declaration is a no-op.
    """
    a_uri = attr_uri(type_name, attr_name)
    return (
        f"WITH <{graph_uri}>\n"
        f"DELETE {{ <{a_uri}> ?p ?o }} WHERE {{ <{a_uri}> ?p ?o }}"
    )


def upsert_type(graph_uri: str, name: str, description: str = "", parent_type: str | None = None) -> str:
    """Atomically UPSERT a type declaration — idempotent under agent retries.

    Unlike :func:`insert_type` (blind ``INSERT DATA``), this REPLACES the
    single-valued ``rdfs:comment`` and ``rdfs:subClassOf`` instead of appending,
    so re-asserting a *changed* description or parent does not leave a second
    stale triple behind.

    Predicate handling:
      - ``rdf:type rdfs:Class`` and ``rdfs:label`` are plain idempotent
        ``INSERT DATA`` (re-asserting an identical triple is a no-op in RDF).
      - ``rdfs:comment`` and ``rdfs:subClassOf`` are SINGLE-VALUED and emitted as
        atomic ``DELETE/INSERT/WHERE`` operations: the old value is removed and
        the new one set in one update.

    Empty-description / None-parent semantics (authoritative upsert): if
    ``description`` is empty or ``parent_type`` is None we still DELETE any
    existing value (clearing it) but do NOT INSERT a replacement. The resulting
    graph state therefore reflects exactly the arguments passed — an upsert with
    no description never leaves a stale comment, and clearing a parent un-roots
    the type. The multi-operation update string separates each DELETE/INSERT/
    WHERE block with ``;``.
    """
    uri = type_uri(name)

    # Plain idempotent inserts: rdf:type rdfs:Class + rdfs:label.
    insert_block = (
        f"INSERT DATA {{\n"
        f"  GRAPH <{graph_uri}> {{\n"
        f'    <{uri}> <{RDF}#type> <{RDFS}#Class> .\n'
        f'    <{uri}> <{RDFS}#label> "{_esc(name)}" .\n'
        f"  }}\n"
        f"}}"
    )
    ops = [insert_block]

    # Single-valued rdfs:comment: delete old, insert new only if non-empty.
    if description:
        comment_insert = f"INSERT {{ GRAPH <{graph_uri}> {{ <{uri}> <{RDFS}#comment> \"{_esc(description)}\" }} }}\n"
    else:
        comment_insert = ""
    ops.append(
        f"DELETE {{ GRAPH <{graph_uri}> {{ <{uri}> <{RDFS}#comment> ?c }} }}\n"
        f"{comment_insert}"
        f"WHERE {{ GRAPH <{graph_uri}> {{ OPTIONAL {{ <{uri}> <{RDFS}#comment> ?c }} }} }}"
    )

    # Single-valued rdfs:subClassOf: delete old, insert new only if parent given.
    if parent_type:
        parent_insert = f"INSERT {{ GRAPH <{graph_uri}> {{ <{uri}> <{RDFS}#subClassOf> <{type_uri(parent_type)}> }} }}\n"
    else:
        parent_insert = ""
    ops.append(
        f"DELETE {{ GRAPH <{graph_uri}> {{ <{uri}> <{RDFS}#subClassOf> ?p }} }}\n"
        f"{parent_insert}"
        f"WHERE {{ GRAPH <{graph_uri}> {{ OPTIONAL {{ <{uri}> <{RDFS}#subClassOf> ?p }} }} }}"
    )

    return " ;\n".join(ops)


def upsert_type_comment(graph_uri: str, name: str, description: str = "") -> str:
    """Idempotently set ONLY a type's ``rdfs:comment`` (single-valued), leaving
    ``rdfs:subClassOf`` and every other triple untouched.

    Unlike :func:`upsert_type` — which also REPLACES ``rdfs:subClassOf`` and so
    CLEARS it when called with no ``parent_type`` — this never touches the
    hierarchy. Writing a subtype's description must not be able to wipe a
    ``subClassOf`` edge that a separate step (``insert_subtype`` /
    ``_synthesize_ancestors``) just created. Re-asserts ``rdf:type rdfs:Class`` +
    ``rdfs:label`` idempotently so the type exists; an empty ``description``
    clears any existing comment without inserting a replacement.
    """
    uri = type_uri(name)
    insert_block = (
        f"INSERT DATA {{\n"
        f"  GRAPH <{graph_uri}> {{\n"
        f'    <{uri}> <{RDF}#type> <{RDFS}#Class> .\n'
        f'    <{uri}> <{RDFS}#label> "{_esc(name)}" .\n'
        f"  }}\n"
        f"}}"
    )
    comment_insert = (
        f"INSERT {{ GRAPH <{graph_uri}> {{ <{uri}> <{RDFS}#comment> \"{_esc(description)}\" }} }}\n"
        if description
        else ""
    )
    comment_block = (
        f"DELETE {{ GRAPH <{graph_uri}> {{ <{uri}> <{RDFS}#comment> ?c }} }}\n"
        f"{comment_insert}"
        f"WHERE {{ GRAPH <{graph_uri}> {{ OPTIONAL {{ <{uri}> <{RDFS}#comment> ?c }} }} }}"
    )
    return f"{insert_block} ;\n{comment_block}"


def upsert_attribute(graph_uri: str, type_name: str, attr_name: str, description: str = "", datatype: str = "string") -> str:
    """Atomically UPSERT an attribute declaration — idempotent under agent retries.

    Unlike :func:`insert_attribute` (blind ``INSERT DATA``), this REPLACES the
    single-valued ``rdfs:range`` and ``rdfs:comment`` instead of appending. This
    matters because ``rdfs:range`` flips between an XSD primitive and a
    ``types/`` URI when an attribute is later seen carrying an entity value (i.e.
    becomes a relationship); a blind re-insert would leave the property with two
    conflicting ranges.

    Predicate handling:
      - ``rdf:type rdf:Property``, ``rdfs:label`` and ``rdfs:domain`` are plain
        idempotent ``INSERT DATA`` (re-asserting identical triples is a no-op).
      - ``rdfs:range`` and ``rdfs:comment`` are SINGLE-VALUED and emitted as
        atomic ``DELETE/INSERT/WHERE`` operations.

    ``rdfs:range`` is always set (``_datatype_to_xsd`` maps a primitive name to
    an XSD URI and any other name to that type's ``types/`` URI), so the range
    block always inserts a fresh value after deleting the old one. ``rdfs:comment``
    follows the same clear-on-empty rule as :func:`upsert_type`: an empty
    ``description`` deletes any existing comment without inserting a replacement.
    """
    t_uri = type_uri(type_name)
    a_uri = attr_uri(type_name, attr_name)
    xsd_type = _datatype_to_xsd(datatype)

    # Plain idempotent inserts: rdf:type rdf:Property + rdfs:label + rdfs:domain.
    insert_block = (
        f"INSERT DATA {{\n"
        f"  GRAPH <{graph_uri}> {{\n"
        f'    <{a_uri}> <{RDF}#type> <{RDF}#Property> .\n'
        f'    <{a_uri}> <{RDFS}#label> "{_esc(attr_name)}" .\n'
        f'    <{a_uri}> <{RDFS}#domain> <{t_uri}> .\n'
        f"  }}\n"
        f"}}"
    )
    ops = [insert_block]

    # Single-valued rdfs:range: always replaced (range is always known).
    ops.append(
        f"DELETE {{ GRAPH <{graph_uri}> {{ <{a_uri}> <{RDFS}#range> ?r }} }}\n"
        f"INSERT {{ GRAPH <{graph_uri}> {{ <{a_uri}> <{RDFS}#range> <{xsd_type}> }} }}\n"
        f"WHERE {{ GRAPH <{graph_uri}> {{ OPTIONAL {{ <{a_uri}> <{RDFS}#range> ?r }} }} }}"
    )

    # Single-valued rdfs:comment: delete old, insert new only if non-empty.
    if description:
        comment_insert = f"INSERT {{ GRAPH <{graph_uri}> {{ <{a_uri}> <{RDFS}#comment> \"{_esc(description)}\" }} }}\n"
    else:
        comment_insert = ""
    ops.append(
        f"DELETE {{ GRAPH <{graph_uri}> {{ <{a_uri}> <{RDFS}#comment> ?c }} }}\n"
        f"{comment_insert}"
        f"WHERE {{ GRAPH <{graph_uri}> {{ OPTIONAL {{ <{a_uri}> <{RDFS}#comment> ?c }} }} }}"
    )

    return " ;\n".join(ops)


def set_object_property_range(graph_uri: str, type_name: str, attr_name: str, target_type: str) -> str:
    """Re-point an existing property's ``rdfs:range`` at a type URI.

    Used to UPGRADE a predicate that was first registered as a primitive
    attribute (range ``xsd:string`` etc.) once it is later seen carrying an
    entity-valued object — i.e. it is really a relationship to ``target_type``.
    Without this the schema-only Explorer overview can't see the edge (it keys
    on ``rdfs:range`` being a ``types/`` URI), even though the instance triple
    exists. Deletes any existing range first so the property keeps exactly one.
    """
    a_uri = attr_uri(type_name, attr_name)
    rng = type_uri(target_type)
    return (
        f"DELETE {{ GRAPH <{graph_uri}> {{ <{a_uri}> <{RDFS}#range> ?old }} }}\n"
        f"INSERT {{ GRAPH <{graph_uri}> {{ <{a_uri}> <{RDFS}#range> <{rng}> }} }}\n"
        f"WHERE {{ GRAPH <{graph_uri}> {{ OPTIONAL {{ <{a_uri}> <{RDFS}#range> ?old }} }} }}"
    )


def retract_object_property(graph_uri: str, type_name: str, attr_name: str) -> str:
    """Retract a type-level object-property declaration to QUARANTINE (ADR 0004 §4).

    The inverse of :func:`set_object_property_range`: when reconciliation finds a
    non-core relationship whose live support has fallen below the drift floor, it
    must stop being a *declared* type-level edge. The schema-only Explorer
    overview keys an edge on the property having an ``rdfs:range`` that points at
    a ``types/`` URI and an ``rdfs:domain`` on the source type; deleting BOTH
    triples removes the edge from the overview while leaving the underlying
    instance triples untouched (row conservation, ADR 0003 §2 — only the schema
    *declaration* is withheld, the data still ingests).

    Quarantine-not-delete (ADR 0004 §2) is the *caller's* responsibility — it
    records support/source/timestamp in the quarantine store before issuing this
    retraction. This builder is the deterministic SPARQL half: it removes exactly
    the range and domain triples for ``attr_name`` and nothing else. The
    ``OPTIONAL`` wrappers make it a no-op-safe retraction (a property already
    missing its range or domain still retracts cleanly, no error).
    """
    a_uri = attr_uri(type_name, attr_name)
    return (
        f"DELETE {{ GRAPH <{graph_uri}> {{\n"
        f"  <{a_uri}> <{RDFS}#range> ?range .\n"
        f"  <{a_uri}> <{RDFS}#domain> ?domain .\n"
        f"}} }}\n"
        f"WHERE {{ GRAPH <{graph_uri}> {{\n"
        f"  OPTIONAL {{ <{a_uri}> <{RDFS}#range> ?range }}\n"
        f"  OPTIONAL {{ <{a_uri}> <{RDFS}#domain> ?domain }}\n"
        f"}} }}"
    )


def upsert_attribute_text_kind(
    graph_uri: str, type_name: str, attr_name: str, text_kind: str = TEXT_KIND_FREE_TEXT,
) -> str:
    """Idempotently set ONLY an attribute's ``<onto/textKind>`` marker
    (single-valued), leaving every other triple of the property untouched.

    ONTA-177: schema-time free-text candidacy (profiler ``ValueShape.TEXT``
    proposes, the LLM REASON layer adjudicates ambiguous cases) is persisted
    as ``<attr_uri> <onto/textKind> "free_text"`` so the semantic instance
    index (ONTA-173) and the query-side type filter (ONTA-176) can read the
    verdict without re-deciding it. Follows the same atomic
    ``DELETE/INSERT/WHERE`` pattern as :func:`upsert_type_comment` /
    :func:`upsert_attribute` for single-valued predicates: re-ingesting the
    same file replaces the marker instead of stacking duplicates, and a
    changed verdict (e.g. a future re-adjudication) never leaves two
    conflicting kinds behind. An empty ``text_kind`` clears any existing
    marker without inserting a replacement (the clear-on-empty rule the rest
    of this module's upserts use).
    """
    a_uri = attr_uri(type_name, attr_name)
    if text_kind:
        kind_insert = (
            f"INSERT {{ GRAPH <{graph_uri}> {{ <{a_uri}> <{INFONA_ONTO}/textKind> \"{_esc(text_kind)}\" }} }}\n"
        )
    else:
        kind_insert = ""
    return (
        f"DELETE {{ GRAPH <{graph_uri}> {{ <{a_uri}> <{INFONA_ONTO}/textKind> ?k }} }}\n"
        f"{kind_insert}"
        f"WHERE {{ GRAPH <{graph_uri}> {{ OPTIONAL {{ <{a_uri}> <{INFONA_ONTO}/textKind> ?k }} }} }}"
    )


def text_kind_map_query(graph_uri: str) -> str:
    """Select every ``textKind`` marker in a graph: ``?attr ?kind`` rows.

    Feeds the per-tenant ``{attribute predicate URI -> is_free_text}`` cache in
    :mod:`infona_client.graph.text_markers` (ONTA-177), which query-side
    consumers (semantic instance index routing, ONTA-176) read instead of
    hitting Neptune per request.
    """
    return (
        f"SELECT ?attr ?kind FROM <{graph_uri}>\n"
        f"WHERE {{\n"
        f"  ?attr <{INFONA_ONTO}/textKind> ?kind .\n"
        f"}}"
    )


def mark_core_slot(graph_uri: str, type_name: str, slot_name: str) -> str:
    """Mark one of ``type_name``'s declared attributes/relationship slots as
    CONSTITUTIVE (a core slot, ADR 0003 §3 / Pass D).

    Emits ``<attr_uri> <onto/coreSlot> "true"^^xsd:boolean``. Core slots may
    have zero data in the ingested file — the marker is what lets enrichment
    later query "instances with empty core slots" as its work queue, and what
    the governance pipeline (COG-56) keys its review on.
    """
    a_uri = attr_uri(type_name, slot_name)
    return (
        f"INSERT DATA {{\n"
        f"  GRAPH <{graph_uri}> {{\n"
        f'    <{a_uri}> <{INFONA_ONTO}/coreSlot> "true"^^<{XSD}#boolean> .\n'
        f"  }}\n"
        f"}}"
    )


def insert_subtype(graph_uri: str, parent_name: str, child_name: str) -> str:
    return (
        f"INSERT DATA {{\n"
        f"  GRAPH <{graph_uri}> {{\n"
        f"    <{type_uri(child_name)}> <{RDFS}#subClassOf> <{type_uri(parent_name)}> .\n"
        f"  }}\n"
        f"}}"
    )

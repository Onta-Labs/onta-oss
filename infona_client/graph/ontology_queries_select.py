"""SPARQL SELECT/ASK builders for ontology and entity lookups."""

from infona_client.graph.ontology_queries_uris import (
    INFONA_ONTO,
    RDF,
    RDFS,
    _esc,
    attr_uri,
    type_uri,
)


def list_types_query(graph_uri: str) -> str:
    return (
        f"SELECT ?type ?label ?comment ?parent FROM <{graph_uri}>\n"
        f"WHERE {{\n"
        f"  ?type <{RDF}#type> <{RDFS}#Class> .\n"
        f"  ?type <{RDFS}#label> ?label .\n"
        f"  OPTIONAL {{ ?type <{RDFS}#comment> ?comment }}\n"
        f"  OPTIONAL {{ ?type <{RDFS}#subClassOf> ?parent }}\n"
        f"}}"
    )


def get_type_detail_query(graph_uri: str, type_name: str) -> str:
    t_uri = type_uri(type_name)
    return (
        f"SELECT ?label ?comment ?parent FROM <{graph_uri}>\n"
        f"WHERE {{\n"
        f"  <{t_uri}> <{RDFS}#label> ?label .\n"
        f"  OPTIONAL {{ <{t_uri}> <{RDFS}#comment> ?comment }}\n"
        f"  OPTIONAL {{ <{t_uri}> <{RDFS}#subClassOf> ?parent }}\n"
        f"}}"
    )


def get_type_attributes_query(graph_uri: str, type_name: str) -> str:
    t_uri = type_uri(type_name)
    return (
        f"SELECT ?attr ?attrLabel ?attrComment ?range FROM <{graph_uri}>\n"
        f"WHERE {{\n"
        f"  ?attr <{RDF}#type> <{RDF}#Property> .\n"
        f"  ?attr <{RDFS}#domain> <{t_uri}> .\n"
        f"  ?attr <{RDFS}#label> ?attrLabel .\n"
        f"  OPTIONAL {{ ?attr <{RDFS}#comment> ?attrComment }}\n"
        f"  OPTIONAL {{ ?attr <{RDFS}#range> ?range }}\n"
        f"}}"
    )


def get_attribute_range_query(graph_uri: str, type_name: str, attr_name: str) -> str:
    """Fetch the single ``rdfs:range`` currently declared for one attribute.

    Returns ``?range`` (zero rows if the attribute / its range is undeclared).
    Used by enrichment to decide whether declaring an enriched attribute would
    DOWNGRADE an existing richer range (an XSD primitive like ``xsd:integer`` or
    a relationship ``types/<Target>`` URI) down to ``xsd:string`` — it must not.
    """
    a_uri = attr_uri(type_name, attr_name)
    return (
        f"SELECT ?range FROM <{graph_uri}>\n"
        f"WHERE {{\n"
        f"  <{a_uri}> <{RDFS}#range> ?range .\n"
        f"}}"
    )


def get_subtypes_query(graph_uri: str, type_name: str) -> str:
    t_uri = type_uri(type_name)
    return (
        f"SELECT ?sub ?label FROM <{graph_uri}>\n"
        f"WHERE {{\n"
        f"  ?sub <{RDFS}#subClassOf> <{t_uri}> .\n"
        f"  ?sub <{RDFS}#label> ?label .\n"
        f"}}"
    )


def get_type_functions_query(graph_uri: str, type_name: str) -> str:
    t_uri = type_uri(type_name)
    return (
        f"SELECT ?name ?endpoint ?desc FROM <{graph_uri}>\n"
        f"WHERE {{\n"
        f"  ?func <{INFONA_ONTO}/attachedTo> <{t_uri}> .\n"
        f"  ?func <{INFONA_ONTO}/name> ?name .\n"
        f"  OPTIONAL {{ ?func <{INFONA_ONTO}/endpointUrl> ?endpoint }}\n"
        f"  OPTIONAL {{ ?func <{INFONA_ONTO}/description> ?desc }}\n"
        f"}}"
    )


def parent_map_query(graph_uri: str | list[str]) -> str:
    """Select every rdfs:subClassOf edge so a caller can build a child->parent map.

    Returns ?child ?parent for all `?child rdfs:subClassOf ?parent` triples.
    The caller turns these bindings into `parent_of: dict[str, str]` (keyed by
    the type *name*, i.e. the last URI path segment) for hierarchy walks used by
    config_for_with_hierarchy / primary_type / ancestor_chain.

    Layer-aware variant (ADR 0002 §1, COG-37): pass a LIST of graph URIs (a
    LayerStack's visible_graph_uris()) and the query reads the UNION of those
    graphs — subClassOf edges may span layers (a tenant leaf under a Public
    parent). Each UNION branch is a GRAPH pattern (the form Neptune handles
    cleanly) and BINDs its graph URI to ?graph so the caller can apply layer
    precedence (shadowing) when merging duplicate child edges.

    The single-graph (str) form is byte-identical to the pre-layer query —
    regression-critical for existing callers.
    """
    if isinstance(graph_uri, str):
        return (
            f"SELECT ?child ?parent FROM <{graph_uri}>\n"
            f"WHERE {{\n"
            f"  ?child <{RDFS}#subClassOf> ?parent .\n"
            f"}}"
        )
    branches = "\n  UNION\n".join(
        f"  {{ GRAPH <{g}> {{ ?child <{RDFS}#subClassOf> ?parent . }} BIND(<{g}> AS ?graph) }}"
        for g in graph_uri
    )
    return (
        f"SELECT ?child ?parent ?graph\n"
        f"WHERE {{\n"
        f"{branches}\n"
        f"}}"
    )


def with_subclass_closure(type_name: str) -> str:
    """Return the SPARQL property-path predicate that matches `type_name` and any
    of its subtypes: `a/<RDFS#subClassOf>*`.

    Used in place of a bare `a`/rdf:type predicate so a query over a parent type
    returns subtype instances too (ADR rule 2 — query-time subclass closure).
    The trailing `<type_uri(type_name)>` object is supplied by the caller.
    """
    return f"<{RDF}#type>/<{RDFS}#subClassOf>*"


def get_full_ontology_query(graph_uri: str) -> str:
    """Get all types, attributes, and functions in one query for the NL pipeline."""
    return (
        f"SELECT ?type ?typeLabel ?attr ?attrLabel ?range ?funcName FROM <{graph_uri}>\n"
        f"WHERE {{\n"
        f"  ?type <{RDF}#type> <{RDFS}#Class> .\n"
        f"  ?type <{RDFS}#label> ?typeLabel .\n"
        f"  OPTIONAL {{\n"
        f"    ?attr <{RDFS}#domain> ?type .\n"
        f"    ?attr <{RDFS}#label> ?attrLabel .\n"
        f"    OPTIONAL {{ ?attr <{RDFS}#range> ?range }}\n"
        f"  }}\n"
        f"  OPTIONAL {{\n"
        f"    ?func <{INFONA_ONTO}/attachedTo> ?type .\n"
        f"    ?func <{INFONA_ONTO}/name> ?funcName .\n"
        f"  }}\n"
        f"}}"
    )


def full_ontology_detail_query(graph_uri: str) -> str:
    """Every type in ONE graph with its full browsable detail, in ONE query.

    Namespace-agnostic (it never mentions a ``types/`` prefix), so the same
    builder reads a tenant ontology graph or either Global layer graph
    (``types/public/<T>`` / ``types/x/<T>``). Superset of
    :func:`get_full_ontology_query`, which the NL pipeline uses: this one also
    projects ``?typeComment``, ``?parent``, ``?attrComment`` and ``?core`` — the
    fields the operator Global-ontology browser searches and renders — and it
    projects the attached-function join in RICHER form (name + description +
    endpoint URL, not just ``?funcName``).

    Row shape is one row per (type × parent × attribute × attached function)
    combination; a type with no attributes and no functions still yields one row
    (both blocks are OPTIONAL), which is what makes an "empty" type visible
    instead of silently dropped. The two OPTIONAL blocks are INDEPENDENT, so a
    type with A attributes and F functions yields A×F rows — the reader folds
    them back per (type, slot) / (type, function name) and is idempotent under
    the repetition, which is why the cross-product is harmless rather than
    something to work around with a second round trip.

    The function pattern is matched WHOLLY inside the graph being read, so a
    function surfaces here only when it was declared against a LAYER-QUALIFIED
    type URI (``types/x/<T>`` for Enhanced; Public may not carry functions —
    ONTA-400) in that layer's graph. ``queries.register_function_triple``
    (ONTA-399) mints the correct layer-qualified attachment and writes Enhanced
    functions into ``graphs/global/enhanced``.

    Deliberately LENIENT on the attribute pattern: it keys on
    ``rdfs:domain`` + ``rdfs:label`` and does NOT require ``rdf:type
    rdf:Property``. Every writer in the repo does assert that triple
    (``insert_attribute``/``upsert_attribute``, and the premium
    ``GlobalShapeWriter``), so requiring it would change nothing today while
    silently hiding any future slot written without it.

    ``ORDER BY`` is load-bearing, not cosmetic. SPARQL leaves solution order
    UNSPECIFIED without it, and the reader folds multi-row results back into one
    record per type. A predicate that is single-valued by contract but doubly
    asserted in practice (a blind ``INSERT DATA``, a half-run migration) would
    otherwise resolve differently between two identical requests — most visibly
    a slot with both an XSD and a ``types/…`` range flipping between
    ``attributes`` and ``relationships``. The reader ALSO folds deterministically
    on its own side (``global_ontology._pick``), so correctness never depends on
    the engine honoring this; the ordering just makes the wire bytes reproducible
    too.
    """
    return (
        f"SELECT ?type ?typeLabel ?typeComment ?parent ?attr ?attrLabel "
        f"?attrComment ?range ?core ?funcName ?funcDesc ?funcEndpoint "
        f"FROM <{graph_uri}>\n"
        f"WHERE {{\n"
        f"  ?type <{RDF}#type> <{RDFS}#Class> .\n"
        f"  ?type <{RDFS}#label> ?typeLabel .\n"
        f"  OPTIONAL {{ ?type <{RDFS}#comment> ?typeComment }}\n"
        f"  OPTIONAL {{ ?type <{RDFS}#subClassOf> ?parent }}\n"
        f"  OPTIONAL {{\n"
        f"    ?attr <{RDFS}#domain> ?type .\n"
        f"    ?attr <{RDFS}#label> ?attrLabel .\n"
        f"    OPTIONAL {{ ?attr <{RDFS}#comment> ?attrComment }}\n"
        f"    OPTIONAL {{ ?attr <{RDFS}#range> ?range }}\n"
        f"    OPTIONAL {{ ?attr <{INFONA_ONTO}/coreSlot> ?core }}\n"
        f"  }}\n"
        f"  OPTIONAL {{\n"
        f"    ?func <{INFONA_ONTO}/attachedTo> ?type .\n"
        f"    ?func <{INFONA_ONTO}/name> ?funcName .\n"
        f"    OPTIONAL {{ ?func <{INFONA_ONTO}/description> ?funcDesc }}\n"
        f"    OPTIONAL {{ ?func <{INFONA_ONTO}/endpointUrl> ?funcEndpoint }}\n"
        f"  }}\n"
        f"}}\n"
        f"ORDER BY ?type ?typeLabel ?typeComment ?parent ?attr ?attrLabel "
        f"?attrComment ?range ?core ?funcName ?funcDesc ?funcEndpoint"
    )


def entity_exists_query(graph_uri: str, entity_uri: str) -> str:
    """Check if an entity URI already exists in the graph."""
    return (
        f"ASK FROM <{graph_uri}> WHERE {{\n"
        f"  <{entity_uri}> ?p ?o .\n"
        f"}}"
    )


def batch_entity_exists_query(graph_uri: str, entity_uris: list[str]) -> str:
    """Check which entity URIs already exist in the graph.

    Uses SPARQL VALUES clause to batch-check up to 500 URIs at once.
    Returns entity URIs that have at least one triple.
    """
    values = " ".join(f"(<{uri}>)" for uri in entity_uris)
    return (
        f"SELECT DISTINCT ?entity FROM <{graph_uri}> WHERE {{\n"
        f"  VALUES (?entity) {{ {values} }}\n"
        f"  ?entity ?p ?o .\n"
        f"}}"
    )


def entities_by_key_value_query(
    graph_uri: str, type_name: str, key_attr: str, key_values: list[str],
) -> str:
    """Find existing entities of ``type_name`` whose ``key_attr`` equals one of
    ``key_values`` — the lookup that powers **join-by-exact-key ingest** (ONTA-250:
    merge a CSV row onto the EXISTING entity that already carries the same key
    value, instead of minting a duplicate).

    Matches on the LEXICAL string of the stored object (``STR(?o) = value``), so a
    key stored as a typed literal (``"123"^^xsd:integer``), a plain string, or even
    an entity IRI whose string form equals the value all join correctly — the same
    datatype-agnostic comparison the promote/delete path learned it needs. Keyed
    by the canonical ``attrs/<key_attr>`` predicate (the schema-declared attribute
    URI every writer stores a literal attribute value on), so this is fully general
    over any (type, key-attribute) pair — no per-domain assumptions.

    Returns ``(?v ?entity)`` rows: the matched value and the existing entity URI.
    A value matching several existing entities yields several rows (the caller
    decides how to resolve an ambiguous key). ``VALUES`` batches up to a few
    hundred values per query.
    """
    a_uri = attr_uri(type_name, key_attr)
    t_uri = type_uri(type_name)
    values = " ".join(f'("{_esc(v)}")' for v in key_values)
    return (
        f"SELECT DISTINCT ?v ?entity FROM <{graph_uri}> WHERE {{\n"
        f"  VALUES (?v) {{ {values} }}\n"
        f"  ?entity <{RDF}#type> <{t_uri}> .\n"
        f"  ?entity <{a_uri}> ?o .\n"
        f"  FILTER(STR(?o) = STR(?v))\n"
        f"}}"
    )

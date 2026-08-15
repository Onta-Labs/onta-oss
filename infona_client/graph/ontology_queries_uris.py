"""URI minting + RDF/XSD constants for ontology SPARQL builders.

``type_uri`` / ``attr_uri`` live here so mutate/select/rewrite siblings can
import them without cycling through the facade. Instance-node minting
(``entity_uri`` / ``_safe_id``) stays defined in ``ontology_queries.py`` —
that is the single mint (``tests/test_entity_uri_convergence.py``).
"""

from infona_client.graph.iri import (
    ONTO_BASE,
    TYPE_URI_PREFIX,
)
from infona_client.graph.queries import (
    require_valid_type_name,
    sparql_string_literal,
)

INFONA_ONTO = ONTO_BASE
RDFS = "http://www.w3.org/2000/01/rdf-schema"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns"
XSD = "http://www.w3.org/2001/XMLSchema"
# OGC GeoSPARQL — the standard vocabulary for geometry literals. A `geo` attribute
# carries the range ``geo:wktLiteral`` and stores its value as WKT ("POINT(lon lat)")
# so coordinates are a first-class, datatype-tagged literal the spatio-temporal index
# can read directly (rather than guessing from attribute names at read time).
GEOSPARQL = "http://www.opengis.net/ont/geosparql"

# The tenant-layer type namespace. A named constant for the same reason
# ``GRAPH_URI_PREFIX`` is one: callers that want the PREFIX (to strip it off a
# URI) used to spell it ``type_uri("")``, which only worked while the builder
# accepted a name it should reject. ``layers._TYPE_NAMESPACES[Layer.TENANT]`` and
# ``explore.TYPE_URI_PREFIX`` are the same string by definition; both now alias
# this one.
_TYPES_URI = TYPE_URI_PREFIX


def type_uri(type_name: str) -> str:
    """Ontology IRI for a type.

    ONTA-425: validates HERE, not at the ~40 call sites, for the same reason
    ``kg_graph_uri`` validates ``kg_name`` (ONTA-414). The result is
    interpolated verbatim inside ``<…>`` in generated SPARQL, and several
    callers take the name straight off a URL path segment
    (``GET /explore/kgs/{kg}/types/{type_name}/summary``) or a request body
    (``POST /ontology/types/{type_name}/attributes``). A name carrying ``>``
    closes the IRI early and the remainder becomes query syntax — on the
    ontology WRITE paths that syntax lands in a ``client.update``, where ``;``
    starts a second operation.

    Read paths that ENUMERATE stored names must not let one bad name break the
    listing: they branch on :func:`is_valid_type_name` and skip, rather than
    catching this. See ``api/routes/explore.search_explorer``.
    """
    return f"{TYPE_URI_PREFIX}{require_valid_type_name(type_name)}"


def attr_uri(type_name: str, attr_name: str) -> str:
    """Ontology IRI for one attribute of a type.

    BOTH segments are validated (ONTA-425): an attribute leaf is interpolated
    into the same IRI and reaches it from the same request bodies. Note the
    validator deliberately still allows ``/`` — two attribute names in
    production contain one — because ``/`` cannot escape ``<…>``.
    """
    return (
        f"{TYPE_URI_PREFIX}{require_valid_type_name(type_name)}"
        f"/attrs/{require_valid_type_name(attr_name, 'attribute name')}"
    )


#: Canonical value of the ``textKind`` marker for free-running prose
#: attributes (ONTA-177). Kept as a plain literal (not a boolean) so future
#: kinds ("code", "label", …) can share the same single-valued predicate.
TEXT_KIND_FREE_TEXT = "free_text"

#: Canonical value of the ``textKind`` marker for a DURABLE decided-NO
#: candidacy verdict (ONTA-173): the candidacy tier genuinely ADJUDICATED this
#: attribute (the LLM REASON layer declined a TEXT-shaped column, or the
#: reconciler's name-blind heuristic classified it NOT_CANDIDATE) and found it
#: not free text. Persisting the NO matters: absence from the marker map means
#: "never decided" — the reconciler would re-sample the attribute every run,
#: and the name-blind ≥120-char auto tier could later overrule the LLM's
#: explicit NO. In ``text_markers.get_free_text_map`` any kind other than
#: ``free_text`` reads back as ``is_free_text=False`` while remaining PRESENT
#: in the map (presence = decided), which is exactly the skip signal the
#: reconciler keys on. NOTE: ``semantic/reconciler.py`` predates this constant
#: and carries a same-valued local duplicate (``TEXT_KIND_NOT_TEXT`` at module
#: scope) — converge it onto this one in a follow-up.
TEXT_KIND_NOT_TEXT = "not_text"


def _esc(s: str) -> str:
    # Delegates to the ONE hardened escaper (ONTA-416). This module's copy was
    # the hardened one (`\r`/`\t` added by ONTA-250 when CSV user values started
    # flowing through entities_by_key_value_query) while two other copies lagged;
    # promoting it to graph/queries.sparql_string_literal and delegating here
    # keeps a single definition so the coverage can never diverge again.
    return sparql_string_literal(s)


PRIMITIVE_TYPES = {"string", "integer", "float", "boolean", "datetime", "uri", "geo"}


# The XSD ``rdfs:range`` URI an attribute carries when it is a plain string
# attribute — the weakest primitive range, the only one enrichment is allowed to
# overwrite (a string range is "untyped enough" that an inferred richer type is a
# strict improvement; anything else is a downgrade we must preserve).
XSD_STRING = f"{XSD}#string"

_DATATYPE_TO_XSD = {
    "string": f"{XSD}#string",
    "integer": f"{XSD}#integer",
    "float": f"{XSD}#float",
    "boolean": f"{XSD}#boolean",
    "datetime": f"{XSD}#dateTime",
    "uri": f"{RDFS}#Resource",
    # WGS84 point/geometry as WKT — read by the spatio-temporal index.
    "geo": f"{GEOSPARQL}#wktLiteral",
}


def _datatype_to_xsd(datatype: str) -> str:
    if datatype in _DATATYPE_TO_XSD:
        return _DATATYPE_TO_XSD[datatype]
    # Treat as a reference to another type
    return type_uri(datatype)


def xsd_to_datatype(range_uri: str) -> str:
    """Reverse of :func:`_datatype_to_xsd`: map a declared ``rdfs:range`` URI back
    to the ``datatype`` name :func:`upsert_attribute` accepts, so an existing
    range can be RE-asserted verbatim.

    A primitive XSD/Resource URI maps to its name (``…#integer`` -> ``integer``);
    a ``types/<X>`` relationship URI maps to the bare type name ``X`` (which
    ``_datatype_to_xsd`` round-trips back to the same ``types/<X>`` URI). Any
    other/unknown URI falls back to ``string`` so a malformed range can never
    crash a declaration."""
    for name, uri in _DATATYPE_TO_XSD.items():
        if range_uri == uri:
            return name
    if range_uri.startswith(_TYPES_URI):
        return range_uri[len(_TYPES_URI):].rstrip("/")
    return "string"

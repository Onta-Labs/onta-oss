"""Shared predicate-hygiene helper — the ONE definition of "is this an internal /
housekeeping predicate?" used by every user-facing surface.

Historically this logic lived only in ``api/routes/explore.py`` and was applied
only on the Explorer's Attributes / Relationships panels, so entity-resolution
internals (``er/blockKey``, ``er/erSignal_*``), ingest housekeeping
(``onto/batch_id``, ``onto/ingested_at``, …) and normalization bookkeeping
(``onto/norm/*``) leaked straight into the NL ``ask`` answer text (a
``SELECT ?p ?o`` "describe this entity" dumped them verbatim). Lifting the filter
here — with the constants and ``is_internal_predicate`` in one module — lets the
Explorer routes AND the NL pipeline share the SAME rule (no copy), so a
newly-added housekeeping marker is excluded on every surface at once.

Namespace/marker-based on purpose:
  * whole-namespace exclusions: RDF/RDFS system vocab, the ER namespace
    (``…/er/``), and the normalization-bookkeeping namespace (``…/onto/norm/``) —
    never legitimate domain data, attribute OR relationship;
  * a curated set of housekeeping markers under ``…/onto/`` (``batch_id``,
    ``ingested_at``, ``source``, ``coreSlot``, ``aliasOf``, ``lambda_refreshed_at``)
    that ingest/lambda attach to every entity.

The whole ``…/onto/`` namespace can NOT be blanket-excluded: real relationship
INSTANCE edges are minted at ``…/onto/<predName>`` (see the instance-edge
convention in CLAUDE.md), so a legitimate relationship such as
``…/onto/hasAffiliation`` shares the namespace with the housekeeping markers.
Hence the curated-marker approach, plus the ``is_relationship`` exemption below.
"""

from __future__ import annotations

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS_NS = "http://www.w3.org/2000/01/rdf-schema#"

# Instance relationship predicates are minted as `…/onto/<predName>`; the whole
# namespace therefore also carries the housekeeping markers below.
ONTO_PRED_PREFIX = "https://cograph.tech/onto/"
# Entity-resolution internals (blockKey, erSignal_*) — never user-facing domain data.
ER_NS = "https://cograph.tech/er/"
# Normalization internals (canonical-value bookkeeping) live under …/onto/norm/.
ONTO_NORM_PREFIX = ONTO_PRED_PREFIX + "norm/"
# Per-attribute provenance companions (`attr_meta/<Type>/<attr>/<suffix>`) —
# metadata OF one attribute (its citation URL / human citation / freshness
# stamp), never an attribute itself (ONTA-262). Queryable instance data (NL
# freshness FILTERs + citation rendering read it by convention), but excluded
# from every surface that renders predicates as domain attributes. Minted by
# graph/provenance.py::attr_provenance_companion_uri for BOTH rails.
ATTR_META_NS = "https://cograph.tech/attr_meta/"
# Valid-time interval predicates (ONTA-277). Live in a SEPARATE companion graph
# (`<data-graph>/validity`, graph/validity.py) and so never actually appear on the
# instance graph a user surface reads — but the whole namespace is classified
# internal for defense-in-depth, so a validity predicate could never be surfaced
# as a domain attribute even if one leaked onto the instance graph.
VALIDITY_NS = "https://cograph.tech/validity/"
# Suppression-list predicates (ONTA-279). The sticky, reopen-proof retraction
# marker; like validity, lives in a SEPARATE companion graph
# (`<data-graph>/suppression`, graph/suppression.py) and is classified internal
# whole-namespace for the same defense-in-depth reason.
SUPPRESSION_NS = "https://cograph.tech/suppression/"
# The three companion suffixes (also the `<attr>_<suffix>` tails of the legacy
# attrs/-namespace shape that pre-ONTA-262 graphs still carry).
ATTR_META_SUFFIXES: tuple[str, ...] = ("source_url", "provenance", "verified_at")

# Literal-valued system markers that ingest attaches to (almost) every entity.
SYSTEM_PREDICATES: frozenset[str] = frozenset({
    f"{RDFS_NS}label",
    ONTO_PRED_PREFIX + "ingested_at",
    ONTO_PRED_PREFIX + "source",
})

# Curated housekeeping markers under …/onto/ (the namespace also holds real
# relationship predicates, so we match the specific markers, not the prefix).
# Namespace/marker-based, not a copy of the three observed leaves, so a newly
# added housekeeping marker in one of these namespaces is excluded too.
INTERNAL_ONTO_MARKERS: frozenset[str] = frozenset({
    ONTO_PRED_PREFIX + "batch_id",
    ONTO_PRED_PREFIX + "ingested_at",
    ONTO_PRED_PREFIX + "source",
    ONTO_PRED_PREFIX + "coreSlot",
    ONTO_PRED_PREFIX + "aliasOf",
    ONTO_PRED_PREFIX + "lambda_refreshed_at",
})


def is_internal_predicate(p_uri: str, is_relationship: bool = False) -> bool:
    """True if ``p_uri`` is an internal/housekeeping predicate, not a user-facing
    domain attribute or relationship.

    Applied everywhere raw predicates reach a user-facing surface — the Explorer's
    Attributes / Relationships panels, the stats scan/recompute, AND the NL
    ``ask`` answer render — so internal triples (``onto/batch_id``,
    ``er/blockKey``, ``er/erSignal_*``, ``onto/norm/*``, rdf*/rdfs*) never appear.
    Kept namespace-based on purpose — see the module docstring for why the whole
    ``…/onto/`` namespace can't be excluded.

    ``is_relationship``: the curated ``INTERNAL_ONTO_MARKERS`` (onto/source,
    onto/batch_id, …) and ``SYSTEM_PREDICATES`` are ALWAYS literal-valued
    housekeeping. A real RELATIONSHIP predicate that happens to share one of those
    leaf names — e.g. a measurement minted with predicate ``…/onto/source``
    pointing at an Organization entity — must NOT be hidden. So when the caller
    knows this predicate is a relationship (its object is an entity IRI / the
    ontology declares an entity range), pass ``is_relationship=True`` and the
    marker check is skipped. The namespace exclusions (ER, onto/norm, rdf/rdfs)
    still apply to relationships, since those are never legitimate domain edges.
    """
    if not p_uri:
        return True
    if p_uri == RDF_TYPE:
        return True
    # Whole-namespace exclusions: RDF/RDFS system vocab + ER + normalization.
    # These are never legitimate domain data, attribute OR relationship.
    if p_uri.startswith(RDF_NS) or p_uri.startswith(RDFS_NS):
        return True
    if p_uri.startswith(ER_NS) or p_uri.startswith(ONTO_NORM_PREFIX):
        return True
    # Valid-time interval predicates (validity/…) are governance bookkeeping in a
    # companion graph, never a domain attribute or relationship (ONTA-277).
    if p_uri.startswith(VALIDITY_NS):
        return True
    # Suppression-list predicates (suppression/…) are governance bookkeeping in a
    # companion graph, never a domain attribute or relationship (ONTA-279).
    if p_uri.startswith(SUPPRESSION_NS):
        return True
    # Per-attribute provenance companions (attr_meta/<Type>/<attr>/<suffix>) are
    # metadata OF an attribute, never a domain attribute or relationship
    # (ONTA-262) — excluded before the is_relationship exemption on purpose:
    # companions are always literal-valued, so nothing legitimate is hidden.
    if p_uri.startswith(ATTR_META_NS):
        return True
    # A relationship is exempt from the literal-only housekeeping checks below:
    # SYSTEM_PREDICATES (rdfs:label, onto/ingested_at, onto/source) and
    # INTERNAL_ONTO_MARKERS are all literal-valued markers, so a same-named
    # relationship is a real domain edge, not housekeeping.
    if is_relationship:
        return False
    if p_uri in SYSTEM_PREDICATES:
        return True
    # Curated housekeeping markers under …/onto/ (the namespace also holds real
    # relationship predicates, so we match the specific markers, not the prefix).
    if p_uri in INTERNAL_ONTO_MARKERS:
        return True
    return False


def companion_leaves(leaves) -> set[str]:
    """Classify LEGACY per-attribute provenance companions among one type's
    attribute/predicate leaf names.

    Graphs written before ONTA-262 carry companions on the ATTRIBUTE namespace
    (``attrs/<attr>_<suffix>``), indistinguishable from domain attributes by URI
    alone. This is the deterministic read-side classifier: given ALL leaf names
    observed/declared for ONE type, a leaf is a companion iff it is
    ``<base>_<suffix>`` (suffix ∈ ``ATTR_META_SUFFIXES``) AND ``<base>`` is
    itself present in the set — so a hypothetical real attribute that merely
    ends in ``_provenance`` with no base attribute alongside it is never
    misclassified. Callers use this to keep legacy companions off user-facing
    attribute surfaces until the attr_meta migration rewrites them.

    ``leaves`` is any iterable of leaf strings; returns the subset classified as
    companions."""
    ls = set(leaves)
    out: set[str] = set()
    for leaf in ls:
        for sfx in ATTR_META_SUFFIXES:
            tail = f"_{sfx}"
            if leaf.endswith(tail) and leaf[: -len(tail)] in ls:
                out.add(leaf)
                break
    return out

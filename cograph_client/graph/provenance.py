"""Per-fact provenance substrate (ADR 0002 §4).

Every attribute assertion can carry provenance — source, timestamp,
confidence — queryable later for conflict resolution, explainability,
and wholesale undo of a bad source.

Encoding decision (Neptune has NO RDF-star, so a triple cannot be
annotated in place): a dedicated **companion provenance named graph** per
data graph (``<data-graph>/provenance``) holding one statement-metadata
node per (fact, source) assertion. Chosen over per-source named graphs
because it composes with the existing single-data-graph layout — instance
triples stay exactly where they are, and "undo a source" / conflict
resolution become SELECTs over one graph instead of a graph-per-source
fan-out.

Keying:
- ``statement_id = sha1(s|p|o)`` identifies the *fact* (over the raw
  strings as written to Neptune, typed-literal convention included), so
  all assertions of the same fact group trivially.
- The metadata node is keyed by ``sha1(s|p|o|source)`` — one node per
  fact *per source* — so two sources asserting the same fact each carry
  their own (source, timestamp, confidence) without cross-products on
  read, and dropping a source is a single filtered DELETE.

For a fact (s, p, o) asserted by ``source`` the provenance graph holds::

    <https://cograph.tech/prov/stmt/{sha1(s|p|o|source)}>
        prov:subject    <s> ;
        prov:predicate  <p> ;
        prov:object     o ;                       # literal or URI, as written
        prov:statement  "{sha1(s|p|o)}" ;
        prov:source     "crm_export.csv" ;
        prov:confidence "1.0"^^xsd:float ;
        prov:timestamp  "2026-06-09T00:00:00+00:00"^^xsd:dateTime ;
        prov:graph      <data graph the fact lives in> .

Triples are idempotent on Neptune: re-ingesting the same fact from the
same source rewrites the same node (a refreshed timestamp accumulates as
an additional literal — last-write-wins policies resolve over max).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from cograph_client.graph.parser import parse_sparql_results
# The companion metadata namespace + suffixes are defined canonically in
# graph/predicates.py (the shared predicate-hygiene module every read surface
# already imports) and re-exported here so writers and readers resolve the SAME
# constants. predicates.py imports nothing from this module (no cycle).
from cograph_client.graph.predicates import ATTR_META_NS, ATTR_META_SUFFIXES  # noqa: F401
from cograph_client.graph.queries import _escape_value

PROV_NS = "https://cograph.tech/prov/"

# --- Per-attribute DISPLAY provenance companions (ADR 0009 / ONTA-245) ---------
#
# The canonical companion-provenance GRAPH above (build_provenance_triples) is the
# governance/undo substrate. Enrichment and discovery ALSO surface a small, shared
# set of per-attribute INSTANCE triples on the entity itself — the user-facing
# citations the Explorer + /ask render:
#
#   <entity> <attr_meta/<Type>/<attr>/source_url>  "https://…"        (plain string)
#   <entity> <attr_meta/<Type>/<attr>/provenance>  "wikidata (…)"      (plain string)
#   <entity> <attr_meta/<Type>/<attr>/verified_at> "…"^^xsd:dateTime   (TYPED date)
#
# Companions are METADATA OF one attribute, not attributes themselves (ONTA-262,
# founder decision 2026-07-10). They therefore live on their OWN top-level
# namespace (``attr_meta/``, mirroring the ``er/`` internals namespace) — NOT on
# ``types/<Type>/attrs/`` — and are NEVER declared in the ontology. The shared
# predicate-hygiene rule (graph/predicates.py::is_internal_predicate) excludes the
# whole namespace, so companions are structurally invisible to the Explorer's
# Attributes/Relationships panels, type-stats, the records table, and NL answer
# dumps, while remaining ordinary queryable instance triples for freshness
# FILTERs and citation rendering. (Graphs written before this convention carry
# companions on ``attrs/<attr>_<suffix>``; predicates.companion_leaves classifies
# those read-side, and the attr_meta migration rewrites them.)
#
# This is the deliberate dual-purpose split CLAUDE.md sanctions: the graph is the
# governance record, these companions are the display projection. BOTH flow through
# the shared write path (insert_facts) — the companions ride in ``instance_triples``,
# the canonical record in ``provenance_triples``.
#
# The ONE reason ``<attr>_verified_at`` is typed ``xsd:dateTime`` (not a plain
# string like the other two): the NL planner emits typed date FILTERs
# (``FILTER(?ts >= NOW() - "P7D"^^xsd:duration)`` — xsd:duration, not
# dayTimeDuration, which Neptune rejects; see nlp/pipeline._neptune_safe_duration),
# and an untyped string stamp is type-incompatible → the freshness query silently
# drops the row (ONTA-247). ``_TYPES_PREFIX`` mirrors the executor's ``TYPE_URI_PREFIX`` and the
# stamp reuses the module ``_XSD`` datetime type, so the SAME literal shape is
# produced whichever rail writes it (cross-rail symmetry).

_TYPES_PREFIX = "https://cograph.tech/types/"
PROV_SUBJECT = f"{PROV_NS}subject"
PROV_PREDICATE = f"{PROV_NS}predicate"
PROV_OBJECT = f"{PROV_NS}object"
PROV_STATEMENT = f"{PROV_NS}statement"
PROV_SOURCE = f"{PROV_NS}source"
PROV_CONFIDENCE = f"{PROV_NS}confidence"
PROV_TIMESTAMP = f"{PROV_NS}timestamp"
PROV_GRAPH = f"{PROV_NS}graph"
# The source AUTHORITY level a fact was asserted under (ONTA-276). Source-of-truth
# priority is set upstream (P1) but must survive to the P6 write-time conflict
# point, so the conflict policy can rank a stored fact's authority against an
# incoming contradicting one. Recorded per (fact, source) alongside confidence;
# optional (absent on pre-ONTA-276 provenance), read back into
# ``ProvenanceRecord.authority``.
PROV_AUTHORITY = f"{PROV_NS}authority"

# Removal / rename events (ADR 0007). Assertions above record a fact ARRIVING;
# these record a fact LEAVING (``tombstone``) or a subject being RENAMED
# (``rewrite``), so governance/undo sees the full lifecycle — not just inserts.
# They live in the same companion provenance graph as assertions and are written
# by the ``delete_facts`` / ``rewrite_subject`` primitives (kg_writer.py), gated
# by ``COGRAPH_PROVENANCE_ENABLED`` exactly like assertion provenance.
PROV_EVENT = f"{PROV_NS}event"  # "tombstone" | "rewrite" | "supersede" | "retract" | "lost_conflict"
PROV_REASON = f"{PROV_NS}reason"
PROV_REWRITTEN_TO = f"{PROV_NS}rewrittenTo"  # rewrite event: old subject → new URI
PROV_AFFECTED_TYPE = f"{PROV_NS}affectedType"  # type(s) touched by the removal/rename

# Supersession / retraction events (ONTA-277). A fact LOSING currency records an
# event here (governance/undo substrate), distinct from the always-on valid-time
# interval (graph/validity.py) that powers the "current facts" read. ``supersede``
# names the replacing fact (``supersededBy``); ``retract`` asserts no-longer-true.
PROV_SUPERSEDED_BY = f"{PROV_NS}supersededBy"  # supersede event: replacement statement id
PROV_VALID_TO = f"{PROV_NS}validTo"  # when the fact stopped being current

EVENT_TOMBSTONE = "tombstone"
EVENT_REWRITE = "rewrite"
EVENT_SUPERSEDE = "supersede"
EVENT_RETRACT = "retract"
# A fact that LOST a functional-attribute conflict at write time (ONTA-276): the
# same string as validity.STATUS_DEPRECATED so the governance event and the
# valid-time closure agree on the reason. Distinct from ``supersede`` (driven by a
# newer fact) — a loss is driven by a stronger CONTEMPORANEOUS source.
EVENT_CONFLICT_LOSS = "lost_conflict"

# First-class merge / split lineage events (ONTA-274). A merge/split is a DESIGNED,
# lineage-preserving P6 operation — NOT post-write ER cleanup. Merge re-keys the
# merged-away URI onto the canonical via ``kg_writer.rewrite_subject`` (one re-key
# event, so the ``rewrite`` event above is ALSO written by that primitive) and, on
# top of that, records a REVERSIBLE lineage snapshot here so a later ``split`` can
# restore the two nodes' independent identities. Unlike the other governance events
# (gated by ``COGRAPH_PROVENANCE_ENABLED``), the merge lineage snapshot is ALWAYS
# written — it is load-bearing for split reversibility, exactly as the valid-time
# interval (graph/validity.py) is always written regardless of the gate.
EVENT_MERGE = "merge"
EVENT_SPLIT = "split"

# The reversible snapshot: each fact of the merged and canonical nodes as it stood
# JUST BEFORE the merge re-keyed them, reified onto its own node so ``split`` can
# re-attribute facts to the right side. Kept on the ``prov/lineage/`` sub-namespace
# so it never collides with an assertion/event node.
LINEAGE_NS = f"{PROV_NS}lineage/"
LIN_OF_MERGE = f"{PROV_NS}lineageOfMerge"  # snapshot fact -> its merge event node
LIN_ORIGIN = f"{PROV_NS}lineageOrigin"  # "merged" | "canonical" — which side it was
LIN_S = f"{PROV_NS}lineageSubject"  # the fact's subject, in ORIGINAL (pre-merge) form
LIN_P = f"{PROV_NS}lineagePredicate"
LIN_O = f"{PROV_NS}lineageObject"  # object, term-faithfully round-tripped (ONTA-247)
ORIGIN_MERGED = "merged"
ORIGIN_CANONICAL = "canonical"

_XSD = "http://www.w3.org/2001/XMLSchema"


def provenance_graph_uri(graph_uri: str) -> str:
    """Companion provenance graph for a data graph."""
    return f"{graph_uri}/provenance"


def statement_id(subject: str, predicate: str, obj: str) -> str:
    """Deterministic fact id: sha1 over the raw s|p|o strings as written."""
    return hashlib.sha1(f"{subject}|{predicate}|{obj}".encode("utf-8")).hexdigest()


def _assertion_uri(subject: str, predicate: str, obj: str, source: str) -> str:
    """Metadata node URI: one per (fact, source) — see module docstring."""
    aid = hashlib.sha1(f"{subject}|{predicate}|{obj}|{source}".encode("utf-8")).hexdigest()
    return f"{PROV_NS}stmt/{aid}"


def build_provenance_triples(
    subject: str,
    predicate: str,
    obj: str,
    source: str,
    confidence: float = 1.0,
    timestamp: datetime | str = "",
    graph_uri: str = "",
    authority: str = "",
) -> list[tuple[str, str, str]]:
    """Build the statement-metadata triples for one fact assertion.

    Returned triples target the companion provenance graph
    (provenance_graph_uri of the data graph) — the caller inserts them
    there; the fact triple itself is untouched.

    Args:
        obj: the object exactly as written to Neptune (typed-literal
            convention included) so writer and reader agree on ids.
        confidence: 0.0-1.0; defaults to 1.0 for directly-ingested facts.
        timestamp: aware datetime or ISO-8601 string. Callers on the
            ingest path pass datetime.now(timezone.utc); tests inject
            fixed values.
        graph_uri: the DATA graph the fact lives in, recorded so a shared
            reader can scope records back to their graph.
        authority: OPTIONAL source-authority level (an
            ``AuthorityLevel`` value string, e.g. ``"source_of_truth"``),
            recorded so the P6 write-time conflict policy (ONTA-276) can
            rank a stored fact's authority against an incoming
            contradicting one. Empty (the default) records no authority —
            back-compat for every existing ingest/enrichment caller.
    """
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence must be in [0, 1], got {confidence}")
    ts = timestamp.isoformat() if isinstance(timestamp, datetime) else timestamp
    node = _assertion_uri(subject, predicate, obj, source)
    triples = [
        (node, PROV_SUBJECT, subject),
        (node, PROV_PREDICATE, predicate),
        (node, PROV_OBJECT, obj),
        (node, PROV_STATEMENT, statement_id(subject, predicate, obj)),
        (node, PROV_SOURCE, source),
        (node, PROV_CONFIDENCE, f"{confidence}^^{_XSD}#float"),
    ]
    if ts:
        triples.append((node, PROV_TIMESTAMP, f"{ts}^^{_XSD}#dateTime"))
    if graph_uri:
        triples.append((node, PROV_GRAPH, graph_uri))
    if authority:
        triples.append((node, PROV_AUTHORITY, authority))
    return triples


def attr_provenance_companion_uri(type_name: str, attribute: str, suffix: str) -> str:
    """The metadata-namespace URI for one per-attribute display companion.

    ``suffix`` is ``source_url`` / ``provenance`` / ``verified_at``. Companions
    are metadata OF an attribute, not attributes (ONTA-262), so they mint on the
    dedicated ``attr_meta/`` namespace — never on ``types/<Type>/attrs/``, whose
    predicates every user-facing surface renders as domain attributes. Defined
    ONCE here so discovery and enrichment mint the identical companion predicate
    for the same fact (cross-rail symmetry — a discovered fact and an enriched
    fact carry provenance the same way)."""
    return f"{ATTR_META_NS}{type_name}/{attribute}/{suffix}"


def legacy_attr_companion_uri(type_name: str, attribute: str, suffix: str) -> str:
    """The PRE-ONTA-262 companion shape: ``types/<Type>/attrs/<attr>_<suffix>``.

    Graphs written before the attr_meta namespace carry companions here (and, for
    enrichment, matching ontology declarations). Kept only for the read-side
    dual-read of un-migrated data and for the migration that rewrites it — never
    mint new companions with this."""
    return f"{_TYPES_PREFIX}{type_name}/attrs/{attribute}_{suffix}"


def _as_iso(ts: datetime | str) -> str:
    """Normalize a datetime/ISO-string stamp to an ISO-8601 string."""
    return ts.isoformat() if isinstance(ts, datetime) else str(ts)


def build_attribute_provenance_companions(
    entity_uri: str,
    type_name: str,
    attribute: str,
    *,
    source_url: str = "",
    provenance: str = "",
    verified_at: datetime | str = "",
) -> list[tuple[str, str, str]]:
    """Build the per-attribute DISPLAY provenance companions for ONE filled fact.

    The user-facing citations the Explorer + /ask render, emitted the SAME way by
    every rail (enrichment + discovery) so a discovered fact and an enriched fact
    are provenance-symmetric (ONTA-245). These are ordinary INSTANCE triples — the
    caller passes them in ``insert_facts(instance_triples=…)``, NOT a separate
    write path.

    - ``<attr>_source_url`` — where the value came from (plain string; only when a
      URL is present).
    - ``<attr>_provenance`` — a short human citation (plain string; only when set).
    - ``<attr>_verified_at`` — the per-fact freshness stamp, ALWAYS emitted and
      ALWAYS TYPED ``xsd:dateTime`` so the NL planner's ``NOW()``-relative FILTER
      matches it (an untyped string would be type-incompatible → the freshness
      query silently drops the row, ONTA-247). Defaults to now-UTC when the caller
      passes no explicit stamp, so every rail advances a recency signal.
    """
    out: list[tuple[str, str, str]] = []
    if source_url:
        out.append(
            (entity_uri, attr_provenance_companion_uri(type_name, attribute, "source_url"), source_url)
        )
    if provenance:
        out.append(
            (entity_uri, attr_provenance_companion_uri(type_name, attribute, "provenance"), provenance)
        )
    stamp = verified_at or datetime.now(timezone.utc)
    out.append((
        entity_uri,
        attr_provenance_companion_uri(type_name, attribute, "verified_at"),
        f"{_as_iso(stamp)}^^{_XSD}#dateTime",
    ))
    return out


def _event_uri(event: str, subject: str, obj: str, ts: str) -> str:
    """Metadata node URI for one removal/rename event.

    Keyed by ``sha1(event|subject|obj|timestamp)`` so distinct removals of the
    same subject over time are distinct nodes (idempotent for a fixed timestamp,
    which is how tests pin them).
    """
    eid = hashlib.sha1(f"{event}|{subject}|{obj}|{ts}".encode("utf-8")).hexdigest()
    return f"{PROV_NS}event/{eid}"


def _event_common(
    node: str,
    event: str,
    subject: str,
    reason: str,
    ts: str,
    graph_uri: str,
    touched_types,
) -> list[tuple[str, str, str]]:
    triples = [
        (node, PROV_EVENT, event),
        (node, PROV_SUBJECT, subject),
    ]
    if reason:
        triples.append((node, PROV_REASON, reason))
    if ts:
        triples.append((node, PROV_TIMESTAMP, f"{ts}^^{_XSD}#dateTime"))
    if graph_uri:
        triples.append((node, PROV_GRAPH, graph_uri))
    for t in touched_types or ():
        if t:
            triples.append((node, PROV_AFFECTED_TYPE, t))
    return triples


def build_tombstone_triples(
    *,
    subjects=(),
    triples=(),
    graph_uri: str = "",
    reason: str = "",
    timestamp: datetime | str = "",
    touched_types=(),
) -> list[tuple[str, str, str]]:
    """Build the statement-metadata triples for a removal (``delete_facts``).

    One ``tombstone`` event node per removed **subject** (whole-subject delete)
    and per removed **triple** (concrete or predicate-scoped). Each records the
    subject (and predicate/object where applicable), the reason, a timestamp, the
    data graph, and any affected types — the mirror of
    :func:`build_provenance_triples`'s assertion node so an undo can see exactly
    what left the graph. ``o is None`` in a ``triples`` entry means a
    predicate-scoped removal (all objects of that ``(subject, predicate)``), so no
    ``prov:object`` is recorded. Returned triples target the companion provenance
    graph; the caller inserts them there.
    """
    ts = timestamp.isoformat() if isinstance(timestamp, datetime) else timestamp
    out: list[tuple[str, str, str]] = []
    for s in subjects or ():
        if not s:
            continue
        node = _event_uri(EVENT_TOMBSTONE, s, "", ts)
        out.extend(_event_common(node, EVENT_TOMBSTONE, s, reason, ts, graph_uri, touched_types))
    for triple in triples or ():
        s, p, o = triple
        if not s:
            continue
        node = _event_uri(EVENT_TOMBSTONE, s, f"{p}|{'' if o is None else o}", ts)
        node_triples = _event_common(node, EVENT_TOMBSTONE, s, reason, ts, graph_uri, touched_types)
        if p:
            node_triples.append((node, PROV_PREDICATE, p))
        if o is not None:
            node_triples.append((node, PROV_OBJECT, o))
        out.extend(node_triples)
    return out


def build_rewrite_triples(
    old_uri: str,
    new_uri: str,
    *,
    graph_uri: str = "",
    reason: str = "",
    timestamp: datetime | str = "",
    touched_types=(),
) -> list[tuple[str, str, str]]:
    """Build the statement-metadata triples for a subject rename (``rewrite_subject``).

    One ``rewrite`` event node mapping ``old_uri → new_uri`` (``prov:rewrittenTo``)
    so governance/undo can follow an ER merge, and derived indexes have a record
    of the re-key. Returned triples target the companion provenance graph.
    """
    ts = timestamp.isoformat() if isinstance(timestamp, datetime) else timestamp
    node = _event_uri(EVENT_REWRITE, old_uri, new_uri, ts)
    out = _event_common(node, EVENT_REWRITE, old_uri, reason, ts, graph_uri, touched_types)
    out.append((node, PROV_REWRITTEN_TO, new_uri))
    return out


def build_supersession_triples(
    subject: str,
    predicate: str,
    old_obj: str,
    new_obj: str,
    *,
    graph_uri: str = "",
    reason: str = "",
    timestamp: datetime | str = "",
    touched_types=(),
) -> list[tuple[str, str, str]]:
    """Build the governance event for a SUPERSESSION (ONTA-277).

    Records that ``(subject, predicate, old_obj)`` lost currency because
    ``(subject, predicate, new_obj)`` arrived — a companion to the always-on
    valid-time interval (``graph/validity.py``), giving governance/undo the "who
    replaced what, and why" record without re-deriving it from two interval nodes.
    The superseded fact is NOT deleted (supersession closes an interval); this
    event simply witnesses the closure. ``prov:supersededBy`` carries the
    replacement fact's ``statement_id``. Returned triples target the companion
    provenance graph; gated by ``COGRAPH_PROVENANCE_ENABLED`` at the call site.
    """
    ts = timestamp.isoformat() if isinstance(timestamp, datetime) else timestamp
    node = _event_uri(EVENT_SUPERSEDE, subject, f"{predicate}|{old_obj}|{new_obj}", ts)
    out = _event_common(node, EVENT_SUPERSEDE, subject, reason, ts, graph_uri, touched_types)
    if predicate:
        out.append((node, PROV_PREDICATE, predicate))
    if old_obj is not None:
        out.append((node, PROV_OBJECT, old_obj))
    out.append((node, PROV_SUPERSEDED_BY, statement_id(subject, predicate, new_obj)))
    if ts:
        out.append((node, PROV_VALID_TO, f"{ts}^^{_XSD}#dateTime"))
    return out


def build_conflict_loss_triples(
    subject: str,
    predicate: str,
    loser_obj: str,
    winner_obj: str,
    *,
    graph_uri: str = "",
    reason: str = "",
    loser_source: str = "",
    loser_confidence: Optional[float] = None,
    loser_authority: str = "",
    timestamp: datetime | str = "",
    touched_types=(),
) -> list[tuple[str, str, str]]:
    """Build the governance event for a functional-attribute CONFLICT LOSS (ONTA-276).

    Records that ``(subject, predicate, loser_obj)`` lost a write-time conflict to
    ``(subject, predicate, winner_obj)`` — the winner was the higher-ranked fact
    under the conflict policy (authority + confidence + recency). A companion to
    the always-on valid-time closure (``graph/validity.py`` with
    ``STATUS_DEPRECATED``): the loser is NOT deleted, its interval is closed and it
    stays queryable, and this event witnesses "why it lost, and to what".
    ``prov:supersededBy`` carries the WINNER fact's ``statement_id``; ``prov:reason``
    the deciding axis; the loser's ``source`` / ``confidence`` / ``authority`` are
    recorded too so the losing claim's provenance is self-contained. Returned
    triples target the companion provenance graph; gated by
    ``COGRAPH_PROVENANCE_ENABLED`` at the call site.
    """
    ts = timestamp.isoformat() if isinstance(timestamp, datetime) else timestamp
    node = _event_uri(EVENT_CONFLICT_LOSS, subject, f"{predicate}|{loser_obj}|{winner_obj}", ts)
    out = _event_common(node, EVENT_CONFLICT_LOSS, subject, reason, ts, graph_uri, touched_types)
    if predicate:
        out.append((node, PROV_PREDICATE, predicate))
    if loser_obj is not None:
        out.append((node, PROV_OBJECT, loser_obj))
    out.append((node, PROV_SUPERSEDED_BY, statement_id(subject, predicate, winner_obj)))
    if loser_source:
        out.append((node, PROV_SOURCE, loser_source))
    if loser_confidence is not None:
        out.append((node, PROV_CONFIDENCE, f"{loser_confidence}^^{_XSD}#float"))
    if loser_authority:
        out.append((node, PROV_AUTHORITY, loser_authority))
    if ts:
        out.append((node, PROV_VALID_TO, f"{ts}^^{_XSD}#dateTime"))
    return out


def build_retraction_triples(
    subject: str,
    predicate: str,
    obj: str,
    *,
    graph_uri: str = "",
    reason: str = "",
    timestamp: datetime | str = "",
    touched_types=(),
) -> list[tuple[str, str, str]]:
    """Build the governance event for a RETRACTION (ONTA-277).

    Records that ``(subject, predicate, obj)`` was explicitly asserted
    no-longer-true (distinct from supersession, which is driven by a replacement).
    The default retraction path closes the fact's valid-time interval rather than
    deleting it (history stays queryable), so this event witnesses the removal of
    currency; when a caller genuinely hard-deletes the triple, the removal also
    goes through ``delete_facts`` (which writes its own tombstone). Returned
    triples target the companion provenance graph; gated by
    ``COGRAPH_PROVENANCE_ENABLED`` at the call site.
    """
    ts = timestamp.isoformat() if isinstance(timestamp, datetime) else timestamp
    node = _event_uri(EVENT_RETRACT, subject, f"{predicate}|{'' if obj is None else obj}", ts)
    out = _event_common(node, EVENT_RETRACT, subject, reason, ts, graph_uri, touched_types)
    if predicate:
        out.append((node, PROV_PREDICATE, predicate))
    if obj is not None:
        out.append((node, PROV_OBJECT, obj))
    if ts:
        out.append((node, PROV_VALID_TO, f"{ts}^^{_XSD}#dateTime"))
    return out


# --------------------------------------------------------------------------- #
# Merge / split lineage (ONTA-274)
# --------------------------------------------------------------------------- #
Triple = tuple[str, str, str]


def _lineage_fact_uri(merge_node: str, origin: str, s: str, p: str, o: str) -> str:
    """Node URI for one reified snapshot fact, keyed so re-runs collide idempotently
    (a fixed merge event + fact always mints the same node)."""
    fid = hashlib.sha1(f"{merge_node}|{origin}|{s}|{p}|{o}".encode("utf-8")).hexdigest()
    return f"{LINEAGE_NS}fact/{fid}"


def merge_event_uri(merged: str, canonical: str, ts: str) -> str:
    """The merge event node URI for ``merged → canonical`` at ``ts`` (public so a
    reader can reconstruct it deterministically)."""
    return _event_uri(EVENT_MERGE, merged, canonical, ts)


def build_merge_lineage_triples(
    canonical: str,
    merged: str,
    *,
    merged_facts: list[Triple],
    canonical_facts: list[Triple],
    graph_uri: str = "",
    reason: str = "",
    timestamp: datetime | str = "",
    touched_types=(),
) -> list[Triple]:
    """Build the ALWAYS-ON, reversible lineage record for a first-class merge (ONTA-274).

    Two parts, both targeting the companion provenance graph:

    1. A ``merge`` EVENT node — ``merged`` was unified INTO ``canonical`` (recorded
       with ``prov:rewrittenTo`` = the survivor, mirroring the ``rewrite`` event the
       ``rewrite_subject`` primitive writes for the same re-key), with the reason
       (the driving evidence — merge is never a silent cleanup) and timestamp.
    2. A reified SNAPSHOT of each node's facts as they stood JUST BEFORE the merge
       re-keyed them (``merged_facts`` / ``canonical_facts``), tagged by origin. This
       is what makes the merge REVERSIBLE: ``split_entity`` reads it back to know
       which facts belonged to which side and restore their independent identities.

    Unlike the gated governance events, this is written UNCONDITIONALLY (the caller
    does not gate it) because it is load-bearing for split — the same principle by
    which ``graph/validity.py`` intervals are always written. Object terms are stored
    in the write-convention form so they round-trip term-faithfully (a typed literal
    survives, per the ONTA-247 lesson) when read back by :func:`fetch_merge_lineage`.
    """
    ts = timestamp.isoformat() if isinstance(timestamp, datetime) else timestamp
    node = merge_event_uri(merged, canonical, ts)
    out = _event_common(node, EVENT_MERGE, merged, reason, ts, graph_uri, touched_types)
    out.append((node, PROV_REWRITTEN_TO, canonical))
    for origin, facts in ((ORIGIN_MERGED, merged_facts), (ORIGIN_CANONICAL, canonical_facts)):
        for (s, p, o) in facts:
            if not s or not p:
                continue
            fnode = _lineage_fact_uri(node, origin, s, p, o)
            out.extend([
                (fnode, LIN_OF_MERGE, node),
                (fnode, LIN_ORIGIN, origin),
                (fnode, LIN_S, s),
                (fnode, LIN_P, p),
                (fnode, LIN_O, o),
            ])
    return out


def build_split_triples(
    canonical: str,
    merged: str,
    *,
    graph_uri: str = "",
    reason: str = "",
    timestamp: datetime | str = "",
    touched_types=(),
) -> list[Triple]:
    """Build the governance event for a first-class SPLIT (ONTA-274).

    Records that ``merged`` was separated back OUT of ``canonical`` (the reverse of a
    merge), with the driving reason. Written gated by ``COGRAPH_PROVENANCE_ENABLED``
    at the call site (like the other governance events) — the merge lineage snapshot
    it consumes is left in place, so history shows the full merge→split story.
    """
    ts = timestamp.isoformat() if isinstance(timestamp, datetime) else timestamp
    node = _event_uri(EVENT_SPLIT, merged, canonical, ts)
    out = _event_common(node, EVENT_SPLIT, merged, reason, ts, graph_uri, touched_types)
    out.append((node, PROV_REWRITTEN_TO, canonical))
    return out


def merge_lineage_query(graph_uri: str, canonical: str, merged: str) -> str:
    """SELECT the reified snapshot of a merge (``merged → canonical``) back out.

    Uses ``GRAPH <companion>`` patterns (not ``FROM``) so it resolves correctly
    against a union-default-graph store. Returns one row per snapshot fact with its
    origin side and the fact's ``(s, p, o)`` in original (pre-merge) form.
    """
    prov = provenance_graph_uri(graph_uri)
    c, m = _escape_value(canonical), _escape_value(merged)
    return (
        f"SELECT ?origin ?s ?p ?o WHERE {{\n"
        f"  GRAPH <{prov}> {{\n"
        f'    ?m <{PROV_EVENT}> "{EVENT_MERGE}" ;\n'
        f"       <{PROV_SUBJECT}> {m} ;\n"
        f"       <{PROV_REWRITTEN_TO}> {c} .\n"
        f"    ?f <{LIN_OF_MERGE}> ?m ;\n"
        f"       <{LIN_ORIGIN}> ?origin ;\n"
        f"       <{LIN_S}> ?s ;\n"
        f"       <{LIN_P}> ?p ;\n"
        f"       <{LIN_O}> ?o .\n"
        f"  }}\n"
        f"}}"
    )


def _term_from_binding(binding: dict | None) -> str:
    """Reconstruct the write-convention term from a raw SPARQL JSON binding.

    The read-side inverse of ``queries._escape_value`` (mirrors
    ``validity._object_term``): a ``uri`` → the URI string; a typed literal →
    ``value^^datatype``; a plain / ``xsd:string`` literal → the bare value. Used so a
    snapshotted object round-trips term-faithfully instead of degrading a typed
    literal to a plain string (ONTA-247)."""
    if not binding:
        return ""
    if binding.get("type") == "uri":
        return binding.get("value", "")
    value = binding.get("value", "")
    dt = binding.get("datatype")
    if dt and dt != f"{_XSD}#string":
        return f"{value}^^{dt}"
    return value


@dataclass
class MergeLineage:
    """The reified snapshot of a merge, read back for a reversible split."""

    canonical: str
    merged: str
    merged_facts: list[Triple]
    canonical_facts: list[Triple]

    @property
    def found(self) -> bool:
        return bool(self.merged_facts or self.canonical_facts)


async def fetch_merge_lineage(
    neptune, graph_uri: str, canonical: str, merged: str
) -> MergeLineage:
    """Read back the reversible snapshot recorded by :func:`build_merge_lineage_triples`.

    Reads the RAW SPARQL JSON (not ``parse_sparql_results``, which drops datatype) so
    each object term is reconstructed exactly (:func:`_term_from_binding`) and a
    restored fact is byte-identical to the original. Best-effort: an empty/failed read
    yields an empty lineage (the caller then requires an explicit partition). If a pair
    was merged more than once, the facts of every such merge are unioned."""
    try:
        raw = await neptune.query(merge_lineage_query(graph_uri, canonical, merged))
    except Exception:  # noqa: BLE001 — a lineage read is best-effort
        return MergeLineage(canonical, merged, [], [])
    bindings = raw.get("results", {}).get("bindings", [])
    merged_facts: list[Triple] = []
    canonical_facts: list[Triple] = []
    for row in bindings:
        origin = (row.get("origin") or {}).get("value", "")
        s = _term_from_binding(row.get("s"))
        p = _term_from_binding(row.get("p"))
        o = _term_from_binding(row.get("o"))
        if not s or not p:
            continue
        (merged_facts if origin == ORIGIN_MERGED else canonical_facts).append((s, p, o))
    return MergeLineage(canonical, merged, merged_facts, canonical_facts)


def provenance_query(graph_uri: str, subject: str, predicate: str | None = None, limit: int = 1000) -> str:
    """SELECT over the companion provenance graph for one subject
    (optionally narrowed to one predicate)."""
    pred_filter = f"  FILTER(?p = {_escape_value(predicate)})\n" if predicate else ""
    return (
        f"SELECT ?p ?o ?stmt ?source ?confidence ?timestamp ?graph ?authority "
        f"FROM <{provenance_graph_uri(graph_uri)}>\n"
        f"WHERE {{\n"
        f"  ?node <{PROV_SUBJECT}> {_escape_value(subject)} ;\n"
        f"        <{PROV_PREDICATE}> ?p ;\n"
        f"        <{PROV_OBJECT}> ?o ;\n"
        f"        <{PROV_STATEMENT}> ?stmt ;\n"
        f"        <{PROV_SOURCE}> ?source ;\n"
        f"        <{PROV_CONFIDENCE}> ?confidence .\n"
        f"  OPTIONAL {{ ?node <{PROV_TIMESTAMP}> ?timestamp }}\n"
        f"  OPTIONAL {{ ?node <{PROV_GRAPH}> ?graph }}\n"
        f"  OPTIONAL {{ ?node <{PROV_AUTHORITY}> ?authority }}\n"
        f"{pred_filter}}}\nLIMIT {limit}"
    )


@dataclass
class ProvenanceRecord:
    """One (fact, source) assertion read back from the provenance graph."""

    statement_id: str
    subject: str
    predicate: str
    obj: str
    source: str
    confidence: float
    timestamp: str
    graph: str = ""
    # ONTA-276: source-authority level the fact was asserted under (an
    # ``AuthorityLevel`` value string). Empty on pre-ONTA-276 provenance.
    authority: str = ""


async def fetch_provenance(
    neptune, graph_uri: str, subject: str, predicate: str | None = None,
) -> list[ProvenanceRecord]:
    """Read parsed provenance records for a subject (optionally one predicate).

    `graph_uri` is the DATA graph; the companion provenance graph is derived.
    Malformed confidence values degrade to 1.0 rather than failing the read.
    """
    raw = await neptune.query(provenance_query(graph_uri, subject, predicate))
    _, bindings = parse_sparql_results(raw)
    records: list[ProvenanceRecord] = []
    for row in bindings:
        try:
            confidence = float(row.get("confidence", "1.0"))
        except ValueError:
            confidence = 1.0
        records.append(
            ProvenanceRecord(
                statement_id=row.get("stmt", ""),
                subject=subject,
                predicate=row.get("p", ""),
                obj=row.get("o", ""),
                source=row.get("source", ""),
                confidence=confidence,
                timestamp=row.get("timestamp", ""),
                graph=row.get("graph", ""),
                authority=row.get("authority", ""),
            )
        )
    return records

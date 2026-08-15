"""URI helpers + companion mint for the provenance substrate (ADR 0002 §4).

The ONE sanctioned companion-URI minter is :func:`attr_provenance_companion_uri`
(ONTA-262). Do not reimplement ``attr_meta/<Type>/<attr>/<suffix>`` inline.

Look up patched names on :mod:`infona_client.graph.provenance` via ``_host()``.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional

from infona_client.graph.iri import (  # noqa: F401 — IRI_BASE re-exported via facade
    ATTR_META_NS,
    IRI_BASE,
    PROV_NS,
    TYPE_URI_PREFIX,
)
from infona_client.graph.parser import parse_sparql_results
# The companion metadata namespace + suffixes are defined canonically in
# graph/predicates.py (the shared predicate-hygiene module every read surface
# already imports) and re-exported here so writers and readers resolve the SAME
# constants. predicates.py imports nothing from this module (no cycle).
from infona_client.graph.predicates import ATTR_META_NS, ATTR_META_SUFFIXES  # noqa: F401
from infona_client.graph.queries import _escape_value


def _host():
    """Call-time lookup of the public provenance module (monkeypatch surface)."""
    from infona_client.graph import provenance as _mod

    return _mod


# --- Per-attribute DISPLAY provenance companions (ADR 0009 / ONTA-245) ---------
#
# The canonical companion-provenance GRAPH (build_provenance_triples) is the
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

_TYPES_PREFIX = TYPE_URI_PREFIX
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
# by ``INFONA_PROVENANCE_ENABLED`` exactly like assertion provenance.
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
# (gated by ``INFONA_PROVENANCE_ENABLED``), the merge lineage snapshot is ALWAYS
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


# --- Per-attribute SURFACE-FORM companion (ONTA-347) ---------------------------
#
# When the A3 clean stage COERCES or CANONICALIZES a value
# (``CleanOutcome.TRANSFORMED``, i.e. ``raw_value != clean_value``), the writer
# persists only the CANONICAL value as the attribute — the ORIGINAL surface form
# the source actually carried (``"12/31/2020"`` before it became
# ``2020-12-31T00:00:00``, ``"yes"`` before ``true``) survives only in a log. P4
# (Verify, next wave) must compare the stored canonical value against evidence and
# needs the original preserved IN THE GRAPH. This companion preserves it as a
# per-attribute metadata triple on the SAME ``attr_meta/`` namespace as the display
# companions above, so it is structurally invisible to Explorer chips/columns +
# type-stats (``predicates.is_internal_predicate`` excludes the whole namespace)
# yet stays an ordinary queryable instance triple. Minted via the SAME
# ``attr_provenance_companion_uri`` minter so every rail agrees on the URI, and it
# rides the shared write path in ``instance_triples`` exactly like the display
# companions — never a separate writer.
SURFACE_FORM_SUFFIX = "surface_form"


def build_surface_form_companion(
    entity_uri: str, type_name: str, attribute: str, surface_form: str,
) -> list[tuple[str, str, str]]:
    """Build the per-attribute SURFACE-FORM companion preserving the ORIGINAL
    pre-clean value (ONTA-347).

    Emitted ONLY when the A3 clean stage transformed the value (the CALLER decides
    that — see ``normalization/clean.py::surface_form_companion_triples``); a value
    written verbatim has no surface-form divergence to record. One plain-string
    instance triple::

        <entity> <attr_meta/<Type>/<attr>/surface_form> "<original value>"

    Returns ``[]`` when any component is empty (nothing to preserve), so a caller
    can unconditionally ``extend`` with the result. The companion is metadata OF the
    attribute — never itself an attribute (ONTA-262) — so it is invisible to every
    user-facing surface while staying queryable for P4 Verify."""
    if not (entity_uri and type_name and attribute and surface_form):
        return []
    return [
        (
            entity_uri,
            attr_provenance_companion_uri(type_name, attribute, SURFACE_FORM_SUFFIX),
            surface_form,
        )
    ]


# --- Per-attribute A4 EPISTEMIC truth-verdict companion (ONTA-375) -------------
#
# When the A4 Verify seam (ONTA-370) runs — a `VerifyPolicy` is enabled — each
# written fact carries an epistemic `TruthVerdict` (`supported` / `refuted` /
# `unverifiable` / `identity_conditional`): whether INDEPENDENT evidence
# corroborates it. This is the TRUTH axis, ENTIRELY distinct from the recency /
# validity-interval `verdict` (`current` / `superseded` / `retracted` /
# `lost_conflict`) the answer layer already surfaces — a fact can be `current`
# AND `unverifiable` at once.
#
# The verdict is persisted as ONE per-attribute companion on the SAME `attr_meta/`
# namespace as the surface-form / display companions above — minted via the SAME
# `attr_provenance_companion_uri` minter (cross-rail symmetry, no bespoke triple),
# so `predicates.is_internal_predicate` excludes it whole-namespace: it is
# structurally invisible to Explorer chips/columns, type-stats, and NL answer
# dumps, yet stays an ordinary QUERYABLE instance triple the P7 answer layer reads
# by convention. It rides the shared write path (`insert_facts` via the
# instance-triple collector) exactly like every other companion — never a separate
# writer. Metadata OF an attribute, never itself an attribute (ONTA-262), so it is
# NOT declared in the ontology and gets no provenance record of its own.
TRUTH_VERDICT_SUFFIX = "truth_verdict"

# The `types/<Type>/attrs/` infix (see `graph/ontology_queries.attr_uri`). A
# literal attribute's DOMAIN predicate is `<_TYPES_PREFIX><Type>/attrs/<leaf>`, so
# the (Type, leaf) a companion is keyed by can be recovered from it on the read
# side (:func:`companion_predicate_for`) without threading the type separately.
_ATTRS_INFIX = "/attrs/"


def build_truth_verdict_companion(
    entity_uri: str, type_name: str, attribute: str, verdict: str,
) -> list[tuple[str, str, str]]:
    """Build the per-attribute A4 EPISTEMIC truth-verdict companion (ONTA-375).

    Emitted for each written fact the A4 Verify seam produced a verdict for. One
    plain-string instance triple::

        <entity> <attr_meta/<Type>/<attr>/truth_verdict> "<verdict>"

    ``verdict`` is a :class:`~infona_client.verification.types.TruthVerdict` VALUE
    string (``"supported"`` / ``"refuted"`` / ``"unverifiable"`` /
    ``"identity_conditional"``). Returns ``[]`` when any component is empty so a
    caller can unconditionally ``extend`` with the result. The companion is
    metadata OF the attribute (ONTA-262) — invisible to every user-facing surface
    (``is_internal_predicate`` excludes the ``attr_meta/`` namespace) while staying
    queryable for the answer layer."""
    if not (entity_uri and type_name and attribute and verdict):
        return []
    return [
        (
            entity_uri,
            attr_provenance_companion_uri(type_name, attribute, TRUTH_VERDICT_SUFFIX),
            verdict,
        )
    ]


def companion_predicate_for(attr_predicate: str, suffix: str) -> Optional[str]:
    """The ``attr_meta/`` companion predicate for a ``types/<Type>/attrs/<leaf>``
    DOMAIN attribute predicate + ``suffix``, or ``None`` when ``attr_predicate`` is
    not that shape (e.g. a relationship on ``onto/<leaf>``, which carries no
    literal-attribute companion).

    The read-side inverse of how a literal attribute's companion is minted: from
    the domain fact's predicate as it appears in a citation row, reconstruct the
    ``(Type, leaf)`` the companion is keyed by and mint the SAME companion URI via
    :func:`attr_provenance_companion_uri` — so the answer layer can look a companion
    up per ``(subject, attribute-predicate)`` without carrying the type separately.
    """
    if not attr_predicate.startswith(_TYPES_PREFIX):
        return None
    rest = attr_predicate[len(_TYPES_PREFIX):]
    type_name, sep, leaf = rest.partition(_ATTRS_INFIX)
    if not sep or not type_name or not leaf or "/" in leaf:
        return None
    return attr_provenance_companion_uri(type_name, leaf, suffix)


def truth_verdict_query(instance_graph: str, subject: str, companion_predicate: str) -> str:
    """SELECT the A4 truth-verdict companion value for one ``(subject, companion)``
    from the INSTANCE graph (the companion is an ordinary instance triple, NOT in
    the provenance/validity companion graphs). ``GRAPH <…>`` (not ``FROM``) so it
    resolves against a union-default-graph store, mirroring the validity reader."""
    return (
        f"SELECT ?verdict WHERE {{\n"
        f"  GRAPH <{instance_graph}> {{ {_escape_value(subject)} <{companion_predicate}> ?verdict }}\n"
        f"}} LIMIT 1"
    )


async def fetch_truth_verdict(
    neptune, instance_graph: str, subject: str, predicate: str,
) -> str:
    """Read the A4 EPISTEMIC truth-verdict companion for one ``(subject, attribute
    predicate)``; ``""`` when absent.

    ``predicate`` is the DOMAIN attribute predicate as it appears in a citation row
    (``types/<Type>/attrs/<leaf>``); the companion it is keyed by is derived from it
    (:func:`companion_predicate_for`) — a non-attribute predicate (a relationship on
    ``onto/<leaf>``) has no companion and yields ``""`` with no read. Best-effort:
    any read failure degrades to ``""`` so a verdict read never breaks the answer
    (mirrors ``answer_meta._safe_provenance``)."""
    companion = companion_predicate_for(predicate, TRUTH_VERDICT_SUFFIX)
    if not companion:
        return ""
    try:
        raw = await neptune.query(truth_verdict_query(instance_graph, subject, companion))
    except Exception:  # noqa: BLE001 — an epistemic-verdict read is best-effort
        return ""
    _, rows = parse_sparql_results(raw)
    for row in rows:
        verdict = row.get("verdict", "")
        if verdict:
            return verdict
    return ""


def _event_uri(event: str, subject: str, obj: str, ts: str) -> str:
    """Metadata node URI for one removal/rename event.

    Keyed by ``sha1(event|subject|obj|timestamp)`` so distinct removals of the
    same subject over time are distinct nodes (idempotent for a fixed timestamp,
    which is how tests pin them).
    """
    eid = hashlib.sha1(f"{event}|{subject}|{obj}|{ts}".encode("utf-8")).hexdigest()
    return f"{PROV_NS}event/{eid}"

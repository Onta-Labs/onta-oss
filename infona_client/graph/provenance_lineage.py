"""Merge / split lineage builders (ONTA-274).

A merge/split is a designed, lineage-preserving P6 operation — NOT post-write
ER cleanup. The merge snapshot is ALWAYS written (load-bearing for split).

Look up patched names on :mod:`infona_client.graph.provenance` via ``_host()``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from infona_client.graph.provenance_events import _event_common
from infona_client.graph.provenance_uris import (
    EVENT_MERGE,
    EVENT_SPLIT,
    LIN_O,
    LIN_OF_MERGE,
    LIN_ORIGIN,
    LIN_P,
    LIN_S,
    LINEAGE_NS,
    ORIGIN_CANONICAL,
    ORIGIN_MERGED,
    PROV_EVENT,
    PROV_REWRITTEN_TO,
    PROV_SUBJECT,
    _XSD,
    _event_uri,
)


def _host():
    """Call-time lookup of the public provenance module (monkeypatch surface)."""
    from infona_client.graph import provenance as _mod

    return _mod


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
    merge), with the driving reason. Written gated by ``INFONA_PROVENANCE_ENABLED``
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
    from infona_client.graph.queries import _escape_value

    prov = _host().provenance_graph_uri(graph_uri)
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

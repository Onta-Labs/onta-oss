"""Assertion-triple builders for the provenance substrate (ADR 0002 §4).

:func:`build_provenance_triples` is the statement-metadata payload for one
(fact, source) assertion. Look up patched names on
:mod:`infona_client.graph.provenance` via ``_host()``.
"""

from __future__ import annotations

from datetime import datetime

from infona_client.graph.provenance_uris import (
    PROV_AUTHORITY,
    PROV_CONFIDENCE,
    PROV_GRAPH,
    PROV_OBJECT,
    PROV_PREDICATE,
    PROV_SOURCE,
    PROV_STATEMENT,
    PROV_SUBJECT,
    PROV_TIMESTAMP,
    _XSD,
    _assertion_uri,
)


def _host():
    """Call-time lookup of the public provenance module (monkeypatch surface)."""
    from infona_client.graph import provenance as _mod

    return _mod


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
        (node, PROV_STATEMENT, _host().statement_id(subject, predicate, obj)),
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

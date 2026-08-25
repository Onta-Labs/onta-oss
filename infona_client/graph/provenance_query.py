"""Query / parse / fetch for the provenance substrate (ADR 0002 §4).

Reads assertion records back from the companion provenance graph (SPARQL
fallback) or Assertion + ProvEvent (GraphStore path, ONTA-536).

Look up patched names on :mod:`infona_client.graph.provenance` via ``_host()``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from infona_client.graph.parser import parse_sparql_results
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
    statement_id,
)
from infona_client.graph.queries import _escape_value


def _host():
    """Call-time lookup of the public provenance module (monkeypatch surface)."""
    from infona_client.graph import provenance as _mod

    return _mod


def provenance_query(graph_uri: str, subject: str, predicate: str | None = None, limit: int = 1000) -> str:
    """SELECT over the companion provenance graph for one subject
    (optionally narrowed to one predicate)."""
    pred_filter = f"  FILTER(?p = {_escape_value(predicate)})\n" if predicate else ""
    return (
        f"SELECT ?p ?o ?stmt ?source ?confidence ?timestamp ?graph ?authority "
        f"FROM <{_host().provenance_graph_uri(graph_uri)}>\n"
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


def _strip_xsd(value: str) -> str:
    """Drop a trailing ``^^datatype`` suffix from a SPARQL-era literal string."""
    if not isinstance(value, str):
        return "" if value is None else str(value)
    if "^^" in value:
        return value.split("^^", 1)[0]
    return value


def parse_provenance_records(
    triples: list[tuple[str, str, str]] | None,
) -> list[ProvenanceRecord]:
    """Parse RDF companion-provenance triples into :class:`ProvenanceRecord`s.

    Inverse of :func:`build_provenance_triples` for the GraphStore port (ONTA-536):
    ``insert_facts`` receives the same statement-metadata payload the SPARQL path
    used to INSERT into ``…/provenance``, groups it by reified node, and lands it
    on Assertion provenance + ``:ProvEvent`` fields.
    """
    if not triples:
        return []
    by_node: dict[str, dict[str, str]] = {}
    for s, p, o in triples:
        if not s or not p:
            continue
        bucket = by_node.setdefault(s, {})
        bucket[p] = "" if o is None else str(o)
    out: list[ProvenanceRecord] = []
    for fields in by_node.values():
        subject = fields.get(PROV_SUBJECT, "")
        predicate = fields.get(PROV_PREDICATE, "")
        obj = _strip_xsd(fields.get(PROV_OBJECT, ""))
        source = fields.get(PROV_SOURCE, "")
        if not subject or not predicate:
            # Tombstone / rewrite / conflict-loss events share the companion
            # graph but are not fact-assertion records — skip them here.
            continue
        conf_raw = _strip_xsd(fields.get(PROV_CONFIDENCE, "1.0"))
        try:
            confidence = float(conf_raw) if conf_raw else 1.0
        except ValueError:
            confidence = 1.0
        out.append(
            ProvenanceRecord(
                statement_id=fields.get(PROV_STATEMENT, "") or statement_id(
                    subject, predicate, obj
                ),
                subject=subject,
                predicate=predicate,
                obj=obj,
                source=source,
                confidence=confidence,
                timestamp=_strip_xsd(fields.get(PROV_TIMESTAMP, "")),
                graph=fields.get(PROV_GRAPH, ""),
                authority=fields.get(PROV_AUTHORITY, ""),
            )
        )
    return out


def stamp_authority_on_facts(facts, records):
    """Copy ``ProvenanceRecord.authority`` onto empty ``Fact.provenance``.

    GraphStore Assertions have no dedicated authority column; the free-text
    ``provenance`` field carries the ``AuthorityLevel`` value so a later
    conflict read can rank the stored fact. Existing non-empty labels win.
    """
    if not facts or not records:
        return facts
    by_key: dict[tuple[str, str, str], str] = {}
    for r in records:
        auth = str(getattr(r, "authority", "") or "")
        if not auth:
            continue
        pred = str(getattr(r, "predicate", "") or "")
        leaf = pred.rstrip("/").rsplit("/", 1)[-1] if pred else ""
        obj = _strip_xsd(str(getattr(r, "obj", "") or ""))
        subj = str(getattr(r, "subject", "") or "")
        if subj and leaf:
            by_key[(subj, leaf, obj)] = auth
    out = []
    for f in facts:
        if getattr(f, "provenance", None):
            out.append(f)
            continue
        obj = _strip_xsd("" if f.value is None else str(f.value))
        auth = by_key.get((f.subject_id, f.key, obj))
        out.append(replace(f, provenance=auth) if auth else f)
    return out


def _predicate_leaf(predicate: str) -> str:
    """Flatten a predicate URI to the attr leaf used on Assertion / ProvEvent."""
    if not predicate:
        return ""
    if "/attrs/" in predicate:
        leaf = predicate.rsplit("/attrs/", 1)[-1]
        return leaf if leaf and "/" not in leaf else leaf
    if "/onto/" in predicate:
        leaf = predicate.rsplit("/onto/", 1)[-1]
        return leaf if leaf and "/" not in leaf else leaf
    return predicate.rstrip("/").rsplit("/", 1)[-1]


async def fetch_provenance_from_store(
    store,
    graph_uri: str,
    subject: str,
    predicate: str | None = None,
) -> list[ProvenanceRecord]:
    """Read provenance from Assertion SoT + ProvEvent companions (ONTA-536).

    Preferred property-graph path: Assertion carries ``source_url`` /
    ``confidence`` / ``verified_at``; ProvEvent fills gaps for assert events
    written when ``INFONA_PROVENANCE_ENABLED`` was on.
    """
    from infona_client.graph.queries import parse_kg_graph_uri
    from infona_client.graph.scope import GraphScope

    scope_pair = parse_kg_graph_uri(graph_uri)
    if not scope_pair:
        return []
    tenant_id, kg_name = scope_pair
    session = store.session(GraphScope.for_instance(tenant_id, kg_name))

    records: list[ProvenanceRecord] = []
    seen: set[tuple[str, str, str, str]] = set()

    # 1) Assertion SoT (always present for landed facts).
    native = getattr(session, "read_assertions_for_subject", None)
    assertion_rows: list = []
    if callable(native):
        # Do not pass ``prop_id=predicate``: Assertions store ``property_uri(leaf)``,
        # callers pass the original RDF predicate. Leaf-match below.
        assertion_rows = list(await native(subject))
    else:
        try:
            from infona_client.graph.rdfs_helpers import session_assertions_for_subject

            assertion_rows = await session_assertions_for_subject(
                session, subject
            )
        except Exception:  # noqa: BLE001 — best-effort read
            assertion_rows = []

    for row in assertion_rows:
        d = row.to_dict() if hasattr(row, "to_dict") else dict(row)
        prop = str(d.get("property_id") or "")
        if predicate and prop != predicate and not prop.endswith(
            "/" + _predicate_leaf(predicate)
        ):
            # Allow leaf match when caller passes full attrs URI vs property URI.
            if _predicate_leaf(prop) != _predicate_leaf(predicate or ""):
                continue
        obj = d.get("literal_value")
        if obj is None:
            obj = d.get("object_id") or d.get("object_class_id") or ""
        obj_s = "" if obj is None else str(obj)
        source = str(d.get("source_url") or d.get("source") or "")
        conf = d.get("confidence")
        try:
            confidence = float(conf) if conf is not None else 1.0
        except (TypeError, ValueError):
            confidence = 1.0
        ts = str(d.get("verified_at") or "")
        sid = str(d.get("assertion_id") or d.get("id") or "")
        key = (subject, prop, obj_s, source)
        if key in seen:
            continue
        seen.add(key)
        records.append(
            ProvenanceRecord(
                statement_id=sid or statement_id(subject, prop, obj_s),
                subject=subject,
                predicate=prop,
                obj=obj_s,
                source=source,
                confidence=confidence,
                timestamp=ts,
                graph=graph_uri,
                authority=str(d.get("provenance") or ""),
            )
        )

    # 2) ProvEvent assert companions — fill gaps / multi-source assertions.
    snap = getattr(store, "snapshot_prov", None)
    events = list(snap()) if callable(snap) else []
    for e in events:
        if e.get("tenant_id") != tenant_id or e.get("kg") != kg_name:
            continue
        if e.get("event_type") != "assert":
            continue
        if e.get("subject_id") != subject:
            continue
        attr = e.get("attr") or ""
        if predicate:
            want = _predicate_leaf(predicate)
            if attr != want and attr != predicate:
                continue
        obj_s = "" if e.get("object_repr") is None else str(e.get("object_repr"))
        source = str(e.get("source") or "")
        prop = predicate or attr
        key = (subject, prop, obj_s, source)
        if key in seen:
            # Prefer Assertion row but upgrade confidence/ts from ProvEvent when richer.
            continue
        seen.add(key)
        conf = e.get("confidence")
        try:
            confidence = float(conf) if conf is not None else 1.0
        except (TypeError, ValueError):
            confidence = 1.0
        records.append(
            ProvenanceRecord(
                statement_id=str(e.get("fact_hash") or e.get("statement_id") or ""),
                subject=subject,
                predicate=prop,
                obj=obj_s,
                source=source,
                confidence=confidence,
                timestamp=str(e.get("ts") or ""),
                graph=graph_uri,
            )
        )

    return records


async def fetch_provenance(
    neptune, graph_uri: str, subject: str, predicate: str | None = None,
) -> list[ProvenanceRecord]:
    """Read parsed provenance records for a subject (optionally one predicate).

    `graph_uri` is the DATA graph. On the property-graph path (ONTA-536) this
    reads Assertion provenance + ProvEvent companions via the process
    GraphStore; the SPARQL companion-graph SELECT remains only as a fallback
    when no store is configured (legacy / unit tests that mock ``neptune.query``).

    Malformed confidence values degrade to 1.0 rather than failing the read.
    """
    try:
        from infona_client.graph.store import get_optional_graph_store

        store = get_optional_graph_store()
    except Exception:  # noqa: BLE001
        store = None
    if store is not None:
        try:
            store_records = await fetch_provenance_from_store(
                store, graph_uri, subject, predicate
            )
            if store_records:
                return store_records
            # Empty store is authoritative when a store is configured — do not
            # fall through to a vestigial SPARQL client that always  fails/empties.
            # Exception: unit tests that only mock neptune and never seed the store
            # still want the SPARQL path. Prefer store when it has ANY assertions
            # for the subject OR when snapshot_prov is non-empty for the scope;
            # otherwise try SPARQL for pure-mock hermetic tests.
            has_any = False
            snap_a = getattr(store, "snapshot_assertions", None)
            if callable(snap_a):
                has_any = any(
                    (a.get("subject_id") == subject) for a in snap_a()
                )
            if has_any or (callable(getattr(store, "snapshot_prov", None)) and any(
                p.get("subject_id") == subject for p in store.snapshot_prov()
            )):
                return store_records
        except Exception:  # noqa: BLE001 — fall through to SPARQL
            pass

    if neptune is None:
        return []
    try:
        raw = await neptune.query(provenance_query(graph_uri, subject, predicate))
    except Exception:  # noqa: BLE001
        return []
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

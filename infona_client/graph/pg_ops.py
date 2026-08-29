"""Property-graph instance mutations for GraphSession (E3 / model §8).

All instance writes go through these helpers (called only from
:mod:`infona_client.graph.kg_writer`). Callers never hand-build Cypher.

Strategy:
* Prefer **session-native** methods when the store implements them
  (``MemoryGraphStore`` — hermetic tests; ``Neo4jGraphStore`` — live).
* Fall back to allowlisted :meth:`GraphSession.execute_template` where a static
  template exists (``entity_merge`` / ``entity_get``).
* Dynamic prop keys / rel types use sanitizers from :mod:`facts` + :mod:`labels`
  so tokens are never free-form user strings.

Scope is always forced by the session. Missing entity ``id`` fails closed.

Companions (E8 / model §4):
* :func:`create_prov_event` — ``:ProvEvent`` + ``[:ABOUT]->(:Entity)``
* :func:`upsert_attr_citation` — ``:AttrCitation`` + ``[:HAS_CITATION]``
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from infona_client.graph.facts import (
    Fact,
    group_facts_by_subject,
    primary_type_from_facts,
    sanitize_prop_key,
    sanitize_rel_type,
)
from infona_client.graph.iri import ATTR_META_NS
from infona_client.graph.scope import GraphScopeError
from infona_client.graph.store import require_entity_write_identity

if TYPE_CHECKING:
    from infona_client.graph.store import GraphRecord, GraphSession


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def prov_fact_hash(
    subject_id: str,
    attr: str | None = None,
    object_repr: str | None = None,
    source: str | None = None,
) -> str:
    """Stable hash of (subject, attr, object, source) — model §4.1 ``fact_hash``.

    Successor of RDF ``sha1(s|p|o|source)`` for property-graph companions.
    """
    payload = (
        f"{subject_id}|{attr or ''}|{object_repr if object_repr is not None else ''}"
        f"|{source or ''}"
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def citation_value_hash(value: Any) -> str:
    """Hash of a multi-value slot for :AttrCitation.value_hash (model §4.2)."""
    if value is None:
        return ""
    return hashlib.sha1(str(value).encode("utf-8")).hexdigest()


async def merge_entity(
    session: "GraphSession",
    entity_id: str,
    *,
    primary_type: str | None = None,
    name: str | None = None,
    source: str | None = None,
    ts: str | None = None,
) -> list["GraphRecord"]:
    """MERGE ``:Entity`` by ``(tenant_id, kg, id)`` (model §1 / §7)."""
    require_entity_write_identity({"id": entity_id})
    native = getattr(session, "write_merge_entity", None)
    params = {
        "id": entity_id,
        "primary_type": primary_type,
        "name": name,
        "source": source,
        "ts": ts or _ts(),
    }
    if callable(native):
        return await native(**params)
    return await session.execute_template("entity_merge", params)


async def set_literal(
    session: "GraphSession",
    entity_id: str,
    leaf: str,
    value: Any,
    *,
    multi_union: bool = True,
) -> list["GraphRecord"]:
    """Set an entity-scoped literal property (model §2.1 / §2.3).

    ``name`` / ``source`` map to reserved Entity display/source props.
    Other leaves go through :func:`sanitize_prop_key`. Multi-value = list union
    when ``multi_union`` is True and a second value arrives for the same key.
    """
    require_entity_write_identity({"id": entity_id})
    if leaf in ("name", "source", "primary_type"):
        prop_key = leaf
    else:
        prop_key = sanitize_prop_key(leaf)
    native = getattr(session, "write_set_literal", None)
    if callable(native):
        return await native(
            entity_id, prop_key, value, multi_union=multi_union, original_leaf=leaf
        )
    raise GraphScopeError(
        "GraphSession does not implement write_set_literal; use MemoryGraphStore "
        "or Neo4jGraphStore for property-graph instance writes"
    )


async def merge_rel(
    session: "GraphSession",
    start_id: str,
    end_id: str,
    attr_leaf: str,
) -> list["GraphRecord"]:
    """MERGE a typed relationship with B4 identity key + ``attr`` property.

    MERGE key = ``(start.id, end.id, rel type, tenant_id, kg)``.
    """
    require_entity_write_identity({"id": start_id})
    require_entity_write_identity({"id": end_id})
    if not attr_leaf or not str(attr_leaf).strip():
        raise GraphScopeError("Relationship attr leaf must be non-empty")
    rel_type = sanitize_rel_type(attr_leaf)
    native = getattr(session, "write_merge_rel", None)
    if callable(native):
        return await native(start_id, end_id, rel_type, attr_leaf)
    raise GraphScopeError(
        "GraphSession does not implement write_merge_rel; use MemoryGraphStore "
        "or Neo4jGraphStore for property-graph instance writes"
    )


async def delete_entity(session: "GraphSession", entity_id: str) -> int:
    """Delete an Entity node and its incident relationships within session scope."""
    require_entity_write_identity({"id": entity_id})
    native = getattr(session, "write_delete_entity", None)
    if callable(native):
        return int(await native(entity_id))
    raise GraphScopeError(
        "GraphSession does not implement write_delete_entity; use MemoryGraphStore "
        "or Neo4jGraphStore for property-graph instance writes"
    )


async def delete_literals(
    session: "GraphSession",
    entity_id: str,
    leaves: Sequence[str],
) -> int:
    """Remove named literal properties from an Entity (predicate-scoped clear).

    Also removes matching datatype Assertions (ADR 0013 SoT). Assertion deletes
    are required on the store path — failures are logged and re-raised (not
    swallowed).
    """
    require_entity_write_identity({"id": entity_id})
    keys: list[str] = []
    original_leaves: list[str] = []
    for leaf in leaves:
        original_leaves.append(leaf)
        if leaf in ("name", "source", "primary_type"):
            keys.append(leaf)
        else:
            keys.append(sanitize_prop_key(leaf))
    native = getattr(session, "write_delete_literals", None)
    if not callable(native):
        raise GraphScopeError(
            "GraphSession does not implement write_delete_literals"
        )
    n = int(await native(entity_id, keys))
    # Assertion SoT cleanup by property IRI — must not be silently skipped.
    from infona_client.graph.assertion_model import property_uri
    from infona_client.graph.rdf_model import delete_assertions_for_subject

    for leaf in original_leaves:
        prop_id = property_uri(leaf)
        try:
            n += await delete_assertions_for_subject(
                session, entity_id, property_id=prop_id
            )
        except Exception:
            import structlog

            structlog.get_logger(__name__).exception(
                "assertion_delete_literals_failed",
                entity_id=entity_id,
                property_id=prop_id,
                leaf=leaf,
            )
            raise
    return n


async def delete_rels(
    session: "GraphSession",
    *,
    start_id: str | None = None,
    end_id: str | None = None,
    attr_leaf: str | None = None,
    end_id_exact: str | None = None,
) -> int:
    """Delete relationships in scope filtered by start / end / attr leaf.

    ``end_id_exact`` is the object endpoint when deleting a concrete edge;
    ``end_id`` is kept as an alias for the same filter. Also removes matching
    object Assertions (ADR 0013 SoT). Assertion delete failures are logged and
    re-raised — never swallowed.
    """
    end = end_id_exact if end_id_exact is not None else end_id
    rel_type = sanitize_rel_type(attr_leaf) if attr_leaf else None
    native = getattr(session, "write_delete_rels", None)
    if not callable(native):
        raise GraphScopeError("GraphSession does not implement write_delete_rels")
    n = int(
        await native(
            start_id=start_id,
            end_id=end,
            rel_type=rel_type,
            attr_leaf=attr_leaf,
        )
    )
    if start_id and attr_leaf:
        from infona_client.graph.assertion_model import property_uri
        from infona_client.graph.rdf_model import delete_assertions_for_subject

        prop_id = property_uri(attr_leaf)
        try:
            n += await delete_assertions_for_subject(
                session,
                start_id,
                property_id=prop_id,
                object_key=end,
            )
        except Exception:
            import structlog

            structlog.get_logger(__name__).exception(
                "assertion_delete_rels_failed",
                start_id=start_id,
                property_id=prop_id,
                object_key=end,
                attr_leaf=attr_leaf,
            )
            raise
    return n


async def rewrite_entity_id(
    session: "GraphSession",
    old_id: str,
    new_id: str,
) -> None:
    """Re-key Entity ``id`` and rebind relationship endpoints (not delete+insert)."""
    require_entity_write_identity({"id": old_id})
    require_entity_write_identity({"id": new_id})
    if old_id == new_id:
        return
    native = getattr(session, "write_rewrite_entity_id", None)
    if callable(native):
        await native(old_id, new_id)
        return
    raise GraphScopeError(
        "GraphSession does not implement write_rewrite_entity_id"
    )


async def create_prov_event(
    session: "GraphSession",
    *,
    event_type: str,
    subject_id: str,
    attr: str | None = None,
    object_repr: str | None = None,
    old_id: str | None = None,
    new_id: str | None = None,
    reason: str = "",
    source: str | None = None,
    fact_hash: str | None = None,
    confidence: float | None = None,
    ts: str | None = None,
    statement_id: str | None = None,
) -> None:
    """Write ``:ProvEvent`` + ``[:ABOUT]->(:Entity)`` (model §4.1 / ONTA-536).

    Event types used on the store path (Wave 1 / E8): ``assert``, ``tombstone``,
    ``rewrite``. Best-effort when the session lacks a native writer.

    ONTA-536: ``assert`` events carry the ADR 0002 governance fields the RDF
    companion graph used to hold — ``source``, ``confidence``, ``statement_id``
    (via ``fact_hash``), and the *asserted-at* ``ts`` (not merely write time) —
    so "who asserted this value, with what confidence, as of when" is recoverable.
    """
    native = getattr(session, "write_prov_event", None)
    if not callable(native):
        return  # optional on stores that do not implement companions yet
    fh = fact_hash or statement_id
    if fh is None and event_type in ("assert", "tombstone"):
        fh = prov_fact_hash(subject_id, attr, object_repr, source)
    elif fh is None and event_type == "rewrite":
        fh = prov_fact_hash(old_id or subject_id, None, new_id or subject_id, source)
    kwargs: dict[str, Any] = dict(
        event_type=event_type,
        subject_id=subject_id,
        attr=attr,
        object_repr=object_repr,
        old_id=old_id,
        new_id=new_id,
        reason=reason,
        source=source,
        fact_hash=fh,
        ts=ts or _ts(),
    )
    # confidence is optional on older store implementations — pass only when set
    # so signature mismatches degrade rather than crash the write path.
    if confidence is not None:
        kwargs["confidence"] = confidence
    try:
        await native(**kwargs)
    except TypeError:
        kwargs.pop("confidence", None)
        await native(**kwargs)


@dataclass(frozen=True, slots=True)
class AttrCitationSpec:
    """One enrichment-style attribute citation (model §4.2 / former attr_meta)."""

    entity_id: str
    attr: str
    source_url: str | None = None
    provenance: str | None = None
    verified_at: str | None = None
    value_hash: str | None = None
    value: Any = None  # optional; used to derive value_hash when unset


async def upsert_attr_citation(
    session: "GraphSession",
    entity_id: str,
    attr: str,
    *,
    source_url: str | None = None,
    provenance: str | None = None,
    verified_at: str | None = None,
    value_hash: str | None = None,
    value: Any = None,
) -> None:
    """Upsert ``:AttrCitation`` + ``(e)-[:HAS_CITATION]->(c)`` (model §4.2).

    Minimal enrichment ``source_url`` / ``provenance`` / ``verified_at`` helper.
    MERGE identity is ``(tenant_id, kg, entity_id, attr, value_hash)`` when the
    store implements it; otherwise create-or-replace best-effort.
    """
    require_entity_write_identity({"id": entity_id})
    if not attr or not str(attr).strip():
        raise GraphScopeError("AttrCitation attr leaf must be non-empty")
    vh = value_hash
    if vh is None and value is not None:
        vh = citation_value_hash(value)
    native = getattr(session, "write_attr_citation", None)
    if not callable(native):
        return
    await native(
        entity_id=entity_id,
        attr=str(attr).strip(),
        source_url=source_url or None,
        provenance=provenance or None,
        verified_at=verified_at or None,
        value_hash=vh or "",
    )


def parse_attr_meta_citations(
    triples: Iterable[tuple[str, str, str]],
) -> list[AttrCitationSpec]:
    """Group RDF-era ``attr_meta/<Type>/<attr>/<suffix>`` triples into citations.

    Type segment is ignored for storage (entity-scoped AttrCitation; B3 spirit).
    Suffixes: ``source_url``, ``provenance``, ``verified_at``.
    """
    # key: (entity_id, attr) -> fields
    buckets: dict[tuple[str, str], dict[str, str]] = {}
    for triple in triples or ():
        if not triple or len(triple) < 3:
            continue
        s, p, o = triple[0], triple[1], triple[2]
        if not s or not p or o is None:
            continue
        # Accept any host's attr_meta/ path, not only the live ATTR_META_NS base
        # (legacy graph.infona.ai / graph.infona.ai companions still map).
        marker = "/attr_meta/"
        idx = p.find(marker)
        if idx < 0:
            # Also accept absolute ATTR_META_NS when base matches.
            if p.startswith(ATTR_META_NS):
                rest = p[len(ATTR_META_NS) :]
            else:
                continue
        else:
            rest = p[idx + len(marker) :]
        parts = rest.split("/")
        if len(parts) < 3:
            continue
        # type_name = parts[0]  — ignored for entity-scoped citation
        attr = parts[1]
        suffix = parts[2]
        if not attr or suffix not in ("source_url", "provenance", "verified_at"):
            continue
        # Strip xsd typing if present on the object string.
        val = str(o)
        if "^^" in val:
            val = val.split("^^", 1)[0]
        key = (s, attr)
        buckets.setdefault(key, {})[suffix] = val
    out: list[AttrCitationSpec] = []
    for (entity_id, attr), fields in buckets.items():
        if not any(fields.get(k) for k in ("source_url", "provenance", "verified_at")):
            continue
        out.append(
            AttrCitationSpec(
                entity_id=entity_id,
                attr=attr,
                source_url=fields.get("source_url"),
                provenance=fields.get("provenance"),
                verified_at=fields.get("verified_at"),
            )
        )
    return out


async def apply_attr_citations(
    session: "GraphSession",
    citations: Sequence[AttrCitationSpec],
) -> int:
    """Write a batch of :class:`AttrCitationSpec` via :func:`upsert_attr_citation`.

    Secondary companion only (model §4.2 residual). Primary enrichment
    citations fold onto Assertion provenance via
    :func:`fold_attr_citations_onto_facts` on the store path.
    """
    n = 0
    for c in citations:
        await upsert_attr_citation(
            session,
            c.entity_id,
            c.attr,
            source_url=c.source_url,
            provenance=c.provenance,
            verified_at=c.verified_at,
            value_hash=c.value_hash,
            value=c.value,
        )
        n += 1
    return n


def fold_attr_citations_onto_facts(
    facts: Sequence[Fact],
    citations: Sequence[AttrCitationSpec],
) -> list[Fact]:
    """Fold attr_meta enrichment citations onto matching domain Facts (ADR 0013).

    Primary store-path destination for ``source_url`` / ``verified_at`` /
    ``provenance`` is the Assertion for the domain attribute — not a sibling
    :AttrCitation node alone. Matching key is ``(entity_id, attr leaf)``.
    Existing non-empty Fact provenance fields win over citation fields.
    """
    if not facts or not citations:
        return list(facts)
    by_key: dict[tuple[str, str], AttrCitationSpec] = {}
    for c in citations:
        if not c.entity_id or not c.attr:
            continue
        by_key[(c.entity_id, c.attr)] = c
    if not by_key:
        return list(facts)

    out: list[Fact] = []
    for f in facts:
        if f.kind not in ("literal", "rel"):
            out.append(f)
            continue
        c = by_key.get((f.subject_id, f.key))
        if c is None:
            out.append(f)
            continue
        # Prefer Fact-side values when already set (structured Fact IR path).
        source_url = f.source_url or c.source_url or f.source
        verified_at = f.verified_at or c.verified_at
        provenance = f.provenance or c.provenance
        if (
            source_url == f.source_url
            and verified_at == f.verified_at
            and provenance == f.provenance
        ):
            out.append(f)
            continue
        out.append(
            Fact(
                subject_id=f.subject_id,
                kind=f.kind,
                key=f.key,
                value=f.value,
                source=source_url,
                source_url=source_url,
                verified_at=verified_at,
                run_id=f.run_id,
                confidence=f.confidence,
                provenance=provenance,
            )
        )
    return out


def _prov_predicate_leaf(predicate: str) -> str:
    """Attr leaf from a full predicate URI (attrs/ or onto/)."""
    if not predicate:
        return ""
    if "/attrs/" in predicate:
        leaf = predicate.rsplit("/attrs/", 1)[-1]
        return leaf if leaf and "/" not in leaf else leaf
    if "/onto/" in predicate:
        leaf = predicate.rsplit("/onto/", 1)[-1]
        return leaf if leaf and "/" not in leaf else leaf
    return predicate.rstrip("/").rsplit("/", 1)[-1]


def fold_provenance_records_onto_facts(
    facts: Sequence[Fact],
    records: Sequence[Any],
) -> list[Fact]:
    """Fold ADR 0002 companion-provenance records onto matching domain Facts.

    Matching key is ``(subject_id, attr leaf, lexical object)``. Existing
    non-empty Fact provenance fields win. Used by :func:`insert_facts` so the
    RDF ``provenance_triples`` payload (still built by ingest/enrichment) lands
    on Assertion ``source_url`` / ``confidence`` / ``verified_at`` (ONTA-536).
    """
    if not facts or not records:
        return list(facts)

    def _lex(v: Any) -> str:
        if v is None:
            return ""
        s = str(v)
        if "^^" in s:
            s = s.split("^^", 1)[0]
        return s

    by_key: dict[tuple[str, str, str], Any] = {}
    by_subj_attr: dict[tuple[str, str], Any] = {}
    for r in records:
        subj = getattr(r, "subject", None) or ""
        pred = getattr(r, "predicate", None) or ""
        obj = _lex(getattr(r, "obj", None))
        leaf = _prov_predicate_leaf(pred)
        if not subj or not leaf:
            continue
        by_key[(subj, leaf, obj)] = r
        by_subj_attr[(subj, leaf)] = r

    out: list[Fact] = []
    for f in facts:
        if f.kind not in ("literal", "rel"):
            out.append(f)
            continue
        obj_s = _lex(f.value)
        r = by_key.get((f.subject_id, f.key, obj_s)) or by_subj_attr.get(
            (f.subject_id, f.key)
        )
        if r is None:
            out.append(f)
            continue
        source = f.source_url or f.source or getattr(r, "source", None) or None
        conf = f.confidence
        if conf is None:
            conf = getattr(r, "confidence", None)
        verified = f.verified_at or getattr(r, "timestamp", None) or None
        if verified == "":
            verified = None
        if (
            source == (f.source_url or f.source)
            and conf == f.confidence
            and verified == f.verified_at
        ):
            out.append(f)
            continue
        out.append(
            Fact(
                subject_id=f.subject_id,
                kind=f.kind,
                key=f.key,
                value=f.value,
                source=source,
                source_url=source,
                verified_at=verified,
                run_id=f.run_id,
                confidence=float(conf) if conf is not None else None,
                provenance=f.provenance,
            )
        )
    return out


async def apply_provenance_records(
    session: "GraphSession",
    records: Sequence[Any],
    *,
    provenance_enabled: bool = True,
) -> int:
    """Write ProvEvents for companion-provenance records that lack a domain Fact.

    Covers the legacy per-entity path that calls
    ``insert_facts([], provenance_triples=…)`` with an empty instance batch, and
    any provenance-only residual after folding (ONTA-536).
    """
    if not provenance_enabled or not records:
        return 0
    n = 0
    for r in records:
        subj = getattr(r, "subject", None) or ""
        pred = getattr(r, "predicate", None) or ""
        if not subj:
            continue
        leaf = _prov_predicate_leaf(pred)
        obj = getattr(r, "obj", None)
        obj_repr = None if obj is None else str(obj)
        if obj_repr is not None and "^^" in obj_repr:
            obj_repr = obj_repr.split("^^", 1)[0]
        source = getattr(r, "source", None) or None
        conf = getattr(r, "confidence", None)
        ts = getattr(r, "timestamp", None) or None
        sid = getattr(r, "statement_id", None) or None
        await create_prov_event(
            session,
            event_type="assert",
            subject_id=subj,
            attr=leaf or None,
            object_repr=obj_repr,
            source=source,
            fact_hash=sid,
            statement_id=sid,
            confidence=float(conf) if conf is not None else None,
            ts=ts or None,
        )
        n += 1
    return n


async def apply_facts(
    session: "GraphSession",
    facts: Sequence[Fact],
    *,
    provenance_enabled: bool = False,
) -> int:
    """Apply a batch of Facts via Assertion SoT + derived Entity cache (ADR 0013).

    Prefer ``session.write_fact_batch`` (Neo4j UNWIND / Memory in-process).
    Sessions without it fall back to per-fact :func:`assert_fact` (same order:
    entity shells, Assertions, then dual-write cache + optional ProvEvents).

    Returns the number of Facts applied.
    """
    if not facts:
        return 0

    write_assertion = getattr(session, "write_assertion", None)
    write_fact_batch = getattr(session, "write_fact_batch", None)
    if not callable(write_assertion) and not callable(write_fact_batch):
        raise GraphScopeError(
            "GraphSession does not implement write_assertion; Assertion is "
            "required source-of-truth on the store path (ADR 0013). Use "
            "MemoryGraphStore or Neo4jGraphStore."
        )

    # Fast path: one UNWIND (Neo4j) / in-process batch (Memory) after the
    # ontology catalog exists. Per-fact assert_fact is thousands of Bolt
    # round-trips and is why CSV ingest was ~1s/row.
    if callable(write_fact_batch):
        from infona_client.graph.fact_batch import prepare_fact_batch

        batch = prepare_fact_batch(
            list(facts), provenance_enabled=provenance_enabled
        )
        return int(await write_fact_batch(batch))

    from infona_client.graph.rdf_model import assert_fact, fact_to_assertion_fact

    grouped = group_facts_by_subject(facts)
    applied = 0

    # First pass: ensure all subjects (and rel targets) exist as Entity nodes.
    target_ids: set[str] = set(grouped)
    for f in facts:
        if f.kind == "rel" and isinstance(f.value, str) and f.value:
            target_ids.add(f.value)

    for sid in target_ids:
        sub_facts = grouped.get(sid, [])
        primary = primary_type_from_facts(sub_facts)
        name = None
        source = None
        for f in sub_facts:
            if f.kind == "literal" and f.key == "name" and f.value is not None:
                name = f.value
            if f.kind == "literal" and f.key == "source" and f.value is not None:
                source = f.value
            if f.source:
                source = f.source
            if f.source_url:
                source = f.source_url
        await merge_entity(
            session, sid, primary_type=primary, name=name, source=source
        )

    # Second pass: Assertion SoT first; dual-write Entity cache after each.
    for f in facts:
        af = fact_to_assertion_fact(
            subject_id=f.subject_id,
            kind=f.kind,
            key=f.key,
            value=f.value,
            source=f.source,
            source_url=f.source_url,
            verified_at=f.verified_at,
            run_id=f.run_id,
            confidence=f.confidence,
            provenance=f.provenance,
        )
        await assert_fact(session, af, dual_write_cache=True)
        applied += 1
        if provenance_enabled and f.kind in ("literal", "rel"):
            obj_repr = (
                str(f.value)
                if f.kind == "literal" and f.value is not None
                else (f.value if f.kind == "rel" else None)
            )
            if obj_repr is not None and not isinstance(obj_repr, str):
                obj_repr = str(obj_repr)
            src = f.source_url or f.source
            await create_prov_event(
                session,
                event_type="assert",
                subject_id=f.subject_id,
                attr=f.key,
                object_repr=obj_repr,
                source=src,
                fact_hash=prov_fact_hash(
                    f.subject_id,
                    f.key,
                    obj_repr,
                    src,
                ),
                confidence=f.confidence,
                ts=f.verified_at or None,
            )

    return applied


async def get_entity(
    session: "GraphSession", entity_id: str
) -> Mapping[str, Any] | None:
    """Fetch one entity record (test/helper)."""
    require_entity_write_identity({"id": entity_id})
    native = getattr(session, "write_get_entity", None)
    if callable(native):
        return await native(entity_id)
    rows = await session.execute_template("entity_get", {"id": entity_id})
    if not rows:
        return None
    return rows[0].to_dict()


__all__ = [
    "AttrCitationSpec",
    "apply_attr_citations",
    "apply_facts",
    "apply_provenance_records",
    "citation_value_hash",
    "create_prov_event",
    "delete_entity",
    "delete_literals",
    "delete_rels",
    "fold_attr_citations_onto_facts",
    "fold_provenance_records_onto_facts",
    "get_entity",
    "merge_entity",
    "merge_rel",
    "parse_attr_meta_citations",
    "prov_fact_hash",
    "rewrite_entity_id",
    "set_literal",
    "upsert_attr_citation",
]


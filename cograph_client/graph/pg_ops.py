"""Property-graph instance mutations for GraphSession (E3 / model §8).

All instance writes go through these helpers (called only from
:mod:`cograph_client.graph.kg_writer`). Callers never hand-build Cypher.

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

from cograph_client.graph.facts import (
    Fact,
    group_facts_by_subject,
    primary_type_from_facts,
    sanitize_prop_key,
    sanitize_rel_type,
)
from cograph_client.graph.iri import ATTR_META_NS
from cograph_client.graph.labels import sanitize_domain_labels, set_entity_type_labels
from cograph_client.graph.scope import GraphScopeError
from cograph_client.graph.store import require_entity_write_identity

if TYPE_CHECKING:
    from cograph_client.graph.store import GraphRecord, GraphSession


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

    Also removes matching datatype Assertions (ADR 0013 SoT) when the session
    implements assertion deletes.
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
    # Best-effort Assertion SoT cleanup by property IRI.
    try:
        from cograph_client.graph.assertion_model import property_uri
        from cograph_client.graph.rdf_model import delete_assertions_for_subject

        for leaf in original_leaves:
            if leaf in ("name", "source", "primary_type"):
                prop_id = property_uri(leaf)
            else:
                prop_id = property_uri(leaf)
            n += await delete_assertions_for_subject(
                session, entity_id, property_id=prop_id
            )
    except Exception:  # noqa: BLE001 — cache delete must not fail on assertion gap
        pass
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
    object Assertions (ADR 0013) when the session supports it.
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
        try:
            from cograph_client.graph.assertion_model import property_uri
            from cograph_client.graph.rdf_model import delete_assertions_for_subject

            n += await delete_assertions_for_subject(
                session,
                start_id,
                property_id=property_uri(attr_leaf),
                object_key=end,
            )
        except Exception:  # noqa: BLE001
            pass
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
) -> None:
    """Write ``:ProvEvent`` + ``[:ABOUT]->(:Entity)`` (model §4.1).

    Event types used on the store path (Wave 1 / E8): ``assert``, ``tombstone``,
    ``rewrite``. Best-effort when the session lacks a native writer.
    """
    native = getattr(session, "write_prov_event", None)
    if not callable(native):
        return  # optional on stores that do not implement companions yet
    fh = fact_hash
    if fh is None and event_type in ("assert", "tombstone"):
        fh = prov_fact_hash(subject_id, attr, object_repr, source)
    elif fh is None and event_type == "rewrite":
        fh = prov_fact_hash(old_id or subject_id, None, new_id or subject_id, source)
    await native(
        event_type=event_type,
        subject_id=subject_id,
        attr=attr,
        object_repr=object_repr,
        old_id=old_id,
        new_id=new_id,
        reason=reason,
        source=source,
        fact_hash=fh,
        ts=_ts(),
    )


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
        # (legacy cograph.tech / omnix.dev companions still map).
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
    """Write a batch of :class:`AttrCitationSpec` via :func:`upsert_attr_citation`."""
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


async def apply_facts(
    session: "GraphSession",
    facts: Sequence[Fact],
    *,
    provenance_enabled: bool = False,
) -> int:
    """Apply a batch of Facts via Assertion SoT + derived Entity cache (ADR 0013).

    1. MERGE Entities + domain labels + property cache + shortcut rels
       (derived projections for Explorer / hot paths).
    2. Write :Assertion nodes (unit of truth) for each Fact, with
       ``INSTANCE_OF`` dual-written for type membership.

    Returns the number of Facts applied. Ensures target entities exist for rels.
    """
    if not facts:
        return 0
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
        await merge_entity(
            session, sid, primary_type=primary, name=name, source=source
        )

    # Second pass: types (labels), literals, rels per subject — derived cache.
    for sid, sub_facts in grouped.items():
        type_leaves = [f.key for f in sub_facts if f.kind == "type"]
        if type_leaves:
            safe = sanitize_domain_labels(type_leaves)
            await set_entity_type_labels(session, sid, safe)
            applied += len(type_leaves)

        for f in sub_facts:
            if f.kind == "literal":
                if f.key == "name":
                    await merge_entity(session, sid, name=f.value)
                elif f.key == "source":
                    await merge_entity(session, sid, source=f.value)
                else:
                    await set_literal(session, sid, f.key, f.value, multi_union=True)
                applied += 1
                if provenance_enabled:
                    obj_repr = str(f.value) if f.value is not None else None
                    await create_prov_event(
                        session,
                        event_type="assert",
                        subject_id=sid,
                        attr=f.key,
                        object_repr=obj_repr,
                        source=f.source,
                        fact_hash=prov_fact_hash(sid, f.key, obj_repr, f.source),
                    )
            elif f.kind == "rel":
                if not isinstance(f.value, str) or not f.value:
                    raise GraphScopeError(
                        f"rel Fact requires target entity id string, got {f.value!r}"
                    )
                await merge_rel(session, sid, f.value, f.key)
                applied += 1
                if provenance_enabled:
                    await create_prov_event(
                        session,
                        event_type="assert",
                        subject_id=sid,
                        attr=f.key,
                        object_repr=f.value,
                        source=f.source,
                        fact_hash=prov_fact_hash(sid, f.key, f.value, f.source),
                    )
            # type facts already counted above

    # Third pass: Assertion SoT (ADR 0013). Dual-write cache already done above;
    # still dual-write INSTANCE_OF for type Assertions via assert_fact.
    if getattr(session, "write_assertion", None) is not None:
        from cograph_client.graph.rdf_model import assert_fact, fact_to_assertion_fact

        for f in facts:
            try:
                af = fact_to_assertion_fact(
                    subject_id=f.subject_id,
                    kind=f.kind,
                    key=f.key,
                    value=f.value,
                    source=f.source,
                )
            except GraphScopeError:
                continue
            # dual_write_cache=False: Entity props/rels already applied; type
            # path still needs INSTANCE_OF which assert_fact writes when True
            # for kind=type only.
            await assert_fact(
                session,
                af,
                dual_write_cache=(f.kind == "type"),
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
    "citation_value_hash",
    "create_prov_event",
    "delete_entity",
    "delete_literals",
    "delete_rels",
    "get_entity",
    "merge_entity",
    "merge_rel",
    "parse_attr_meta_citations",
    "prov_fact_hash",
    "rewrite_entity_id",
    "set_literal",
    "upsert_attr_citation",
]

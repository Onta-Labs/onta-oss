"""Qualified extraction-id keys so two types can share a raw id in one batch.

CRM exports (Ontraport, HubSpot, …) number Contact 17 and Purchase 17
independently. The ingest write path used to key ``entity_uri_map`` by
``entity.id`` alone, so the last writer silently overwrote the other type's
URI — purchases vanished into contacts, attributes bled, edges inverted.

Keys are ``{declared_type}\\x1e{id}``. Unqualified ``id`` is also stored when
unique so legacy relationships (no ``source_type``) still resolve. URI minting
still uses the raw id: ``entity_uri(type, id)`` is unchanged.
"""

from __future__ import annotations

from infona_client.resolver.models_extract import ExtractedEntity, ExtractedRelationship

SEP = "\x1e"


def qualified_id(type_name: str, entity_id: str) -> str:
    return f"{type_name}{SEP}{entity_id}"


def map_key(entity: ExtractedEntity) -> str:
    return qualified_id(entity.type_name, entity.id)


def rel_source_key(rel: ExtractedRelationship) -> str:
    typ = getattr(rel, "source_type", None) or ""
    if typ:
        return qualified_id(typ, rel.source_id)
    return rel.source_id


def rel_target_key(rel: ExtractedRelationship) -> str:
    typ = getattr(rel, "target_type", None) or ""
    if typ:
        return qualified_id(typ, rel.target_id)
    return rel.target_id


def register_entity(
    *,
    declared_type: str,
    entity_id: str,
    resolved_type: str,
    uri: str,
    uri_map: dict[str, str],
    type_map: dict[str, str],
    resolved_types: dict[str, str],
    unqualified_owner: dict[str, str],
    collided: set[str],
) -> str:
    """Index ``uri`` under a type-qualified key; keep unqualified only if unique.

    Returns the qualified key. ``resolved_types`` is keyed the same way as
    ``uri_map`` (qualified + unqualified-when-unique) so Pass 2 / key-join can
    look up either form.
    """
    q = qualified_id(declared_type, entity_id)
    uri_map[q] = uri
    type_map[q] = resolved_type
    resolved_types[q] = resolved_type
    if entity_id in collided:
        return q
    owner = unqualified_owner.get(entity_id)
    if owner is None:
        unqualified_owner[entity_id] = declared_type
        uri_map[entity_id] = uri
        type_map[entity_id] = resolved_type
        resolved_types[entity_id] = resolved_type
    elif owner != declared_type:
        collided.add(entity_id)
        uri_map.pop(entity_id, None)
        type_map.pop(entity_id, None)
        resolved_types.pop(entity_id, None)
    return q


def lookup_uri(
    uri_map: dict[str, str],
    entity_id: str,
    declared_type: str | None = None,
) -> str | None:
    if declared_type:
        q = qualified_id(declared_type, entity_id)
        if q in uri_map:
            return uri_map[q]
    return uri_map.get(entity_id)


def lookup_type(
    type_map: dict[str, str],
    entity_id: str,
    declared_type: str | None = None,
) -> str | None:
    if declared_type:
        q = qualified_id(declared_type, entity_id)
        if q in type_map:
            return type_map[q]
    return type_map.get(entity_id)


def qualified_count(uri_map: dict[str, str]) -> int:
    return sum(1 for k in uri_map if SEP in k)


def entity_is_skipped(entity: ExtractedEntity, skip_ids: set[str]) -> bool:
    """Skip only the qualified key — never a collided raw id."""
    return map_key(entity) in skip_ids


def rel_is_skipped(rel: ExtractedRelationship, skip_ids: set[str]) -> bool:
    return rel_source_key(rel) in skip_ids or rel_target_key(rel) in skip_ids


def fan_in_natural_uris(
    uri_map: dict[str, str],
    type_map: dict[str, str],
    mint_uri,
) -> dict[str, str]:
    """ER/key-join fan-in: natural URI → surviving URI. Qualified keys only."""
    ids_by_uri: dict[str, list[str]] = {}
    for eid, uri in uri_map.items():
        if SEP not in eid:
            continue
        ids_by_uri.setdefault(uri, []).append(eid)
    fan_in: dict[str, str] = {}
    for uri, eids in ids_by_uri.items():
        if len(eids) < 2:
            continue
        for eid in eids:
            _decl, raw = eid.split(SEP, 1)
            natural = mint_uri(type_map.get(eid, ""), raw)
            if natural != uri:
                fan_in[natural] = uri
    return fan_in

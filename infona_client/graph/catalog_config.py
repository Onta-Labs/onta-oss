"""Tenant-catalog config entities (rules / policies) on GraphStore (ONTA-529).

Normalization rules, clean policies, and verify policies live as ordinary
``:Entity`` nodes in the **tenant catalog** scope
(``GraphScope.for_catalog(layer='tenant')``, ``kg=__ontology__``). Writes go
through the converged :func:`~infona_client.graph.kg_writer.insert_facts` /
:func:`~infona_client.graph.kg_writer.delete_facts` path (which resolves the
tenant ontology graph URI to that catalog scope). Reads used to be raw SPARQL
against a vestigial Neptune client — this module is the ONE GraphStore read
seam for those config rows.

Property keys on the Entity are the sanitized form of the RDF leaf
(``…/onto/norm/kgName`` → Fact key ``norm/kgName`` → prop ``norm_kgName``).
Readers rebuild an RDF-style ``{predicate_uri: lexical_value}`` field map so
the existing ``_rule_from_fields`` / ``_policy_from_fields`` serializers keep
working unchanged.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"

# Soft page cap for config-entity list scans. Config rows are tenant metadata
# (dozens, not millions); a single page is enough and avoids unbounded fan-out.
_LIST_LIMIT = 5000


def catalog_session(tenant_id: str, *, store=None, session=None):
    """Return a tenant-catalog :class:`GraphSession` for config-entity work."""
    from infona_client.graph.ontology_catalog import resolve_catalog_session

    return resolve_catalog_session(
        store=store, session=session, layer="tenant", tenant_id=tenant_id
    )


def lexical(value: Any) -> str:
    """Coerce a GraphStore property value to the SPARQL-style lexical form.

    Bools become ``"true"``/``"false"`` (matching xsd:boolean lexical forms the
    policy stores parse). Everything else is ``str(...)``. ``None`` → ``""``.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def entity_to_fields(
    entity: Mapping[str, Any],
    *,
    type_uri: str,
    prop_ns: str,
    key_prefix: str,
) -> Optional[dict[str, str]]:
    """Map a GraphStore entity record to an RDF-style field dict.

    Returns ``None`` when the entity's ``primary_type`` does not match the
    leaf of ``type_uri`` (so a mistyped subject is never reconstructed as a
    rule/policy).

    Parameters
    ----------
    type_uri:
        Full type IRI, e.g. ``…/types/NormalizationRule``.
    prop_ns:
        Predicate namespace, e.g. ``…/onto/norm/`` or ``…/onto/policy/``.
    key_prefix:
        Sanitized Entity property key prefix produced by
        :func:`~infona_client.graph.facts.sanitize_prop_key` on
        ``norm/<leaf>`` / ``policy/<leaf>`` — i.e. ``"norm_"`` / ``"policy_"``.
    """
    primary = entity.get("primary_type") or ""
    type_leaf = type_uri.rsplit("/", 1)[-1] if type_uri else ""
    if primary and type_leaf and primary != type_leaf:
        return None

    fields: dict[str, str] = {RDF_TYPE: type_uri}
    props = entity.get("props")
    if not isinstance(props, Mapping):
        # Some session backends flatten props onto the top-level record.
        props = {
            k: v
            for k, v in entity.items()
            if k
            not in {
                "id",
                "tenant_id",
                "kg",
                "primary_type",
                "name",
                "source",
                "labels",
                "props",
            }
        }
    for key, value in props.items():
        if not isinstance(key, str):
            continue
        leaf: str | None = None
        if key.startswith(key_prefix):
            leaf = key[len(key_prefix) :]
        elif "/" in key and key.startswith(key_prefix.rstrip("_") + "/"):
            # Unsanitized Fact key form ``norm/kgName`` if a backend ever keeps it.
            leaf = key.split("/", 1)[1]
        if not leaf:
            continue
        fields[prop_ns + leaf] = lexical(value)
    return fields


async def get_entity_fields(
    tenant_id: str,
    entity_uri: str,
    *,
    type_uri: str,
    prop_ns: str,
    key_prefix: str,
    store=None,
    session=None,
) -> Optional[dict[str, str]]:
    """Load one config entity and return its RDF-style field map, or ``None``."""
    from infona_client.graph import pg_ops

    gs = catalog_session(tenant_id, store=store, session=session)
    entity = await pg_ops.get_entity(gs, entity_uri)
    if not entity:
        return None
    return entity_to_fields(
        entity, type_uri=type_uri, prop_ns=prop_ns, key_prefix=key_prefix
    )


async def list_entity_fields(
    tenant_id: str,
    *,
    type_leaf: str,
    type_uri: str,
    prop_ns: str,
    key_prefix: str,
    store=None,
    session=None,
) -> list[tuple[str, dict[str, str]]]:
    """List config entities of ``type_leaf`` as ``(entity_uri, fields)`` pairs."""
    from infona_client.graph import pg_ops
    from infona_client.graph.explore_store import list_entities_by_type_pg

    gs = catalog_session(tenant_id, store=store, session=session)
    page = await list_entities_by_type_pg(
        gs, type_leaf, match="primary_type", limit=_LIST_LIMIT
    )
    out: list[tuple[str, dict[str, str]]] = []
    for summary in page.entities:
        entity = await pg_ops.get_entity(gs, summary.id)
        if not entity:
            continue
        fields = entity_to_fields(
            entity, type_uri=type_uri, prop_ns=prop_ns, key_prefix=key_prefix
        )
        if fields is not None:
            out.append((summary.id, fields))
    return out


__all__ = [
    "RDF_TYPE",
    "catalog_session",
    "entity_to_fields",
    "get_entity_fields",
    "lexical",
    "list_entity_fields",
]

"""Legacy SPARQL alias / rename / retire / deprecate apply path.

Looks up patched names on :mod:`infona_client.graph.ontology_commit` via
``_host()``. Schema writes still go through ontology_queries builders +
``insert_triples`` (the existing SPARQL schema path), not instance
``insert_facts``.
"""

from __future__ import annotations

import structlog

from infona_client.graph.aliases import register_alias, retire_alias
from infona_client.graph.ontology_commit_core import DEPRECATED_AT, SUPERSEDED_BY
from infona_client.graph.ontology_queries import (
    XSD,
    attr_uri,
    delete_attribute_declaration,
    type_uri,
    upsert_attribute,
)
from infona_client.graph.queries import insert_triples
from infona_client.models.ontology import ChangeRecord, OntologyMutation

logger = structlog.stdlib.get_logger("infona.graph.ontology_commit")


def _host():
    """Call-time lookup of the public ontology_commit module (monkeypatch surface)."""
    from infona_client.graph import ontology_commit as _mod

    return _mod


async def _apply_deprecate(
    neptune, graph_uri: str, mut: OntologyMutation,
) -> list[ChangeRecord]:
    """Mark a type or attribute deprecated without deleting it (ONTA-404).

    Writes ``onto/deprecatedAt`` (+ optional ``onto/supersededBy``) on the
    type or attribute subject. The subject still resolves; read paths can
    surface the marker. Markers are schema identity (fingerprint-covered).
    """
    if not mut.type_name:
        raise ValueError("DEPRECATE requires type_name")
    ts = _host().datetime.now(_host().timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if mut.slot_name:
        subject = attr_uri(mut.type_name, mut.slot_name)
    else:
        subject = type_uri(mut.type_name)

    sup_uri: str | None = None
    if mut.superseded_by:
        raw = mut.superseded_by.strip()
        if raw.startswith("http://") or raw.startswith("https://"):
            sup_uri = raw
        elif mut.slot_name and "/" not in raw:
            # Bare leaf → attribute on the same type (caller can pass full IRI
            # for cross-type supersession).
            sup_uri = attr_uri(mut.type_name, raw)
        else:
            # Bare type name (or Type/attrs/leaf path is not supported bare).
            leaf = raw.rsplit("/", 1)[-1]
            sup_uri = type_uri(leaf)

    # Clear then insert so re-deprecate is idempotent / updateable. Two
    # single-predicate DELETEs keep the SPARQL shape simple for in-memory
    # test stores (and for Neptune).
    for pred in (DEPRECATED_AT, SUPERSEDED_BY):
        await neptune.update(
            f"DELETE {{ GRAPH <{graph_uri}> {{ <{subject}> <{pred}> ?v }} }}\n"
            f"WHERE {{ GRAPH <{graph_uri}> {{ "
            f"OPTIONAL {{ <{subject}> <{pred}> ?v }} }} }}"
        )
    triples: list[tuple[str, str, str]] = [
        (subject, DEPRECATED_AT, f"{ts}^^{XSD}#dateTime"),
    ]
    if sup_uri:
        triples.append((subject, SUPERSEDED_BY, sup_uri))
    await neptune.update(insert_triples(graph_uri, triples))

    from infona_client.models.ontology import ChangeKind

    return [
        ChangeRecord(
            kind=ChangeKind.DEPRECATE,
            type_name=mut.type_name,
            slot_name=mut.slot_name,
            superseded_by=mut.superseded_by,
            new_value=ts,
        )
    ]


async def _apply_register_alias(
    neptune, graph_uri: str, mut: OntologyMutation,
) -> list[ChangeRecord]:
    """Author an ``old aliasOf new`` triple via :func:`register_alias` (ONTA-407a).

    Alias-edge only — both attributes are assumed to already exist (or the
    caller only needs the query-path rewrite). For a full rename that also
    updates the schema, use :func:`_apply_rename_attribute`.
    """
    from infona_client.models.ontology import ChangeKind

    if not mut.alias_from or not mut.alias_to:
        raise ValueError("REGISTER_ALIAS requires alias_from and alias_to")
    old_uri = _host()._resolve_attr_endpoint(
        mut.alias_from, type_name=mut.type_name, op_label="REGISTER_ALIAS",
    )
    new_uri = _host()._resolve_attr_endpoint(
        mut.alias_to,
        type_name=mut.type_name,
        target_type=mut.target_type,
        op_label="REGISTER_ALIAS",
    )
    await register_alias(neptune, graph_uri, old_uri, new_uri)
    from_name = _host()._leaf_name(mut.alias_from, old_uri)
    to_name = _host()._leaf_name(mut.alias_to, new_uri)
    return [
        ChangeRecord(
            kind=ChangeKind.RENAME_WITH_ALIAS,
            type_name=mut.type_name or None,
            slot_name=from_name if not mut.alias_from.startswith("http") else None,
            from_name=from_name,
            to_name=to_name,
            old_value=old_uri,
            new_value=new_uri,
        )
    ]


async def _apply_rename_attribute(
    neptune, graph_uri: str, mut: OntologyMutation,
) -> list[ChangeRecord]:
    """Full attribute rename — ALWAYS creates an alias (ONTA-407b).

    Steps (atomic within the commit batch):
    1. Ensure the **new** attribute declaration exists (upsert).
    2. Record ``old aliasOf new`` — there is no rename without an alias.
    3. Drop the **old** attribute's schema declaration (instance triples stay
       on the old predicate until backfill).

    Cannot be used to "just rename" without the alias edge: that would break
    ADR 0002 §7 (published URIs never break; migration is alias-first).
    """
    from infona_client.models.ontology import ChangeKind

    # Accept alias_from or slot_name as the old leaf for ergonomics.
    old_leaf = mut.alias_from or mut.slot_name
    if not old_leaf or not mut.alias_to:
        raise ValueError(
            "RENAME_ATTRIBUTE requires alias_from (or slot_name) and alias_to"
        )
    if not mut.type_name:
        raise ValueError("RENAME_ATTRIBUTE requires type_name")

    old_uri = _host()._resolve_attr_endpoint(
        old_leaf, type_name=mut.type_name, op_label="RENAME_ATTRIBUTE",
    )
    new_owner = (mut.target_type or mut.type_name).strip()
    new_uri = _host()._resolve_attr_endpoint(
        mut.alias_to,
        type_name=mut.type_name,
        target_type=mut.target_type,
        op_label="RENAME_ATTRIBUTE",
    )
    if old_uri == new_uri:
        raise ValueError(
            f"RENAME_ATTRIBUTE must change the attribute, got {old_uri} -> itself"
        )

    from_name = _host()._leaf_name(old_leaf, old_uri)
    to_name = _host()._leaf_name(mut.alias_to, new_uri)
    records: list[ChangeRecord] = []

    # 1. Mint / refresh the new attribute declaration.
    datatype = mut.datatype or "string"
    await neptune.update(
        upsert_attribute(
            graph_uri,
            new_owner,
            to_name,
            description=mut.description or "",
            datatype=datatype,
        )
    )
    records.append(
        ChangeRecord(
            kind=ChangeKind.ADD_ATTRIBUTE,
            type_name=new_owner,
            slot_name=to_name,
            new_value=datatype,
        )
    )

    # 2. Drop the old schema declaration FIRST (instance data untouched).
    # delete_attribute_declaration wipes every triple with subject=old_uri,
    # which would also remove aliasOf — so the alias is written *after* this.
    if not old_leaf.startswith("http"):
        await neptune.update(
            delete_attribute_declaration(graph_uri, mut.type_name, from_name)
        )
    else:
        # Full IRI: strip declaration by subject wipe (same effect as the builder).
        await neptune.update(
            f"WITH <{graph_uri}>\n"
            f"DELETE {{ <{old_uri}> ?p ?o }} WHERE {{ <{old_uri}> ?p ?o }}"
        )
    records.append(
        ChangeRecord(
            kind=ChangeKind.REMOVE_ATTRIBUTE,
            type_name=mut.type_name,
            slot_name=from_name,
        )
    )

    # 3. ALWAYS create the alias — the rename vehicle, not optional. Must run
    # after the old-declaration wipe so the subject wipe does not delete it.
    await register_alias(neptune, graph_uri, old_uri, new_uri)
    records.append(
        ChangeRecord(
            kind=ChangeKind.RENAME_WITH_ALIAS,
            type_name=mut.type_name,
            slot_name=from_name if not old_leaf.startswith("http") else None,
            from_name=from_name,
            to_name=to_name,
            old_value=old_uri,
            new_value=new_uri,
        )
    )
    return records


async def _apply_retire_alias(
    neptune, graph_uri: str, mut: OntologyMutation,
) -> list[ChangeRecord]:
    """Retire an alias after backfill — refuses while references remain (ONTA-407b).

    Requires ``data_graph_uri`` so the real reference check runs against the
    instance graph. Zero remaining old-predicate triples is mandatory.
    """
    from infona_client.models.ontology import ChangeKind

    old_leaf = mut.alias_from or mut.slot_name
    if not old_leaf:
        raise ValueError("RETIRE_ALIAS requires alias_from (or slot_name)")
    if not mut.data_graph_uri:
        raise ValueError(
            "RETIRE_ALIAS requires data_graph_uri for the instance reference check"
        )
    old_uri = _host()._resolve_attr_endpoint(
        old_leaf, type_name=mut.type_name, op_label="RETIRE_ALIAS",
    )
    await retire_alias(
        neptune, graph_uri, old_uri, data_graph_uri=mut.data_graph_uri,
    )
    from_name = _host()._leaf_name(old_leaf, old_uri)
    return [
        ChangeRecord(
            kind=ChangeKind.RENAME_WITH_ALIAS,
            type_name=mut.type_name or None,
            slot_name=from_name if not old_leaf.startswith("http") else None,
            from_name=from_name,
            to_name=None,
            old_value=old_uri,
            new_value=None,
        )
    ]

"""Legacy SPARQL apply-one / upsert / revision / changelog path.

Looks up patched names on :mod:`infona_client.graph.ontology_commit` via
``_host()``. Schema writes still go through ontology_queries builders +
``insert_triples`` (the existing SPARQL schema path), not instance
``insert_facts``.
"""

from __future__ import annotations

from uuid import uuid4

import structlog

from infona_client.graph.iri import GRAPH_URI_PREFIX
from infona_client.graph.ontology_commit_core import (
    _GOV_ACTION,
    _GOV_ACTOR,
    _GOV_DELTA,
    _GOV_MESSAGE,
    _GOV_NS,
    _GOV_REVISION,
    _GOV_SUBJECT,
    _GOV_TENANT,
    _GOV_TIMESTAMP,
    _GOV_VERSION_AFTER,
    _GOV_VERSION_BEFORE,
    _REV_PRED,
)
from infona_client.graph.ontology_queries import (
    INFONA_ONTO,
    XSD,
    attr_uri,
    delete_attribute_declaration,
    insert_subtype,
    insert_type,
    mark_core_slot,
    set_object_property_range,
    type_uri,
    upsert_attribute,
    upsert_attribute_text_kind,
    upsert_type,
    upsert_type_comment,
)
from infona_client.graph.parser import parse_sparql_results
from infona_client.graph.queries import insert_triples
from infona_client.models.ontology import ChangeRecord, OntologyMutation, OntologyOpKind

logger = structlog.stdlib.get_logger("infona.graph.ontology_commit")


def _host():
    """Call-time lookup of the public ontology_commit module (monkeypatch surface)."""
    from infona_client.graph import ontology_commit as _mod

    return _mod


async def _apply_one(
    neptune, graph_uri: str, mut: OntologyMutation,
) -> list[ChangeRecord]:
    """Apply one mutation via the existing SPARQL builders. Returns change records."""
    from infona_client.models.ontology import ChangeKind

    op = mut.op
    if op is OntologyOpKind.UPSERT_TYPE:
        return await _host()._apply_upsert_type(neptune, graph_uri, mut)
    if op is OntologyOpKind.UPSERT_ATTRIBUTE:
        return await _host()._apply_upsert_attribute(neptune, graph_uri, mut)
    if op is OntologyOpKind.UPSERT_RELATIONSHIP:
        return await _host()._apply_upsert_relationship(neptune, graph_uri, mut)
    if op is OntologyOpKind.SET_SUBCLASS:
        if not mut.parent_type:
            raise ValueError("SET_SUBCLASS requires parent_type")
        await neptune.update(insert_subtype(graph_uri, mut.parent_type, mut.type_name))
        return [
            ChangeRecord(
                kind=ChangeKind.ADD_SUBCLASS,
                type_name=mut.type_name,
                parent_type=mut.parent_type,
            )
        ]
    if op is OntologyOpKind.DELETE_ATTRIBUTE:
        if not mut.slot_name:
            raise ValueError("DELETE_ATTRIBUTE requires slot_name")
        await neptune.update(
            delete_attribute_declaration(graph_uri, mut.type_name, mut.slot_name)
        )
        return [
            ChangeRecord(
                kind=ChangeKind.REMOVE_ATTRIBUTE,
                type_name=mut.type_name,
                slot_name=mut.slot_name,
            )
        ]
    if op is OntologyOpKind.DELETE_TYPE:
        # Best-effort: drop every triple whose subject is the type URI.
        # Attributes and reverse subClassOf edges are not cascaded (no production
        # caller uses DELETE_TYPE yet; ONTA-404/compat gate owns the policy).
        uri = type_uri(mut.type_name)
        await neptune.update(
            f"WITH <{graph_uri}>\n"
            f"DELETE {{ <{uri}> ?p ?o }} WHERE {{ <{uri}> ?p ?o }}"
        )
        return [ChangeRecord(kind=ChangeKind.REMOVE_TYPE, type_name=mut.type_name)]
    if op is OntologyOpKind.SET_CORE_SLOT:
        if not mut.slot_name:
            raise ValueError("SET_CORE_SLOT requires slot_name")
        if mut.core_slot is False:
            # ONTA-425: was an inline f-string, the one attribute IRI in this
            # module that did not go through `attr_uri` and so kept its own
            # unvalidated copy of the URI shape. It reaches a `neptune.update`,
            # where a `>` in either name closes the IRI and the remainder becomes
            # statement-level SPARQL.
            a_uri = attr_uri(mut.type_name, mut.slot_name)
            await neptune.update(
                f"DELETE {{ GRAPH <{graph_uri}> {{ <{a_uri}> <{INFONA_ONTO}/coreSlot> ?c }} }}\n"
                f"WHERE {{ GRAPH <{graph_uri}> {{ OPTIONAL {{ <{a_uri}> <{INFONA_ONTO}/coreSlot> ?c }} }} }}"
            )
        else:
            await neptune.update(mark_core_slot(graph_uri, mut.type_name, mut.slot_name))
        return [
            ChangeRecord(
                kind=ChangeKind.CHANGE_CORE_SLOT,
                type_name=mut.type_name,
                slot_name=mut.slot_name,
                new_value="true" if mut.core_slot is not False else "false",
            )
        ]
    if op is OntologyOpKind.SET_TEXT_KIND:
        if not mut.slot_name:
            raise ValueError("SET_TEXT_KIND requires slot_name")
        kind = mut.text_kind or ""
        await neptune.update(
            upsert_attribute_text_kind(
                graph_uri, mut.type_name, mut.slot_name, text_kind=kind,
            )
        )
        return [
            ChangeRecord(
                kind=ChangeKind.CHANGE_TEXT_KIND,
                type_name=mut.type_name,
                slot_name=mut.slot_name,
                new_value=kind or None,
            )
        ]
    if op is OntologyOpKind.SET_COMMENT:
        # Type-level when no slot_name; attribute-level comment via upsert_attribute
        # range-preserving path is not a single builder — use upsert_type_comment
        # for types. Attribute comments go through UPSERT_ATTRIBUTE.
        if mut.slot_name:
            await neptune.update(
                upsert_attribute(
                    graph_uri,
                    mut.type_name,
                    mut.slot_name,
                    description=mut.description or "",
                    datatype=mut.datatype or "string",
                )
            )
            return [
                ChangeRecord(
                    kind=ChangeKind.CHANGE_COMMENT,
                    type_name=mut.type_name,
                    slot_name=mut.slot_name,
                    new_value=mut.description,
                )
            ]
        await neptune.update(
            upsert_type_comment(graph_uri, mut.type_name, mut.description or "")
        )
        return [
            ChangeRecord(
                kind=ChangeKind.CHANGE_COMMENT,
                type_name=mut.type_name,
                new_value=mut.description,
            )
        ]
    if op is OntologyOpKind.REGISTER_ALIAS:
        return await _host()._apply_register_alias(neptune, graph_uri, mut)
    if op is OntologyOpKind.RENAME_ATTRIBUTE:
        return await _host()._apply_rename_attribute(neptune, graph_uri, mut)
    if op is OntologyOpKind.RETIRE_ALIAS:
        return await _host()._apply_retire_alias(neptune, graph_uri, mut)
    if op is OntologyOpKind.DEPRECATE:
        return await _host()._apply_deprecate(neptune, graph_uri, mut)
    raise ValueError(f"unknown ontology op: {op!r}")


async def _apply_upsert_type(
    neptune, graph_uri: str, mut: OntologyMutation,
) -> list[ChangeRecord]:
    from infona_client.models.ontology import ChangeKind

    records: list[ChangeRecord] = []
    # parent set → upsert_type (atomic class+label+comment+subClassOf replace).
    # description-only → upsert_type_comment so an existing subClassOf edge is
    # NEVER cleared (the new-parent-edge bug insert_type/upsert_type dual).
    # neither → non-destructive insert_type (class+label only).
    if mut.parent_type is not None:
        desc = mut.description if mut.description is not None else ""
        await neptune.update(
            upsert_type(graph_uri, mut.type_name, desc, mut.parent_type)
        )
        records.append(ChangeRecord(kind=ChangeKind.ADD_TYPE, type_name=mut.type_name))
        records.append(
            ChangeRecord(
                kind=ChangeKind.ADD_SUBCLASS,
                type_name=mut.type_name,
                parent_type=mut.parent_type,
            )
        )
        if mut.description:
            records.append(
                ChangeRecord(
                    kind=ChangeKind.CHANGE_COMMENT,
                    type_name=mut.type_name,
                    new_value=mut.description,
                )
            )
    elif mut.description is not None:
        await neptune.update(
            upsert_type_comment(graph_uri, mut.type_name, mut.description)
        )
        records.append(ChangeRecord(kind=ChangeKind.ADD_TYPE, type_name=mut.type_name))
        if mut.description:
            records.append(
                ChangeRecord(
                    kind=ChangeKind.CHANGE_COMMENT,
                    type_name=mut.type_name,
                    new_value=mut.description,
                )
            )
    else:
        await neptune.update(insert_type(graph_uri, mut.type_name, ""))
        records.append(ChangeRecord(kind=ChangeKind.ADD_TYPE, type_name=mut.type_name))
    return records


async def _apply_upsert_attribute(
    neptune, graph_uri: str, mut: OntologyMutation,
) -> list[ChangeRecord]:
    from infona_client.models.ontology import ChangeKind

    if not mut.slot_name:
        raise ValueError("UPSERT_ATTRIBUTE requires slot_name")
    datatype = mut.datatype or "string"
    await neptune.update(
        upsert_attribute(
            graph_uri,
            mut.type_name,
            mut.slot_name,
            description=mut.description or "",
            datatype=datatype,
        )
    )
    return [
        ChangeRecord(
            kind=ChangeKind.ADD_ATTRIBUTE,
            type_name=mut.type_name,
            slot_name=mut.slot_name,
            new_value=datatype,
        )
    ]


async def _apply_upsert_relationship(
    neptune, graph_uri: str, mut: OntologyMutation,
) -> list[ChangeRecord]:
    from infona_client.models.ontology import ChangeKind

    if not mut.slot_name:
        raise ValueError("UPSERT_RELATIONSHIP requires slot_name")
    if not mut.target_type:
        raise ValueError("UPSERT_RELATIONSHIP requires target_type")
    # description=None → range-only upgrade (preserves human-authored comment).
    # description provided (including "") → full upsert_attribute.
    if mut.description is None:
        await neptune.update(
            set_object_property_range(
                graph_uri, mut.type_name, mut.slot_name, mut.target_type,
            )
        )
        return [
            ChangeRecord(
                kind=ChangeKind.CHANGE_RANGE,
                type_name=mut.type_name,
                slot_name=mut.slot_name,
                new_value=mut.target_type,
            )
        ]
    await neptune.update(
        upsert_attribute(
            graph_uri,
            mut.type_name,
            mut.slot_name,
            description=mut.description,
            datatype=mut.target_type,
        )
    )
    return [
        ChangeRecord(
            kind=ChangeKind.ADD_RELATIONSHIP,
            type_name=mut.type_name,
            slot_name=mut.slot_name,
            new_value=mut.target_type,
        )
    ]


async def _bump_revision(neptune, graph_uri: str) -> int:
    """Monotonic workspace revision counter on the versions companion graph.

    Minimal RDF counter (no Postgres store exists — plan §4). Read current,
    write current+1 as a single-valued replace. Concurrent commits are
    serialized by :func:`ontology_write_lock`, so lost updates cannot occur.
    """
    rev_graph = _host().versions_graph_uri(graph_uri)
    subject = graph_uri
    current = 0
    try:
        raw = await neptune.query(
            f"SELECT ?r FROM <{rev_graph}> WHERE {{ <{subject}> <{_REV_PRED}> ?r }}"
        )
        _, rows = parse_sparql_results(raw)
        if rows and rows[0].get("r") is not None:
            try:
                current = int(str(rows[0]["r"]).split("^")[0])
            except (TypeError, ValueError):
                current = 0
    except Exception:
        current = 0
    nxt = current + 1
    sparql = (
        f"DELETE {{ GRAPH <{rev_graph}> {{ <{subject}> <{_REV_PRED}> ?old }} }}\n"
        f"INSERT {{ GRAPH <{rev_graph}> {{ "
        f'<{subject}> <{_REV_PRED}> "{nxt}"^^<{XSD}#integer> }} }}\n'
        f"WHERE {{ GRAPH <{rev_graph}> {{ "
        f"OPTIONAL {{ <{subject}> <{_REV_PRED}> ?old }} }} }}"
    )
    await neptune.update(sparql)
    return nxt


async def _emit_changelog(
    neptune,
    graph_uri: str,
    *,
    version_before: str,
    version_after: str,
    actor: str | None,
    message: str | None,
    change_records: list[ChangeRecord],
    revision: int,
) -> None:
    """One append-only changelog entry with a full delta payload (ONTA-403/401).

    Shape mirrors :func:`infona_client.resolver.governance.changelog_triples`
    (action / subject / timestamp / tenant) and extends it with version before/
    after, actor, message, revision, and a JSON delta of **full**
    :class:`ChangeRecord` objects (including ``from_name`` / ``to_name`` /
    ``superseded_by``) so ONTA-401 can describe a change without re-reading the
    live ontology graph. ``gov:subject`` is the **target graph URI** for
    workspace commits. Entry nodes use a fresh uuid (``gov/log/{uuid4}``) so
    two commits in the same millisecond never collide.
    """
    # Local import avoids a circular import at module load (changelog imports
    # nothing from this module's write path; commit is the sole writer).
    from infona_client.graph.ontology_changelog import serialize_change_records

    ts = _host().datetime.now(_host().timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = f"{_GOV_NS}log/{uuid4()}"
    # Tenant id is the first path segment of graphs/{tenant}[…].
    tenant_id = ""
    prefix = GRAPH_URI_PREFIX
    if graph_uri.startswith(prefix):
        rest = graph_uri[len(prefix):]
        tenant_id = rest.split("/", 1)[0]
    triples: list[tuple[str, str, str]] = [
        (entry, _GOV_ACTION, "commit_ontology"),
        (entry, _GOV_SUBJECT, graph_uri),  # target graph URI
        (entry, _GOV_TIMESTAMP, f"{ts}^^{XSD}#dateTime"),
        (entry, _GOV_VERSION_BEFORE, version_before),
        (entry, _GOV_VERSION_AFTER, version_after),
        (entry, _GOV_REVISION, f"{revision}^^{XSD}#integer"),
        (entry, _GOV_DELTA, serialize_change_records(change_records)),
    ]
    if tenant_id:
        triples.append((entry, _GOV_TENANT, tenant_id))
    if actor:
        triples.append((entry, _GOV_ACTOR, actor))
    if message:
        triples.append((entry, _GOV_MESSAGE, message))
    await neptune.update(
        insert_triples(_host().changelog_graph_uri_for(graph_uri), triples)
    )

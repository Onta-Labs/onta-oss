"""Ontology changelog reader + shared delta codec (ONTA-401).

Workspace schema commits write append-only entries into the per-ontology
companion ``{ontology-graph}/changelog`` (see
:func:`cograph_client.graph.ontology_commit.changelog_graph_uri_for` and
:func:`cograph_client.graph.ontology_commit._emit_changelog`). Each entry
carries a JSON :class:`~cograph_client.models.ontology.ChangeRecord` delta so
a reader can describe the change **without consulting the live ontology
graph**.

Global governance still writes the thinner action/subject/timestamp/tenant
shape into ``https://cograph.tech/graphs/global/changelog`` (ADR 0002 §8).
This reader is workspace-scoped by default (tenant isolation by named graph);
optional fields (delta, actor, versions, …) are OPTIONAL so governance-shaped
entries remain readable if pointed at that graph.

Modeled on :mod:`cograph_client.graph.history` (value-history query + fetch)
and the route surface of ``GET /graphs/{tenant}/history``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Sequence

from cograph_client.graph.parser import parse_sparql_results
from cograph_client.graph.queries import _escape_literal, _escape_value
from cograph_client.models.ontology import ChangeRecord

# ---------------------------------------------------------------------------
# GOV_* vocabulary — shared with ontology_commit / governance writers
# ---------------------------------------------------------------------------

GOV_NS = "https://cograph.tech/gov/"
GOV_ACTION = f"{GOV_NS}action"
GOV_SUBJECT = f"{GOV_NS}subject"
GOV_TIMESTAMP = f"{GOV_NS}timestamp"
GOV_TENANT = f"{GOV_NS}tenant"
GOV_ACTOR = f"{GOV_NS}actor"
GOV_MESSAGE = f"{GOV_NS}message"
GOV_VERSION_BEFORE = f"{GOV_NS}versionBefore"
GOV_VERSION_AFTER = f"{GOV_NS}versionAfter"
GOV_DELTA = f"{GOV_NS}delta"
GOV_REVISION = f"{GOV_NS}revision"

_XSD = "http://www.w3.org/2001/XMLSchema"


def changelog_graph_uri_for(graph_uri: str) -> str:
    """Append-only changelog companion for an ontology graph.

    Identical to
    :func:`cograph_client.graph.ontology_commit.changelog_graph_uri_for` —
    re-exported here so the reader module is self-contained. Workspace
    isolation is by named graph: tenant A never appears in tenant B's FROM.
    """
    return f"{graph_uri.rstrip('/')}/changelog"


# ---------------------------------------------------------------------------
# Delta codec — writer and reader must agree (ONTA-401 acceptance)
# ---------------------------------------------------------------------------


def serialize_change_records(records: Sequence[ChangeRecord]) -> str:
    """JSON array of ChangeRecords for the ``gov:delta`` literal.

    Uses ``model_dump(mode="json", exclude_none=True)`` so every field on
    :class:`ChangeRecord` (including ``from_name`` / ``to_name`` /
    ``superseded_by``) is preserved without a hand-maintained field list.
    Stable key order keeps identical commits byte-identical.
    """
    payload = [r.model_dump(mode="json", exclude_none=True) for r in records]
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def parse_change_records(raw: str | None) -> list[ChangeRecord]:
    """Inverse of :func:`serialize_change_records`; tolerant of legacy thin entries.

    Missing / unparseable / non-list JSON → empty list (governance writers still
    produce valid entries without a delta payload).
    """
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    out: list[ChangeRecord] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or "kind" not in item:
            continue
        try:
            out.append(ChangeRecord.model_validate(item))
        except Exception:  # noqa: BLE001 — skip one bad row, keep the rest
            continue
    return out


# ---------------------------------------------------------------------------
# Entry model + query / fetch
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChangelogEntry:
    """One append-only changelog row, fully reconstructible from the entry alone."""

    entry_uri: str
    action: str
    subject: str  # target graph URI for commit_ontology; type/shape URI for governance
    timestamp: str
    tenant_id: str | None = None
    actor: str | None = None
    message: str | None = None
    version_before: str | None = None
    version_after: str | None = None
    revision: int | None = None
    changes: list[ChangeRecord] = field(default_factory=list)


def ontology_changelog_query(
    ontology_graph_uri: str,
    *,
    since: str | None = None,
    subject: str | None = None,
    action: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> str:
    """SELECT over the workspace changelog companion, newest → oldest.

    Filters
    -------
    since:
        ISO-8601 date/dateTime; only entries with ``timestamp`` STRICTLY AFTER
        the cutoff (typed xsd:dateTime comparison — ONTA-247 lesson).
    subject:
        Absolute IRI matched against ``gov:subject`` (the target ontology graph
        for workspace commits, or the type/shape URI for governance entries).
    action:
        Exact match on ``gov:action`` (e.g. ``commit_ontology``, ``add_type``).

    Ordering
    --------
    ``DESC(?timestamp) DESC(?entry)`` — newest first; same-millisecond entries
    (uuid nodes under ``gov/log/``) get a stable secondary order so pagination
    never drops or double-counts siblings.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    if offset < 0:
        raise ValueError("offset must be >= 0")

    cl_graph = changelog_graph_uri_for(ontology_graph_uri)
    subj_pat = _escape_value(subject) if subject else "?subject"
    action_filter = ""
    if action is not None:
        action_filter = (
            f'  FILTER(?action = "{_escape_literal(action)}")\n'
        )
    since_filter = ""
    if since:
        since_filter = (
            f'  FILTER(?timestamp > "{_escape_literal(since)}"^^<{_XSD}#dateTime>)\n'
        )
    # When subject is bound as a constant, still project it as ?subject for a
    # uniform result shape (BIND the constant).
    subject_bind = ""
    if subject:
        subject_bind = f"  BIND({subj_pat} AS ?subject)\n"
        subj_triple = subj_pat
    else:
        subj_triple = "?subject"

    return (
        f"SELECT ?entry ?action ?subject ?timestamp ?tenant ?actor ?message "
        f"?versionBefore ?versionAfter ?revision ?delta\n"
        f"FROM <{cl_graph}>\n"
        f"WHERE {{\n"
        f"  ?entry <{GOV_ACTION}> ?action ;\n"
        f"         <{GOV_SUBJECT}> {subj_triple} ;\n"
        f"         <{GOV_TIMESTAMP}> ?timestamp .\n"
        f"{subject_bind}"
        f"  OPTIONAL {{ ?entry <{GOV_TENANT}> ?tenant }}\n"
        f"  OPTIONAL {{ ?entry <{GOV_ACTOR}> ?actor }}\n"
        f"  OPTIONAL {{ ?entry <{GOV_MESSAGE}> ?message }}\n"
        f"  OPTIONAL {{ ?entry <{GOV_VERSION_BEFORE}> ?versionBefore }}\n"
        f"  OPTIONAL {{ ?entry <{GOV_VERSION_AFTER}> ?versionAfter }}\n"
        f"  OPTIONAL {{ ?entry <{GOV_REVISION}> ?revision }}\n"
        f"  OPTIONAL {{ ?entry <{GOV_DELTA}> ?delta }}\n"
        f"{action_filter}"
        f"{since_filter}"
        f"}}\n"
        f"ORDER BY DESC(?timestamp) DESC(?entry)\n"
        f"LIMIT {int(limit)} OFFSET {int(offset)}"
    )


def _parse_revision(raw: str | None) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(str(raw).split("^")[0])
    except (TypeError, ValueError):
        return None


async def fetch_ontology_changelog(
    neptune,
    ontology_graph_uri: str,
    *,
    since: str | None = None,
    subject: str | None = None,
    action: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[ChangelogEntry]:
    """Read parsed changelog entries (newest → oldest).

    ``ontology_graph_uri`` is the LIVE ontology graph; the companion changelog
    graph is derived. Returns an empty list on any read failure so a changelog
    read never breaks a caller (same contract as
    :func:`cograph_client.graph.history.fetch_value_history`).
    """
    try:
        raw = await neptune.query(
            ontology_changelog_query(
                ontology_graph_uri,
                since=since,
                subject=subject,
                action=action,
                limit=limit,
                offset=offset,
            )
        )
    except Exception:  # noqa: BLE001 — informational read
        return []
    _, bindings = parse_sparql_results(raw)
    out: list[ChangelogEntry] = []
    for row in bindings:
        out.append(
            ChangelogEntry(
                entry_uri=row.get("entry", ""),
                action=row.get("action", ""),
                subject=row.get("subject", ""),
                timestamp=row.get("timestamp", ""),
                tenant_id=row.get("tenant") or None,
                actor=row.get("actor") or None,
                message=row.get("message") or None,
                version_before=row.get("versionBefore") or None,
                version_after=row.get("versionAfter") or None,
                revision=_parse_revision(row.get("revision")),
                changes=parse_change_records(row.get("delta")),
            )
        )
    return out

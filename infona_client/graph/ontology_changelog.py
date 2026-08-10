"""Ontology changelog reader + shared delta codec (ONTA-401).

Workspace schema commits write append-only entries into the per-ontology
companion ``{ontology-graph}/changelog`` (see
:func:`infona_client.graph.ontology_commit.changelog_graph_uri_for` and
:func:`infona_client.graph.ontology_commit._emit_changelog`). Each entry
carries a JSON :class:`~infona_client.models.ontology.ChangeRecord` delta so
a reader can describe the change **without consulting the live ontology
graph**.

Global governance still writes the thinner action/subject/timestamp/tenant
shape into ``https://graph.onta.sh/graphs/global/changelog`` (ADR 0002 §8).
This reader is workspace-scoped by default (tenant isolation by named graph);
optional fields (delta, actor, versions, …) are OPTIONAL so governance-shaped
entries remain readable if pointed at that graph.

Modeled on :mod:`infona_client.graph.history` (value-history query + fetch)
and the route surface of ``GET /graphs/{tenant}/history``.
"""

from __future__ import annotations

from infona_client.graph.iri import GOV_NS, IRI_BASE


import json
from dataclasses import dataclass, field
from typing import Sequence

from infona_client.graph.parser import parse_sparql_results
from infona_client.graph.queries import _escape_literal, _escape_value
from infona_client.models.ontology import ChangeRecord

# ---------------------------------------------------------------------------
# GOV_* vocabulary — shared with ontology_commit / governance writers
# ---------------------------------------------------------------------------

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
    :func:`infona_client.graph.ontology_commit.changelog_graph_uri_for` —
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
    :func:`infona_client.graph.history.fetch_value_history`).
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


# ---------------------------------------------------------------------------
# History grouping (ONTA-410) — pure; shared by API + UI consumers
# ---------------------------------------------------------------------------

#: Default burst window for automatic mid-ingest commits (seconds).
DEFAULT_HISTORY_GROUP_WINDOW_S = 60.0


@dataclass(frozen=True)
class HistoryGroup:
    """One collapsed history row over consecutive changelog entries.

    ``entries`` are newest → oldest (same order as the changelog reader).
    ``start`` is the earliest timestamp in the group; ``end`` the latest.
    """

    id: str
    start: str
    end: str
    count: int
    actor: str | None
    message: str | None
    sample_actions: tuple[str, ...]
    change_summary_counts: dict[str, int]
    entries: tuple[ChangelogEntry, ...] = ()


def _parse_iso_ts(raw: str | None):
    """Best-effort ISO-8601 parse; returns aware UTC datetime or None."""
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Tolerate trailing Z and missing timezone (treat as UTC).
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        from datetime import datetime, timezone

        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _job_identity(entry: ChangelogEntry) -> tuple[str, str] | None:
    """Non-empty (actor, message) job key, or None when both blank."""
    actor = (entry.actor or "").strip()
    message = (entry.message or "").strip()
    if not actor and not message:
        return None
    return (actor, message)


def _within_window(
    a: ChangelogEntry,
    b: ChangelogEntry,
    window_s: float,
) -> bool:
    ta = _parse_iso_ts(a.timestamp)
    tb = _parse_iso_ts(b.timestamp)
    if ta is None or tb is None:
        return False
    return abs((ta - tb).total_seconds()) <= window_s


def _should_group_pair(
    newer: ChangelogEntry,
    older: ChangelogEntry,
    *,
    window_s: float,
) -> bool:
    """True when two *consecutive* newest→older entries belong in one group.

    Rules (ONTA-410):
    * Same non-empty job identity (actor + message) — mid-ingest commits
      from one job collapse even if slightly staggered.
    * OR timestamps within ``window_s`` (default 60s) — rapid automatic
      bursts without a shared message still collapse.
    Isolated commits with different identity and distant timestamps stay
    separate.
    """
    id_a = _job_identity(newer)
    id_b = _job_identity(older)
    if id_a is not None and id_a == id_b:
        return True
    return _within_window(newer, older, window_s)


def _summarize_kinds(entries: Sequence[ChangelogEntry]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in entries:
        for c in e.changes or []:
            kind = c.kind.value if hasattr(c.kind, "value") else str(c.kind)
            counts[kind] = counts.get(kind, 0) + 1
    return dict(sorted(counts.items()))


def _sample_actions(entries: Sequence[ChangelogEntry], *, limit: int = 5) -> tuple[str, ...]:
    seen: list[str] = []
    for e in entries:
        a = (e.action or "").strip()
        if a and a not in seen:
            seen.append(a)
        if len(seen) >= limit:
            break
    return tuple(seen)


def _group_id(entries: Sequence[ChangelogEntry]) -> str:
    first = entries[0]
    last = entries[-1]
    # Stable, URL-safe-ish id from the boundary entry URIs / timestamps.
    head = (first.entry_uri or first.timestamp or "e").rsplit("/", 1)[-1]
    if len(entries) == 1:
        return f"g:{head}"
    tail = (last.entry_uri or last.timestamp or "e").rsplit("/", 1)[-1]
    return f"g:{head}..{tail}:{len(entries)}"


def group_changelog_entries(
    entries: Sequence[ChangelogEntry],
    *,
    window_s: float = DEFAULT_HISTORY_GROUP_WINDOW_S,
) -> list[HistoryGroup]:
    """Collapse consecutive changelog rows into history groups (ONTA-410).

    Expects ``entries`` newest → oldest (changelog reader order). Empty input
    → empty list (never an error). Pure function — safe to unit-test without
    Neptune and to share between the history API and any client-side display
    helpers.
    """
    if not entries:
        return []

    groups: list[HistoryGroup] = []
    bucket: list[ChangelogEntry] = [entries[0]]

    def _flush(bucket_entries: list[ChangelogEntry]) -> HistoryGroup:
        # bucket is newest → oldest; start = earliest, end = latest.
        start = bucket_entries[-1].timestamp or ""
        end = bucket_entries[0].timestamp or ""
        # Prefer the most recent entry's actor/message as the group label.
        actor = bucket_entries[0].actor
        message = bucket_entries[0].message
        for e in bucket_entries:
            if actor is None and e.actor:
                actor = e.actor
            if message is None and e.message:
                message = e.message
        return HistoryGroup(
            id=_group_id(bucket_entries),
            start=start,
            end=end,
            count=len(bucket_entries),
            actor=actor,
            message=message,
            sample_actions=_sample_actions(bucket_entries),
            change_summary_counts=_summarize_kinds(bucket_entries),
            entries=tuple(bucket_entries),
        )

    for cur in entries[1:]:
        # ``bucket[-1]`` is the oldest so far in the open group; ``cur`` is
        # next-older. Compare the previous consecutive pair's older edge
        # (bucket[-1]) with cur.
        if _should_group_pair(bucket[-1], cur, window_s=window_s):
            bucket.append(cur)
        else:
            groups.append(_flush(bucket))
            bucket = [cur]
    groups.append(_flush(bucket))
    return groups


__all__ = [
    "ChangelogEntry",
    "DEFAULT_HISTORY_GROUP_WINDOW_S",
    "GOV_ACTION",
    "GOV_ACTOR",
    "GOV_DELTA",
    "GOV_MESSAGE",
    "GOV_NS",
    "GOV_REVISION",
    "GOV_SUBJECT",
    "GOV_TENANT",
    "GOV_TIMESTAMP",
    "GOV_VERSION_AFTER",
    "GOV_VERSION_BEFORE",
    "HistoryGroup",
    "changelog_graph_uri_for",
    "fetch_ontology_changelog",
    "group_changelog_entries",
    "ontology_changelog_query",
    "parse_change_records",
    "serialize_change_records",
]

"""ONTA-401 — ontology changelog delta payload + reader.

Covers:
- full ChangeRecord delta serialize/parse (entry describes change without live graph)
- query builder: companion graph scoping, since/subject/action, pagination, same-ms order
- append-only property of commit_ontology writers (uuid entry nodes; no DELETE on changelog)
- governance thin entries remain parseable (no delta required)
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock

import pytest

from infona_client.graph.ontology_changelog import (
    GOV_ACTION,
    GOV_NS,
    GOV_SUBJECT,
    GOV_TIMESTAMP,
    changelog_graph_uri_for,
    fetch_ontology_changelog,
    ontology_changelog_query,
    parse_change_records,
    serialize_change_records,
)
from infona_client.graph.ontology_commit import commit_ontology
from infona_client.models.ontology import (
    ChangeKind,
    ChangeRecord,
    OntologyMutation,
    OntologyOpKind,
)
from infona_client.resolver.governance import changelog_triples


# ---------------------------------------------------------------------------
# Delta codec
# ---------------------------------------------------------------------------


def test_serialize_parse_roundtrip_full_change_record():
    """Delta carries every ChangeRecord field — enough to describe the change
    without consulting the live ontology graph (ONTA-401 acceptance)."""
    records = [
        ChangeRecord(kind=ChangeKind.ADD_TYPE, type_name="Person"),
        ChangeRecord(
            kind=ChangeKind.ADD_ATTRIBUTE,
            type_name="Person",
            slot_name="name",
            new_value="string",
        ),
        ChangeRecord(
            kind=ChangeKind.RENAME_WITH_ALIAS,
            type_name="Guest",
            slot_name="phone_num",
            from_name="phone_num",
            to_name="phone",
            old_value="https://graph.onta.sh/types/Guest/attrs/phone_num",
            new_value="https://graph.onta.sh/types/Guest/attrs/phone",
        ),
        ChangeRecord(
            kind=ChangeKind.DEPRECATE,
            type_name="LegacyThing",
            superseded_by="Thing",
        ),
    ]
    raw = serialize_change_records(records)
    assert "from_name" in raw and "phone_num" in raw
    assert "superseded_by" in raw and "Thing" in raw
    back = parse_change_records(raw)
    assert len(back) == 4
    assert back[0].kind is ChangeKind.ADD_TYPE and back[0].type_name == "Person"
    assert back[2].from_name == "phone_num" and back[2].to_name == "phone"
    assert back[3].superseded_by == "Thing"


def test_parse_change_records_tolerates_missing_and_garbage():
    """Governance writers omit delta — empty/malformed must not raise."""
    assert parse_change_records(None) == []
    assert parse_change_records("") == []
    assert parse_change_records("not-json") == []
    assert parse_change_records("{}") == []  # object, not list
    assert parse_change_records('[{"no_kind": true}]') == []
    # One bad row does not drop siblings.
    raw = (
        '[{"kind":"add_type","type_name":"A"},'
        '{"kind":"not_a_real_kind"},'
        '{"kind":"remove_type","type_name":"B"}]'
    )
    out = parse_change_records(raw)
    assert [r.type_name for r in out] == ["A", "B"]


def test_serialize_excludes_none_fields():
    raw = serialize_change_records(
        [ChangeRecord(kind=ChangeKind.ADD_TYPE, type_name="X")]
    )
    assert "slot_name" not in raw
    assert "null" not in raw


# ---------------------------------------------------------------------------
# Query builder
# ---------------------------------------------------------------------------


def test_changelog_graph_uri_is_companion():
    g = "https://graph.onta.sh/graphs/acme"
    assert changelog_graph_uri_for(g) == f"{g}/changelog"
    # Trailing slash stripped — no double slash.
    assert changelog_graph_uri_for(g + "/") == f"{g}/changelog"


def test_query_scopes_to_tenant_companion_only():
    """TENANT ISOLATION: FROM is exactly the caller's companion — never another
    tenant's graph and never a cross-tenant UNION."""
    g = "https://graph.onta.sh/graphs/tenant-a"
    q = ontology_changelog_query(g)
    assert f"FROM <{g}/changelog>" in q
    assert "tenant-b" not in q
    assert "UNION" not in q.upper()
    assert "https://graph.onta.sh/graphs/global/changelog" not in q


def test_query_filters_and_pagination():
    g = "https://graph.onta.sh/graphs/t"
    q = ontology_changelog_query(
        g,
        since="2026-07-01T00:00:00Z",
        subject="https://graph.onta.sh/graphs/t",
        action="commit_ontology",
        limit=25,
        offset=50,
    )
    assert 'FILTER(?timestamp > "2026-07-01T00:00:00Z"^^' in q
    assert f"<{GOV_SUBJECT}> <https://graph.onta.sh/graphs/t>" in q or (
        f"<{GOV_SUBJECT}>" in q and "https://graph.onta.sh/graphs/t" in q
    )
    assert 'FILTER(?action = "commit_ontology")' in q
    assert "LIMIT 25" in q
    assert "OFFSET 50" in q
    # Same-ms stable order: secondary key on entry URI.
    assert "ORDER BY DESC(?timestamp) DESC(?entry)" in q


def test_query_rejects_bad_limit_offset():
    with pytest.raises(ValueError):
        ontology_changelog_query("https://graph.onta.sh/graphs/t", limit=0)
    with pytest.raises(ValueError):
        ontology_changelog_query("https://graph.onta.sh/graphs/t", offset=-1)


def test_query_escapes_action_and_since_literals():
    """Crafted action/since cannot break out of the SPARQL string literal."""
    q = ontology_changelog_query(
        "https://graph.onta.sh/graphs/t",
        action='x" } FILTER(true) #',
        since='2020-01-01"^^<http://evil>',
    )
    # Quotes inside the payload are escaped — the SPARQL string does not close early.
    assert '\\"' in q
    assert 'FILTER(?action = "x\\"' in q
    # since payload is inside a typed-literal string, not a second datatype IRI.
    assert 'FILTER(?timestamp > "2020-01-01\\"^^<http://evil>"^^' in q


# ---------------------------------------------------------------------------
# fetch_ontology_changelog (mocked Neptune)
# ---------------------------------------------------------------------------


def _sparql_json(rows: list[dict[str, str]]) -> dict:
    vars_: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for k in row:
            if k not in seen:
                seen.add(k)
                vars_.append(k)
    return {
        "head": {"vars": vars_},
        "results": {
            "bindings": [
                {k: {"value": v} for k, v in row.items()} for row in rows
            ]
        },
    }


@pytest.mark.asyncio
async def test_fetch_reconstructs_entry_from_delta_alone():
    """Acceptance: entry describes the change without consulting the live graph."""
    delta = serialize_change_records(
        [
            ChangeRecord(kind=ChangeKind.ADD_TYPE, type_name="Invoice"),
            ChangeRecord(
                kind=ChangeKind.ADD_ATTRIBUTE,
                type_name="Invoice",
                slot_name="amount",
                new_value="float",
            ),
        ]
    )
    neptune = AsyncMock()
    neptune.query.return_value = _sparql_json(
        [
            {
                "entry": f"{GOV_NS}log/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "action": "commit_ontology",
                "subject": "https://graph.onta.sh/graphs/acme",
                "timestamp": "2026-07-28T12:00:00Z",
                "tenant": "acme",
                "actor": "tester",
                "message": "add Invoice",
                "versionBefore": "aaa",
                "versionAfter": "bbb",
                "revision": "3",
                "delta": delta,
            }
        ]
    )
    entries = await fetch_ontology_changelog(
        neptune, "https://graph.onta.sh/graphs/acme"
    )
    assert len(entries) == 1
    e = entries[0]
    assert e.action == "commit_ontology"
    assert e.subject == "https://graph.onta.sh/graphs/acme"
    assert e.revision == 3
    assert e.actor == "tester"
    assert [c.kind for c in e.changes] == [
        ChangeKind.ADD_TYPE,
        ChangeKind.ADD_ATTRIBUTE,
    ]
    assert e.changes[0].type_name == "Invoice"
    assert e.changes[1].slot_name == "amount"
    # The fetch only ever FROM-s the companion changelog graph.
    sent = neptune.query.await_args.args[0]
    assert "FROM <https://graph.onta.sh/graphs/acme/changelog>" in sent
    assert sent.count("FROM <") == 1
    assert "UNION" not in sent.upper()


@pytest.mark.asyncio
async def test_fetch_tolerates_governance_thin_entry():
    """Governance changelog_triples shape (no delta) still yields a valid entry."""
    # Mirror what changelog_triples writes.
    thin = changelog_triples(
        "add_type",
        "https://graph.onta.sh/types/public/Hotel",
        "acme",
        "2026-07-28T12:00:00+00:00",
    )
    by_pred = {p: o for (_s, p, o) in thin}
    neptune = AsyncMock()
    neptune.query.return_value = _sparql_json(
        [
            {
                "entry": thin[0][0],
                "action": by_pred[GOV_ACTION],
                "subject": by_pred[GOV_SUBJECT],
                "timestamp": by_pred[GOV_TIMESTAMP].split("^^")[0],
                "tenant": "acme",
            }
        ]
    )
    entries = await fetch_ontology_changelog(
        neptune, "https://graph.onta.sh/graphs/global/public"
    )
    assert len(entries) == 1
    assert entries[0].action == "add_type"
    assert entries[0].changes == []  # no delta — still valid
    assert entries[0].tenant_id == "acme"


@pytest.mark.asyncio
async def test_fetch_returns_empty_on_neptune_error():
    neptune = AsyncMock()
    neptune.query.side_effect = RuntimeError("neptune down")
    out = await fetch_ontology_changelog(
        neptune, "https://graph.onta.sh/graphs/t"
    )
    assert out == []


# ---------------------------------------------------------------------------
# Append-only property of commit_ontology writers
# ---------------------------------------------------------------------------


class _WriteCaptureNeptune:
    """Records SPARQL updates; answers fingerprint/revision SELECTs as empty."""

    def __init__(self) -> None:
        self.updates: list[str] = []

    async def update(self, sparql: str) -> None:
        self.updates.append(sparql)

    async def query(self, sparql: str) -> dict:
        return {"head": {"vars": []}, "results": {"bindings": []}}


def _changelog_inserts(updates: list[str], graph_uri: str) -> list[str]:
    cl = changelog_graph_uri_for(graph_uri)
    return [
        u for u in updates
        if f"GRAPH <{cl}>" in u and "INSERT" in u.upper()
    ]


def _entry_uris_from_inserts(inserts: list[str]) -> list[str]:
    """One entry uuid per INSERT (each triple line reuses the same subject)."""
    uris: list[str] = []
    for u in inserts:
        found = re.findall(rf"<{re.escape(GOV_NS)}log/([0-9a-fA-F-]+)>", u)
        # Dedup within one INSERT — every triple shares the entry subject.
        uris.extend(dict.fromkeys(found))
    return uris


@pytest.mark.asyncio
async def test_commit_emits_full_delta_and_target_graph_uri():
    n = _WriteCaptureNeptune()
    g = "https://graph.onta.sh/graphs/acme"
    await commit_ontology(
        n,
        g,
        [
            OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="Person"),
            OntologyMutation(
                op=OntologyOpKind.REGISTER_ALIAS,
                type_name="Guest",
                alias_from="phone_num",
                alias_to="phone",
            ),
        ],
        actor="alice",
        message="seed",
    )
    inserts = _changelog_inserts(n.updates, g)
    assert len(inserts) == 1
    body = inserts[0]
    # Target graph URI on gov:subject
    assert f"<{GOV_SUBJECT}> <{g}>" in body
    # Full delta fields for rename
    assert "from_name" in body
    assert "phone_num" in body
    assert "to_name" in body
    assert '"kind":"add_type"' in body or '"kind": "add_type"' in body or "add_type" in body
    # Actor + message + versions present
    assert "alice" in body
    assert "seed" in body
    assert "versionBefore" in body or "versionBefore>" in body
    # uuid entry node under gov/log/
    assert re.search(rf"<{re.escape(GOV_NS)}log/[0-9a-fA-F-]+>", body)


@pytest.mark.asyncio
async def test_append_only_n_commits_n_distinct_uuid_entries():
    """N commits → N distinct entry URIs; no DELETE against the changelog graph."""
    n = _WriteCaptureNeptune()
    g = "https://graph.onta.sh/graphs/t-append"
    for i in range(5):
        await commit_ontology(
            n,
            g,
            [OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name=f"T{i}")],
        )
    inserts = _changelog_inserts(n.updates, g)
    assert len(inserts) == 5
    uris = _entry_uris_from_inserts(inserts)
    assert len(uris) == 5
    assert len(set(uris)) == 5  # all distinct (uuid nodes)

    cl = changelog_graph_uri_for(g)
    deletes = [
        u for u in n.updates
        if cl in u and ("DELETE" in u.upper() or "DROP" in u.upper() or "CLEAR" in u.upper())
    ]
    assert deletes == [], f"changelog must be append-only; found {deletes!r}"


@pytest.mark.asyncio
async def test_same_ms_commits_still_distinct_entry_nodes(monkeypatch):
    """Two commits forced to the same timestamp still mint distinct uuid nodes."""
    from infona_client.graph import ontology_commit as oc

    fixed = "2026-07-28T00:00:00Z"

    class _FixedDT:
        @staticmethod
        def now(tz=None):
            from datetime import datetime, timezone

            return datetime(2026, 7, 28, 0, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(oc, "datetime", _FixedDT)

    n = _WriteCaptureNeptune()
    g = "https://graph.onta.sh/graphs/t-samems"
    await commit_ontology(
        n, g, [OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="A")]
    )
    await commit_ontology(
        n, g, [OntologyMutation(op=OntologyOpKind.UPSERT_TYPE, type_name="B")]
    )
    inserts = _changelog_inserts(n.updates, g)
    assert len(inserts) == 2
    # Both carry the same timestamp literal.
    assert all(fixed in u for u in inserts)
    uris = _entry_uris_from_inserts(inserts)
    assert len(set(uris)) == 2


@pytest.mark.asyncio
async def test_empty_commit_writes_no_changelog():
    n = _WriteCaptureNeptune()
    g = "https://graph.onta.sh/graphs/t-empty"
    await commit_ontology(n, g, [])
    assert _changelog_inserts(n.updates, g) == []
